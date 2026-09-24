"""DOAJ 检索来源（开放获取期刊目录，官方 REST API，**免密钥**）。

**为什么加它**（2026-09-19）：用户要的是"别人装了就能用、不用自己去注册"。
已有的三个源里 OpenAlex 自 2026 年起要求免费 key、匿名只有小额试用额度，
CORE 匿名限流很紧，arXiv 只有英文预印本——中文期刊这一块一直是空的。
DOAJ 补的正是这块：它是**开放获取期刊的官方目录**，收录含中文在内的
多语种 OA 期刊（实测"充填体"185 条，命中《工业水处理》等中文刊）。

契约（2026-09-19 实测，全部有据）：
- 检索 `GET /api/search/articles/<query>?pageSize=&page=`；`<query>` 是
  URL 编码的查询串，支持 Lucene 风格语法（`bibjson.year:[2015 TO 2026]`
  实测有效：185 → 115 条），普通词直接搜也走同一入口。
- 响应 `{total, page, pageSize, results: [{id, bibjson: {...}}]}`；
  条目 id 是 **32 位十六进制**（既是我们的 PaperResult.id，也是导入时反查
  的键——`id:"<id>"` 实测 total=1）。
- **年份是字符串**（"2022"），解析要防御；`identifier[]` 里
  `type=="doi"` 的才是 DOI（很多中文刊只有 ISSN）。
- **`link[]` 全是出版商落地页**（实测 25 条里 0 条以 .pdf 结尾），所以
  `pdf_url` 只在真出现 .pdf 链接时才给——否则为 None（界面据此禁用导入，
  这是如实的降级，不是"没有开放获取"）。
- 未实测到支持的：`sort=` 与语言过滤（两个查询都在 50 秒处超时）。
  能力声明按**不支持**写：caps 里 recent_sort=False、language=False，
  服务层会把它们记进 notes 并让界面置灰——宁可少给选项，不假装能过滤。
- 限速：官方未公布配额，按 1 请求/秒自律（本模块级节流），并共用
  papers/http.py 的 UA 与网络级重试。
- 授权：DOAJ 声明其**元数据为 CC0**（公共领域），程序化检索是它明确
  支持的用法；全文只在出版商自己的页面上，我们不经手、不转发。
- 测试性：base URL 由 `MIKASA_PAPERS_DOAJ_BASE` 在调用时覆盖，E2E 用
  本地假源整体替换；单测打桩 `_http_get`（唯一网络缝），永不真联网。
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse

from mikasa.papers.errors import PaperError
from mikasa.papers.http import ThrottledClient
from mikasa.papers.sources import PaperFilters, PaperResult, SourceCaps
from mikasa.utils.text import strip_markup

# 条目 id：32 位十六进制（实测 "04f84a13de1b4cd8a19e9af019205cbf"）
_DOAJ_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# 官方未公布配额：按 1 请求/秒自律（比 arXiv 的 3 秒松、比匿名 CORE 稳）
_DOAJ_MIN_INTERVAL = 1.0

# 单请求超时（秒）：与另外三个源同一档（见 papers/http.py 的实测记录）
_TIMEOUT = 20.0

# 单页上限（官方支持 100）：深翻页时窗口可能取不满，has_more 自然为假
_MAX_PER_PAGE = 100
# 深翻页上限：官方没写明，按与 OpenAlex 同量级保守取（见 openalex._MAX_OFFSET）
_MAX_OFFSET = 10_000


def _base_url() -> str:
    """检索 API 根（调用时读环境变量：E2E 可整体换成本地假源）。"""
    return os.environ.get("MIKASA_PAPERS_DOAJ_BASE", "https://doaj.org/api/search/articles")


# HTTP 纪律在 papers/http.py（三源共用）。
_client = ThrottledClient(
    "DOAJ", _DOAJ_MIN_INTERVAL, busy_hint="DOAJ 请求过于频繁（HTTP 429），请稍等片刻再试"
)


def _http_get(url: str, timeout: float) -> bytes:
    """唯一网络缝：GET 并读全部字节；发起前按自律间隔等够，失败重试一次。"""
    return _client.get(url, timeout)


def _load_json(data: bytes) -> dict:
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PaperError("DOAJ 返回内容无法解析（响应不是有效 JSON）") from exc
    if not isinstance(parsed, dict):
        raise PaperError("DOAJ 返回内容无法解析（响应结构异常）")
    return parsed


def validate_id(paper_id: str) -> bool:
    """id 形状校验（解析与路由双重调用；拼接查询串前的卫生检查）。"""
    return bool(_DOAJ_ID_RE.fullmatch(paper_id))


def _parse_year(raw: object) -> int | None:
    """`year` 是字符串且可能是脏值：只认 4 位纯数字且在合理区间内。"""
    text = str(raw or "").strip()
    if len(text) != 4 or not text.isdigit():
        return None
    year = int(text)
    return year if 1900 <= year <= 2100 else None


def _doi_of(bib: dict) -> str:
    """identifier[] 里 type=="doi" 的那条（中文刊常常只有 ISSN，那就没有 DOI）。"""
    for item in bib.get("identifier") or []:
        if isinstance(item, dict) and str(item.get("type")) == "doi":
            return str(item.get("id") or "")
    return ""


def _links(bib: dict) -> list[str]:
    """link[] 里的 URL（实测全是出版商落地页；真出现 .pdf 才当全文直链）。"""
    urls = []
    for link in bib.get("link") or []:
        if isinstance(link, dict) and link.get("url"):
            urls.append(str(link["url"]))
    return urls


def _language_of(bib: dict) -> str:
    """语言只在 journal.language（列表，形如 ["ZH"]）上，正文没有该字段。"""
    journal = bib.get("journal") or {}
    langs = journal.get("language") if isinstance(journal, dict) else None
    if isinstance(langs, list) and langs:
        return str(langs[0]).lower()
    return ""


def _venue_of(bib: dict) -> str:
    journal = bib.get("journal") or {}
    if isinstance(journal, dict):
        return str(journal.get("title") or journal.get("publisher") or "")
    return ""


def _normalize(row: dict) -> PaperResult:
    """DOAJ 条目 → PaperResult（缺什么补什么，不因单条元数据缺失而失败）。"""
    item_id = str(row.get("id") or "")
    if not validate_id(item_id):
        raise PaperError(f"DOAJ 返回了无法识别的条目编号：{item_id}")
    bib = row.get("bibjson")
    if not isinstance(bib, dict):
        raise PaperError("DOAJ 返回内容无法解析（条目缺少 bibjson）")
    doi = _doi_of(bib)
    urls = _links(bib)
    # 全文直链只在**确实以 .pdf 结尾**时才认；落地页不算（导入要的是 PDF 字节）
    pdf_url = next((u for u in urls if u.lower().endswith(".pdf")), None)
    landing = urls[0] if urls else (f"https://doi.org/{doi}" if doi else "")
    if not landing:
        landing = f"https://doaj.org/article/{item_id}"
    return PaperResult(
        source="doaj",
        id=item_id,
        # 标题与摘要都要过 strip_markup：DOAJ 把论文 HTML 的标记原样塞进
        # 元数据（实测 `<sub>`/`<sup>` 直接显示在界面上，2026-09-19）
        title=strip_markup(str(bib.get("title") or "(无标题)")),
        authors=tuple(
            str(a.get("name"))
            for a in (bib.get("author") or [])
            if isinstance(a, dict) and a.get("name")
        ),
        year=_parse_year(bib.get("year")),
        venue=_venue_of(bib),
        abstract=strip_markup(str(bib.get("abstract") or "")),
        doi=doi,
        pdf_url=pdf_url,
        landing_url=landing,
        oa=True,  # DOAJ 收录的前提就是开放获取
        cited_by=None,  # 目录不提供被引数据（caps.cited_sort=False）
        language=_language_of(bib),
    )


def _year_clause(filters: PaperFilters | None) -> str:
    """年份区间拼进查询串（DOAJ 的 filter 参数不存在，语法在 query 里）。

    **逐边取默认值，不按"填了几个"定上下界**（2026-09-24 修）：这里原先按
    列表位置取界——只填「至」时那唯一的年份成了下界、上界兜底 2100，于是
    "检索 2020 年以前"返回的是 2020 年**以后**的（正好是补集，且 `caps.year=True`
    不产生降级提示，静默错）。另外三个来源都是逐边默认（arxiv 1900/2100、
    core 两条独立子句、openalex 两个独立参数），这里对齐它们。
    """
    if filters is None:
        return ""
    low = filters.date_from[:4] if filters.date_from else "1900"
    high = filters.date_to[:4] if filters.date_to else "2100"
    if low == "1900" and high == "2100":
        return ""  # 两边都没给：不拼子句（与原先"空条件返回空串"一致）
    return f" AND bibjson.year:[{low} TO {high}]"


class DoajSource:
    """DOAJ 来源：search 走查询串检索，fetch 走 `id:"<id>"` 反查。

    能力（实测）：年份区间支持（查询语法）；**不支持**按被引/按时间排序与
    语言过滤（两个查询实测超时，见模块头）——caps 里如实写 False，
    界面会置灰并说明原因。
    """

    name = "doaj"
    label = "DOAJ"
    caps = SourceCaps(year=True, cited_sort=False, recent_sort=False, language=False, oa="always")

    def search(
        self,
        q: str,
        start: int,
        count: int,
        *,
        filters: PaperFilters | None = None,
    ) -> tuple[list[PaperResult], int | None]:
        query = q.replace('"', " ").strip() + _year_clause(filters)
        # 窗口对齐：DOAJ 只有 page/pageSize 没有原生 offset → 取**包含窗口的那
        # 一页**再切片（窗口跨页时再取下一页）。与 OpenAlex 同一套纪律，那边
        # 有完整的三轮教训注释；2026-09-20 之前两处都是"从第 1 页取到
        # start+count"，受单页上限拖累、深翻页会悄悄没结果。
        per_page = _MAX_PER_PAGE if start + count > _MAX_PER_PAGE else start + count
        page = start // per_page + 1
        skip = start - (page - 1) * per_page

        def _page(number: int) -> tuple[list[PaperResult], int | None]:
            params = urllib.parse.urlencode({"pageSize": per_page, "page": number})
            url = f"{_base_url()}/{urllib.parse.quote(query, safe='')}?{params}"
            data = _load_json(_http_get(url, timeout=_TIMEOUT))
            rows = data.get("results")
            if not isinstance(rows, list):
                raise PaperError("DOAJ 返回内容无法解析（缺少 results 列表）")
            total = data.get("total")
            return [_normalize(r) for r in rows if isinstance(r, dict)], (
                total if isinstance(total, int) else None
            )

        if start >= _MAX_OFFSET:  # 见 openalex.py 的说明：太深就如实留空
            return [], None
        results, total = _page(page)
        if skip + count > per_page:
            results += _page(page + 1)[0]
        return results[skip : skip + count], total

    def fetch(self, paper_id: str) -> PaperResult:
        if not validate_id(paper_id):
            raise PaperError(f"非法的 DOAJ 条目编号：{paper_id}")
        params = urllib.parse.urlencode({"pageSize": 1, "page": 1})
        url = f"{_base_url()}/{urllib.parse.quote(f'id:{paper_id}', safe='')}?{params}"
        rows = _load_json(_http_get(url, timeout=_TIMEOUT)).get("results")
        if not isinstance(rows, list) or not rows:
            raise PaperError(f"DOAJ 未找到该条目（{paper_id}）")
        return _normalize(rows[0])
