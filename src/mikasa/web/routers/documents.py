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

**笔记（M6 ①，2026-09-16，ADR-0021）**：文件末尾的 `/api/notes` 三端点让
用户在知识库页直接写 Markdown。笔记没有自己的表、没有自己的类型——它是
`source_ref = "note:<key>"` 的**普通文档**，走的就是上面的上传入库链路。
设计要点与三个坑见该段头注释。
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
from mikasa.errors import ProviderError, ZhiwenError, strip_paths
from mikasa.ingest.pagelocate import locate_in_page
from mikasa.ingest.stitch import stitch_chunks
from mikasa.models.document import Document
from mikasa.storage import repo
from mikasa.storage.db import open_db
from mikasa.utils.hashing import sha256_file
from mikasa.utils.logging import get_logger
from mikasa.utils.text import decode_text
from mikasa.web.deps import get_services, get_settings
from mikasa.web.schemas import DocumentPatchIn, FolderIn, FolderPatchIn, NoteIn, NoteUpdateIn
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
    索引信息不出库。source_ref 是**公开元数据**（同一个 arXiv id / DOI
    本来就在检索结果里明文展示），故不属脱敏对象——但这是一次有意的
    边界扩张，已记入 ADR-0020。
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
        "source_ref": doc.source_ref,
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
    source_ref: str | None = None,
    force: bool = False,
    title: str | None = None,
    folder_id: int | None = None,
) -> JSONResponse:
    """web-tmp 文件 → 入库 → 201/200/409 响应（上传、论文导入、笔记共用尾链）。

    论文导入端点（papers.py）下载完 PDF 后走这里，产出与上传**同形**的响应
    （同一个 `_public_document` + 同类文案，前端树刷新/toast 判定零改动；
    "同形"指三条链路彼此一致，不指与历史版本逐字节相同——响应体会随公开
    字段的扩张而新增键，例如 ADR-0020 起多出的 `source_ref`）。
    入口契约：tmp_path 是 web-tmp 下本次操作独占子目录里的文件，safe_name
    是净化后的文件名（ingest 以其 basename 决定 uploads 副本名与标题）。

    source_ref：论文导入传 "arxiv:2401.12345"，笔记链路传 "note:<key>"，
    上传/CLI 恒为 None。

    force/title 只由笔记链路使用（见 notes 段）：
      - force=True 绕过 sha 内容去重——否则"第二条内容相同的笔记"会被静默
        跳过（用户以为存了、树里没有），编辑到与另一条笔记同内容时响应还会
        指到别人那行。同名替换（编辑笔记）在 force 下照常原地更新。
      - title 是编辑器里写的标题，优先级高于继承来的旧标题（见 IngestService）。
      - folder_id 只在"新建笔记"时给（上传/论文导入都落根级）：落夹必须在
        入库后、**同一把锁内**完成，否则会出现"入库成功但没落到指定文件夹"
        的中间态（调用方传它时已持有 services.ingest.exclusive()）。
    """
    try:
        # 入库失败（空文档/解析错误）→ 业务 400，文案即 exc 消息
        status, added_chunks, added_chars = services.ingest.ingest_one(
            tmp_path, force=force, source_ref=source_ref, title=title
        )
    except (ZhiwenError, OSError) as exc:
        # 嵌入/模型服务失败（密钥、额度、网络）是服务端问题 → 502，与 app 层
        # 处理器对 ProviderError 的判定一致；此前一律 400，把"上游额度用尽"
        # 报成客户端错误（2026-09-16 记录、2026-09-17 修复）。其余（空文档/
        # 解析错误/文件锁）仍是业务 400。
        raise HTTPException(
            status_code=502 if isinstance(exc, ProviderError) else 400,
            detail=f"入库失败：{type(exc).__name__}: {strip_paths(str(exc))}",
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
        # 来源回填：用户自己拖过这篇 PDF（source_ref 为 NULL）之后又在
        # 「找论文」页导入同一篇，sha256 会在这里直接跳过——不回填的话
        # 这行永远是 NULL，搜索结果会一直显示"未在库中"，而字节明明在库里。
        # 只在原本没有来源时写（有来源的不覆盖：先来的更权威）。
        if source_ref and duplicate.source_ref is None and duplicate.id is not None:
            with open_db(settings.db_path) as conn:
                repo.set_document_source_ref(conn, duplicate.id, source_ref)
                conn.commit()
                refreshed = repo.get_document(conn, duplicate.id)
            duplicate = refreshed or duplicate
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
        if row is not None and folder_id is not None and row.id is not None:
            # 落夹：与入库同一个连接、同一把锁内补一次 UPDATE（见 docstring）。
            # 不走 PATCH 端点是为了避免"入库成功、落夹 404/失败"的半拉子状态。
            repo.move_document(conn, row.id, folder_id)
            conn.commit()
            row = repo.get_document(conn, row.id)
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
                try:
                    copy.unlink(missing_ok=True)
                except OSError:
                    # 只读/被别的程序占着（Windows 文件锁）：行已经删了，再抛就是
                    # 500——而用户看到的"删除失败"其实只差一个文件（2026-09-16
                    # 对抗性实测）。如实记日志；这条副本仍是 reindex 的复活隐患，
                    # 所以日志要说清楚怎么收拾。
                    logger.warning(
                        "文档已从库中删除，但 uploads 副本删不掉（可能被占用或只读），"
                        "下次全量重建会把它当新文档扫回来，请手工删除：%s",
                        copy,
                    )
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
    services: AppServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
) -> dict:
    """语料文件夹改名/移动（可只带其一）。防环：新父 ∈ 自身∪后代 → 409；
    移回根（显式 null）恒允许。

    持 ingest 锁（同 DELETE，见那边的理由）：文件夹的存在性是入库链路
    在锁内校验过的前提，锁外改它会让"校验通过 → 落夹"之间出现空档。
    """
    with services.ingest.exclusive(), open_db(settings.db_path) as conn:
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
    services: AppServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
) -> dict:
    """删除**空**语料文件夹：直接含子文件夹或文档 → 409 带计数文案（先移出）。
    容器不连坐内容，绝不级联删除用户文档（kb_folders 外键 NO ACTION 兜底）。

    持 ingest 锁（同 delete_document）：新建笔记是"锁内校验文件夹存在 →
    入库（可能十几秒）→ 落夹"，文件夹若能在锁外被删掉，落夹的 UPDATE 会撞
    外键 → 全局处理器翻成 409，而**笔记其实已经入库成功**——用户看到"保存
    失败"再重试，就多出一篇重复笔记（2026-09-16 审查实测）。
    """
    with services.ingest.exclusive(), open_db(settings.db_path) as conn:
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


