"""论文检索编排：双源交错分页 + 逐源降级。

**交错分页**：全局位置偶数位归 arXiv、奇数位归 OpenAlex。两源的
排序尺度不同（arXiv 相关性 vs OpenAlex 相关性），无法公平合并成一个
有序列表——交错保证每一页两个来源都有出镜，中文论文（OpenAlex 路）
不会因为排在英文论文（arXiv 路）后面而永远翻不到。

**逐源降级**：单源失败只进 errors 字典（200 照常返回另一源的结果），
两个源全灭才值得整体报错（路由层转 502）。

**has_more 取代 total**：两源的 total 都不可靠（arXiv 相关性计数会漂、
OpenAlex meta.count 是近似值），"取满 count"才是诚实的翻页信号。
"""

from __future__ import annotations

from dataclasses import dataclass

from mikasa.papers.arxiv import ArxivSource
from mikasa.papers.errors import PaperError
from mikasa.papers.openalex import OpenAlexSource
from mikasa.papers.sources import PaperResult, PaperSource

# 模块级注册表：测试可整表 monkeypatch 成假源（调用时查找，热可换）
SOURCES: dict[str, PaperSource] = {
    "arxiv": ArxivSource(),
    "openalex": OpenAlexSource(),
}


@dataclass(frozen=True)
class PaperPage:
    """一页检索结果（含逐源降级信息）。"""

    results: tuple[PaperResult, ...]
    errors: dict[str, str]  # source → 中文消息
    has_more: bool


def _window(source: str, offset: int, limit: int) -> tuple[int, int]:
    """全局窗口 [offset, offset+limit) → 该来源的 (start, count)。

    全局偶数位归 arxiv、奇数位归 openalex：全局位置 2k → arxiv 第 k 条，
    全局位置 2k+1 → openalex 第 k 条。窗口换算：
    - start：该来源在窗口内的第一条全局位置 / 2；
    - end：窗口内最后一条全局位置 / 2（整除）；
    - count = end - start + 1（end < start 时为 0，即窗口内没有该来源的位）。
    """
    if source == "arxiv":  # 偶数位 2k：k ∈ [ceil(offset/2), floor((offset+limit-1)/2)]
        start = (offset + 1) // 2
        end = (offset + limit - 1) // 2
    else:  # 奇数位 2k+1：k ∈ [floor(offset/2), floor((offset+limit-2)/2)]
        start = offset // 2
        end = (offset + limit - 2) // 2
    return start, max(0, end - start + 1)


def search(q: str, source: str, offset: int, limit: int) -> PaperPage:
    """检索一页。source="all" 时两源交错；未知 source 抛 PaperError。"""
    errors: dict[str, str] = {}
    if source == "all":
        arxiv_start, arxiv_count = _window("arxiv", offset, limit)
        oa_start, oa_count = _window("openalex", offset, limit)
        try:
            arxiv_results, arxiv_full = SOURCES["arxiv"].search(q, arxiv_start, arxiv_count)
        except PaperError as exc:
            arxiv_results, arxiv_full = [], False
            errors["arxiv"] = str(exc)
        try:
            oa_results, oa_full = SOURCES["openalex"].search(q, oa_start, oa_count)
        except PaperError as exc:
            oa_results, oa_full = [], False
            errors["openalex"] = str(exc)
        # 交错合并：奇数 offset 起头时 openalex 先行（全局位置奇偶决定了顺序）
        first, second = (
            (arxiv_results, oa_results) if offset % 2 == 0 else (oa_results, arxiv_results)
        )
        merged: list[PaperResult] = []
        # strict=False：两源条数本就可能不齐（一源耗尽），余量走尾巴补足
        for x, y in zip(first, second, strict=False):
            merged.append(x)
            merged.append(y)
        merged.extend(first[len(second) :])
        merged.extend(second[len(first) :])
        return PaperPage(
            results=tuple(merged),
            errors=errors,
            has_more=arxiv_full or oa_full,
        )
    if source not in SOURCES:
        raise PaperError(f"未知的论文来源：{source}")
    results, full = SOURCES[source].search(q, offset, limit)
    return PaperPage(results=tuple(results), errors=errors, has_more=full)


def fetch_paper(source: str, paper_id: str) -> PaperResult:
    """按 id 反查单条元数据（导入端点用；不信客户端传的标题）。"""
    if source not in SOURCES:
        raise PaperError(f"未知的论文来源：{source}")
    return SOURCES[source].fetch(paper_id)
