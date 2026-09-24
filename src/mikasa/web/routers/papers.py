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

import mikasa.papers.openalex as openalex
from mikasa.config.settings import Settings, clear_api_key, write_api_key
from mikasa.errors import strip_paths
from mikasa.papers import PaperError, PaperFilters, fetch_paper, source_catalog
from mikasa.papers import search as papers_search
from mikasa.papers.arxiv import validate_id as validate_arxiv_id
from mikasa.papers.core import validate_id as validate_core_id
from mikasa.papers.doaj import validate_id as validate_doaj_id
from mikasa.papers.download import download_pdf
from mikasa.papers.openalex import validate_id as validate_openalex_id
from mikasa.storage import repo
from mikasa.storage.db import open_db
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


_ID_VALIDATORS = {
    "arxiv": validate_arxiv_id,
    "openalex": validate_openalex_id,
    "core": validate_core_id,
    "doaj": validate_doaj_id,
}


def _validate_source_id(source: str, paper_id: str) -> None:
    """id 白名单（客户端输入，双重校验：正则过了才进来源 API 的拼接 URL）。"""
    validator = _ID_VALIDATORS.get(source)
    if validator is None or not validator(paper_id):
        raise HTTPException(status_code=422, detail=f"非法的论文编号：{paper_id}")


_RELATED_TITLE_CHARS = 8
_RELATED_TITLE_WORDS = 10


def _related_query(title: str) -> str:
    """相关论文的查询串：**中文标题只取前 8 字**，英文取前 10 个词。

    这不是拍脑袋，是量出来的（2026-09-19，两篇真实中文论文各试四种截断）：
      · "南海北部天然气水合物富集特征及定量评价" —— 整串与 12 字都在 OpenAlex
        上**一条都搜不到**（30 秒超时），取前 8 字立刻给出神狐海域天然气水合物
        那一系列论文；
      · "旋喷灌浆锚杆的结构设计及其工程应用分析" —— 前 8 字给出"高压旋喷扩大头
        抗浮锚杆"等同主题文章。
    原因推测是长 CJK 串在它的检索里被切得太碎、相关性算不出来。中文标题的核心
    词几乎总在前半句（"XX的YY研究"），所以截前 8 字是个稳的启发式。
    英文标题没有这个问题，保留整串的前 10 个词即可。
    """
    cjk = sum(1 for ch in title if "一" <= ch <= "鿿")
    if cjk >= 4:
        return title[:_RELATED_TITLE_CHARS]
    return " ".join(title.split()[:_RELATED_TITLE_WORDS])


def _cjk_bigrams(text: str) -> set[str]:
    """汉字二元组集合（只取汉字，标点/数字/字母不参与）。"""
    zh = [ch for ch in text if "一" <= ch <= "鿿"]
    return {zh[i] + zh[i + 1] for i in range(len(zh) - 1)}


def _shares_topic(title: str, candidate: str, minimum: int = 2) -> bool:
    """词面守卫：候选与原标题至少共享 `minimum` 个汉字二元组。

    为什么需要（2026-09-19 实测）：检索是按词袋打分的，中文长标题里随便
    哪个词撞上都能把无关文献抬进前列——「复合填料及其分层」召回了"基于大
    数据的学生综合数据分析"，「南海北部天然气水」召回了"白石砬子地区蛇类
    生态习性"。共享两个**连续汉字**的候选则实测都是同主题（"天然气水合物"
    /"渗滤系统"）。**词面留得住，语义留不住**——所以守在词面这一层。
    """
    if sum(1 for ch in title if "一" <= ch <= "鿿") < 4:
        return True  # 非中文标题不套这条（二元组在英文上没意义）
    return len(_cjk_bigrams(title) & _cjk_bigrams(candidate)) >= minimum


