"""OpenAlex 检索来源（works API）。

关键事实（2026-09-15 已核实）：
- **2026-02-13 起必须免费 API key**（openalex.org 免费注册）：免费档
  每日 10 万 credits（列表查询 = 10 credits，个人使用绰绰有余）；无 key
  只有小额测试额度，超了回 409/429。key 在**调用时**读环境变量
  `OPENALEX_API_KEY`（设置面板写入 .env 后热生效，见 ADR-0018）。
- 中文检索实测可用（"水库坝" 4455 条）：中文关键词直接命中中文论文。
- 摘要存倒排索引（`abstract_inverted_index`：词 → 出现位置列表），
  需要重建；重建后是小写无标点文本（OpenAlex 不存标点与大小写）。
- 覆盖国内理工核心期刊元数据（有 DOI 的）；CSSCI 社科类 ≈ 0；知网
  独家全文拿不到——UI 与文档如实告知，给 DOI 链接跳转。
- 测试性：base URL 由 `MIKASA_PAPERS_OPENALEX_BASE` 在调用时覆盖，
  单测打桩 `_http_get` 网络缝，永不真联网。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from mikasa.papers.errors import PaperError
from mikasa.papers.sources import PaperFilters, PaperResult, SourceCaps

# OpenAlex id 形如 "W" + 数字（六位起）：解析与路由双重校验
_OPENALEX_ID_RE = re.compile(r"^W\d{6,}$")

# 只取用到的字段，payload 从 ~50KB 瘦到 ~10KB（额度不变，流量与解析更快）
_SELECT = (
    "id,doi,display_name,publication_year,authorships,primary_location,"
    "abstract_inverted_index,open_access,language,cited_by_count"
)

_USER_AGENT = "Mikasa/0.1 (local paper search)"

# OpenAlex 单页上限（超出会 400）；深翻页时窗口可能取不满，has_more 自然为假
_MAX_PER_PAGE = 200

_SORT_PARAMS = {
    "relevance": "relevance_score:desc",
    "cited": "cited_by_count:desc",
    "recent": "publication_date:desc",
}


def _sort_param(filters: PaperFilters | None) -> str:
    """排序翻译；未知取值回落相关度（能力校验在服务层，这里只做翻译）。"""
    key = filters.sort if filters is not None else "relevance"
    return _SORT_PARAMS.get(key, _SORT_PARAMS["relevance"])


def _filter_params(filters: PaperFilters | None) -> dict[str, str]:
    """年份/开放获取/语言合并进一个 filter 参数（逗号分隔，OpenAlex 语法）。"""
    if filters is None:
        return {}
    clauses = []
    # 完整日期直传（OpenAlex 认 YYYY-MM-DD）——界面选的是日期，就按日期过滤
    if filters.date_from is not None:
        clauses.append(f"from_publication_date:{filters.date_from}")
    if filters.date_to is not None:
        clauses.append(f"to_publication_date:{filters.date_to}")
    if filters.oa_only:
        clauses.append("is_oa:true")
    if filters.language:
        clauses.append(f"language:{filters.language}")
    return {"filter": ",".join(clauses)} if clauses else {}


def _base_url() -> str:
    """API 根（调用时读环境变量：E2E 可整体换成本地假源）。"""
    return os.environ.get("MIKASA_PAPERS_OPENALEX_BASE", "https://api.openalex.org")


def _openalex_key() -> str | None:
    """免费 key（调用时读取——设置面板写 .env 后无需重启）。"""
    return os.environ.get("OPENALEX_API_KEY") or None


def validate_id(paper_id: str) -> bool:
    return bool(_OPENALEX_ID_RE.fullmatch(paper_id))


def reconstruct_abstract(index: dict[str, list[int]] | None) -> str:
    """倒排索引 → 正文：每个词挂回它出现的所有位置，按位置排序拼句。

    OpenAlex 的摘要索引没有标点与大小写信息，重建为小写无标点文本
    （业界通用做法）；index 缺失/为空 → ""。位置互不重复是格式保证。
    """
    if not index:
        return ""
    positions: dict[int, str] = {}
    for word, idxs in index.items():
        for i in idxs:
            positions[i] = word
    return " ".join(positions[i] for i in sorted(positions))


def _http_get(url: str, timeout: float) -> bytes:
    """唯一网络缝：GET 并读全部字节；错误翻译成可回显中文。

    429/409/403 分有 key / 无 key 两种文案：无 key 时用户能做的事是
    注册 + 粘贴（面板有入口），有 key 时只能等额度重置。
    """
    key = _openalex_key()
    if key:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}api_key={urllib.parse.quote(key)}"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (429, 409, 403):
            if not key:
                raise PaperError(
                    "OpenAlex 需要免费 API 密钥：到 openalex.org 免费注册后，"
                    "在「OpenAlex 密钥」里粘贴保存即可（每日 10 万积分额度）"
                ) from exc
            raise PaperError(
                f"OpenAlex 请求被限流或额度用尽（HTTP {exc.code}），请稍后重试"
            ) from exc
        if exc.code == 404:
            raise PaperError("OpenAlex 未找到该论文（id 可能已变更）") from exc
        raise PaperError(f"OpenAlex 请求失败（HTTP {exc.code}）") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise PaperError("无法连接 OpenAlex（网络不可达或超时），请稍后重试") from exc


def _load_json(data: bytes) -> dict:
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PaperError("OpenAlex 返回内容无法解析（响应不是有效 JSON）") from exc
    if not isinstance(parsed, dict):
        raise PaperError("OpenAlex 返回内容无法解析（响应结构异常）")
    return parsed


def _normalize(work: dict) -> PaperResult:
    """OpenAlex work → PaperResult（缺什么补什么，不因单条元数据缺失而失败）。"""
    work_id = str(work.get("id") or "").rstrip("/")
    short_id = work_id.rsplit("/", 1)[-1]
    if not validate_id(short_id):
        raise PaperError(f"OpenAlex 返回了无法识别的论文编号：{work_id}")
    doi = str(work.get("doi") or "")
    if doi.lower().startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/") :]
    open_access = work.get("open_access") or {}
    is_oa = bool(open_access.get("is_oa"))
    oa_url = open_access.get("oa_url")
    venue = ""
    primary = work.get("primary_location") or {}
    source = primary.get("source") or {}
    if isinstance(source, dict):
        venue = str(source.get("display_name") or "")
    authors = tuple(
        str(a.get("author", {}).get("display_name"))
        for a in (work.get("authorships") or [])
        if isinstance(a, dict) and a.get("author", {}).get("display_name")
    )
    cited = work.get("cited_by_count")
    return PaperResult(
        source="openalex",
        id=short_id,
        title=str(work.get("display_name") or "(无标题)"),
        authors=authors,
        year=work.get("publication_year"),
        venue=venue,
        abstract=reconstruct_abstract(work.get("abstract_inverted_index")),
        doi=doi,
        pdf_url=str(oa_url) if (is_oa and oa_url) else None,
        landing_url=f"https://doi.org/{doi}"
        if doi
        else work_id or f"https://openalex.org/{short_id}",
        oa=is_oa,
        cited_by=int(cited) if isinstance(cited, int) else None,
        language=str(work.get("language") or ""),
    )


class OpenAlexSource:
    """OpenAlex 来源：search 走 /works 检索，fetch 走 /works/{id} 反查。

    能力（实测）：年份/开放获取/语言过滤与三种排序全部支持（被引数来自
    `cited_by_count`）。
    """

    name = "openalex"
    label = "OpenAlex"
    caps = SourceCaps(year=True, cited_sort=True, recent_sort=True, language=True, oa="filterable")

    def search(
        self,
        q: str,
        start: int,
        count: int,
        *,
        filters: PaperFilters | None = None,
    ) -> tuple[list[PaperResult], bool]:
        # **窗口对齐**（2026-09-16 两轮才修对，教训记在下面）：OpenAlex 只有
        # page/per-page，没有原生 offset，而页边界固定在 per-page 的整数倍上
        # ——凑不出"边界刚好落在 start"。
        #   · 旧实现 `page = start // count + 1` 只在 start 是 count 整数倍时
        #     成立（两源各拿一半、恒 10 条时侥幸正确）；N 源轮转后 start 一般
        #     不是 count 的倍数，取回的窗口整体左移 → 翻页重复（E2E 实测
        #     40 条里 5 条重复）。
        #   · 第一版修法按 count 对齐页号、却按 per-page 取页，两者不是同一
        #     倍数，照样错位。
        # 最终：**从第 1 页一直取到 start+count 为止，再切片** —— 一次请求、
        # 与页边界无关。OpenAlex 按"每次查询 10 credits"计费、与 per-page 无关，
        # 多取不额外花钱；代价只是多传一点流量。
        # 超过单页上限（200）的深翻页取不满 → has_more 自然为假，该源的深翻
        # 到此为止（如实降级，不假装后面还有）。
        per_page = min(_MAX_PER_PAGE, start + count)
        params = urllib.parse.urlencode(
            {
                "search": q,
                "per-page": per_page,
                "page": 1,
                "sort": _sort_param(filters),
                "select": _SELECT,
                **_filter_params(filters),
            }
        )
        data = _load_json(_http_get(f"{_base_url()}/works?{params}", timeout=15.0))
        works = data.get("results")
        if not isinstance(works, list):
            raise PaperError("OpenAlex 返回内容无法解析（缺少 results 列表）")
        results = [_normalize(w) for w in works if isinstance(w, dict)]
        window = results[start : start + count]
        return window, len(window) == count

    def fetch(self, paper_id: str) -> PaperResult:
        if not validate_id(paper_id):
            raise PaperError(f"非法的 OpenAlex 论文编号：{paper_id}")
        params = urllib.parse.urlencode({"select": _SELECT})
        work = _load_json(_http_get(f"{_base_url()}/works/{paper_id}?{params}", timeout=15.0))
        return _normalize(work)
