"""论文检索编排单测：双源交错分页 / 逐源降级 / has_more。

全离线：整表 monkeypatch `mikasa.papers.service.SOURCES` 为假源，
假源返回"带全局位置标记"的结果以断言交错顺序。
"""

from __future__ import annotations

import pytest

import mikasa.papers.service as service
from mikasa.papers.errors import PaperError
from mikasa.papers.sources import PaperResult


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
    """可编排的假源：记录 (start, count)，返回指定数量的标记结果。"""

    def __init__(self, name: str, total: int | None = None, fail: str | None = None) -> None:
        self.name = name
        self.total = total  # None = 每页都取满
        self.fail = fail
        self.calls: list[tuple[int, int]] = []

    def search(self, q: str, start: int, count: int) -> tuple[list[PaperResult], bool]:
        self.calls.append((start, count))
        if self.fail:
            raise PaperError(self.fail)
        n = count if self.total is None else max(0, min(count, self.total - start))
        return [_paper(self.name, start + i) for i in range(n)], n == count

    def fetch(self, paper_id: str) -> PaperResult:
        raise AssertionError("编排层不该调用 fetch")


def _use(*specs: tuple[str, _FakeSource]) -> None:
    service.SOURCES.clear()
    service.SOURCES.update(specs)


def _ids(page: service.PaperPage) -> list[str]:
    return [r.id for r in page.results]


# ---------------------------------------------------------------------------
# 交错分页
# ---------------------------------------------------------------------------


def test_interleave_offset_zero(monkeypatch):
    """offset=0：arxiv 占偶数位、openalex 占奇数位，逐对交错。"""
    _use(("arxiv", _FakeSource("arxiv")), ("openalex", _FakeSource("openalex")))
    page = service.search("x", "all", 0, 6)
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
    _use(("arxiv", arxiv_src), ("openalex", oa_src))
    page = service.search("x", "all", 20, 6)
    assert _ids(page) == [
        "arxiv-10",
        "openalex-10",
        "arxiv-11",
        "openalex-11",
        "arxiv-12",
        "openalex-12",
    ]
    assert arxiv_src.calls == [(10, 3)] and oa_src.calls == [(10, 3)]


def test_interleave_odd_offset_openalex_first(monkeypatch):
    """奇数 offset 起头：openalex 先行（全局位置奇偶决定顺序）。"""
    _use(("arxiv", _FakeSource("arxiv")), ("openalex", _FakeSource("openalex")))
    page = service.search("x", "all", 5, 4)
    assert _ids(page) == [
        "openalex-2",
        "arxiv-3",
        "openalex-3",
        "arxiv-4",
    ]


def test_interleave_source_exhausted_tail(monkeypatch):
    """一源先耗尽：余下全归另一源（交错 zip 后接尾巴）。

    limit=6 的窗口只有 3 个奇数位（全局 1/3/5）归 openalex，arxiv 只出
    1 条（total=1）——交错后 openalex 的余量补在尾巴上。
    """
    _use(("arxiv", _FakeSource("arxiv", total=1)), ("openalex", _FakeSource("openalex")))
    page = service.search("x", "all", 0, 6)
    assert _ids(page) == [
        "arxiv-0",
        "openalex-0",
        "openalex-1",
        "openalex-2",
    ]
    assert page.has_more is True  # openalex 仍取满


def test_single_source_passthrough(monkeypatch):
    src = _FakeSource("arxiv")
    _use(("arxiv", src), ("openalex", _FakeSource("openalex")))
    page = service.search("x", "arxiv", 7, 3)
    assert _ids(page) == ["arxiv-7", "arxiv-8", "arxiv-9"]
    assert src.calls == [(7, 3)]  # 单源：start=offset 原样传递


def test_unknown_source_raises():
    with pytest.raises(PaperError, match="未知的论文来源"):
        service.search("x", "google", 0, 5)
    with pytest.raises(PaperError, match="未知的论文来源"):
        service.fetch_paper("google", "id")


# ---------------------------------------------------------------------------
# 逐源降级与 has_more
# ---------------------------------------------------------------------------


def test_one_source_fails_degrades(monkeypatch):
    """单源失败：结果照常返回另一源，errors 记录中文消息。"""
    _use(
        ("arxiv", _FakeSource("arxiv", fail="无法连接 arXiv（网络不可达或超时），请稍后重试")),
        ("openalex", _FakeSource("openalex")),
    )
    page = service.search("x", "all", 0, 4)
    assert _ids(page) == ["openalex-0", "openalex-1"]  # arxiv 缺席，openalex 全量补位
    assert "arxiv" in page.errors
    assert "无法连接 arXiv" in page.errors["arxiv"]
    assert page.has_more is True


def test_both_sources_fail(monkeypatch):
    """双源全灭：空结果 + 双 errors + has_more=False（路由层转 502）。"""
    _use(
        ("arxiv", _FakeSource("arxiv", fail="A 挂了")),
        ("openalex", _FakeSource("openalex", fail="B 挂了")),
    )
    page = service.search("x", "all", 0, 4)
    assert page.results == ()
    assert page.errors == {"arxiv": "A 挂了", "openalex": "B 挂了"}
    assert page.has_more is False


def test_has_more_false_when_short(monkeypatch):
    _use(
        ("arxiv", _FakeSource("arxiv", total=3)),
        ("openalex", _FakeSource("openalex", total=1)),
    )
    page = service.search("x", "all", 0, 10)
    assert page.has_more is False


# ---------------------------------------------------------------------------
# 窗口换算（_window 纯函数）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "offset", "limit", "expected"),
    [
        ("arxiv", 0, 20, (0, 10)),
        ("openalex", 0, 20, (0, 10)),
        ("arxiv", 20, 20, (10, 10)),
        ("openalex", 20, 20, (10, 10)),
        ("arxiv", 5, 5, (3, 2)),  # 全局 6,8
        ("openalex", 5, 5, (2, 3)),  # 全局 5,7,9
        ("arxiv", 0, 1, (0, 1)),
        ("openalex", 0, 1, (0, 0)),  # 窗口只有全局 0 号位，归 arxiv
        ("arxiv", 1, 1, (1, 0)),  # 窗口只有全局 1 号位，归 openalex
        ("openalex", 1, 1, (0, 1)),
    ],
)
def test_window_math(source, offset, limit, expected):
    assert service._window(source, offset, limit) == expected
