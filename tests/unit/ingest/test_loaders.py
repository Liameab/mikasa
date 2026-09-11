"""四格式 loader 单测：md/txt(GBK)/docx/pdf + 编码探测 + 页眉页脚剔除。

PDF 部分是对 _drop_page_furniture 的回归测试：
样本 PDF 由本测试实时生成（脚本即"源代码"，无二进制 fixture）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mikasa.errors import IngestError
from mikasa.ingest.loaders import load_docx, load_markdown, load_pdf, load_txt

# ---------------------------------------------------------------------------
# Markdown：标题层级 + Setext
# ---------------------------------------------------------------------------


def test_markdown_headings_and_paragraphs(tmp_path: Path):
    md = tmp_path / "notes.md"
    md.write_text(
        "# 主标题\n\n## 第一节\n\n段落甲。\n\n段落乙。\n\n### 子节\n\n段落丙。",
        encoding="utf-8",
    )
    doc = load_markdown(md)
    assert doc.title == "主标题"
    paths = [p.heading_path for p in doc.paragraphs]
    assert paths == [
        "主标题 > 第一节",
        "主标题 > 第一节",
        "主标题 > 第一节 > 子节",
    ]
    assert [p.text for p in doc.paragraphs] == ["段落甲。", "段落乙。", "段落丙。"]


def test_markdown_setext_heading(tmp_path: Path):
    md = tmp_path / "setext.md"
    md.write_text("一级标题\n=======\n\n正文段落。", encoding="utf-8")
    doc = load_markdown(md)
    assert doc.title == "一级标题"
    assert doc.paragraphs[0].heading_path == "一级标题"


def test_markdown_setext_heading_at_eof(tmp_path: Path):
    """文件以 Setext 标题结尾（笔记末尾不空行的常见写法）也要识别。

    2026-09-11 修复：旧条件 `i + 1 < len(lines)` 要求下划线之后还有行，
    末尾标题识别不出，下划线本身还会当正文混进段落（进向量与引用展示）。
    """
    md = tmp_path / "setext-eof.md"
    md.write_text("# 开头\n\n正文一段。\n\n结尾标题\n===\n", encoding="utf-8")
    doc = load_markdown(md)
    texts = [p.text for p in doc.paragraphs]
    assert "正文一段。" in texts
    assert not any("===" in t for t in texts), "下划线不能混进正文段落"


def test_markdown_skips_fenced_code(tmp_path: Path):
    md = tmp_path / "code.md"
    code = "# 标题\n\n```python\nprint('含噪代码不该入段')\n```\n\n真实正文。"
    md.write_text(code, encoding="utf-8")
    doc = load_markdown(md)
    texts = [p.text for p in doc.paragraphs]
    assert "真实正文。" in texts
    assert not any("print(" in t for t in texts)


# ---------------------------------------------------------------------------
# TXT：GBK 编码探测
# ---------------------------------------------------------------------------


def test_txt_gbk_decoded(tmp_path: Path):
    txt = tmp_path / "提纲.txt"
    txt.write_bytes("机器学习方向复习提纲\n\n线性代数：矩阵与特征值。".encode("gbk"))
    doc = load_txt(txt)
    assert doc.title == "提纲"
    assert "线性代数：矩阵与特征值。" in [p.text for p in doc.paragraphs]


def test_txt_utf8_and_folded_whitespace(tmp_path: Path):
    txt = tmp_path / "utf8.txt"
    txt.write_text("第一段　全角空格折叠。\n\n  第二段带缩进。", encoding="utf-8")
    doc = load_txt(txt)
    assert [p.text for p in doc.paragraphs] == ["第一段 全角空格折叠。", "第二段带缩进。"]


# ---------------------------------------------------------------------------
# DOCX：Heading 样式 + 表格
# ---------------------------------------------------------------------------


def test_docx_headings_and_table(tmp_path: Path):
    import docx

    path = tmp_path / "路线.docx"
    document = docx.Document()
    document.add_heading("备考路线", level=1)
    document.add_heading("第一阶段", level=2)
    document.add_paragraph("线性代数与概率论收尾。")
    table = document.add_table(rows=2, cols=2)
    table.style = "Table Grid"
    table.rows[0].cells[0].text = "周次"
    table.rows[0].cells[1].text = "任务"
    table.rows[1].cells[0].text = "第 1 周"
    table.rows[1].cells[1].text = "推公式"
    document.save(path)

    loaded = load_docx(path)
    paras = [(p.heading_path, p.text) for p in loaded.paragraphs]
    assert ("备考路线 > 第一阶段", "线性代数与概率论收尾。") in paras
    assert any("周次 | 任务" in text for _, text in paras)  # 表格按行 | 连接
    assert any("第 1 周 | 推公式" in text for _, text in paras)


def test_docx_table_keeps_document_order(tmp_path: Path):
    """表格必须留在正文原位，不能整体挪到文末（2026-09-11 修复）。

    旧实现分两趟遍历（先 document.paragraphs、再 document.tables）：
    表格全排到全文末尾，还继承文档最后一个标题——正文顺序与标题归属都错。
    """
    import docx

    path = tmp_path / "次序.docx"
    document = docx.Document()
    document.add_heading("第一章", level=1)
    document.add_paragraph("表格之前的正文。")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "列A"
    table.rows[0].cells[1].text = "列B"
    document.add_heading("第二章", level=1)
    document.add_paragraph("表格之后的正文。")
    document.save(path)

    loaded = load_docx(path)
    texts = [p.text for p in loaded.paragraphs]
    assert texts.index("表格之前的正文。") < texts.index("列A | 列B"), "表格跑到正文前面了"
    assert texts.index("列A | 列B") < texts.index("表格之后的正文。"), "表格被排到文末了"
    # 表格归它**前面**的标题，而不是文档最后一个标题
    table_para = next(p for p in loaded.paragraphs if p.text == "列A | 列B")
    assert table_para.heading_path == "第一章"


# ---------------------------------------------------------------------------
# PDF：多页 + 页眉页脚剔除（loader 层真实 bug 的回归）
# ---------------------------------------------------------------------------


def _make_pdf(path: Path, lines_per_page: list[list[str]], footer: list[str]) -> None:
    """每页：相同页眉 + 内容行 + 各自页脚（模拟真实论文笔记排版）。"""
    import pymupdf

    doc = pymupdf.open()
    for i, content in enumerate(lines_per_page, start=1):
        page = doc.new_page()
        page.insert_text(
            (72, 56), "Mikasa语料库 · 每页相同的页眉行", fontname="china-s", fontsize=8
        )
        y = 100
        for line in content:
            for chunk in [line[j : j + 40] for j in range(0, len(line), 40)]:
                page.insert_text((72, y), chunk, fontname="china-s", fontsize=10)
                y += 24
        page.insert_text((72, 780), footer[i - 1], fontname="china-s", fontsize=8)
    doc.save(path)
    doc.close()


def test_pdf_paragraphs_keep_page_and_drop_furniture(tmp_path: Path):
    path = tmp_path / "笔记.pdf"
    _make_pdf(
        path,
        lines_per_page=[
            ["缩放点积注意力要除以根号 dk 防止点积方差过大。", "这是第一页的完整内容。"],
            ["词法检索 BM25 与稠密向量检索互补。", "这是第二页的完整内容。"],
        ],
        footer=["第 1 页", "第 2 页"],
    )
    doc = load_pdf(path)
    texts = "".join(p.text for p in doc.paragraphs)
    assert doc.title == "笔记"
    assert "页眉" not in texts  # 跨页重复行整体剔除
    assert "第 1 页" not in texts and "第 2 页" not in texts  # 页码行剔除
    assert "缩放点积注意力" in texts and "词法检索 BM25" in texts  # 正文完整
    assert {p.page for p in doc.paragraphs} <= {1, 2}  # 页码元数据保留


def test_pdf_single_page_keeps_unique_lines(tmp_path: Path):
    """单页 PDF 无跨页重复：所有正文行都保留（重复行规则不误伤）。"""
    path = tmp_path / "单页.pdf"
    _make_pdf(path, lines_per_page=[["只有一页的独特正文内容行。"]], footer=["第 1 页"])
    doc = load_pdf(path)
    assert "只有一页的独特正文内容行。" in "".join(p.text for p in doc.paragraphs)


# ---------------------------------------------------------------------------
# loader 分发与异常包装
# ---------------------------------------------------------------------------


def test_load_document_dispatch_and_unknown_extension(tmp_path: Path):
    from mikasa.ingest.loader import load_document

    md = tmp_path / "x.md"
    md.write_text("# T\n\n正文", encoding="utf-8")
    assert load_document(md).file_type == "md"

    bad = tmp_path / "x.xyz"
    bad.write_bytes(b"whatever")
    with pytest.raises(IngestError):
        load_document(bad)

    corrupt = tmp_path / "坏.pdf"
    corrupt.write_bytes(b"not a real pdf at all")
    with pytest.raises(IngestError):
        load_document(corrupt)  # pymupdf 打不开 → 包装为 IngestError


def _make_scrambled_table_pdf(path: Path) -> None:
    """表格内容流乱序 PDF：单元格按**非版面序**写入内容流（真实论文
    PDF 的常见内部结构——流序与读序不符，旧 loader 按流序抽取即错位）。
    版面：表1 标题(y100)；行1 = 零件甲|3.0|0.50 (y124)；行2 = 零件乙|7.5|1.84
    (y148)。三列 = 每行 ≥2 个列隙才触发几何判据（真实 7 列表格的微型复刻；
    0.50/1.84 正是 2026-09-09 真实论文里旧 loader 抽丢的两个单元格值）。
    写入顺序故意乱（右→左→左→标题→右→左→中，跨行交错），内容流序
    读出必乱，几何重建必须还原版面行序/列序。"""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((330, 148), "1.84", fontname="china-s", fontsize=10)  # 行2 第3列
    page.insert_text((200, 148), "7.5", fontname="china-s", fontsize=10)  # 行2 中列
    page.insert_text((72, 148), "零件乙", fontname="china-s", fontsize=10)  # 行2 左列
    page.insert_text((72, 100), "表1 刚度参数", fontname="china-s", fontsize=10)  # 标题
    page.insert_text((330, 124), "0.50", fontname="china-s", fontsize=10)  # 行1 第3列
    page.insert_text((72, 124), "零件甲", fontname="china-s", fontsize=10)  # 行1 左列
    page.insert_text((200, 124), "3.0", fontname="china-s", fontsize=10)  # 行1 中列
    doc.save(path)
    doc.close()


def test_pdf_table_geometric_reflow(tmp_path: Path):
    """表格页几何重建：内容流乱序的表格按版面行序/列序还原。

    回归：2026-09-09 用户论文"表2 数值模型力学参数"用旧 loader 按内容
    流抽取丢单元格、值错列（第四系行丢 0.50/0.50、灰岩/矿体尾列丢 1.84），
    检索结果与 PDF 原文对不上。几何重建 = 词级按 y 聚类文本行、行内按
    x 排序，本 fixture 的乱序流是同一类问题的微型复刻。
    """
    path = tmp_path / "乱序表格.pdf"
    _make_scrambled_table_pdf(path)
    doc = load_pdf(path)
    flat = "".join(p.text for p in doc.paragraphs).replace(" ", "")
    # 版面行内左列先于右列；行按先上后下
    assert "零件甲3.0" in flat and "零件乙7.5" in flat
    assert flat.index("表1刚度参数") < flat.index("零件甲3.0") < flat.index("零件乙7.5")


def test_pdf_table_mark_survives_multipage(tmp_path: Path):
    """多页各带表格块：【表格】标记行跨页逐字重复，页眉剔除不得吞它。

    回归：2026-09-10 表格形态呈现收尾时发现 _drop_page_furniture 的
    “跨页逐字重复行整体剔除”把每页表格块首的【表格】标记行（必然逐字
    重复）当页眉页脚删除，且行级 join 吞掉段内空行——表格块与前后正文
    并成一大段、形态前缀丢失，问答端识别不到表格形态。修复 = 重复剔除
    豁免 TABLE_MARK + 保留原页空行。本测试用两页**内容不同**的表格块
    （表行自身不重复，只有标记行重复）精确复刻该场景。
    """
    import pymupdf

    doc = pymupdf.open()
    for title, rows in [
        ("表1 刚度参数", [["零件甲", "3.0", "0.50"], ["零件乙", "7.5", "1.84"]]),
        ("表2 更多参数", [["矿体", "4.6", "0.23"], ["灰岩", "9.0", "1.90"]]),
    ]:
        page = doc.new_page()
        page.insert_text((72, 100), title, fontname="china-s", fontsize=10)
        for row_i, cells in enumerate(rows):  # 3 列 = 每行 ≥2 个列隙，触发几何重建
            for x, cell in zip([72, 200, 330], cells, strict=True):
                page.insert_text((x, 124 + row_i * 24), cell, fontname="china-s", fontsize=10)
    doc.save(tmp_path / "两页表格.pdf")
    doc.close()

    loaded = load_pdf(tmp_path / "两页表格.pdf")
    marked = [p.text for p in loaded.paragraphs if p.text.startswith("【表格】")]
    assert len(marked) == 2, f"每页表格块都应独立成段带标记，实得 {len(marked)} 段"
    # 表格内容完整、且未与正文段并段（零件甲/矿体只在表格段内）
    assert "零件甲 3.0 0.50" in marked[0] and "零件乙 7.5 1.84" in marked[0]
    assert "矿体 4.6 0.23" in marked[1] and "灰岩 9.0 1.90" in marked[1]
    body = [p.text for p in loaded.paragraphs if not p.text.startswith("【表格】")]
    assert not any("零件甲" in t or "矿体" in t for t in body)


def _make_header_offset_pdf(path: Path) -> None:
    """表头基线偏移 PDF：列名词的基线参差（真实论文表2 的微型复刻）。

    版面（复刻 2026-09-09 真论文）：列1 岩石类型 / 列2 密度 / 列3 剪切模量 /
    列4 黏聚力/MPa，其中"岩石类型"与"黏聚力/MPa"的基线比其他列名**低
    7.5~7.8pt**（Word 导出 PDF 的常见现象）；单位层 /(g/cm3) 与 /GPa 再低
    7.4pt。旧固定 6pt y 桶会把偏移词甩出列名行 → 表头碎裂 → 列名与数据列
    对应丢失（用户实测：表2 的黏聚力/抗拉强度两列名错配）。写入顺序
    故意乱，几何重建必须还原 列名行→单位行→数据行 的完整结构。
    """
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((409.9, 533.3), "1.80", fontname="china-s", fontsize=10)  # 数据 列4
    page.insert_text((394.5, 501.9), "黏聚力/MPa", fontname="china-s", fontsize=10)  # 列名(基线低)
    page.insert_text((281.3, 494.4), "剪切模量", fontname="china-s", fontsize=10)  # 列名
    page.insert_text((291.4, 533.3), "0.50", fontname="china-s", fontsize=10)  # 数据 列3
    page.insert_text((170.9, 533.3), "2.20", fontname="china-s", fontsize=10)  # 数据 列2
    page.insert_text((103.9, 502.2), "岩石类型", fontname="china-s", fontsize=10)  # 列名(基线低)
    page.insert_text((108.4, 533.7), "第四系", fontname="china-s", fontsize=10)  # 数据 列1
    page.insert_text((290.3, 509.6), "/GPa", fontname="china-s", fontsize=10)  # 单位层
    page.insert_text((164.0, 509.3), "/(g/cm3)", fontname="china-s", fontsize=10)  # 单位层
    page.insert_text((169.8, 494.4), "密度", fontname="china-s", fontsize=10)  # 列名
    doc.save(path)
    doc.close()


def test_pdf_header_baseline_misalign(tmp_path: Path):
    """表头基线偏移的表格页：列名行完整按列序、单位词独立成行。

    回归：2026-09-09 用户复核对出真论文"表2"答案表格列名错配（黏聚力/
    抗拉强度互换）——旧 6pt y 桶把基线低 7.5pt 的列名词甩出列名行，
    "黏聚力/MPa"落单 → 模型只能猜列序且猜反。本 fixture 复刻该版面：
    偏移词（岩石类型/黏聚力/MPa）必须仍并入列名行且按 x 序排位。
    """
    path = tmp_path / "表头偏移.pdf"
    _make_header_offset_pdf(path)
    doc = load_pdf(path)
    flat = "".join(p.text for p in doc.paragraphs).replace(" ", "")
    # 列名行 4 词按 x 序完整连续（偏移词未落单）
    assert "岩石类型密度剪切模量黏聚力/MPa" in flat
    # 单位词剥离成独立行（不混入列名行 x 序）
    assert "/(g/cm3)/GPa" in flat
    # 数据行完整
    assert "第四系2.200.501.80" in flat


def test_pdf_references_tail_cut(tmp_path: Path):
    """参考文献区整段剔除：尾部"参考文献"标题行起的条目不再入库。"""
    import pymupdf

    def make(texts: list[str]) -> Path:
        path = tmp_path / "refs.pdf"
        doc = pymupdf.open()
        for block in texts:
            page = doc.new_page()
            y = 100
            for line in block.split("\n"):
                page.insert_text((72, y), line, fontname="china-s", fontsize=10)
                y += 24
        doc.save(path)
        doc.close()
        return path

    # 情况一：正文在前、参考文献标题在尾部 → 标题与其后条目全剔除
    path = make(
        [
            "这是一段很长的正文，讲述侧限压缩条件下充填体与围岩的协同承载规律，"
            "正文内容足够长以越过全文 45% 位置。其中提到 充填体侧限压缩的试验过程。",
            "参考文献\n张三, 李四. 充填体力学特性研究[J]. 岩石力学与工程学报, 2020.",
        ]
    )
    flat = "".join(p.text for p in load_pdf(path).paragraphs)
    assert "正文" in flat and "参考文献" not in flat and "张三" not in flat

    # 情况二：标题在全文前 45% 内（正文误引用风险）→ 守卫生效不切
    path = make(
        [
            "参考文献\n这一段出现在全文前部（未越过 45%），正文内容紧随其后应当保留。",
            "这是第二页正文内容，讲述压缩试验中应变与应力的关系曲线形态。",
        ]
    )
    flat = "".join(p.text for p in load_pdf(path).paragraphs)
    assert "这是第二页正文内容" in flat  # 未被误切


def test_pdf_page_numbers_survive_blank_pages(tmp_path: Path):
    """空白页不能让后续页码前移（2026-09-11 修复的真实 bug）。

    初版 `_drop_page_furniture` 结尾写成 `if kept: cleaned.append(...)`，把
    "剔除页眉页脚后为空"的页从列表里丢掉；而 `load_pdf` 用
    `enumerate(cleaned, start=1)` 编号 —— 每丢一页，后面所有页码前移一位。
    doc 71 实测：PDF 第 3/5/7 页是空白页，到第 67 页累积偏移 +4~+5 页，
    文本视图的「第 N 页」标记与文本跳页全跟着错。
    """
    import pymupdf

    path = tmp_path / "空白页.pdf"
    doc = pymupdf.open()
    # 第 2 页故意留空（只有重复页眉 + 页码行，剔除后为空）
    for i, lines in enumerate(
        [["第一页的正文。"], [], ["第三页的正文。"], ["第四页的正文。"]], start=1
    ):
        page = doc.new_page()
        page.insert_text(
            (72, 56), "Mikasa语料库 · 每页相同的页眉行", fontname="china-s", fontsize=8
        )
        y = 100
        for line in lines:
            page.insert_text((72, y), line, fontname="china-s", fontsize=10)
            y += 24
        page.insert_text((72, 780), f"第 {i} 页", fontname="china-s", fontsize=8)
    doc.save(path)
    doc.close()

    loaded = load_pdf(path)
    by_text = {p.text.strip(): p.page for p in loaded.paragraphs}
    assert by_text["第一页的正文。"] == 1
    assert by_text["第三页的正文。"] == 3  # 关键：中间那页空着也必须占住位次
    assert by_text["第四页的正文。"] == 4
    assert set(by_text) == {"第一页的正文。", "第三页的正文。", "第四页的正文。"}  # 页眉页脚仍被剔


def test_cut_reference_tail_keeps_blank_page_slots(tmp_path: Path):
    """截断参考文献区时**不得重排页序**（2026-09-11 修复的第二处同类 bug）。

    `_drop_page_furniture` 修好后偏移依旧，就是因为这份函数在命中参考文献时
    又 `[t for t in rest if t.strip()]` 过滤了一遍空页——doc 71 实测：301 页
    被截成 189，且开头 3 个空白页被抹掉，导致前部页码整整前移 3 位。
    """
    from mikasa.ingest.loaders import _cut_reference_tail

    # 8 页：第 2/4 页空白；第 7 页只有"References"标题行；前 6 页字符量 >45%
    pages = [
        "正文一。" * 40,
        "",
        "正文二。" * 40,
        "",
        "正文三。" * 40,
        "正文四。" * 40,
        "References",
        "[1] Someone. A paper. 2020.",
    ]
    out = _cut_reference_tail(pages)
    assert len(out) == 7, "参考文献页应被截掉，但前面的空页必须占位"
    assert out[1] == "" and out[3] == "", "空页必须原样保留（页序即以此为准）"
    # 标题行是该页第一行 → "截到标题行前"即整页清空（页位仍在，不能删元素）
    assert out[6] == "", "标题行所在页应被清空但保留页位"
    assert "[1] Someone" not in "".join(out), "参考文献条目及其后各页被丢弃"


def test_cross_page_table_header_survives_furniture_dedup():
    """跨页表格的**表头行**不能被当成页眉页脚删掉。

    表头在断表续排时会跨页逐字重复，而它只出现在表格跨越的那两三页；页眉页脚
    则几乎每页都有。判据因此不能只是"重复 >= 2 次"（那会把表头从所有页删光，
    列名彻底丢失、切块后只剩无表头的数据行），还要看**占比**。
    """
    from mikasa.ingest.loaders import _drop_page_furniture

    pages = [
        "论文页眉\nTable header A B C\nTable header A B C\ndata1",
        "论文页眉\nTable header A B C\ndata2",
        "论文页眉\ndata3",
        "论文页眉\ndata4",
    ]
    out = _drop_page_furniture(pages)

    assert len(out) == len(pages), "一页一元素的不变量不能破"
    assert all("Table header A B C" in out[i] for i in (0, 1)), "表头必须保留"
    assert all("论文页眉" not in page for page in out), "页眉仍要被剔除"


def test_same_page_repeat_is_not_furniture():
    """同一页内重复出现不算"跨页重复"——按页去重后才累加。"""
    from mikasa.ingest.loaders import _drop_page_furniture

    out = _drop_page_furniture(["重复行\n重复行\n正文"])
    assert out[0].count("重复行") == 2


def test_large_text_skips_repeated_line_cleanup(tmp_path, monkeypatch):
    """超大文本文件跳过"重复行清理"——那是内存峰值 4× 的主要来源。

    该步骤按行建计数器，内存与文件大小同阶（实测 10.9MB → 峰值 43.7MB）。
    upload_max_mb 允许到 500MB，换算下来峰值近 2GB，桌面版会被系统杀掉。
    宁可不做噪声清理（检索里多几行噪声），也不能让整篇文档进不来。
    """
    import mikasa.ingest.loaders as loaders

    big = tmp_path / "big.md"
    big.write_text("每页重复的页眉\n正文一行\n" * 200, encoding="utf-8")

    monkeypatch.setattr(loaders, "_DEDUP_MAX_BYTES", 10)  # 调低阈值以走大文件分支
    out = loaders._read_text(big)

    assert "每页重复的页眉" in out, "跳过清理时重复行应原样保留"


def test_small_text_still_cleans_repeated_lines(tmp_path):
    """小文件仍要做清理（别把降级路径写成默认路径）。"""
    from mikasa.ingest.loaders import _read_text

    small = tmp_path / "small.md"
    small.write_text("页眉\n正文\n页眉\n正文\n页眉\n", encoding="utf-8")

    assert _read_text(small) == "页眉\n正文\n正文"