# ---------------------------------------------------------------------------
# 笔记（M6 ①，2026-09-16）：知识库页直接写 Markdown → 保存即入库可检索
# ---------------------------------------------------------------------------
#
# **笔记 = 带标记的普通文档**（ADR-0021）：标记是 `source_ref = "note:<key>"`，
# 除此之外与上传的 .md 毫无区别——删除/改名/拖进文件夹/被提问引用/阅读视图
# 全部复用既有链路，本段只有"新建 / 编辑 / 读原文"三个端点。
#
# 标记为什么复用 source_ref 而不加 schema 列：
#   - `_KeptProps` 已把它列入 reindex 与同名替换的保留清单 → 标记天然活过
#     全量重建，零额外改动；
#   - `_public_document` 已暴露它（论文"已在库中"就是这么做的）；
#   - 找论文页的 in_library 是精确匹配 `arxiv:<id>` / `core:<id>`，`note:`
#     前缀不会误命中。
# 代价是这一列语义要从"从哪个在线来源导入"泛化成"文档来源标识"（已同步
# 改 db.py 的列注释）。
#
# 三个必须做对的地方（每条都对应一个"看着能跑、其实静默出错"的坑）：
#   1) 入库必须 force=True：`_ingest_one` 的 sha 内容去重会让"第二条内容相同
#      的笔记"直接 skipped 不落库（用户以为存上了、树里没有）；编辑到与另一条
#      笔记同内容时，响应还会按 sha 查到**别人那行**上。
#   2) 标题走 ingest 的显式 title 参数（分块前生效）：分块用标题构造索引词
#      空间，入库后再补 set_document_title 会让"显示名"与"索引里的名字"漂移
#      ——改完标题搜不到新名字。
#   3) 整个保存流程套 services.ingest.exclusive()：否则"保存 vs 删除"会让刚被
#      删掉的笔记以**没有标记的普通文档**复活（ingest 照 tmp 重建副本并插行）。
#
# 已知边界（另见 limitations-and-failures.md）：同名替换会重建行 → 笔记的
# documents.id 每次保存都变（旧回答里引用 chip 指向的 chunk 随之失效）；
# uploads 副本是笔记的**唯一权威副本**（没有"用户手里的原件"可回退）。

