"""结构化分块器单测：边界 / 重叠 / 不变量（含两个真实 bug 的回归）。

回归锚点（都曾是线上 bug，见 chunker docstring）：
1. "块 ≤ size"不变量：段落以 \n\n 连接，预算必须计入分隔符；
   重叠尾巴按剩余容量截短——"重叠尾 + 整段"不得超发；
2. 页码跟随正文起始段，feed 内部的 flush 会清空窗口状态，
   页码必须在 feed 之后、真正入窗时才赋值。
"""

from __future__ import annotations

from mikasa.ingest.chunker import ChunkSpec, chunk_paragraphs
from mikasa.ingest.types import Para


def _para(text: str, heading: str | None = None, page: int | None = None) -> Para:
    return Para(text=text, heading_path=heading, page=page)


def _text(chunks: list[ChunkSpec]) -> str:
    return "\n\n".join(c.content for c in chunks)


# ---------------------------------------------------------------------------
# 回归 1：分隔符计入预算 + 重叠截短（块 ≤ size 恒成立）
# ---------------------------------------------------------------------------


def test_paragraph_separator_counted_in_budget():
    """两个 199 字符段 + \n\n = 恰好 400：应合成一个块而非两个。"""
    chunks = chunk_paragraphs([_para("甲" * 199), _para("乙" * 199)], size=400, overlap=50)
    assert len(chunks) == 1
    assert len(chunks[0].content) == 400


def test_overlap_respects_size():
    """真实 bug：302 段 + 388 段，size=400 —— 曾产出 440 字符的超发块。

    现在重叠尾巴按剩余容量截短（min(50, 400-388-2)=10），块恰好 400。
    """
    para1 = "一" * 302
    para2 = "二" * 388
    chunks = chunk_paragraphs([_para(para1), _para(para2)], size=400, overlap=50)
    assert len(chunks) == 2
    for c in chunks:
        assert len(c.content) <= 400
    assert len(chunks[1].content) == 400
    assert chunks[1].content.startswith(para1[-10:])  # 重叠尾巴确实续上了
    assert chunks[1].content.endswith(para2)  # 整段保留，无截断


def test_overlap_tail_skips_table_block():
    """重叠尾巴不截取表格段（2026-09-10 真实问答事故回归）。

    事故链：表格段在窗口尾部时，旧实现的 overlap 尾巴取自段内，把表尾
    “数据行”复制成下一块开头 → 无表头的表尾碎片块。问“胶结充填体这
    一行的各项参数”检索恰命中碎片（含数据行、缺列名），而含表头列名
    的完整表块因掺大量无关行被稀释排名落后未注入 → 模型只能答“未明确
    标注参数名称”。断言：表行数据只允许出现在以【表格】开头的块内。
    """
    table = (
        "【表格】\n岩石类型 密度 体积模量 剪切模量\n"
        "第四系 2.20 0.50 0.25\n胶结充填体 1.83 0.38 0.23"
    )
    paras = [
        _para("岩石力学参数选取直接影响数值模拟结果，表 2 列出本模型各层参数。"),
        _para(table),
        _para("后续正文：对矿体模型旋转模拟开采…" + "监测线沿井筒方向布置，" * 40),
    ]
    chunks = chunk_paragraphs(paras, size=200, overlap=50)
    marked = [c for c in chunks if "【表格】" in c.content]
    assert len(marked) == 1  # 表格块完整存在（与正文同窗也合法）
    assert "胶结充填体 1.83 0.38 0.23" in marked[0].content  # 表行齐全
    for c in chunks:
        if "【表格】" not in c.content:
            # 表行数据绝不出现在无标记块里（表尾碎片块事故）
            assert "胶结充填体" not in c.content and "2.20" not in c.content
    assert any("监测线沿井筒方向布置" in c.content for c in chunks)  # 后续正文照常分块


def test_oversized_table_split_by_rows():
    """超长表格段（>size）：按行打包切块，数据行不被标点/硬切劈半。"""
    rows = [f"岩层{i} 密度2.{i % 10} 模量45.0 黏聚1.8{i % 10}" for i in range(12)]
    table = "【表格】\n列名 密度 模量 黏聚\n" + "\n".join(rows)
    chunks = chunk_paragraphs([_para(table)], size=150, overlap=30)
    assert chunks and all(len(c.content) <= 150 for c in chunks)
    head = chunks[0].content.split("\n")
    assert head[0] == "【表格】" and head[1] == "列名 密度 模量 黏聚"
    joined = "\n".join(c.content for c in chunks)
    for row in rows:
        assert row in joined  # 每行整行存在（行切不劈行）


