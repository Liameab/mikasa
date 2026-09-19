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
from mikasa.papers.http import USER_AGENT, with_retry
from mikasa.papers.sources import PaperFilters, PaperResult, SourceCaps

# OpenAlex id 形如 "W" + 数字（六位起）：解析与路由双重校验
_OPENALEX_ID_RE = re.compile(r"^W\d{6,}$")

# 只取用到的字段，payload 从 ~50KB 瘦到 ~10KB（额度不变，流量与解析更快）
_SELECT = (
    "id,doi,display_name,publication_year,authorships,primary_location,"
    "abstract_inverted_index,open_access,language,cited_by_count"
)

# OpenAlex 单页上限（超出会 400）；深翻页时窗口可能取不满，has_more 自然为假
_MAX_PER_PAGE = 200
# page×per-page 的文档上限：再深只能走 cursor，而 cursor 跳不到任意页
_MAX_OFFSET = 10_000

# 单请求超时（秒）：15 秒对本机到这些站点的链路太紧（实测同一 URL 在
# 0.8–22.5 秒之间浮动，2026-09-19）。取 20 秒 + 网络级重试一次；
# 一页的**总时限**另有页级截止兜底（service.py 的 _PAGE_DEADLINE）——
# 单个源挂住不该把整页拖到一分钟（实测串行 73 秒那次就是这么来的）。
_TIMEOUT = 20.0

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
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        return with_retry(lambda: _read(req, timeout))
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
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise PaperError("无法连接 OpenAlex（网络不可达或超时），请稍后重试") from exc


def _read(req: urllib.request.Request, timeout: float) -> bytes:
    """裸 HTTP 调用（重试包裹的那一层）。"""
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


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
    ) -> tuple[list[PaperResult], int | None]:
        # **窗口对齐**（2026-09-16 两轮才修对；2026-09-20 又加深了一层）：
        # OpenAlex 只有 page/per-page，没有原生 offset，而页边界固定在 per-page
        # 的整数倍上——凑不出"边界刚好落在 start"。
        #   · 旧实现 `page = start // count + 1` 只在 start 是 count 整数倍时
        #     成立（两源各拿一半、恒 10 条时侥幸正确）；N 源轮转后 start 一般
        #     不是 count 的倍数，取回的窗口整体左移 → 翻页重复（E2E 实测
        #     40 条里 5 条重复）。
        #   · 第一版修法按 count 对齐页号、却按 per-page 取页，两者不是同一
        #     倍数，照样错位。
        #   · 第二版"从第 1 页取到 start+count 再切片"对齐是对的，但受单页
        #     上限 200 拖累：start ≥ 200 就永远取不满（该源从第 ~16 页起悄悄
        #     不再出结果）。2026-09-20 用户要"翻得越多越好"，于是改成**只取
        #     包含窗口的那一页**（窗口跨页时再取下一页），深度从 200 条抬到
        #     文档允许的一万条。
        per_page = _MAX_PER_PAGE if start + count > _MAX_PER_PAGE else start + count
        page = start // per_page + 1
        skip = start - (page - 1) * per_page

        def _page(number: int) -> tuple[list[PaperResult], int | None]:
            params = urllib.parse.urlencode(
                {
                    "search": q,
                    "per-page": per_page,
                    "page": number,
                    "sort": _sort_param(filters),
                    "select": _SELECT,
                    **_filter_params(filters),
                }
            )
            data = _load_json(_http_get(f"{_base_url()}/works?{params}", timeout=_TIMEOUT))
            works = data.get("results")
            if not isinstance(works, list):
                raise PaperError("OpenAlex 返回内容无法解析（缺少 results 列表）")
            # meta.count = 上游命中总数（与窗口无关）：界面用它显示"命中 N 条"，
            # 让"这库到底有多大"可见——知网那种规模感里我们唯一能诚实给出的部分
            meta = data.get("meta")
            count_total = meta.get("count") if isinstance(meta, dict) else None
            return [_normalize(w) for w in works if isinstance(w, dict)], count_total

        # 第 N 页的 page×per-page 不能超过 1 万（文档限制，再深只能走 cursor，
        # 而 cursor 没法"跳到任意页"）——所以到这里就如实说"本页没有"，
        # 由服务层的窗口公式留洞，界面上该源的命中数仍在
        if start >= _MAX_OFFSET:
            return [], None

        rows, total = _page(page)
        if skip + count > per_page:  # 窗口跨在两页上
            rows += _page(page + 1)[0]
        window = rows[skip : skip + count]
        return window, total if isinstance(total, int) else None

    def fetch(self, paper_id: str) -> PaperResult:
        if not validate_id(paper_id):
            raise PaperError(f"非法的 OpenAlex 论文编号：{paper_id}")
        params = urllib.parse.urlencode({"select": _SELECT})
        work = _load_json(_http_get(f"{_base_url()}/works/{paper_id}?{params}", timeout=_TIMEOUT))
        return _normalize(work)