NOTE_REF_PREFIX = "note:"
"""笔记标记前缀，**跨端格式合同**：后端在这里生成，前端（kb-tree.js 判菜单项、
note-editor.js 判可编辑性）用同一个前缀判定。改一处必须同步改另一处。"""

_NOTE_STEM_MAX = 60
"""笔记文件名里标题部分的长度上限。

`sanitize_filename` 的主名上限是 115，笔记还要再追加 ` (note <key12>).md`
（23 字符）——深数据目录下留足 MAX_PATH 余量，60 也远超正常标题长度。
"""

_NOTE_NAME_FALLBACK = "无标题"
"""标题净化后没有可见字符时的文件名兜底。

`sanitize_filename` 对 `"???"` / `"..."` / `"   "` 会直接抛 ValueError，
而 `"\\u3000"`（全角空格）能通过它的 `strip(" .")` —— 不兜底会产出一个
**看不见的文件名**（用户与排查者都无从辨认）。
"""


def _note_stem(title: str) -> str:
    """标题 → 可用作文件名主名的形式（与 sanitize_filename 同一套字符规则）。"""
    stem = _FILENAME_BAD.sub("", Path(title).name).strip(" .")
    # **先截断再判可见性**：反过来的话，60 个零宽字符 + "abc" 这种标题会在
    # 判定时被 "abc" 救活，截断后又把 "abc" 切掉 → 产出 60 个看不见的字符，
    # 恰好是 _NOTE_NAME_FALLBACK 要防的那种文件名（2026-09-16 审查实测）
    stem = stem[:_NOTE_STEM_MAX]
    # 全角空格/零宽字符都算不可见：只按 str.strip() 判会漏掉它们
    if not any(ch.isprintable() and ch != "　" and not ch.isspace() for ch in stem):
        return _NOTE_NAME_FALLBACK
    return stem


def _note_name(conn, title: str, uploads: Path) -> tuple[str, str]:
    """生成 (key, uploads 副本文件名)，避开既有来源与既有文件。

    key 是 48 bit 随机（`uuid4().hex[:12]`），碰撞概率可忽略，但仍然显式查
    一次：碰撞的后果不是报错而是**顶掉别人的行**（同名替换会继承旧行的标题与
    标记），属于"低概率高后果"，检测成本只有一次集合查询。
    """
    taken = set(repo.list_source_refs(conn))
    stem = _note_stem(title)
    for _ in range(20):
        key = uuid4().hex[:12]
        name = f"{stem} (note {key}).md"
        if f"{NOTE_REF_PREFIX}{key}" in taken:
            continue
        # 副本名与上传文件共用 uploads 这个扁平命名空间：重名会被同名替换
        # 逻辑吃掉（上传件顶掉笔记，或反之）
        if not (uploads / name).exists():
            return key, name
    raise HTTPException(status_code=500, detail="笔记标识生成失败，请重试")  # pragma: no cover


