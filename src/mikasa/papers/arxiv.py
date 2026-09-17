"""arXiv 检索来源（Atom XML API）。

端点与网络约束（2026-09-15 实测）：
- 检索 `https://export.arxiv.org/api/query`（**必须 https**——http 的 80
  端口在本机实测被墙，https 200/2.6s）；PDF 走 `https://arxiv.org/pdf/{id}`
  （实测可下载，arXiv 论文全部开放获取）。
- 官方限速约 1 请求/3 秒：模块内自节流（成功一次后补睡到间隔），
  搜索两次间隔 ≥3s 时零开销。
- 测试性：base URL 由环境变量在**调用时**读取（`MIKASA_PAPERS_ARXIV_BASE` /
  `MIKASA_PAPERS_ARXIV_PDF_BASE`），E2E 用本地假源整体替换；单测直接
  打桩 `_http_get`（唯一的网络缝），永远不真联网。
"""

from __future__ import annotations

import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from mikasa.papers.errors import PaperError
from mikasa.papers.sources import PaperFilters, PaperResult, SourceCaps

# 官方建议的最低请求间隔（秒）：礼貌限速，别把公共 API 打爆
_ARXIV_MIN_INTERVAL = 3.0

# id 白名单：拒绝查询串/空白/路径穿越（pdf URL 由拼接构造，这是卫生检查）
_ARXIV_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./-]{1,63}$")

_USER_AGENT = "Mikasa/0.1 (local paper search)"


def _base_url() -> str:
    """检索 API 根（调用时读环境变量：E2E 可整体换成本地假源）。"""
    return os.environ.get("MIKASA_PAPERS_ARXIV_BASE", "https://export.arxiv.org/api/query")


def _pdf_base() -> str:
    """PDF 下载根（独立覆盖：export.arxiv.org 与 arxiv.org 是两台主机，
    只覆盖 API 根救不了 PDF 下载——E2E 需要两个都指到假源）。"""
    return os.environ.get("MIKASA_PAPERS_ARXIV_PDF_BASE", "https://arxiv.org/pdf")


# ---- 节流状态：模块级（单进程单服务，见 services.py 的进程约定） ----
_throttle = threading.Lock()
_last_ok = 0.0


def _http_get(url: str, timeout: float) -> bytes:
    """唯一网络缝：GET 并读全部字节；**发起前**按官方限速等够间隔。

    单测/E2E 都从这里打桩（或覆盖 base URL），stdout 之外不再有第二处联网。

    **节流记在发起前，成功失败都算一次请求**（2026-09-16 修正）：原实现
    "成功后补睡"，理由是"失败重试不该再付等待成本"——实测站不住：连打几次
    后 arXiv 回 429，而它恰恰把**失败请求也算进配额**，于是"越失败越猛打"
    把额度越打越死（当天三个源里两个被自己打成 429）。发前节流才是正确的
    礼貌客户端行为。
    """
    global _last_ok
    with _throttle:
        wait = _last_ok + _ARXIV_MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_ok = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise PaperError("arXiv 请求过于频繁（HTTP 429），请等半分钟再试") from exc
        raise PaperError(f"arXiv 服务返回错误（HTTP {exc.code}），请稍后重试") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise PaperError("无法连接 arXiv（网络不可达或超时），请稍后重试") from exc


def _local(tag: str) -> str:
    """Atom 命名空间剥壳："{http://www.w3.org/2005/Atom}title" → "title"。

    不建命名空间映射表：arXiv 返回的命名空间前缀随版本变过，按局部名
    匹配最稳（stdlib ElementTree 足够，不必为它引入依赖）。"""
    return tag.rsplit("}", 1)[-1]


def _child(elem: ET.Element, name: str) -> ET.Element | None:
    return next((c for c in elem if _local(c.tag) == name), None)


def _text(elem: ET.Element | None) -> str:
    """元素文本，折叠空白（arXiv 标题自带换行）。"""
    return " ".join((elem.text or "").split()) if elem is not None else ""


def validate_id(paper_id: str) -> bool:
    """id 形状校验（解析与路由双重调用；拼接 PDF URL 前的卫生检查）。"""
    return bool(_ARXIV_ID_RE.fullmatch(paper_id))


def _compact(date_iso: str | None, fallback: str) -> str:
    """ISO 日期 "2021-03-15" → "20210315"（arXiv 的 submittedDate 格式）。"""
    return (date_iso or fallback).replace("-", "")