# ---------------------------------------------------------------------------
# 引证关系（v0.1.4「更像知网」）：相关论文 / 引用了它
# ---------------------------------------------------------------------------
#
# 为什么放在 OpenAlex 模块里：**只有它有引证关系**。arXiv 是预印本库、
# DOAJ 是期刊目录、CORE 是全文聚合，三家都没有"谁引了谁"。而 OpenAlex
# 覆盖全学科并带 DOI，所以**别的来源的论文可以按 DOI 桥接过来**——
# 这正是"点开一篇中文刊的文章，也能看到相关论文"的实现方式。
#
# 实测（2026-09-19）：cites 过滤
# （filter=cites:W…）返回全量被引（一次实测 1254 条），按被引降序取前 N。
# 代价：每查一次 1~2 个请求，都是"用户点了才发"，不占检索链路预算。


def resolve_work_id(*, doi: str = "", openalex_id: str = "") -> str | None:
    """把一篇论文定位到 OpenAlex 的 W-id；定位不到返回 None（调用方如实说明）。

    优先用**已有**的 W-id（OpenAlex 来源直接就是）；否则用 DOI 反查——
    DOI 是跨来源的公共钥匙（arXiv/DOAJ/CORE 都带），也是这条桥能搭起来的原因。
    """
    if openalex_id:
        return openalex_id if validate_id(openalex_id) else None
    if not doi:
        return None
    params = urllib.parse.urlencode({"filter": f"doi:{doi}", "select": "id", "per-page": 1})
    data = _load_json(_http_get(f"{_base_url()}/works?{params}", timeout=_TIMEOUT))
    rows = data.get("results")
    if not isinstance(rows, list) or not rows:
        return None
    short_id = str(rows[0].get("id") or "").rstrip("/").rsplit("/", 1)[-1]
    return short_id if validate_id(short_id) else None


def fetch_works_by_ids(ids: list[str], limit: int) -> list[PaperResult]:
    """按 W-id 批量取元数据（related_works 给的是 id 列表，要再取一次详情）。"""
    wanted = [i for i in ids if validate_id(i)][:limit]
    if not wanted:
        return []
    params = urllib.parse.urlencode(
        {"filter": "openalex_id:" + "|".join(wanted), "select": _SELECT, "per-page": len(wanted)}
    )
    data = _load_json(_http_get(f"{_base_url()}/works?{params}", timeout=_TIMEOUT))
    rows = data.get("results")
    if not isinstance(rows, list):
        raise PaperError("OpenAlex 返回内容无法解析（缺少 results 列表）")
    # 上游不保证返回顺序 → 按请求的 id 顺序重排，界面上的"相关度"才不会乱跳
    by_id = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        short = str(row.get("id") or "").rstrip("/").rsplit("/", 1)[-1]
        by_id[short] = row
    return [_normalize(by_id[i]) for i in wanted if i in by_id]


def references_of(work_id: str, limit: int) -> list[PaperResult]:
    """它引用了谁（referenced_works）——知网那排出口里的"参考文献"。"""
    params = urllib.parse.urlencode({"select": "referenced_works"})
    data = _load_json(_http_get(f"{_base_url()}/works/{work_id}?{params}", timeout=_TIMEOUT))
    refs = data.get("referenced_works")
    if not isinstance(refs, list):
        return []
    ids = [str(u).rstrip("/").rsplit("/", 1)[-1] for u in refs]
    return fetch_works_by_ids(ids, limit)


def cited_by(work_id: str, limit: int) -> tuple[list[PaperResult], int | None]:
    """引用了它的论文（按被引降序——最经典的那些排前面）。返回 (结果, 总数)。"""
    params = urllib.parse.urlencode(
        {
            "filter": f"cites:{work_id}",
            "sort": "cited_by_count:desc",
            "select": _SELECT,
            "per-page": limit,
        }
    )
    data = _load_json(_http_get(f"{_base_url()}/works?{params}", timeout=_TIMEOUT))
    rows = data.get("results")
    if not isinstance(rows, list):
        raise PaperError("OpenAlex 返回内容无法解析（缺少 results 列表）")
    meta = data.get("meta")
    total = meta.get("count") if isinstance(meta, dict) else None
    return [_normalize(r) for r in rows if isinstance(r, dict)], (
        total if isinstance(total, int) else None
    )
