"""论文检索编排单测：多源轮转分页 / 逐源降级 / 能力降级 / has_more。

全离线：每例 monkeypatch `mikasa.papers.service.SOURCES` 为假源（**整表
替换而非原地 clear+update**——后者会在进程内永久污染注册表，跑到后面的
用例会拿到假源），假源返回"带全局位置标记"的结果以断言交错顺序。
"""

from __future__ import annotations

import pytest

import mikasa.papers.service as service
from mikasa.papers.errors import PaperError
from mikasa.papers.sources import PaperFilters, PaperResult, SourceCaps


def _paper(source: str, idx: int) -> PaperResult:
    """假结果：id 编码来源与来源内序号，合并顺序一眼可辨。"""
    return PaperResult(
        source=source,
        id=f"{source}-{idx}",
        title=f"{source}-{idx}",
        authors=(),
        year=None,
        venue="",
        abstract="",
        doi="",
        pdf_url=None,
        landing_url="",
        oa=False,
    )


class _FakeSource:
    """可编排的假源：记录 (start, count, filters)，返回指定数量的标记结果。"""

    def __init__(
        self,
        name: str,
        total: int | None = None,
        fail: str | None = None,
        caps: SourceCaps | None = None,
    ) -> None:
        self.name = name
        self.label = name
        self.total = total  # None = 每页都取满
        self.fail = fail
        self.caps = caps or SourceCaps()
        self.calls: list[tuple[int, int, PaperFilters | None]] = []

    def search(
        self,
        q: str,
        start: int,
        count: int,
        *,
        filters: PaperFilters | None = None,
    ) -> tuple[list[PaperResult], bool]:
        self.calls.append((start, count, filters))
        if self.fail:
            raise PaperError(self.fail)
        n = count if self.total is None else max(0, min(count, self.total - start))
        return [_paper(self.name, start + i) for i in range(n)], n == count

    def fetch(self, paper_id: str) -> PaperResult:
        raise AssertionError("编排层不该调用 fetch")


def _use(monkeypatch, *specs: tuple[str, _FakeSource]) -> None:
    monkeypatch.setattr(service, "SOURCES", dict(specs))


def _ids(page: service.PaperPage) -> list[str]:
    return [r.id for r in page.results]


# ---------------------------------------------------------------------------
# 轮转交错分页
# ---------------------------------------------------------------------------


def test_interleave_offset_zero(monkeypatch):
    """offset=0：arxiv 占偶数位、openalex 占奇数位，逐对交错。"""
    _use(monkeypatch, ("arxiv", _FakeSource("arxiv")), ("openalex", _FakeSource("openalex")))
    page = service.search("x", None, 0, 6)
    assert _ids(page) == [
        "arxiv-0",
        "openalex-0",
        "arxiv-1",
        "openalex-1",
        "arxiv-2",
        "openalex-2",
    ]
    assert page.errors == {} and page.has_more is True


def test_interleave_continuation_no_overlap(monkeypatch):
    """offset=20 续页：各源从第 10 条开始，与首页零重叠。"""
    arxiv_src = _FakeSource("arxiv", total=25)
    oa_src = _FakeSource("openalex", total=25)
    _use(monkeypatch, ("arxiv", arxiv_src), ("openalex", oa_src))
    page = service.search("x", None, 20, 6)
    assert _ids(page) == [
        "arxiv-10",
        "openalex-10",
        "arxiv-11",
        "openalex-11",
        "arxiv-12",
        "openalex-12",
    ]
    assert [c[:2] for c in arxiv_src.calls] == [(10, 3)] and [c[:2] for c in oa_src.calls] == [
        (10, 3)
    ]


def test_interleave_odd_offset_openalex_first(monkeypatch):
    """奇数 offset 起头：openalex 先行（全局位置奇偶决定顺序）。"""
    _use(monkeypatch, ("arxiv", _FakeSource("arxiv")), ("openalex", _FakeSource("openalex")))
    page = service.search("x", None, 5, 4)
    assert _ids(page) == [
        "openalex-2",
        "arxiv-3",
        "openalex-3",
        "arxiv-4",
    ]


