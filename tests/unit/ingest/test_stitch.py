"""分块逆向拼接单测：去接缝的四条守卫与两条规则。

回归锚点（与 chunker 的真实行为同源，改 chunking.overlap 时两处一起看）：
1. 重叠只在相邻块**同 heading_path** 时产生（chunker.py:110-114 在标题变化处
   清空 tail）——跨标题裁剪是纯粹的误伤，必须守住；
2. 长段真实重复（术语、表头）不该被当接缝裁掉——近尾规则必须限制匹配起点
   落在前块尾部一小段内，否则领域文档里的高频术语会被误裁；
3. 偏移不变量（首 0 末 len、相邻连续）是前端锚点定位的唯一依据。
"""

from __future__ import annotations

from mikasa.ingest.stitch import stitch_chunks
from mikasa.models.document import Chunk

# 一段有实义的"重叠片段"（模拟 chunker 从同标题前块尾部取的尾巴）
_SEAM = "循环荷载作用下螺旋锚的承载力退化"


def _chunk(
    content: str,
    seq: int,
    *,
    chunk_id: int | None = None,
    heading: str | None = "标题甲",
    page: int | None = None,
) -> Chunk:
    return Chunk(
        id=chunk_id if chunk_id is not None else seq + 1,
        document_id=1,
        seq=seq,
        content=content,
        content_sha256=f"sha{seq}",
        heading_path=heading,
        page_number=page,
    )


# ---------------------------------------------------------------------------
# 规则 ①：后缀规则（重叠延伸到前块末尾）
# ---------------------------------------------------------------------------


def test_full_suffix_overlap_is_trimmed():
    prev = "甲" * 100
    cur = prev[-50:] + "乙" * 100  # 后块开头 = 前块最后 50 字
    stitched = stitch_chunks([_chunk(prev, 0), _chunk(cur, 1)])
    assert stitched.text == "甲" * 100 + "乙" * 100  # 重叠只出现一次
    assert stitched.trimmed_chars == 50


def test_no_overlap_concatenates_verbatim():
    stitched = stitch_chunks([_chunk("前半段。", 0), _chunk("后半段。", 1)])
    assert stitched.text == "前半段。后半段。"
    assert stitched.trimmed_chars == 0


# ---------------------------------------------------------------------------
# 规则 ②：近尾规则（重叠被 chunker 按剩余容量截短，不再是后缀）
# ---------------------------------------------------------------------------


def test_truncated_overlap_caught_by_near_end_rule():
    """chunker.py:94 的 take 会截短尾巴——此时片段后面还跟着前块的正文。"""
    prev = "前" * 300 + _SEAM + "尾" * 10  # 接缝起点距末尾 26 字
    cur = _SEAM + "新正文" * 20
    stitched = stitch_chunks([_chunk(prev, 0), _chunk(cur, 1)])
    assert stitched.trimmed_chars == len(_SEAM)
    assert stitched.text.count(_SEAM) == 1  # 只出现一次
    assert stitched.text.startswith("前" * 300 + _SEAM + "尾" * 10 + "新正文")


def test_far_repetition_is_not_trimmed():
    """误裁守卫：同样一段话出现在前块**开头**（超出 seam_window）→ 不动它。

    真实场景：论文里反复出现的术语/表头。裁掉它会让原文缺一块。
    """
    prev = _SEAM + "正" * 300
    cur = _SEAM + "续" * 100
    stitched = stitch_chunks([_chunk(prev, 0), _chunk(cur, 1)])
    assert stitched.trimmed_chars == 0
    assert stitched.text.count(_SEAM) == 2  # 两处都在


# ---------------------------------------------------------------------------
# 守卫
# ---------------------------------------------------------------------------


def test_cross_heading_never_trimmed():
    """跨标题不可能是重叠（chunker.py:110-114 在标题变化处清空 tail）。"""
    prev = "甲" * 100
    cur = prev[-50:] + "乙" * 100
    stitched = stitch_chunks([_chunk(prev, 0, heading="标题甲"), _chunk(cur, 1, heading="标题乙")])
    assert stitched.trimmed_chars == 0
    assert stitched.text == prev + cur  # 原样保留


def test_short_seam_below_min_seam_not_trimmed():
    """<8 字的匹配太容易是巧合（实测真实残留 ≤7 字，视觉无感）。"""
    prev = "甲" * 100 + "AB"
    cur = "AB" + "乙" * 100
    stitched = stitch_chunks([_chunk(prev, 0), _chunk(cur, 1)])
    assert stitched.trimmed_chars == 0


def test_whole_chunk_overlap_never_trims_to_empty():
    """保底：k >= len(cur) 一律不裁——绝不把一块裁空。"""
    same = "甲" * 100
    stitched = stitch_chunks([_chunk(same, 0), _chunk(same, 1)])
    assert stitched.trimmed_chars == 0
    assert stitched.text == same + same


# ---------------------------------------------------------------------------
# 偏移不变量（前端锚点定位的唯一依据）
# ---------------------------------------------------------------------------


def test_spans_are_contiguous_and_cover_text():
    chunks = [
        _chunk("第一块正文。", 0, heading="甲"),
        _chunk("第一块正文。续写的第二块。", 1, heading="甲"),  # 有重叠
        _chunk("换了标题的第三块。", 2, heading="乙", page=3),  # 跨标题
        _chunk("第四块。", 3, heading="乙", page=3),
    ]
    stitched = stitch_chunks(chunks)

    assert stitched.spans[0].start == 0
    assert stitched.spans[-1].end == len(stitched.text)
    for prev, cur in zip(stitched.spans, stitched.spans[1:], strict=False):
        assert prev.end == cur.start  # 无空洞、无重叠
    assert sum(s.end - s.start for s in stitched.spans) == len(stitched.text)
    # 切出的片段必是原块的**后缀**（本函数只裁块首接缝，不裁块尾）
    for span in stitched.spans:
        assert chunks[span.seq].content.endswith(stitched.text[span.start : span.end])


def test_span_carries_chunk_identity_and_metadata():
    stitched = stitch_chunks([_chunk("正文。", 0, chunk_id=77, heading="路线 > 甲", page=None)])
    span = stitched.spans[0]
    assert (span.chunk_id, span.seq, span.heading_path, span.page_number) == (
        77,
        0,
        "路线 > 甲",
        None,
    )


def test_empty_and_single_chunk():
    empty = stitch_chunks([])
    assert empty.text == "" and empty.spans == [] and empty.trimmed_chars == 0

    single = stitch_chunks([_chunk("唯一一块。", 0)])
    assert single.text == "唯一一块。"
    assert single.spans[0].start == 0 and single.spans[0].end == len("唯一一块。")