def _items_with_library(settings: Settings, papers) -> list[dict]:  # noqa: ANN001 - 序列即可
    """把 PaperResult 序列转成响应项（含"已在库中"标记）。

    检索与引证两个端点共用：引证结果同样是可导入的论文条目，前端因此能
    复用同一套行渲染与导入链路（ADR-0020 的 source_ref 语义不变）。
    """
    with open_db(settings.db_path) as conn:
        in_library = repo.list_source_refs(conn)
    items = []
    for paper in papers:
        item = dataclasses.asdict(paper)
        item["in_library"] = f"{paper.source}:{paper.id}" in in_library
        items.append(item)
    return items


def _discard_tmp(tmp_path: Path) -> None:
    """下载失败时清掉半成品与本次独占子目录（成功路径的收尾归 ingest_web_file）。

    子目录一并删：只删文件会让 web-tmp 每次失败都积一个空目录。
    """
    try:
        tmp_path.unlink(missing_ok=True)
        tmp_path.parent.rmdir()
    except OSError:  # pragma: no cover - 句柄被外部程序（杀毒/索引器）占用
        logger.warning("web-tmp 临时目录未能清理：%s", tmp_path.parent)


@router.get("/api/papers/sources")
def list_paper_sources() -> dict:
    """来源目录与能力声明（前端据此渲染来源多选与筛选器的可用性）。

    能力值来自各来源类的 `caps`（只以实测为准，见 ADR-0020）——前端
    **不硬编码任何来源名或能力**。
    """
    return {"sources": source_catalog()}


@router.post("/api/papers/search")
def search_papers(
    body: PaperSearchIn,
    services=Depends(get_services),
    settings: Settings = Depends(get_settings),
) -> dict:
    """检索一页。逐源降级：单源失败进 errors（照常 200），全灭才 502。"""
    filters = PaperFilters(
        date_from=body.filters.date_from.isoformat() if body.filters.date_from else None,
        date_to=body.filters.date_to.isoformat() if body.filters.date_to else None,
        oa_only=body.filters.oa_only,
        language=body.filters.language,
        sort=body.filters.sort,
    )
    page = papers_search(body.q, body.sources, body.offset, body.limit, filters)
    # 502 判据看**实际发出请求**的来源（本页分配 0 个位、或选择集为空的源
    # 不参与）——用"选中数"当分母会永远凑不齐，该 502 的场景会变成 200 空结果
    if not page.results and page.attempted and set(page.errors) == set(page.attempted):
        raise HTTPException(
            status_code=502, detail="论文检索暂时不可用：" + "；".join(page.errors.values())
        )
    return {
        "results": _items_with_library(settings, page.results),
        "errors": page.errors,
        "notes": page.notes,
        "has_more": page.has_more,
        # 各源上游命中总数（"这库到底有多大"）：拿不到总数的源不出现，
        # 前端据此显示"OpenAlex 命中 6.9 万条"，不显示的就是不知道
        "totals": page.totals,
    }


