"""在线论文检索端点（知识库页「在线找论文」面板，见 ADR-0019）。

四件事：
  POST /api/papers/search     检索（双源交错、逐源降级：单源失败进 errors，全灭才 502）
  POST /api/papers/import     下载 PDF 并入库（响应与上传端点逐字节同形）
  GET  /api/papers/settings   当前是否已存 OpenAlex 密钥（只回布尔，永不回值）
  PUT  /api/papers/settings   保存/清除 OpenAlex 密钥（.env + 热生效，三段语义）

**导入链路**：按 {source, id} 反查来源 API 取元数据（**不信客户端标题**）
→ 无开放获取全文 → 409（提示 DOI 跳转）→ 防护下载（SSRF/类型/大小/重定向，
见 papers/download.py）→ web-tmp 独占子目录 → 与上传共用
`documents.ingest_web_file`（201/200/409 响应形状一致）。

**文件名防撞**：`标题[:80] (来源 id).pdf`——ingest 的"同名替换"语义下，
两篇同名论文会互相踩掉；来源 id 后缀保证唯一，重复内容仍走 sha256 跳过。
"""

from __future__ import annotations

import dataclasses
import os
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from mikasa.config.settings import Settings, clear_api_key, write_api_key
from mikasa.papers import PaperError, fetch_paper
from mikasa.papers import search as papers_search
from mikasa.papers.arxiv import validate_id as validate_arxiv_id
from mikasa.papers.download import download_pdf
from mikasa.papers.openalex import validate_id as validate_openalex_id
from mikasa.papers.service import SOURCES
from mikasa.utils.hashing import sha256_file
from mikasa.utils.logging import get_logger
from mikasa.web.deps import get_services, get_settings
from mikasa.web.routers.documents import ingest_web_file, sanitize_filename
from mikasa.web.schemas import PaperImportIn, PaperKeyIn, PaperSearchIn

router = APIRouter(tags=["papers"])
logger = get_logger(__name__)

# 密钥写回串行化：写文件 + 改进程环境必须是一个整体（同 settings.py 的 _APPLY_LOCK 语义）
_KEY_LOCK = threading.Lock()

# 标题截断上限：文件名总长留给 sanitize 的 115 字符 stem 限制，
# 后缀 " (来源 id).pdf" 必须完整存活（sanitize 是保头截尾，先截标题再加后缀）
_TITLE_CAP = 80


def _validate_source_id(source: str, paper_id: str) -> None:
    """id 白名单（客户端输入，双重校验：正则过了才进来源 API 的拼接 URL）。"""
    valid = validate_arxiv_id(paper_id) if source == "arxiv" else validate_openalex_id(paper_id)
    if not valid:
        raise HTTPException(status_code=422, detail=f"非法的论文编号：{paper_id}")


def _discard_tmp(tmp_path: Path) -> None:
    """下载失败时清掉半成品与本次独占子目录（成功路径的收尾归 ingest_web_file）。

    子目录一并删：只删文件会让 web-tmp 每次失败都积一个空目录。
    """
    try:
        tmp_path.unlink(missing_ok=True)
        tmp_path.parent.rmdir()
    except OSError:  # pragma: no cover - 句柄被外部程序（杀毒/索引器）占用
        logger.warning("web-tmp 临时目录未能清理：%s", tmp_path.parent)


def _search_source_names(body: PaperSearchIn) -> int:
    """本次请求实际打几个源（用于"全灭才 502"的判定）。"""
    return len(SOURCES) if body.source == "all" else 1


@router.post("/api/papers/search")
def search_papers(body: PaperSearchIn) -> dict:
    """检索一页。逐源降级：单源失败进 errors 字典（照常 200），全灭才 502。"""
    page = papers_search(body.q, body.source, body.offset, body.limit)
    if not page.results and len(page.errors) == _search_source_names(body):
        raise HTTPException(
            status_code=502, detail="论文检索暂时不可用：" + "；".join(page.errors.values())
        )
    return {
        "results": [dataclasses.asdict(r) for r in page.results],
        "errors": page.errors,
        "has_more": page.has_more,
    }


@router.post("/api/papers/import")
def import_paper(
    body: PaperImportIn,
    services=Depends(get_services),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    """下载论文 PDF 并入库（响应形状与上传端点一致）。"""
    _validate_source_id(body.source, body.id)
    try:
        result = fetch_paper(body.source, body.id)
    except PaperError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not result.pdf_url:
        hint = "，可点结果里的「打开」跳转到详情页查看" if result.landing_url else ""
        raise HTTPException(
            status_code=409,
            detail=f"这篇论文没有开放获取全文（{result.venue or result.source}）{hint}",
        )

    # 文件名：标题（先截断）+ 来源 id 后缀 → sanitize 净化（sanitize 保头截尾，
    # 后缀必须放在截断**之后**追加，否则长标题会把去重后缀截掉）
    safe_title = sanitize_filename(result.title[:_TITLE_CAP] or "(无标题)")
    safe_name = f"{safe_title} ({result.source} {result.id}).pdf"

    tmp_path = settings.data_dir / "web-tmp" / uuid.uuid4().hex / safe_name
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        download_pdf(result.pdf_url, tmp_path)
    except PaperError as exc:
        _discard_tmp(tmp_path)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 下载器之外的环境性失败也归 502
        _discard_tmp(tmp_path)
        raise HTTPException(
            status_code=502, detail=f"论文下载失败：{type(exc).__name__}: {exc}"
        ) from exc
    file_sha = sha256_file(tmp_path)
    # ingest_web_file 自带 ZhiwenError→400 翻译与 finally 清理，这里直接交棒
    return ingest_web_file(tmp_path, safe_name, file_sha, services, settings)


@router.get("/api/papers/settings")
def get_papers_settings() -> dict:
    """当前是否已存 OpenAlex 密钥（**只回布尔**，值永不回显，同 ADR-0002 纪律）。"""
    return {"has_api_key": bool(os.environ.get("OPENALEX_API_KEY"))}


@router.put("/api/papers/settings")
def save_papers_settings(body: PaperKeyIn) -> dict:
    """保存/清除 OpenAlex 密钥：写数据目录 .env + 立即写进程环境（热生效）。

    无 locked 检查：密钥写的是 .env（读取链第一候选），不被 --config 遮蔽。
    """
    with _KEY_LOCK:
        if body.api_key == "":
            clear_api_key("OPENALEX_API_KEY")
            os.environ.pop("OPENALEX_API_KEY", None)
        elif body.api_key:
            write_api_key("OPENALEX_API_KEY", body.api_key)
            os.environ["OPENALEX_API_KEY"] = body.api_key
    return {"has_api_key": bool(os.environ.get("OPENALEX_API_KEY"))}
