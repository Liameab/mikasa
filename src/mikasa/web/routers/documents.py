"""文档端点：列表、上传、删除 + v3 语料文件夹管理（kb_folders 树）。

上传链路：multipart → web-tmp 落盘 → IngestService.ingest_one（自身
完成复制到 uploads + 同名替换 + 分块嵌入）→ finally unlink web-tmp。
入库后 ask.invalidate_index()：下次提问自动重建索引快照。

文件夹树（v3）是 qa 会话文件夹的镜像（routers/sessions.py 同套端点）：
嵌套结构由前端按 parent_id 组树，后端平铺存储。错误语义一致：
  404 目标不存在（文档/文件夹/父文件夹）；
  400 空文件夹名 / 空标题；
  409 防环（新父 ∈ 自身∪后代）与非空文件夹删除（含文档计数文案）。
PATCH /api/documents 语义同 qa 的 PATCH /api/sessions：model_fields_set
判定字段是否出现——显式 null folder_id = 移回根，字段缺省 = 不动。
改名/移夹只写 documents 行的组织属性，不动文件与索引（无需 invalidate）。

安全三件套：文件名净化（防路径穿越）、后缀白名单（415）、大小上限
（413）。Document 的本地路径与内容哈希不对浏览器暴露。

**阅读视图开口（2026-09-10，ADR-0016）**：新增 `/content`（拼好的正文 +
块偏移）与 `/file`（原文件流）两个端点，是**有意的**脱敏例外——正文与
原文件本就是用户自己导入的语料（问答链路早已通过 `Citation.snippet` 回显
过正文片段），开这个口是为了让引用可核查、文档可通读。边界：
  - `file_path` / `file_sha256` **永不进响应体**（`_public_document` 不变）；
  - `/file` 只在 uploads 目录内按白名单解析（`_resolve_upload_file`），
    媒体类型白名单禁 text/html 与 image/svg+xml（同源存储型 XSS 的唯一入口）；
  - 两个新端点都只按 doc_id 读库，不接收任何路径参数。
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path, PureWindowsPath
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from mikasa.config.settings import Settings
from mikasa.errors import ZhiwenError, strip_paths
from mikasa.ingest.pagelocate import locate_in_page
from mikasa.ingest.stitch import stitch_chunks
from mikasa.models.document import Document
from mikasa.storage import repo
from mikasa.storage.db import open_db
from mikasa.utils.hashing import sha256_file
from mikasa.utils.logging import get_logger
from mikasa.web.deps import get_services, get_settings
from mikasa.web.schemas import DocumentPatchIn, FolderIn, FolderPatchIn
from mikasa.web.services import AppServices

router = APIRouter(tags=["documents"])

logger = get_logger("web.routers.documents")

# 后缀白名单**必须与 loader 的 LOADERS 表对齐**：`ingest/loader.py` 里
# `.markdown` 是一等公民，Web 侧漏了它就会出现"同一个文件 CLI 能入库、网页却说
# 不支持的文件类型"（2026-09-11 审查发现——注释早就写着"与 loader 支持表对齐"，
# 但代码里从来没有 .markdown）。
_ALLOWED_SUFFIXES = {".md", ".markdown", ".txt", ".pdf", ".docx"}
# 文件名净化：剥路径成分、去控制字符与 Windows 保留字符、去首尾空白
_FILENAME_BAD = re.compile(r"[\x00-\x1f\x7f/\\:*?\"<>|]")
# 文件主名上限（不含后缀）：文件名总长 ≈ 115 + 后缀
_STEM_MAX = 115


def _public_document(doc: Document) -> dict:
    """Document → 浏览器可见子集（本地路径 / 内容哈希不外泄）。

    folder_id 随列表给到前端（语料页组树/详情卡需要），文档内容与
    索引信息不出库。
    """
    return {
        "id": doc.id,
        "title": doc.title,
        "file_type": doc.file_type,
        "char_count": doc.char_count,
        "chunk_count": doc.chunk_count,
        "ingest_status": doc.ingest_status,
        "error_message": doc.error_message,
        "folder_id": doc.folder_id,
        "created_at": doc.created_at,
        "updated_at": doc.updated_at,
    }


def sanitize_filename(raw: str) -> str:
    """净化上传文件名：剥路径成分、去控制字符与 Windows 保留字符、去首尾空白
    （保留中文），并按"主名 ≤ _STEM_MAX + 后缀"截断——先截主名再加后缀，
    避免整名截断把扩展名切掉。净化后名称为空抛 ValueError。

    后缀白名单不在本函数判断：路由层先净化后查白名单（415 文案需区分
    "无扩展名"与"扩展名不受支持"，且净化与校验职责各归其位）。
    """
    name = _FILENAME_BAD.sub("", Path(raw).name).strip(" .")
    if not name:
        raise ValueError("文件名为空")
    path = Path(name)
    return path.stem[:_STEM_MAX] + path.suffix


@router.get("/api/documents")
def list_documents(
    settings: Settings = Depends(get_settings),
) -> dict:
    """文档列表（新建倒序）。"""
    with open_db(settings.db_path) as conn:
        docs = [_public_document(d) for d in repo.list_documents(conn)]
    return {"documents": docs}


# ---------------------------------------------------------------------------
# 阅读视图（2026-09-10）：正文 / 原文件 / 片段定位
# ---------------------------------------------------------------------------

# 原文件视图的媒体类型白名单：显式映射，不查 mimetypes/系统注册表。
# **禁 text/html 与 image/svg+xml**：服务跑在 127.0.0.1 同源下，这两类被
# 浏览器当文档解析就是存储型 XSS 的唯一入口，白名单把它堵死。
_MEDIA_TYPES = {
    ".md": "text/plain; charset=utf-8",
    ".markdown": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_DEFAULT_MEDIA_TYPE = "application/octet-stream"


def _inside_uploads(path: Path, uploads: Path) -> Path | None:
    """路径必须在 uploads 目录内（防穿越），不在则 None。"""
    try:
        resolved = path.resolve()
    except OSError:  # 非法路径（Windows 保留名等）
        return None
    return resolved if resolved.is_relative_to(uploads) and resolved.is_file() else None


def _resolve_upload_file(settings: Settings, doc: Document) -> Path:
    """把 `documents.file_path` 解析到 uploads 下真实存在的文件；失败抛 404。

    DB 里的 file_path 是**不可信的历史值**：实测 23 行里有 21 行指向已废弃的
    旧项目路径（`D:\\Code\\MyProject1\\data\\uploads\\…`）。两跳解析：

      ① 按名称：`uploads_dir / PureWindowsPath(file_path).name`。必须用
         `PureWindowsPath` 而非 `Path`——脏数据是反斜杠形式，POSIX 下
         `Path("D:\\a\\b.md").name` 会把整串当成单个文件名而失配，而
         `PureWindowsPath` 同时兼容 `\\` 与 `/`，跨平台行为一致。
      ② 回退按标题：uploads 下 `stem == doc.title` 的候选，逐个用
         `sha256_file` 与入库时的 `file_sha256` 验身（与 ingest 同口径）。

    每跳都做容器校验；**不做模糊匹配**——发错文件比 404 更糟。
    """
    uploads = settings.uploads_dir.resolve()

    hit = _inside_uploads(uploads / PureWindowsPath(doc.file_path or "").name, uploads)
    if hit is not None:
        return hit

    candidates = [
        p
        for p in uploads.iterdir()
        if p.is_file() and p.stem == doc.title and _inside_uploads(p, uploads) is not None
    ]
    if not candidates:
        raise HTTPException(status_code=404, detail="原文件不可用：uploads 中未找到对应文件")
    if not doc.file_sha256:
        if len(candidates) == 1:
            return candidates[0]
        raise HTTPException(status_code=404, detail="原文件不可用：同名候选不唯一且无哈希可比对")
    for path in candidates:
        if sha256_file(path) == doc.file_sha256:
            return path
    raise HTTPException(status_code=404, detail="原文件与库中内容不一致")


def _pdf_page_count(settings: Settings, doc: Document) -> int | None:
    """原 PDF 的真实页数（跳页输入框的上界）；取不到返回 None。

    为什么不用 chunks 的 `max(page_number)`：那是**解析出的正文**的最大页码
    ——参考文献区被剔除、尾页也可能没有块，于是它比原文件小。doc 71 实测
    正文到 189 页，而原文件是 301 页（Chrome 阅读器工具栏所示）。原文件
    视图给的是原件，上界就该按原件算。
    """
    try:
        import pymupdf

        path = _resolve_upload_file(settings, doc)
        with pymupdf.open(path) as pdf:
            return int(pdf.page_count)
    except Exception:  # noqa: BLE001 —— 页数只是输入框上界，取不到不该拖垮正文
        return None


@router.get("/api/documents/{doc_id}/content")
def document_content(
    doc_id: int,
    settings: Settings = Depends(get_settings),
) -> dict:
    """阅读视图的正文：chunk 按 seq 拼接并去掉块间重叠，附各块偏移。

    只发偏移不发每块正文（前端按 `text[start:end]` 切即可），避免同一份
    内容在响应里出现两遍。`page_max` 供 PDF 页码跳转输入框定上界。

    pending / failed 文档返回 200 + 空 chunks：前端据此显示"尚未入库完成"
    引导，不为这种情况新增错误分支（用测试锁定该语义）。
    """
    with open_db(settings.db_path) as conn:
        doc = repo.get_document(conn, doc_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="文档不存在")
        chunks = repo.chunks_by_document(conn, doc_id)

    stitched = stitch_chunks(chunks)
    pages = [c.page_number for c in chunks if c.page_number is not None]
    file_pages = _pdf_page_count(settings, doc) if doc.file_type == "pdf" else None
    return {
        "document": _public_document(doc),
        "text": stitched.text,
        "chunks": [
            {
                "chunk_id": span.chunk_id,
                "seq": span.seq,
                "start": span.start,
                "end": span.end,
                "heading_path": span.heading_path,
                "page_number": span.page_number,
            }
            for span in stitched.spans
        ],
        "page_max": max(pages) if pages else None,
        # 原件真实页数（PDF 才有）：跳页输入框的上界。与 page_max（正文页数）
        # 分开给，前端优先用它。
        "file_pages": file_pages,
        "trimmed_chars": stitched.trimmed_chars,
    }


@router.get("/api/documents/{doc_id}/file")
def document_file(
    doc_id: int,
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    """原文件流（PDF 交给浏览器自带阅读器，md/txt 直接显示原文）。

    `content_disposition_type="inline"` **必须显式传**：Starlette 的
    FileResponse 默认是 `attachment`，那样 Chrome 会直接下载而不内嵌渲染，
    PDF 视图会整个失效。Range 由 FileResponse 自身支持（206/416/多段），
    28MB 的 PDF 可拖动进度条。
    """
    with open_db(settings.db_path) as conn:
        doc = repo.get_document(conn, doc_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="文档不存在")
    path = _resolve_upload_file(settings, doc)
    suffix = path.suffix.lower() or f".{doc.file_type.lower()}"
    return FileResponse(
        path,
        media_type=_MEDIA_TYPES.get(suffix, _DEFAULT_MEDIA_TYPE),
        content_disposition_type="inline",
        headers={"X-Content-Type-Options": "nosniff"},
    )


# 页面渲染的 DPI 夹取：太小看不清、太大单页几十 MB（301 页的 PDF 尤其）
_DPI_MIN, _DPI_MAX, _DPI_DEFAULT = 60, 240, 110


@router.get("/api/documents/{doc_id}/page/{page_no}.png")
def document_page_image(
    doc_id: int,
    page_no: int,
    dpi: int = _DPI_DEFAULT,
    settings: Settings = Depends(get_settings),
) -> Response:
    """把 PDF 的某一页**按需**渲染成 PNG（阅读视图"页面"视图用）。

    不预渲染、不落盘：单页渲染实测毫秒级，301 页全渲染会产生几十 MB 无用
    数据；浏览器侧靠 URL 缓存即可。渲染是确定性的，故给一小时私有缓存。
    """
    with open_db(settings.db_path) as conn:
        doc = repo.get_document(conn, doc_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="文档不存在")
    if doc.file_type != "pdf":
        raise HTTPException(status_code=400, detail="只有 PDF 有页面图")
    path = _resolve_upload_file(settings, doc)  # 解析失败自己抛 404

    try:
        import pymupdf

        with pymupdf.open(path) as pdf:
            if not 1 <= page_no <= pdf.page_count:
                raise HTTPException(status_code=404, detail=f"页号超出范围（1-{pdf.page_count}）")
            pix = pdf[page_no - 1].get_pixmap(dpi=max(_DPI_MIN, min(int(dpi), _DPI_MAX)))
            data = pix.tobytes("png")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 —— 渲染失败要给出可读原因，不裸抛 500
        raise HTTPException(status_code=500, detail=f"页面渲染失败：{exc}") from exc

    return Response(
        content=data,
        media_type="image/png",
        headers={
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/api/documents/{doc_id}/page/{page_no}/text")
def document_page_text(
    doc_id: int,
    page_no: int,
    settings: Settings = Depends(get_settings),
) -> dict:
    """PDF 某页的**文字层**：页面图上叠透明文字，使其可选中、可复制。

    为什么需要（2026-09-11 用户诉求）：页面视图是位图，本身选不了字。把 PDF
    里真实的文字按原位透明地铺一层，拖动选择时选中的就是文字本身，复制出来
    是真文本——与 PDF.js 的 text layer 同一思路。
    坐标一律**归一化**（除以页宽/页高）：与渲染 DPI 无关，前端缩放不用重算；
    `size` 也按页高归一化，前端换算成字号（见 reader.js 的 em 技巧）。
    """
    with open_db(settings.db_path) as conn:
        doc = repo.get_document(conn, doc_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="文档不存在")
    if doc.file_type != "pdf":
        raise HTTPException(status_code=400, detail="只有 PDF 有文字层")
    path = _resolve_upload_file(settings, doc)  # 解析失败自己抛 404

    try:
        import pymupdf

        with pymupdf.open(path) as pdf:
            if not 1 <= page_no <= pdf.page_count:
                raise HTTPException(status_code=404, detail=f"页号超出范围（1-{pdf.page_count}）")
            page = pdf[page_no - 1]
            width, height = page.rect.width, page.rect.height
            spans: list[dict] = []
            for block in page.get_text("dict")["blocks"]:
                if block.get("type") != 0:  # 只取文本块；图片块没有可选文字
                    continue
                for line in block["lines"]:
                    for span in line["spans"]:
                        text = span["text"]
                        if not text.strip():
                            continue  # 纯空白 span（排版占位）不铺，免得干扰选择
                        x0, y0, x1, y1 = span["bbox"]
                        spans.append(
                            {
                                "x": round(x0 / width, 5),
                                "y": round(y0 / height, 5),
                                "w": round((x1 - x0) / width, 5),
                                "h": round((y1 - y0) / height, 5),
                                "size": round(span["size"] / height, 5),
                                "t": text,
                            }
                        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 —— 提取失败要给可读原因，不裸抛 500
        raise HTTPException(status_code=500, detail=f"文字层提取失败：{exc}") from exc

    return {"page": page_no, "spans": spans}


@router.get("/api/documents/{doc_id}/locate/{chunk_id}")
def locate_chunk_on_page(
    doc_id: int,
    chunk_id: int,
    settings: Settings = Depends(get_settings),
) -> dict:
    """把某个块定位到它的 PDF 页面上的矩形（阅读视图的引用高亮用）。

    返回**归一化到 0~1** 的矩形列表（按行分组），前端按任意 DPI 缩放即可。
    定位算法见 `ingest/pagelocate.py`（词元序列匹配，实测 98%）。

    定位不到不是错误：块的文本可能跨页或被清洗得与原文差太远，此时返回空
    rects，前端只显示页面图不高亮——**优雅降级，不打断阅读**。
    """
    empty: dict = {"page": None, "rects": [], "matched": 0}
    with open_db(settings.db_path) as conn:
        doc = repo.get_document(conn, doc_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="文档不存在")
        chunk = repo.chunk_by_id(conn, chunk_id)
        if chunk is None or chunk.document_id != doc_id:
            raise HTTPException(status_code=404, detail="原文片段不存在")
    if doc.file_type != "pdf" or chunk.page_number is None:
        return empty
    path = _resolve_upload_file(settings, doc)

    try:
        import pymupdf

        with pymupdf.open(path) as pdf:
            # 块可能跨页：先试它所属的页，没命中就试前后各一页（页码历史数据
            # 曾有偏移，这个窗口也顺带兜住那类残留）
            for n in (chunk.page_number, chunk.page_number + 1, chunk.page_number - 1):
                if not 1 <= n <= pdf.page_count:
                    continue
                page = pdf[n - 1]
                found = locate_in_page(
                    page.get_text("words"),
                    chunk.content,
                    page_width=page.rect.width,
                    page_height=page.rect.height,
                )
                if found.ok:
                    return {
                        "page": n,
                        "rects": [list(r) for r in found.rects],
                        "matched": found.matched,
                    }
    except Exception as exc:  # noqa: BLE001 —— 定位是增强，失败只降级不报错
        logger.warning("块定位失败（%s/%s）：%s", doc_id, chunk_id, exc)
    return empty


@router.get("/api/chunks/{chunk_id}")
def locate_chunk(
    chunk_id: int,
    settings: Settings = Depends(get_settings),
) -> dict:
    """chunk_id → 所属文档与位置（引用跳转的那一跳）。

    `Citation` 只带 chunk_id 不带 document_id（models/answer.py），前端要
    打开"该引用所属的那篇文档"就得先反查这一下。
    """
    with open_db(settings.db_path) as conn:
        chunk = repo.chunk_by_id(conn, chunk_id)
        if chunk is None:
            raise HTTPException(status_code=404, detail="原文片段不存在（文档可能已被删除）")
        doc = repo.get_document(conn, chunk.document_id)
    return {
        "chunk_id": chunk.id,
        "document_id": chunk.document_id,
        "seq": chunk.seq,
        "heading_path": chunk.heading_path,
        "page_number": chunk.page_number,
        "document_title": doc.title if doc is not None else "",
    }


@router.post("/api/documents", status_code=201)
def upload_document(
    file: UploadFile,
    services: AppServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    """上传文档入库（md/txt/pdf/docx，≤ upload_max_mb）。"""
    # ---- 文件名校验：净化失败 → 415；净化后再查后缀白名单 → 415 ----
    try:
        safe_name = sanitize_filename(file.filename or "")
    except ValueError:
        raise HTTPException(status_code=415, detail="文件名不合法") from None
    suffix = Path(safe_name).suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        shown = suffix if suffix else "（无扩展名）"
        raise HTTPException(
            status_code=415,
            detail=f"不支持的文件类型 {shown}（支持：md / markdown / txt / pdf / docx）",
        )

    # ---- 大小上限：seek 量真实字节数 → 413 ----
    # multipart 上传体已由 Starlette 落在磁盘临时文件，seek 量大小零内存
    # 开销——超限文件不会被整载入内存，上限因此只受"解析/嵌入耗时"约束
    # 而非内存（upload_max_mb 默认 500MB，覆盖单本扫描书）。
    raw_file = file.file
    raw_file.seek(0, os.SEEK_END)
    size = raw_file.tell()
    raw_file.seek(0)
    max_bytes = settings.web.upload_max_mb * 1024 * 1024
    if size > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过大小上限（{settings.web.upload_max_mb} MB）",
        )
    if size == 0:
        raise HTTPException(status_code=400, detail="文件为空")

    # ---- 流式落地 web-tmp（拷贝与内容哈希同循环，峰值内存与文件大小
    # 解耦；哈希口径与 ingest 的 sha256_file 一致：1MB 分块 + hexdigest），
    # 完成后交给 IngestService（幂等/同名替换/嵌入） ----
    tmp_dir = settings.data_dir / "web-tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # 每次上传独占一个临时子目录：前端多选/重复选文件是并发 POST
    # （documents.js 逐个 upload 不 await），同名请求若共用同一路径会互相
    # 截断覆盖（落盘内容损坏却照常入库），先完成者的 unlink 还会撞上后来者
    # 仍持有的句柄（Windows 报 WinError 32，missing_ok 兜不住）。
    # 唯一性只能加在**目录**上——文件名必须保持 safe_name：ingest 以 basename
    # 决定 uploads 副本名与标题（改文件名会连带改掉文档名与同名替换语义）。
    tmp_path = tmp_dir / uuid4().hex / safe_name
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with tmp_path.open("wb") as out:
        while block := raw_file.read(1024 * 1024):
            digest.update(block)
            out.write(block)
    file_sha = digest.hexdigest()
    return ingest_web_file(tmp_path, safe_name, file_sha, services, settings)


def ingest_web_file(
    tmp_path: Path,
    safe_name: str,
    file_sha: str,
    services: AppServices,
    settings: Settings,
) -> JSONResponse:
    """web-tmp 文件 → 入库 → 201/200/409 响应（上传与论文导入共用同一尾链）。

    论文导入端点（papers.py）下载完 PDF 后走这里，产出与上传**逐字节同形**
    的响应：前端树刷新/toast 判定零改动。入口契约：tmp_path 是 web-tmp 下
    本次操作独占子目录里的文件，safe_name 是净化后的文件名（ingest 以其
    basename 决定 uploads 副本名与标题）。
    """
    try:
        # 入库失败（空文档/解析错误）→ 业务 400，文案即 exc 消息
        status, added_chunks, added_chars = services.ingest.ingest_one(tmp_path)
    except (ZhiwenError, OSError) as exc:
        raise HTTPException(
            status_code=400, detail=f"入库失败：{type(exc).__name__}: {strip_paths(str(exc))}"
        ) from exc
    finally:
        try:
            tmp_path.unlink(missing_ok=True)  # uploads 副本由 ingest_one 管理
            tmp_path.parent.rmdir()  # 连同本次独占的子目录一起收走
        except OSError:  # pragma: no cover - 句柄被外部程序（杀毒/索引器）占用
            # web-tmp 不是权威数据，删不掉也只是留个临时文件，不升级为用户可见错误
            logger.warning("web-tmp 临时目录未能清理：%s", tmp_path.parent)
    services.ask.invalidate_index()  # 语料变了：下次提问自动重建快照

    if status == "skipped":
        # 同内容文档已入库：幂等成功（HTTP 语义：资源已存在）。按内容
        # 哈希找既有文档展示——可能"同内容不同名"（旧文档在前）
        with open_db(settings.db_path) as conn:
            duplicate = repo.get_document_by_sha(conn, file_sha)
        # 不能用 assert：它是**请求路径上的校验**，并发下真会不成立（这个文档
        # 可能刚被另一个请求删掉），此时 assert 变成 500；而 `python -O` 会把断言
        # 整个剥掉，`_public_document(None)` 再抛 AttributeError——同样 500 但更难查
        # （2026-09-11 审查指出）。
        if duplicate is None:
            raise HTTPException(status_code=409, detail="该内容对应的文档刚被删除，请重新上传")
        return JSONResponse(
            status_code=200,
            content={
                "document": _public_document(duplicate),
                "message": "内容与库中现有文档相同，已跳过重复导入",
            },
        )

    # ingested：uploads 副本路径确定（uploads/<净化名>），直接按路径查行
    with open_db(settings.db_path) as conn:
        row = repo.get_document_by_path(conn, str(settings.uploads_dir / safe_name))
    # 同上：入库刚成功、这一行也可能被并发删除请求清掉，assert 会变成 500
    if row is None:
        raise HTTPException(status_code=409, detail="文档刚被删除，请刷新列表确认")

    logger.info("Web 上传入库：%s（+%d 块）", safe_name, added_chunks)
    return JSONResponse(
        status_code=201,
        content={
            "document": _public_document(row),
            "message": f"入库成功：新增 {added_chunks} 块 / {added_chars} 字符",
        },
    )


@router.delete("/api/documents/{doc_id}")
def delete_document(
    doc_id: int,
    services: AppServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
) -> dict:
    """删除文档（chunks/embeddings 级联清理）+ 失效索引快照。

    **整段与 ingest/reindex 互斥**：删行与"按路径 unlink 副本"是两步，而 reindex
    会按同一路径重建新行——不互斥时 reindex 刚建好的行会用着已被删掉的副本，
    文档凭空消失且不可恢复（2026-09-11 审查发现）。
    """
    with services.ingest.exclusive():
        with open_db(settings.db_path) as conn:
            doc = repo.get_document(conn, doc_id)
            if doc is None:
                raise HTTPException(status_code=404, detail="文档不存在")
            repo.delete_document(conn, doc_id)
        # 删 uploads 副本：否则 reindex 会扫到它"复活"成新文档。
        # 必须先过容器校验——file_path 可能是历史脏值（项目改名前的
        # D:\Code\MyProject1\... 路径，实测 23 行里 21 行如此），无条件 unlink
        # 会删掉 uploads 之外的同名文件（2026-09-11 加固，口径同 _inside_uploads）
        if doc.file_path:
            # uploads 目录要 resolve：与 _resolve_upload_file 同口径。传相对路径时
            # is_relative_to 永远为假 → 副本静默不删 → 下次 reindex 把它"复活"
            copy = _inside_uploads(Path(doc.file_path), settings.uploads_dir.resolve())
            if copy is not None:
                copy.unlink(missing_ok=True)
    services.ask.invalidate_index()
    return {"deleted": doc_id, "title": doc.title}


# ---------------------------------------------------------------------------
# 语料文件夹树（v3）：镜像 sessions.py 的 qa 文件夹端点，一套同构语义
# ---------------------------------------------------------------------------


def _clean_folder_name(raw: str) -> str:
    """文件夹名：strip 后空 → 400（与 qa 文件夹同名语义）。"""
    name = raw.strip()
    if not name:
        raise HTTPException(status_code=400, detail="文件夹名不能为空")
    return name


def _clean_doc_title(raw: str | None) -> str:
    """文档标题：null/空串 → 400（文档标题没有"清除"概念，见 DocumentPatchIn）。"""
    title = (raw or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="标题不能为空")
    return title


@router.get("/api/kb-folders")
def list_kb_folders(settings: Settings = Depends(get_settings)) -> dict:
    """全部语料文件夹（id 升序平铺）——语料页树形栏的文件夹数据源。"""
    with open_db(settings.db_path) as conn:
        return {"folders": repo.list_kb_folders(conn)}


@router.post("/api/kb-folders", status_code=201)
def create_kb_folder(
    body: FolderIn,
    settings: Settings = Depends(get_settings),
) -> dict:
    """新建语料文件夹（parent_id 缺省/None = 根级；父不存在 → 404）。"""
    name = _clean_folder_name(body.name)
    with open_db(settings.db_path) as conn:
        if body.parent_id is not None and repo.get_kb_folder(conn, body.parent_id) is None:
            raise HTTPException(status_code=404, detail="父文件夹不存在")
        folder_id = repo.create_kb_folder(conn, name, parent_id=body.parent_id)
        folder = repo.get_kb_folder(conn, folder_id)
    assert folder is not None  # 刚创建必在
    return {"folder": folder}


@router.patch("/api/kb-folders/{folder_id}")
def patch_kb_folder(
    folder_id: int,
    body: FolderPatchIn,
    settings: Settings = Depends(get_settings),
) -> dict:
    """语料文件夹改名/移动（可只带其一）。防环：新父 ∈ 自身∪后代 → 409；
    移回根（显式 null）恒允许。"""
    with open_db(settings.db_path) as conn:
        folder = repo.get_kb_folder(conn, folder_id)
        if folder is None:
            raise HTTPException(status_code=404, detail="文件夹不存在")
        if "name" in body.model_fields_set:
            repo.rename_kb_folder(conn, folder_id, _clean_folder_name(body.name or ""))
        if "parent_id" in body.model_fields_set:
            parent_id = body.parent_id
            if parent_id is not None:
                if repo.get_kb_folder(conn, parent_id) is None:
                    raise HTTPException(status_code=404, detail="父文件夹不存在")
                if parent_id in repo.kb_folder_descendant_ids(conn, folder_id):
                    raise HTTPException(status_code=409, detail="不能把文件夹移入自身或其子文件夹")
            repo.move_kb_folder(conn, folder_id, parent_id)
        updated = repo.get_kb_folder(conn, folder_id)
    assert updated is not None  # 上一步 404 已挡
    return {"folder": updated}


@router.delete("/api/kb-folders/{folder_id}")
def delete_kb_folder(
    folder_id: int,
    settings: Settings = Depends(get_settings),
) -> dict:
    """删除**空**语料文件夹：直接含子文件夹或文档 → 409 带计数文案（先移出）。
    容器不连坐内容，绝不级联删除用户文档（kb_folders 外键 NO ACTION 兜底）。"""
    with open_db(settings.db_path) as conn:
        folder = repo.get_kb_folder(conn, folder_id)
        if folder is None:
            raise HTTPException(status_code=404, detail="文件夹不存在")
        counts = repo.kb_folder_children_counts(conn, folder_id)
        if counts["subfolders"] or counts["documents"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"文件夹非空：含 {counts['subfolders']} 个子文件夹、"
                    f"{counts['documents']} 篇文档，请先移出后再删除"
                ),
            )
        repo.delete_kb_folder(conn, folder_id)
    return {"deleted": folder_id, "name": folder["name"]}


@router.patch("/api/documents/{doc_id}")
def patch_document(
    doc_id: int,
    body: DocumentPatchIn,
    settings: Settings = Depends(get_settings),
) -> dict:
    """文档改名/移夹（可只带其一）。folder_id 显式 null = 移回根；
    只改组织属性（documents 行），不碰 uploads 文件与索引快照。"""
    with open_db(settings.db_path) as conn:
        doc = repo.get_document(conn, doc_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="文档不存在")
        if "title" in body.model_fields_set:
            repo.set_document_title(conn, doc_id, _clean_doc_title(body.title))
        if "folder_id" in body.model_fields_set:
            folder_id = body.folder_id
            if folder_id is not None and repo.get_kb_folder(conn, folder_id) is None:
                raise HTTPException(status_code=404, detail="文件夹不存在")
            repo.move_document(conn, doc_id, folder_id)
        updated = repo.get_document(conn, doc_id)
    assert updated is not None  # 上一步 404 已挡
    return {"document": _public_document(updated)}