def _normalize_id(raw: str) -> str:
    """ "http://arxiv.org/abs/2401.12345v2" → "2401.12345"。

    去 /abs/ 前缀与版本后缀；老式 id（cs/0011001）没有版本后缀，正则
    自然不命中。解析不出来 = 上游返回了我们不认识的形状 → PaperError。
    """
    marker = "/abs/"
    if marker in raw:
        raw = raw.split(marker, 1)[1]
    paper_id = re.sub(r"v\d+$", "", raw)
    if not validate_id(paper_id):
        raise PaperError(f"arXiv 返回了无法识别的论文编号：{raw}")
    return paper_id


def _parse_entry(entry: ET.Element) -> PaperResult:
    """Atom entry → PaperResult（缺什么补什么，单条解析失败即整体失败）。"""
    paper_id = _normalize_id(_text(_child(entry, "id")))
    title = _text(_child(entry, "title")) or "(无标题)"
    published = _text(_child(entry, "published"))
    year = int(published[:4]) if published and published[:4].isdigit() else None
    # 作者名在 author 的子元素 name 里（<author><name>…</name></author>），
    # 不在 author.text——直接 _text(author) 会拿到空串
    authors = tuple(
        name
        for author in entry
        if _local(author.tag) == "author"
        for name in [_text(_child(author, "name"))]
        if name
    )
    summary = _text(_child(entry, "summary"))
    doi = ""
    doi_elem = next(
        (c for c in entry if _local(c.tag) == "doi" and c.text),
        None,  # arxiv:doi
    )
    if doi_elem is not None:
        doi = (doi_elem.text or "").strip()
    landing = ""
    for link in entry:
        if _local(link.tag) == "link" and link.get("rel") == "alternate":
            landing = link.get("href") or ""
    return PaperResult(
        source="arxiv",
        id=paper_id,
        title=title,
        authors=authors,
        year=year,
        venue="arXiv",
        abstract=summary,
        doi=doi,
        pdf_url=f"{_pdf_base()}/{paper_id}",
        landing_url=landing or f"https://arxiv.org/abs/{paper_id}",
        oa=True,
    )


def _parse_feed(data: bytes) -> list[PaperResult]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise PaperError("arXiv 返回内容无法解析（响应不是有效 XML）") from exc
    entries = [e for e in root if _local(e.tag) == "entry"]
    return [_parse_entry(e) for e in entries]


class ArxivSource:
    """arXiv 来源：search 走 all: 检索，fetch 走 id_list 反查。

    能力（实测）：年份区间与"按时间排序"都支持（前者走查询语法
    `submittedDate:[…]`，后者走 sortBy/sortOrder）；**没有被引数据**，
    所以 cited_sort=False、results 的 cited_by 恒为 None；全部开放获取。
    """

    name = "arxiv"
    label = "arXiv"
    caps = SourceCaps(year=True, cited_sort=False, recent_sort=True, language=False, oa="always")

    def search(
        self,
        q: str,
        start: int,
        count: int,
        *,
        filters: PaperFilters | None = None,
    ) -> tuple[list[PaperResult], bool]:
        # 引号会改变 arXiv 的查询语义（精确短语），用户输入不该有这种权力
        query = " ".join(q.replace('"', " ").split())
        # 日期区间：arXiv 只认查询语法里的 submittedDate，格式 YYYYMMDDHHMM
        # （端点参数没有这一项）——完整日期直接进，不做"只取年份"的暗改
        if filters is not None and (filters.date_from or filters.date_to):
            low = f"{_compact(filters.date_from, '1900-01-01')}0000"
            high = f"{_compact(filters.date_to, '2100-12-31')}2359"
            query = f"{query} AND submittedDate:[{low} TO {high}]"
        sort_by, sort_order = "relevance", "descending"
        if filters is not None and filters.sort == "recent":
            sort_by = "submittedDate"
        params = urllib.parse.urlencode(
            {
                "search_query": f'all:"{query}"',
                "start": start,
                "max_results": count,
                "sortBy": sort_by,
                "sortOrder": sort_order,
            }
        )
        results = _parse_feed(_http_get(f"{_base_url()}?{params}", timeout=15.0))
        return results, len(results) == count

    def fetch(self, paper_id: str) -> PaperResult:
        if not validate_id(paper_id):
            raise PaperError(f"非法的 arXiv 论文编号：{paper_id}")
        params = urllib.parse.urlencode({"id_list": paper_id})
        results = _parse_feed(_http_get(f"{_base_url()}?{params}", timeout=15.0))
        if not results:
            raise PaperError(f"arXiv 未找到该论文（{paper_id}）")
        return results[0]