@router.get("/api/papers/related")
def related_papers(
    source: str,
    id: str,
    kind: str = "related",
    limit: int = 10,
    settings: Settings = Depends(get_settings),
) -> dict:
    """相关论文 / 引用了它（引证关系只 OpenAlex 有，别的来源按 DOI 桥接）。

    为什么值得单独一个端点：这是"像知网"最实用的一环——看到一篇好文章，
    接着看谁引了它、以及同一主题还有哪些，再一键导入。中文刊的文章大多
    没有直链 PDF（DOAJ），但**它们有 DOI**，所以照样能桥到 OpenAlex 的
    引证网络里来。

    诚实降级：没有 DOI、或 OpenAlex 里查不到该 DOI → 200 + note 说明原因，
    而不是编几篇"相关"出来（前端据此显示一行灰字）。
    """
    _validate_source_id(source, id)
    if kind not in ("related", "cited", "references"):
        raise HTTPException(status_code=422, detail=f"不支持的关系类型：{kind}")
    limit = max(1, min(limit, 25))
    try:
        paper = fetch_paper(source, id)
    except PaperError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if kind == "related":
        # **相关论文不用上游的"推荐"**（2026-09-19 实测）：OpenAlex 的
        # related_works 对中文论文基本是噪声——实测一篇"南海北部天然气水合物"
        # 的相关推荐是心脏外科、教育学和一场合唱音乐会，连它的 primary_topic
        # 都标成了"军事技术"。
        # 改用**我们自己的检索**：拿标题当查询再搜一次，跨来源、结果天然相关，
        # 而且能带出 DOAJ 里的中文开放获取期刊。
        page = papers_search(_related_query(paper.title), None, 0, limit + 1, None)
        papers = [
            p
            for p in page.results
            if not (p.source == source and p.id == id) and _shares_topic(paper.title, p.title)
        ][:limit]
        return {
            "results": _items_with_library(settings, papers),
            "note": "按标题关键词检索（跨来源）",
            "total": None,
        }

    doi = paper.doi
    openalex_id = id if source == "openalex" else ""
    if not openalex_id and not doi:
        return {
            "results": [],
            "note": "这篇论文没有 DOI（或该来源未提供），无法关联到引证网络",
            "total": None,
        }
    try:
        work_id = openalex.resolve_work_id(doi=doi, openalex_id=openalex_id)
    except PaperError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if work_id is None:
        return {
            "results": [],
            "note": "OpenAlex 里没有这篇论文的记录（中文期刊的 DOI 覆盖不全），暂时给不出引证关系",
            "total": None,
        }
    try:
        if kind == "cited":
            papers, total = openalex.cited_by(work_id, limit)
            note = ""
        else:  # references
            papers = openalex.references_of(work_id, limit)
            total = None
            note = "" if papers else "OpenAlex 没有记录这篇论文的参考文献"
    except PaperError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"results": _items_with_library(settings, papers), "note": note, "total": total}


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
    # 后缀必须放在截断**之后**追加，否则长标题会把去重后缀截掉）。
    # **id 也要净化，且斜杠要先换成 `-`**：老式 arXiv id 形如 `cs/0701001`
    # （`_ARXIV_ID_RE` 明确放行 `/`），直接拼进去的话——斜杠会让文件写进
    # web-tmp 的子目录、ingest 取 basename → 文档标题与 uploads 副本名退化成
    # `0701001)`，尾链又按带斜杠的路径查行 → **明明入库成功却回 409「文档刚被删除」**
    # （2026-09-20 审查实测）。先换斜杠再净化还有个副作用是堵住同一个点的路径
    # 穿越：`..` 也在那个 id 正则的允许集里（`sanitize_filename` 只取 basename，
    # 不先换斜杠的话 `cs/0701001` 会把 `arxiv ` 前缀整段丢掉）。
    # 净化可能抛 ValueError——`or "(无标题)"` 只挡空串，挡不住"全是保留字符"的
    # 脏标题（上游把标题写成 `???` / `...` / 纯空白时净化后为空）。不接就是
    # 一条脏元数据换一个 500，而这是用户输入层的问题（2026-09-24 修）。
    try:
        safe_title = sanitize_filename(result.title[:_TITLE_CAP] or "(无标题)")
        safe_id = sanitize_filename(
            f"{result.source} {result.id}".replace("/", "-").replace("\\", "-")
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"这篇论文的标题无法用作文件名，请改用「打开」跳转原页：{exc}"
        ) from exc
    safe_name = f"{safe_title} ({safe_id}).pdf"

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
            status_code=502, detail=f"论文下载失败：{strip_paths(f'{type(exc).__name__}: {exc}')}"
        ) from exc
    try:
        file_sha = sha256_file(tmp_path)
    except OSError:
        # 读取期失败（句柄被占等）：把半成品与本次独占子目录一起收干净再上抛
        _discard_tmp(tmp_path)
        raise
    # ingest_web_file 自带 ZhiwenError→400 翻译与 finally 清理，这里直接交棒。
    # source_ref 是**唯一**知道"这篇文档来自哪条在线记录"的地方：入库后它
    # 落进 documents.source_ref，「找论文」页据此标"已在库中"（ADR-0020）。
    return ingest_web_file(
        tmp_path,
        safe_name,
        file_sha,
        services,
        settings,
        source_ref=f"{body.source}:{body.id}",
    )


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