def _note_payload(body: str) -> bytes:
    """正文 → 落盘字节：换行统一成 LF，再按 UTF-8 编码。

    浏览器 textarea 的 value 已按 HTML 规范把 CRLF 规范成 LF，但直连 API 的
    调用方可能送 CRLF；不归一的话同一份内容会因换行风格不同算出不同 sha
    （每次打开就保存 = 白重嵌一次），而 Windows 上文本模式写盘还会把 \\n 变
    \\r\\n（写进去的和读出来的不是一份东西）。所以：**二进制写 + 显式归一**。

    编码失败（正文含**孤立代理项**——粘贴自坏 UTF-16 来源）翻成 400：不接住
    就是 UnicodeEncodeError → 500，用户正文里只有一个坏字符却反复"服务器
    内部错误"，还要去翻日志才知道为什么（2026-09-16 审查实测）。
    """
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    try:
        return normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise HTTPException(
            status_code=400,
            detail="正文含无法保存的字符（不完整的代理项，通常来自粘贴），请检查后重试",
        ) from exc


def _is_note(doc: Document) -> bool:
    """是不是笔记（前缀判定；NULL 与 `arxiv:` 都返回 False）。"""
    ref = doc.source_ref
    return ref is not None and ref.startswith(NOTE_REF_PREFIX)


def _is_plain_filename(name: str) -> bool:
    """是不是一个"干净的文件名"：非空、无目录成分、不是 `.` / `..`。

    库里的 file_path 是不可信的历史值。脏到 basename 取成 `..` 时，
    `web-tmp/<uuid>/..` 指向的是目录 → IsADirectoryError → 500（审查实测）。
    POSIX 下反斜杠不是分隔符，所以必须显式再挡一次 `\\`。
    """
    return bool(name) and name not in {".", ".."} and Path(name).name == name and "\\" not in name


def _note_rev(doc: Document) -> str:
    """笔记的乐观锁令牌：**由内容哈希派生**（不直接吐 `file_sha256`）。

    只取内容做派生，不掺 id / updated_at：同名替换会换 id（删旧行插新行），
    而 updated_at 是秒级精度、同一秒内的两次保存看起来一模一样——用它们做版本
    号会出现"自己刚存完却被判成别人改过"的假冲突。内容一变 rev 必变，这正是
    "我打开的那一版还在不在"要判的东西。

    用途见 `update_note`：两个标签页同编一条笔记时，后保存的那个收到 409 而不是
    静默覆盖前一个（2026-09-20 用户点名要修的那条）。
    """
    return hashlib.sha256(f"note:{doc.file_sha256 or ''}".encode()).hexdigest()[:16]


def _get_note(conn, doc_id: int) -> Document:
    """按 id 取笔记行；不存在或不是笔记 → 404（不泄露"这个 id 存在但不是笔记"）。"""
    doc = repo.get_document(conn, doc_id)
    if doc is None or not _is_note(doc):
        raise HTTPException(status_code=404, detail="笔记不存在，或该文档不是笔记")
    return doc


def _write_note_tmp(settings: Settings, safe_name: str, payload: bytes) -> Path:
    """正文落 web-tmp 独占子目录（与上传同款：唯一性加在目录上、文件名保持目标名）。

    文件名必须逐字等于 uploads 副本名——ingest 以 basename 决定副本路径，
    改一个字就变成"新建另一篇"而不是"更新这一篇"。
    """
    tmp_dir = settings.data_dir / "web-tmp"
    tmp_path = tmp_dir / uuid4().hex / safe_name
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path.write_bytes(payload)  # 二进制写：不经过 Windows 的换行翻译
    return tmp_path


def _ingest_note_file(
    tmp_path: Path,
    safe_name: str,
    payload: bytes,
    services: AppServices,
    settings: Settings,
    *,
    title: str,
    source_ref: str | None = None,
    folder_id: int | None = None,
) -> JSONResponse:
    """笔记入库的统一入口：复用上传尾链，只把"文档为空"翻成笔记用户看得懂的话。

    整篇只有标题 / 全是空行时，loader 一个可检索段落都产不出来 →
    尾链抛「入库失败：ZhiwenError: 文档为空（无任何可检索段落）」——对写笔记的
    人这是天书：得知道"哪些东西进不了索引"才看得懂。

    （2026-09-20 起围栏代码块**会**作为纯文本段落进索引，所以"整篇只有代码"
    不再走这条路——本函数的文案只说剩下的那种：只有标题。）
    """
    try:
        return ingest_web_file(
            tmp_path,
            safe_name,
            hashlib.sha256(payload).hexdigest(),
            services,
            settings,
            source_ref=source_ref,
            force=True,
            title=title,
            folder_id=folder_id,
        )
    except HTTPException as exc:
        if exc.status_code == 400 and "文档为空" in str(exc.detail):
            raise HTTPException(
                status_code=400,
                detail=(
                    "正文里没有可被检索的段落——只有标题、或全是空行吗？"
                    "补一段正文（代码块也可以）再保存"
                ),
            ) from exc
        raise