def test_exhausted_source_leaves_holes_not_duplicates(monkeypatch):
    """一源先耗尽：空位留洞，**不补位**（补位会让翻页重复）。

    offset=0/limit=6，arxiv 只有 1 条 → 全局位置 2、4 是洞，本页 4 条。
    续页必须按**窗口大小**推进 offset（→6）；若按"已收到条数"推（→4），
    第二页会把 openalex-2 再吐一次（2026-09-16 修掉的真实缺陷）。
    """
    _use(
        monkeypatch, ("arxiv", _FakeSource("arxiv", total=1)), ("openalex", _FakeSource("openalex"))
    )
    page1 = service.search("x", None, 0, 6)
    assert _ids(page1) == ["arxiv-0", "openalex-0", "openalex-1", "openalex-2"]
    assert page1.has_more is True  # openalex 取满
    page2 = service.search("x", None, 6, 6)  # 按窗口大小推进
    assert _ids(page2) == ["openalex-3", "openalex-4", "openalex-5"]
    assert not set(_ids(page1)) & set(_ids(page2))  # 零重叠 = 不重复


def test_three_sources_rotate(monkeypatch):
    """三源轮转：全局位置 p 归 p % 3，顺序取注册表序。"""
    _use(
        monkeypatch,
        ("arxiv", _FakeSource("arxiv")),
        ("openalex", _FakeSource("openalex")),
        ("core", _FakeSource("core")),
    )
    page = service.search("x", None, 0, 6)
    assert _ids(page) == [
        "arxiv-0",
        "openalex-0",
        "core-0",
        "arxiv-1",
        "openalex-1",
        "core-1",
    ]


def test_selection_order_follows_registry(monkeypatch):
    """选择集的数组顺序不影响结果（按注册表序取交集）。"""
    _use(
        monkeypatch,
        ("arxiv", _FakeSource("arxiv")),
        ("openalex", _FakeSource("openalex")),
        ("core", _FakeSource("core")),
    )
    a = service.search("x", ["arxiv", "core"], 0, 4)
    b = service.search("x", ["core", "arxiv"], 0, 4)
    assert _ids(a) == _ids(b) == ["arxiv-0", "core-0", "arxiv-1", "core-1"]


def test_single_source_passthrough(monkeypatch):
    src = _FakeSource("arxiv")
    _use(monkeypatch, ("arxiv", src), ("openalex", _FakeSource("openalex")))
    page = service.search("x", ["arxiv"], 7, 3)
    assert _ids(page) == ["arxiv-7", "arxiv-8", "arxiv-9"]
    assert [c[:2] for c in src.calls] == [(7, 3)]  # 单源：start=offset 原样传递


def test_unknown_or_empty_sources_raise(monkeypatch):
    _use(monkeypatch, ("arxiv", _FakeSource("arxiv")))
    with pytest.raises(PaperError, match="未知的论文来源"):
        service.search("x", ["google"], 0, 5)
    with pytest.raises(PaperError, match="没有可用的论文来源"):
        service.search("x", [], 0, 5)
    with pytest.raises(PaperError, match="未知的论文来源"):
        service.fetch_paper("google", "id")


# ---------------------------------------------------------------------------
# 逐源降级 / 能力降级 / has_more
# ---------------------------------------------------------------------------


def test_one_source_fails_degrades(monkeypatch):
    """单源失败：结果照常返回其余来源，errors 记录中文消息。"""
    _use(
        monkeypatch,
        ("arxiv", _FakeSource("arxiv", fail="无法连接 arXiv（网络不可达或超时），请稍后重试")),
        ("openalex", _FakeSource("openalex")),
    )
    page = service.search("x", None, 0, 4)
    assert _ids(page) == ["openalex-0", "openalex-1"]  # arxiv 缺席，留洞不补位
    assert "arxiv" in page.errors
    assert "无法连接 arXiv" in page.errors["arxiv"]
    assert page.has_more is True


def test_both_sources_fail(monkeypatch):
    """多源全灭：空结果 + 满 errors + has_more=False（路由层转 502）。"""
    _use(
        monkeypatch,
        ("arxiv", _FakeSource("arxiv", fail="A 挂了")),
        ("openalex", _FakeSource("openalex", fail="B 挂了")),
    )
    page = service.search("x", None, 0, 4)
    assert page.results == ()
    assert page.errors == {"arxiv": "A 挂了", "openalex": "B 挂了"}
    assert page.has_more is False
    assert page.attempted == ("arxiv", "openalex")


