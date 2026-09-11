"""页面定位纯函数单测（阅读视图引用高亮的坐标来源）。

回归锚点（都是实测定下来的，见 pagelocate 模块 docstring）：
1. **必须容忍清洗差异**：chunk 文本入库时被折叠空白、剥标点，与 PDF 原文
   不是逐字一致——`page.search_for` 因此只有 55% 命中率，词元归一后 98%；
2. **必须按行分组**：一个跨十几行的块若只给一个大包围盒，高亮会糊住整页；
3. **短文本不许乱命中**：<min_run 个词元在任何页面上都能撞上，宁可不高亮。
"""

from __future__ import annotations

from mikasa.ingest.pagelocate import locate_in_page, normalize_token

# 一张 600x800 的假页面：第一行 6 个词（够 min_run）、第二行 4 个、末尾另起一段
_WORDS = [
    (72.0, 100.0, 120.0, 112.0, "Under"),
    (124.0, 100.0, 190.0, 112.0, "cyclic"),
    (194.0, 100.0, 260.0, 112.0, "loading"),
    (264.0, 100.0, 300.0, 112.0, "the"),
    (304.0, 100.0, 360.0, 112.0, "capacity"),
    (364.0, 100.0, 440.0, 112.0, "degrades."),
    (72.0, 124.0, 110.0, 136.0, "when"),
    (114.0, 124.0, 150.0, 136.0, "the"),
    (154.0, 124.0, 220.0, 136.0, "amplitude"),
    (224.0, 124.0, 300.0, 136.0, "increases."),
    (72.0, 300.0, 130.0, 312.0, "图89"),
    (134.0, 300.0, 200.0, 312.0, "荷载位移"),
]


def _locate(text: str):
    return locate_in_page(_WORDS, text, page_width=600.0, page_height=800.0)


def test_exact_text_locates_and_normalizes():
    got = _locate("Under cyclic loading the capacity degrades when the amplitude increases.")
    assert got.ok
    assert got.matched == 10
    # 两行 → 两个矩形；坐标归一化到 0~1
    assert len(got.rects) == 2
    x0, y0, x1, y1 = got.rects[0]
    assert 0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0
    assert abs(x0 - 72.0 / 600.0) < 1e-6
    assert abs(y0 - 100.0 / 800.0) < 1e-6


def test_tolerates_whitespace_and_punctuation_differences():
    """清洗差异（折行、多余空白、标点）不该影响定位——这是选词元匹配的原因。"""
    got = _locate("Under\n\ncyclic   loading, the capacity degrades!")
    assert got.ok and got.matched == 6


def test_fullwidth_normalizes_to_halfwidth():
    assert normalize_token("ＡＢＣ１２３") == "abc123"
    assert normalize_token("循环，荷载。") == "循环荷载"
    assert normalize_token("  ") == ""


def test_single_line_chunk_gives_single_rect():
    got = _locate("Under cyclic loading the capacity degrades.")
    assert got.ok and got.matched == 6 and len(got.rects) == 1


def test_too_short_text_is_refused():
    """短块在任何页面都能撞上，宁可不高亮也不打错地方。"""
    assert not _locate("the capacity").ok
    assert not _locate("Under cyclic").ok  # 2 个词元 < min_run(6)


def test_absent_text_returns_empty():
    got = _locate("quantum annealing cools a system into its ground state")
    assert not got.ok and got.rects == [] and got.matched == 0


def test_anchors_at_the_match_not_the_first_coincidence():
    """页面上有重复词元时，锚点要落在**真正那一段**上，不能撞在最前面的偶合处。

    "the capacity" 在页面上出现两次；文本接的是 changes/with/…，只有第二处
    能连续匹配下去，所以高亮必须落在第二处（y 从 40 起）。
    """
    words = [
        (0.0, 0.0, 10.0, 10.0, "the"),
        (0.0, 20.0, 10.0, 30.0, "capacity"),
        (0.0, 40.0, 10.0, 50.0, "the"),
        (0.0, 60.0, 10.0, 70.0, "capacity"),
        (0.0, 80.0, 10.0, 90.0, "changes"),
        (0.0, 100.0, 10.0, 110.0, "with"),
        (0.0, 120.0, 10.0, 130.0, "depth"),
        (0.0, 140.0, 10.0, 150.0, "and"),
        (0.0, 160.0, 10.0, 170.0, "more"),
    ]
    got = locate_in_page(
        words, "the capacity changes with depth and more", page_width=10.0, page_height=200.0
    )
    assert got.matched == 7
    assert got.rects[0][1] == 40.0 / 200.0  # 落在第二处，不是 y=0 那处


def test_empty_inputs():
    assert not locate_in_page([], "abc def ghi jkl mno pqr", page_width=1.0, page_height=1.0).ok
    assert not locate_in_page(_WORDS, "   ", page_width=600.0, page_height=800.0).ok
    # 页面尺寸非法（未渲染）时不猜
    bad_size = locate_in_page(
        _WORDS, "Under cyclic loading the capacity", page_width=0, page_height=0
    )
    assert not bad_size.ok