@router.post("/api/notes", status_code=201)
def create_note(
    body: NoteIn,
    services: AppServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    """新建笔记：Markdown 正文落成 uploads 里的 .md 副本，再走既有入库尾链。

    响应与上传**逐字节同形**（同一个 `ingest_web_file`），前端树刷新/toast
    判定零改动。
    """
    title = _clean_doc_title(body.title)
    if not body.body.strip():
        raise HTTPException(status_code=400, detail="笔记正文不能为空")
    payload = _note_payload(body.body)

    with services.ingest.exclusive():  # 与删除/reindex 互斥：见本段头注释第 3 条
        with open_db(settings.db_path) as conn:
            # 文件夹先校验：ingest 里的 move_document 不做存在性检查，直接撞
            # 外键会被全局处理器翻成 409「目标已被其他操作改动」，误导排查
            if body.folder_id is not None and repo.get_kb_folder(conn, body.folder_id) is None:
                raise HTTPException(status_code=404, detail="文件夹不存在")
            key, safe_name = _note_name(conn, title, settings.uploads_dir)
        tmp_path = _write_note_tmp(settings, safe_name, payload)
        return _ingest_note_file(
            tmp_path,
            safe_name,
            payload,
            services,
            settings,
            title=title,
            source_ref=f"{NOTE_REF_PREFIX}{key}",
            folder_id=body.folder_id,
        )


@router.put("/api/notes/{doc_id}")
def update_note(
    doc_id: int,
    body: NoteUpdateIn,
    services: AppServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    """编辑笔记：**同名替换**既有 uploads 副本 → 行原地更新（不产生第二篇）。

    复用 `ingest_web_file` 的同名替换语义：旧行的标题/文件夹/来源会被继承，
    其中标题由显式 `title=` 压成新值（见 IngestService.ingest_one）。
    副本文件名**永不重生成**——名字里的 key 是创建时定下的身份，改名不换文件
    （与 set_document_title"upload 副本文件名不动"的既有政策一致）。
    """
    title = _clean_doc_title(body.title)
    if not body.body.strip():
        raise HTTPException(status_code=400, detail="笔记正文不能为空")
    payload = _note_payload(body.body)
    digest = hashlib.sha256(payload).hexdigest()

    with services.ingest.exclusive():
        uploads = settings.uploads_dir
        with open_db(settings.db_path) as conn:
            doc = _get_note(conn, doc_id)
            # 乐观锁（2026-09-20）：编辑器回传它**打开那一版**的 rev，与库里现在的
            # 对不上就拒绝——两个标签页同编一条笔记时，后保存的那个从此收到 409，
            # 而不是把前一个的改动静默覆盖掉（uploads 副本是笔记唯一的副本，
            # 覆盖不可恢复）。只在**回传了** rev 时判：旧构建不带它，不能因此断路。
            if body.rev is not None and body.rev != _note_rev(doc):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "这条笔记在你打开之后被别处改过了（多半是另一个标签页）——"
                        "已拦下这次保存，免得覆盖那份改动。"
                        "请复制你写的正文，关掉编辑器重新打开这篇，再粘贴保存。"
                    ),
                )
            safe_name = PureWindowsPath(doc.file_path or "").name
            if not _is_plain_filename(safe_name):
                raise HTTPException(
                    status_code=409, detail="笔记的副本文件名异常（历史脏数据），无法编辑"
                )
            copy_path = uploads / safe_name
            # 历史的脏 file_path 修回真实副本路径（见 repo.set_document_file_path）：
            # 不修的话 ingest 的"同名替换"判定用全路径比对，脏路径会被判成
            # "这是个新文件" → **插出第二行**（多一篇、丢标记、索引仍旧）。
            if str(doc.file_path or "") != str(copy_path):
                if repo.get_document_by_path(conn, str(copy_path)) is not None:
                    raise HTTPException(
                        status_code=409,
                        detail="已存在同名的另一篇文档（可能来自历史重复行），请先删除后重试",
                    )
                repo.set_document_file_path(conn, doc_id, str(copy_path))
                conn.commit()
                logger.info("笔记 #%s 的副本路径已修复为 %s", doc_id, safe_name)
            # 无改动短路：正文 sha 与标题都没变就直接返回，不动库。
            # 没有它，"打开编辑器随手点保存"= 删旧行插新行（id 变、重嵌入、
            # 树里的选中态被打断），纯属白付代价。**短路要求副本真的在**：
            # 副本被手工删掉时还回"内容没有变化"是谎报（PUT 本来能自愈）。
            still_there = _inside_uploads(copy_path, uploads.resolve()) is not None
            if (
                still_there
                and doc.title == title
                and doc.file_sha256 == digest
                and sha256_file(copy_path) == doc.file_sha256  # 副本没被外部改坏
                and str(doc.file_path or "") == str(copy_path)
            ):
                return JSONResponse(
                    status_code=200,
                    content={
                        "document": _public_document(doc),
                        "message": "内容没有变化，无需保存",
                    },
                )
        tmp_path = _write_note_tmp(settings, safe_name, payload)
        return _ingest_note_file(
            tmp_path,
            safe_name,
            payload,
            services,
            settings,
            title=title,
            # **显式带上标记**，不指望同名替换从旧行继承：另一个进程的
            # `mikasa ingest --reindex` 能在这两句之间清空全部行（进程内锁管不到
            # 别的进程），那样新行会落成没有标记的普通文档——笔记**永久**降级成
            # 普通文档（文件名里还留着 (note …)）（2026-09-16 对抗性实测命中）。
            source_ref=doc.source_ref,
        )


