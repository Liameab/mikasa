"""论文检索编排：多源轮转交错分页 + 逐源降级 + 能力对齐。

**交错分页**：全局位置 `p` 归来源 `p % n`、是该源的第 `p // n` 条（n =
本次选中的来源数）。各源的相关性尺度互不可比，合并排序是编出来的；轮转
保证每一页每个来源都有出镜，中文论文（OpenAlex/CORE 路）不会因为排在
英文论文（arXiv 路）后面而永远翻不到。**来源顺序取注册表顺序 ∩ 选择集**
——保证 `["arxiv","core"]` 与 `["core","arxiv"]` 产出同一页。

**装填式合并**（2026-09-16 修正）：旧的 `zip + 尾巴补位` 在"某个源先耗尽"
时会让翻页**重复**——首页拿 4 条（a0,o0,o1,o2）后 offset 推进到 4，第二页
该源从第 2 条重新开始，o2 又出现一次。现在按全局位置装填：该位置的源若
没数据就留洞，不补位、不重复。

**逐源降级**：单源失败只进 errors（200 照常返回其余来源的结果）；实际
发出过请求的来源全灭才值得整体报错（路由层据 `attempted` 转 502）——
本页分配 0 个位置的源不进 attempted，用"选中数"做分母会永远凑不齐。

**notes 与 errors 是两回事**：errors = 失败，notes = 降级（某个筛选条件
该来源不支持、已按回落方式返回）。能力声明见 sources.SourceCaps。

**has_more 取代 total**：各源的 total 都不可靠，"取满 count"才是诚实的
翻页信号；`count == 0` 的源不参与判定（`0 == 0` 会造成假阳性）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from mikasa.papers.arxiv import ArxivSource
from mikasa.papers.core import CoreSource
from mikasa.papers.errors import PaperError
from mikasa.papers.openalex import OpenAlexSource
from mikasa.papers.sources import PaperFilters, PaperResult, PaperSource

# 模块级注册表：测试可整表 monkeypatch 成假源（调用时查找，热可换）。
# **顺序即交错顺序**（新源追加在末尾），也是 `sources=None`（全部来源）
# 的展开顺序——改顺序会改变已有来源的全局位置映射，非必要别动。
SOURCES: dict[str, PaperSource] = {
    "arxiv": ArxivSource(),
    "openalex": OpenAlexSource(),
    "core": CoreSource(),
}


@dataclass(frozen=True)
class PaperPage:
    """一页检索结果（含逐源降级与能力降级信息）。"""

    results: tuple[PaperResult, ...]
    errors: dict[str, str]  # source → 中文消息（失败）
    has_more: bool
    notes: dict[str, str] = field(default_factory=dict)  # source → 中文消息（降级）
    attempted: tuple[str, ...] = ()  # 本页实际发出请求的来源（502 判据用）


def _window(index: int, n: int, offset: int, limit: int) -> tuple[int, int]:
    """全局窗口 [offset, offset+limit) → 第 index 个来源的 (start, count)。

    全局位置 p 归来源 `p % n`、是该源的第 `p // n` 条。于是该来源取
    k ∈ [ceil((offset - index) / n), floor((offset + limit - 1 - index) / n)]，
    夹到 0 以下为空。整数上取整用 `-(-x // n)`（Python 的 // 向下取整）。

    n=2 时逐位还原旧实现：index=0 → ((offset+1)//2, (offset+limit-1)//2)，
    index=1 → (offset//2, (offset+limit-2)//2)——等价性由单测钉住。
    """
    lo = max(0, -(-(offset - index) // n))
    hi = (offset + limit - 1 - index) // n
    return lo, max(0, hi - lo + 1)


def _downgrade_notes(caps, filters: PaperFilters | None) -> str | None:
    """该来源本次请求里"不支持但用户要求了"的降级说明（无则 None）。

    `oa == "always"` 不算降级：它天然满足"只看开放获取"，用户的要求已经
    兑现——这一条如果不写清，后人会给它误加一条"不支持"的假提示。
    """
    if filters is None:
        return None
    parts: list[str] = []
    if filters.sort == "cited" and not caps.cited_sort:
        parts.append("不支持按被引排序，已按相关度返回")
    if filters.sort == "recent" and not caps.recent_sort:
        parts.append("不支持按时间排序，已按相关度返回")
    if (filters.date_from or filters.date_to) and not caps.year:
        parts.append("不支持按时间过滤，结果未按时间筛选")
    if filters.language and not caps.language:
        parts.append("不支持语言过滤，结果未按语言筛选")
    return "；".join(parts) or None


def search(
    q: str,
    sources: Sequence[str] | None,
    offset: int,
    limit: int,
    filters: PaperFilters | None = None,
) -> PaperPage:
    """检索一页。sources=None 表示注册表里的全部来源；空列表/未知来源抛 PaperError。"""
    if sources is not None:
        unknown = [n for n in sources if n not in SOURCES]
        if unknown:
            raise PaperError(f"未知的论文来源：{'、'.join(unknown)}")
    # 按**注册表顺序**取选择集：请求里数组顺序不影响结果（同一页对同一集合稳定）
    names = [n for n in SOURCES if sources is None or n in sources]
    if not names:
        raise PaperError("没有可用的论文来源（选择集为空）")
    n = len(names)
    errors: dict[str, str] = {}
    notes: dict[str, str] = {}
    collected: dict[str, list[PaperResult]] = {}
    starts: dict[str, int] = {}
    counts: dict[str, int] = {}
    attempted: list[str] = []
    for index, name in enumerate(names):
        start, count = _window(index, n, offset, limit)
        starts[name] = start
        counts[name] = count
        if count == 0:
            collected[name] = []  # 本页没有它的位：不发请求（limit=0 是无效请求）
            continue
        attempted.append(name)
        source = SOURCES[name]
        note = _downgrade_notes(source.caps, filters)
        if note:
            notes[name] = note
        try:
            results, _full = source.search(q, start, count, filters=filters)
            collected[name] = list(results)
        except PaperError as exc:
            collected[name] = []
            errors[name] = str(exc)
    # 装填式合并：按全局位置取，缺位留洞（不补位 = 不重复）
    merged: list[PaperResult] = []
    for p in range(offset, offset + limit):
        name = names[p % n]
        j = p // n - starts[name]
        bucket = collected[name]
        if 0 <= j < len(bucket):
            merged.append(bucket[j])
    has_more = any(counts[name] > 0 and len(collected[name]) >= counts[name] for name in names)
    return PaperPage(
        results=tuple(merged),
        errors=errors,
        has_more=has_more,
        notes=notes,
        attempted=tuple(attempted),
    )


def fetch_paper(source: str, paper_id: str) -> PaperResult:
    """按 id 反查单条元数据（导入端点用；不信客户端传的标题）。"""
    if source not in SOURCES:
        raise PaperError(f"未知的论文来源：{source}")
    return SOURCES[source].fetch(paper_id)


def source_catalog() -> list[dict]:
    """来源目录（`GET /api/papers/sources` 用）：界面按它渲染来源与能力。"""
    return [
        {
            "name": src.name,
            "label": src.label,
            "caps": {
                "year": src.caps.year,
                "cited_sort": src.caps.cited_sort,
                "recent_sort": src.caps.recent_sort,
                "language": src.caps.language,
                "oa": src.caps.oa,
            },
        }
        for src in SOURCES.values()
    ]