def test_overlap_fits_fully_when_room_allows():
    """整段放得下先打包（200+60 → 1 块）；真要切分时重叠给满 50。"""
    packed = chunk_paragraphs([_para("一" * 200), _para("二" * 60)], size=400, overlap=50)
    assert len(packed) == 1  # 262 ≤ 400：优先整段打包，不提前切

    p1 = "一" * 380  # 380 放得下，+ p2 就超（380+2+60 > 400）
    p2 = "二" * 60
    chunks = chunk_paragraphs([_para(p1), _para(p2)], size=400, overlap=50)
    assert len(chunks) == 2
    assert len(chunks[1].content) == 50 + 2 + 60  # tail 给满 50 + \n\n + para
    assert chunks[1].content == p1[-50:] + "\n\n" + p2


# ---------------------------------------------------------------------------
# 结构边界
# ---------------------------------------------------------------------------


def test_heading_change_flushes_and_never_seeds_overlap():
    p_a1 = _para("内容甲一", heading="A")
    p_a2 = _para("内容甲二", heading="A")
    p_b = _para("内容乙", heading="B")
    chunks = chunk_paragraphs([p_a1, p_a2, p_b], size=400, overlap=50)
    assert len(chunks) == 2
    assert chunks[0].heading_path == "A"
    assert chunks[1].heading_path == "B"
    # 跨标题的块不含上一块尾巴（B 块 = 自己的内容，无 A 的残留）
    assert chunks[1].content == "内容乙"


def test_heading_same_prefix_no_duplicate_flush():
    paras = [
        _para("第一句", heading="H"),
        _para("第二句", heading="H"),
        _para("第三句", heading="H > H2"),
    ]
    chunks = chunk_paragraphs(paras, size=400, overlap=50)
    assert [c.heading_path for c in chunks] == ["H", "H > H2"]
    assert _text(chunks) == "第一句\n\n第二句\n\n第三句"


def test_long_para_split_at_sentence_boundaries():
    """600 字符单段（句末标点齐全）应被切成整句块，不拦腰断句。"""
    sentence = "深度学习让模型学到有用的表示。"  # 15 字符
    text = sentence * 40  # 600 字符
    chunks = chunk_paragraphs([_para(text)], size=400, overlap=50)
    for c in chunks:
        assert len(c.content) <= 400
    # 句末标点切分：每块都以句号结尾（除最后一块可能是句号齐全）
    assert len(chunks) == 2
    assert all(c.content.endswith("。") for c in chunks)


def test_long_para_hard_cut_without_punctuation():
    """无任何句末/逗号标点的超长段：兜底硬切，但仍守 ≤size。

    重叠尾巴让第 3 块以第 2 块末 50 字符开头（信息重复属预期，
    拼接相等不成立；用覆盖性断言保证无信息丢失）。
    """
    text = "甲" * 900
    chunks = chunk_paragraphs([_para(text)], size=400, overlap=50)
    assert len(chunks) == 3  # 400 + 400 + (50 重叠 + 100)
    assert all(len(c.content) <= 400 for c in chunks)
    assert chunks[0].content == text[:400]
    assert chunks[1].content == text[400:800]
    assert chunks[2].content == text[750:800] + "\n\n" + text[800:]


def test_small_paras_pack_into_chunks():
    paras = [_para(f"我是第{i}段短内容") for i in range(60)]  # 每段 8 字符
    chunks = chunk_paragraphs(paras, size=400, overlap=50)
    assert len(chunks) >= 2
    assert all(len(c.content) <= 400 for c in chunks)
    assert chunks[0].content.startswith("我是第0段短内容")  # 顺序保持


# ---------------------------------------------------------------------------
# 页码归属（回归 2：feed 内部 flush 清空窗口状态）
# ---------------------------------------------------------------------------


def test_page_follows_starting_paragraph():
    """PDF 逐页成段：页码跟随块内正文起始段。"""
    p1 = _para("一" * 302, page=1)
    p2 = _para("二" * 388, page=2)
    p3 = _para("三" * 229, page=3)
    chunks = chunk_paragraphs([p1, p2, p3], size=400, overlap=50)
    assert [c.page for c in chunks] == [1, 2, 3]


def test_page_absent_when_paragraphs_have_none():
    paras = [_para("普通段落一" * 20), _para("普通段落二" * 30)]
    chunks = chunk_paragraphs(paras, size=400, overlap=50)
    assert all(c.page is None for c in chunks)


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------


def test_invalid_parameters_raise():
    import pytest

    with pytest.raises(ValueError):
        chunk_paragraphs([_para("x")], size=0, overlap=0)
    with pytest.raises(ValueError):
        chunk_paragraphs([_para("x")], size=10, overlap=10)  # overlap >= size
    with pytest.raises(ValueError):
        chunk_paragraphs([_para("x")], size=10, overlap=-1)


def test_blank_paragraphs_ignored():
    chunks = chunk_paragraphs([_para("   "), _para("正文"), _para("\n\n")], size=400, overlap=50)
    assert _text(chunks) == "正文"
