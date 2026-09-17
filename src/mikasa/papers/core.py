"""CORE 检索来源（开放获取聚合库，works API）。

关键事实（2026-09-16 本机实测，**与官方文档不一致处以后者为准**）：
- **免密钥可用**：`GET /v3/search/works/?q=…&limit=…&offset=…` 匿名 200
  （官方文档提过要 key，实测不需要；仍按"随时可能变"的态度给中文错误文案）。
- **限流很凶**：连打 5~6 次即 429——比 arXiv 更敏感，模块级节流取 2 秒。
- **年份过滤只能走查询语法**：`yearFrom`/`yearTo` 参数返回 200 但**被静默
  忽略**（要 2020+ 却返回 2012/2018 的论文）；写进 `q` 里的
  `AND yearPublished>=YYYY` 实测有效（命中数与年份都对）。所以 caps.year
  为 True，但翻译走 q 而不是参数——"参数不生效"与"能力不支持"是两回事。
- **`sort=recency` 有效**（降序）；`sort=citationCount` 直接 HTTP 500，
  所以按被引排序不支持，caps.cited_sort=False。
- **`yearPublished` 有脏值**：实测见过 `710300` / `202022`（像是 YYYYMM
  或脏数据），解析必须防御——取前四位并校验范围，不合规一律 None，
  否则界面上会出现"202022 年"。
- **`language` 是对象**（`{"code": "en", "name": "English"}`）不是字符串。
- **`journals` 常为空数组**，venue 退到 `publisher`；`links` 里有
  `type=reader` 的落地页；`downloadUrl` 是直链 PDF（实测能下到 `%PDF-`
  字节），但**有相当比例是 http:// 的仓储地址**，会被 download.py 的
  安全策略拒收（只放行回环 http）——这是已知功能缺口，见 limitations。
- 测试性：base URL 由 `MIKASA_PAPERS_CORE_BASE` 在调用时覆盖，单测打桩
  `_http_get` 网络缝，永不真联网。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from mikasa.papers.errors import PaperError
from mikasa.papers.sources import PaperFilters, PaperResult, SourceCaps

# 官方文档未见明确限速，但实测连打 5~6 次即 429 → 保守取 2 秒
_CORE_MIN_INTERVAL = 2.0

# id 白名单：CORE 的 work id 是纯数字（实测 "72543"）
_CORE_ID_RE = re.compile(r"^\d{1,20}$")

_USER_AGENT = "Mikasa/0.1 (local paper search)"

# 年份合法区间：脏值（710300/202022）一律判 None
_YEAR_MIN, _YEAR_MAX = 1900, 2100

_throttle = threading.Lock()
_last_ok = 0.0


def _base_url() -> str:
    """API 根（调用时读环境变量：E2E 可整体换成本地假源）。"""
    return os.environ.get("MIKASA_PAPERS_CORE_BASE", "https://api.core.ac.uk/v3")


def _http_get(url: str, timeout: float) -> bytes:
    """唯一网络缝：GET 并读全部字节；**发起前**按实测限速等够间隔。

    与 arxiv.py 同构：节流记在发起前、成功失败都算一次请求——CORE 限流更紧，
    "失败不付等待成本"会让重试把额度越打越死（2026-09-16 实测踩过）。
    """
    global _last_ok
    with _throttle:
        wait = _last_ok + _CORE_MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_ok = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise PaperError(
                "CORE 请求过于频繁（HTTP 429），请稍等半分钟再试（该来源限流较紧）"
            ) from exc
        raise PaperError(f"CORE 服务返回错误（HTTP {exc.code}），请稍后重试") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise PaperError("无法连接 CORE（网络不可达或超时），请稍后重试") from exc


def validate_id(paper_id: str) -> bool:
    """id 形状校验（解析与路由双重调用；拼接 URL 前的卫生检查）。"""
    return bool(_CORE_ID_RE.fullmatch(paper_id))


def _parse_year(raw: object) -> int | None:
    """`yearPublished` → 年份或 None（脏值防御，见模块头）。"""
    text = str(raw or "").strip()
    if len(text) < 4 or not text[:4].isdigit():
        return None
    year = int(text[:4])
    return year if _YEAR_MIN <= year <= _YEAR_MAX else None


def _language_code(raw: object) -> str:
    """`language` 是对象（{"code": "en"}），也兼容字符串形态。"""
    if isinstance(raw, dict):
        return str(raw.get("code") or "")
    return str(raw or "")


def _strip_doi(raw: object) -> str:
    doi = str(raw or "")
    if doi.lower().startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/") :]
    return doi


def _link_of(work: dict, kind: str) -> str:
    for link in work.get("links") or []:
        if isinstance(link, dict) and link.get("type") == kind:
            return str(link.get("url") or "")
    return ""


def _venue(work: dict) -> str:
    """期刊名：journals[0].title 优先（实测常为空），退到 publisher。"""
    for journal in work.get("journals") or []:
        if isinstance(journal, dict) and journal.get("title"):
            return str(journal["title"])
    return str(work.get("publisher") or "")


def _normalize(work: dict) -> PaperResult:
    """CORE work → PaperResult（缺什么补什么，不因单条元数据缺失而失败）。"""
    work_id = str(work.get("id") or "")
    if not validate_id(work_id):
        raise PaperError(f"CORE 返回了无法识别的论文编号：{work_id}")
    download = str(work.get("downloadUrl") or "")
    doi = _strip_doi(work.get("doi"))
    # 落地页优先级：DOI（出版商页，最权威）→ links 里的 reader 页 → CORE 详情页
    landing = (
        f"https://doi.org/{doi}"
        if doi
        else _link_of(work, "reader") or f"https://core.ac.uk/works/{work_id}"
    )
    cited = work.get("citationCount")
    return PaperResult(
        source="core",
        id=work_id,
        title=str(work.get("title") or "(无标题)"),
        authors=tuple(
            str(a.get("name"))
            for a in (work.get("authors") or [])
            if isinstance(a, dict) and a.get("name")
        ),
        year=_parse_year(work.get("yearPublished")),
        venue=_venue(work),
        abstract=str(work.get("abstract") or ""),
        doi=doi,
        # 无 downloadUrl = 这条记录没有可直接抓的全文 → 走 409 提示路径
        pdf_url=download or None,
        landing_url=landing,
        oa=True,  # CORE 本身是开放获取聚合库
        cited_by=int(cited) if isinstance(cited, int) else None,
        language=_language_code(work.get("language")),
    )


def _load_json(data: bytes) -> dict:
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PaperError("CORE 返回内容无法解析（响应不是有效 JSON）") from exc
    if not isinstance(parsed, dict):
        raise PaperError("CORE 返回内容无法解析（响应结构异常）")
    return parsed


def _year_clause(filters: PaperFilters | None) -> str:
    """年份过滤拼进查询语句（参数形态被静默忽略，见模块头）。

    收到的日期是完整 ISO 日期，但 CORE 只到年（`yearPublished` 是年份或
    YYYYMM 的脏值），所以这里**取前四位**——能力边界已在 caps 里如实声明，
    不是悄悄丢输入。两端可同时给（区间）。
    """
    if filters is None:
        return ""
    clauses = []
    if filters.date_from is not None:
        clauses.append(f"yearPublished>={filters.date_from[:4]}")
    if filters.date_to is not None:
        clauses.append(f"yearPublished<={filters.date_to[:4]}")
    return "".join(f" AND {c}" for c in clauses)


class CoreSource:
    """CORE 来源：search 走 /search/works，fetch 走 /works/{id}。"""

    name = "core"
    label = "CORE"
    caps = SourceCaps(year=True, cited_sort=False, recent_sort=True, language=False, oa="always")

    def search(
        self,
        q: str,
        start: int,
        count: int,
        *,
        filters: PaperFilters | None = None,
    ) -> tuple[list[PaperResult], bool]:
        query = q.replace('"', " ").strip()
        # cited 排序 CORE 不支持（HTTP 500），服务层会记 note；这里回落相关度
        want_recent = filters is not None and filters.sort == "recent"
        sort = "recency" if want_recent else "relevance"
        params = urllib.parse.urlencode(
            {
                "q": query + _year_clause(filters),
                "limit": count,  # 必须显式传：CORE 默认 10
                "offset": start,  # 原生 offset，与窗口公式直连
                "sort": sort,
            }
        )
        data = _load_json(_http_get(f"{_base_url()}/search/works/?{params}", timeout=20.0))
        works = data.get("results")
        if not isinstance(works, list):
            raise PaperError("CORE 返回内容无法解析（缺少 results 列表）")
        results = [_normalize(w) for w in works if isinstance(w, dict)]
        return results, len(results) == count

    def fetch(self, paper_id: str) -> PaperResult:
        if not validate_id(paper_id):
            raise PaperError(f"非法的 CORE 论文编号：{paper_id}")
        work = _load_json(_http_get(f"{_base_url()}/works/{paper_id}", timeout=20.0))
        return _normalize(work)