@router.get("/api/notes/{doc_id}")
def get_note(
    doc_id: int,
    settings: Settings = Depends(get_settings),
) -> dict:
    """读回笔记正文（编辑器的回填数据源）。

    **不能复用 `/content`**：那是按 seq 拼接 chunk 的阅读视图正文，会丢掉
    `#` 标题行、代码围栏、引用标记等 Markdown 语法（chunk 只保留正文），
    而编辑器回填必须与用户写下的**逐字节一致**，否则"打开→保存"就会静默
    重写用户的笔记。这里直接读 uploads 副本原始字节。
    """
    with open_db(settings.db_path) as conn:
        doc = _get_note(conn, doc_id)

    uploads = settings.uploads_dir.resolve()
    hit = _inside_uploads(uploads / PureWindowsPath(doc.file_path or "").name, uploads)
    if hit is None:
        # 只读侧救不了：副本没了，正文只剩索引里的分块（阅读视图的"文本"能给，
        # 但会丢 Markdown 语法）。如实告诉用户去哪儿捞，不劝人"删了重写"。
        raise HTTPException(
            status_code=404,
            detail=(
                "笔记的副本文件在 uploads 里找不到了——可在阅读视图里查看并复制正文，"
                "或删除这篇后用「新建笔记」另存"
            ),
        )
    try:
        raw = hit.read_bytes()
    except OSError as exc:  # 句柄被外部程序占用等：404 优于 500，前端有明确文案
        raise HTTPException(
            status_code=404, detail="笔记副本暂时读不到（可能被其他程序占用），请稍后重试"
        ) from exc
    # rev：编辑器保存时回传，用于"别处改过没有"的判定（见 _note_rev）
    return {"document": _public_document(doc), "body": decode_text(raw), "rev": _note_rev(doc)}