def test_has_more_false_when_short(monkeypatch):
    _use(
        monkeypatch,
        ("arxiv", _FakeSource("arxiv", total=3)),
        ("openalex", _FakeSource("openalex", total=1)),
    )
    page = service.search("x", None, 0, 10)
    assert page.has_more is False


def test_empty_page_never_has_more(monkeypatch):
    """空页 ⇒ has_more=False（不变量）：某源本页分配 0 个位置时，
    `len(results) == count` 会退化成 `0 == 0` 造成假阳性。"""
    src = _FakeSource("openalex", total=0)
    _use(monkeypatch, ("openalex", src))
    page = service.search("x", ["openalex"], 0, 10)
    assert page.results == ()
    assert page.has_more is False


def test_zero_slot_source_not_attempted(monkeypatch):
    """本页没分到位（count=0）的源不发请求，也不进 attempted。"""
    a, b = _FakeSource("arxiv"), _FakeSource("openalex")
    _use(monkeypatch, ("arxiv", a), ("openalex", b))
    page = service.search("x", None, 0, 1)  # 窗口只有全局 0 号位 → 归 arxiv
    assert a.calls and not b.calls
    assert page.attempted == ("arxiv",)


def test_filters_passed_through_and_notes(monkeypatch):
    """筛选条件透传给每个来源；不支持的能力产出中文降级说明。"""
    arxiv = _FakeSource("arxiv", caps=SourceCaps(year=True, cited_sort=False, oa="always"))
    oa = _FakeSource("openalex", caps=SourceCaps(year=True, cited_sort=True, oa="filterable"))
    _use(monkeypatch, ("arxiv", arxiv), ("openalex", oa))
    filters = PaperFilters(date_from="2020-01-01", sort="cited", oa_only=True)
    page = service.search("x", None, 0, 4, filters)
    # 两个源都收到了同一份筛选条件（翻译发生在来源内部）
    assert all(c[2] is filters for c in arxiv.calls + oa.calls)
    assert "arxiv" in page.notes and "被引" in page.notes["arxiv"]
    # openalex 支持按被引排序 → 无 note；`oa="always"` 是"已满足"不是降级 → 无 note
    assert "openalex" not in page.notes


def test_year_and_language_notes(monkeypatch):
    src = _FakeSource("core", caps=SourceCaps(year=False, language=False))
    _use(monkeypatch, ("core", src))
    page = service.search("x", None, 0, 4, PaperFilters(date_from="2021-05-01", language="zh"))
    assert "时间" in page.notes["core"] and "语言" in page.notes["core"]


# ---------------------------------------------------------------------------
# 窗口换算（_window 纯函数）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("index", "n", "offset", "limit", "expected"),
    [
        # n=2：与旧实现（按来源名硬编码奇偶）逐位等价，这里钉住等价性
        (0, 2, 0, 20, (0, 10)),
        (1, 2, 0, 20, (0, 10)),
        (0, 2, 20, 20, (10, 10)),
        (1, 2, 20, 20, (10, 10)),
        (0, 2, 5, 5, (3, 2)),  # 全局 6,8
        (1, 2, 5, 5, (2, 3)),  # 全局 5,7,9
        (0, 2, 0, 1, (0, 1)),
        (1, 2, 0, 1, (0, 0)),  # 窗口只有全局 0 号位，归 0 号源
        (0, 2, 1, 1, (1, 0)),
        (1, 2, 1, 1, (0, 1)),
        # n=3：轮转分配（4+3+3 = 10）
        (0, 3, 0, 10, (0, 4)),
        (1, 3, 0, 10, (0, 3)),
        (2, 3, 0, 10, (0, 3)),
        (0, 3, 10, 3, (4, 1)),  # 全局 12 → 源 0 的第 4 条
        (1, 3, 10, 3, (3, 1)),  # 全局 10 → 源 1 的第 3 条
        (2, 3, 10, 3, (3, 1)),  # 全局 11 → 源 2 的第 3 条
    ],
)
def test_window_math(index, n, offset, limit, expected):
    assert service._window(index, n, offset, limit) == expected


def test_window_total_covers_limit():
    """不变量：n 个来源的窗口条数之和 == limit（位置要么有主、要么是洞）。"""
    for n in (1, 2, 3, 4):
        for offset in (0, 1, 7, 20):
            for limit in (1, 3, 10, 20):
                total = sum(service._window(i, n, offset, limit)[1] for i in range(n))
                assert total == limit, f"n={n} offset={offset} limit={limit}"
