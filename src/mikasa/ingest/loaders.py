"""格式解析器：md / txt / pdf / docx → LoadedDocument。

按扩展名分发（loader.py），每种格式单独模块。产出统一段落流，
分块细节全部交给 chunker（本层不感知块大小）。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from mikasa.errors import IngestError
from mikasa.ingest.types import LoadedDocument, Para
from mikasa.utils.text import decode_text, normalize_text, strip_repeated_lines

# 头部规则：H1-H3（ATX 与 Setext 两种写法）
_ATX_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")
_SEPARATOR_HEADING = re.compile(r"^(=+|-+)\s*$")

_BLOCKQUOTE_STRIP = re.compile(r"^>\s?", re.MULTILINE)
_FENCED_CODE = re.compile(r"^```.*$|^~~~.*$", re.MULTILINE)

# 独立成行的页码噪声：第 1 页 / 第 1 页 共 3 页 / 1 / - 3 - / Page 2 of 5 / 12 / 34 等
_CN_NUM = r"[0-9一二三四五六七八九十百千]+"
_PAGE_NUMBER_ONLY = re.compile(
    rf"^(?:"
    rf"第\s*{_CN_NUM}\s*页(?:\s*/\s*共\s*{_CN_NUM}\s*页)?"
    rf"|共\s*{_CN_NUM}\s*页"
    rf"|[Pp]age\s*\d+\s*(?:of\s*\d+)?"
    rf"|[-–—]?\s*\d+\s*[-–—]?"
    rf"|页\s*\d+\s*/\s*\d+"
    rf"|\d+\s*/\s*\d+\s*页?"
    rf")$"
)


def _extract_page_text(page) -> str:
    """单页文本抽取。普通正文页走 PyMuPDF 内容流（快且稳、词距保真）；
    检测到"同行多列"（表格/分栏排版）的页改用几何重建（``_reflow_by_geometry``）。

    为什么需要几何重建（2026-09-09 真实案例）：PDF 内容流顺序 ≠ 版面
    读序。复杂表格（含合并单元格）中，单元格文字按流序输出会**丢值与
    列错位**——用户上传论文"表2 数值模型力学参数"（7 列 × 7 行），
    内容流抽取丢了两处单元格、且把"充填体"合并行的值错挂在后续行，
    使检索结果与论文原文对不上。几何重建 = 词级坐标按 y 聚成文本行、
    行内按 x 排序，还原人读的版面行序。
    """
    text = page.get_text("text")
    words = page.get_text("words")  # (x0, y0, x1, y1, word, block, line, word_no)
    if words and _has_table_rows(words):
        return _reflow_by_geometry(words)
    return text


# 单位行词模式：无汉字、且含 / % ° φ × ³ 等符号（/(g/cm3)、/GPa、φ/(°)、/MPa）。
# 表格表头的"单位层"（如 密度 下的 /(g/cm3)）若与列名层基线差 > 旧 6pt 桶
# 会被拆碎行、或混入列名行打乱列序——识别后剥离成独立行（见 _reflow_by_geometry）。
_UNIT_WORD = re.compile(r"^[^一-鿿]*[/%°φ×³²·][^一-鿿]*$")

# 行聚类容差：同一视觉行内词基线可差 7.5pt（Word 导出 PDF 时单元格内
# 个别词基线偏移，2026-09-09 真实案例"黏聚力/MPa"比同列名低 7.5pt 被旧
# 6pt 桶甩出行外 → 表头碎裂、列名列序丢失）。正文行距 ≥ 12.6pt（10.5pt
# 字单倍行距），取 12pt：7.5pt 归一行、正文多行照常分开。
_ROW_Y_TOL = 12.0

# 表格段落前缀标记：连续表格行（≥2 行）的几何重建结果以它开头独立成段，
# 问答时模型按标记决定呈现形态（与 prompts.py SYSTEM_PROMPT 的表格式
# 输出规则配套，两处字面量须一致——2026-09-10 用户诉求：原文是表格就
# 自动出表格，不必每次说"用表格呈现"）。
TABLE_MARK = "【表格】"


def _cluster_text_rows(words: list[tuple]) -> list[list[tuple]]:
    """词级文本行聚类：按 y0 升序贪心，邻差 ≤ _ROW_Y_TOL 归同一行。

    不用固定 y/6 桶的原因：桶宽固定会切碎基线差 6~12pt 的同行词（真实案例
    见表 2 表头）。邻差贪心没有桶界抖动——同行词基线差（< 8pt）与行距
    （≥ 12.6pt）之间取 12pt 一刀切，表头基线偏移并入同行、正文多行照常分开。
    """
    ordered = sorted(words, key=lambda w: w[1])
    rows: list[list[tuple]] = []
    for w in ordered:
        if rows and w[1] - rows[-1][-1][1] <= _ROW_Y_TOL:
            rows[-1].append(w)
        else:
            rows.append([w])
    return rows


def _row_gap_count(line: list[tuple]) -> int:
    """一行内 > 8pt 的横向空隙数（调用方负责按 x0 排序）。"""
    pairs = zip(line, line[1:], strict=False)  # 相邻词对（自身切片短 1，属预期）
    return sum(1 for a, b in pairs if b[0] - a[2] > 8)


def _has_table_rows(words: list[tuple]) -> bool:
    """版面判定：任一文本行（_cluster_text_rows 聚类）含 ≥2 个横向空隙
    > 8pt 的词对，即认为该页有表格/多栏结构（正文行内词距 < 8pt；
    表格列隙常达数十 pt）。"""
    for line in _cluster_text_rows(words):
        line.sort(key=lambda w: w[0])
        if _row_gap_count(line) >= 2:
            return True
    return False


def _reflow_by_geometry(words: list[tuple]) -> str:
    """几何重建：词级按 y 聚类成文本行 → 行内按 x0 排序 → 行文本按 y 序拼接。

    表头拆行剥离：同一聚类行若混入"单位层"纯符号词（/(g/cm3)、/GPa 等，
    见 _UNIT_WORD），先按 x 序输出列名/数据词，单位词紧随其后独立成行——
    否则单位词 x0 略偏会把列序带乱。

    表格块独立成段：**连续 ≥2 行的表格行**（列隙 ≥2 的聚类行）合并成块，
    块首加 TABLE_MARK、块前后留空行——下游 load_pdf 按空行分段时表格
    自成段落，问答可识别其形态按表呈现（2026-09-10 用户诉求）。孤立
    表格行（如正文里带大空隙的公式行）不标表格、按正文行处理。
    词间一律补一个空格（列边界信息在纯文本里本就不可表达）。
    """
    rows = _cluster_text_rows(words)
    # (文本行, 是否表格行)：单位词剥离后同行两行输出继承同一表格判定
    lines: list[tuple[str, bool]] = []
    for row in rows:
        row.sort(key=lambda w: w[0])
        tab = _row_gap_count(row) >= 2
        units = [w for w in row if _UNIT_WORD.fullmatch(w[4])]
        if 0 < len(units) < len(row):
            lines.append((" ".join(w[4] for w in row if w not in units), tab))
            lines.append((" ".join(w[4] for w in units), tab))
        else:
            lines.append((" ".join(w[4] for w in row), tab))
    out: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i][1]:
            j = i
            while j < len(lines) and lines[j][1]:
                j += 1
            if j - i >= 2:
                if out and out[-1]:
                    out.append("")  # 块前空行（与正文段隔开）
                out.append(TABLE_MARK)
                out.extend(t for t, _ in lines[i:j])
                out.append("")  # 块尾空行
            else:
                out.extend(t for t, _ in lines[i:j])  # 孤表格行按正文行处理
            i = j
        else:
            out.append(lines[i][0])
            i += 1
    return "\n".join(out)


def _cut_reference_tail(page_texts: list[str]) -> list[str]:
    """剔除文档尾部的参考文献区（如"参考文献"标题行起直至末尾）。

    守卫：只认**单独成行**的标题（参 考 文 献 / References 等），且标题
    所在页**之前**的累计字符须达全文 45% 才生效——正文前部若出现
    "参考文献见附录"这类句子不会被误伤；正文引述式文字也不单独成行。
    按"页前累计"而非"含本页"判定：整页超长时标题明明在文档 0% 处，
    含本页的算法会把守卫冲掉误切（2026-09-09 回归测试情况二暴露）。
    参考文献条目（尤其英文）被逐行切碎成大量低价值块，检索命中它们
    会让"名词卡片"显示一堆引文而非正文知识（2026-09-09 真实案例）。
    """
    ref_head = re.compile(r"^\s*(参\s*考\s*文\s*献|References?|REFERENCES?)\s*$")
    total = sum(len(t) for t in page_texts)
    seen = 0
    for i, text in enumerate(page_texts):
        if seen >= total * 0.45:  # 标题须出现在全文后段（以本页之前的累计字符计）
            kept: list[str] = []
            for line in text.splitlines():
                if ref_head.match(line.strip()):
                    # 命中：此前各页原样保留，当前页截到标题行前。
                    # **不做 `if t.strip()` 过滤**（2026-09-11 修复）：过滤会丢掉
                    # 空页、让"一页一元素"的位次错乱——与 `_drop_page_furniture`
                    # 是同一类 bug 的第二处。此前只修了前者，doc 71 的偏移依旧，
                    # 因为参考文献守卫一旦触发这里就会再删一次（实测 301 页被截成
                    # 189，且开头 3 个空白页被抹掉 → 前部页码整整前移 3 位）。
                    return [*page_texts[:i], "\n".join(kept)]
                kept.append(line)
        seen += len(text)
    return page_texts


def load_markdown(path: Path) -> LoadedDocument:
    """Markdown：按 H1-H3 标题生成 heading_path，段落 = 空行分隔。"""
    text = _read_text(path, strip_repeats=False)  # 结构化格式不做重复行清理（见 _read_text）
    lines = text.splitlines()

    paragraphs: list[Para] = []
    heading_stack: list[str] = []
    buffer: list[str] = []
    title: str | None = None

    def flush_buffer() -> None:
        if buffer:
            clean = _clean_line_buffer(buffer)
            if clean:
                paragraphs.append(Para(text=clean, heading_path=_path_of(heading_stack)))
            buffer.clear()

    def add_line(line: str) -> None:
        """正文行入缓冲；空行 = 段落边界（与 TXT/DOCX 语义一致）。"""
        if line.strip():
            buffer.append(line)
        else:
            flush_buffer()

    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        if _FENCED_CODE.match(raw.strip()):
            # 围栏代码块：整块按**纯文本段落**收进索引（2026-09-20 改）。
            # 旧行为是整块跳过，理由写的是"对问答帮助不大"——代价却是两件实事：
            # 整篇只有代码的笔记一个可检索段落都产不出（保存被 400 拒），混合笔记
            # 里搜代码搜不到（参见 limitations 的"笔记：编辑器里看到的，不总是
            # 检索用的那份"）。跳过当初真正的动机是"代码里的 `#` 会被读成标题"，
            # 而那是**解析**问题：这里整块当一个段落，根本不做标题/围栏解析，
            # 于是既能搜、又不会把代码读成结构。
            flush_buffer()
            code: list[str] = []
            i += 1
            while i < len(lines) and not _FENCED_CODE.match(lines[i].strip()):
                code.append(lines[i])
                i += 1
            i += 1  # 跳过收尾围栏；缺收尾围栏时 i 已越界，循环自然结束
            body = "\n".join(line.rstrip() for line in code).strip("\n")
            if body.strip():
                paragraphs.append(Para(text=body, heading_path=_path_of(heading_stack)))
            continue
        atx = _ATX_HEADING.match(line)
        setext_next = False
        # 注意：**不能**要求"下划线之后还有行"（旧代码的 `i + 1 < len(lines)`）——
        # 文件以 Setext 标题结尾是常见写法（笔记末尾无空行），那样会识别不出标题，
        # 下划线本身还会被当正文混进段落（2026-09-11 修复）
        if atx is None and _SEPARATOR_HEADING.match(line):
            prev = lines[i - 1].strip() if i > 0 else ""
            if prev and not _ATX_HEADING.match(prev):
                # Setext：上行为标题文本
                setext_next = True
                level = 1 if line.startswith("=") else 2
                atx_text = prev
                atx = _ATX_HEADING.match("#" * level + " " + atx_text)
                # 标题行已经在 buffer 里（作为正文），需要回退一行
                if buffer and buffer[-1].strip() == prev:
                    buffer.pop()
                else:
                    flush_buffer()
        if atx:
            flush_buffer()
            level, heading_text = len(atx.group(1)), atx.group(2).strip()
            if title is None:
                title = heading_text
            _push_heading(heading_stack, level, heading_text)
            i += 1 if not setext_next else 2
            continue
        add_line(raw)
        i += 1
    flush_buffer()

    return LoadedDocument(title=title or path.stem, file_type="md", paragraphs=paragraphs)


def load_txt(path: Path) -> LoadedDocument:
    """纯文本：空行分隔的段落；无标题结构（编码自动探测 GBK/UTF-8）。"""
    text = _read_text(path)
    paragraphs = [Para(text=clean) for clean in _para_texts(text)]
    return LoadedDocument(title=path.stem, file_type="txt", paragraphs=paragraphs)


def load_pdf(path: Path) -> LoadedDocument:
    """PDF：逐页提取文本（PyMuPDF），剔除页眉页脚与参考文献区，记录页码。

    页眉页脚处理（见 ``_drop_page_furniture``）：页码行按模式剔除，
    跨页逐字重复的行按”正文不会跨页逐字重复”的事实整体剔除。

    文本抽取（``_extract_page_text``）：普通正文页用 PyMuPDF 内容流；
    检测到”同行多列”（表格/分栏）的页改为几何重建——内容流顺序在
    复杂表格（合并单元格）下与版面读序不符，会丢单元格、列错位
    （2026-09-09 真实案例：论文”表2 数值模型力学参数”抽成错位文本，
    数值全部对错列）。参考文献区（``_cut_reference_tail``）整段剔除：
    引用条目碎片对问答无价值，还会污染检索命中（同一真实案例）。
    """
    try:
        import pymupdf  # 延迟导入（官方新命名，勿用旧名 fitz）
    except ImportError as exc:
        raise IngestError("PDF 解析需要 PyMuPDF：pip install -e “.” 已含此依赖") from exc

    paragraphs: list[Para] = []
    with pymupdf.open(path) as doc:
        page_texts = [_extract_page_text(page) for page in doc]
    cleaned = _cut_reference_tail(_drop_page_furniture(page_texts))
    for page_no, page_text in enumerate(cleaned, start=1):
        if not page_text.strip():
            continue
        paragraphs.extend(Para(text=clean, page=page_no) for clean in _para_texts(page_text))

    if not paragraphs:
        # 常见场景：扫描版 PDF 无文本层（真实失败案例，见 limitations 文档）
        raise IngestError(
            "未能从 PDF 中提取到文本。若为扫描版/图片版 PDF（无文本层），"
            "请先转成文字版 PDF 或导出为 Markdown/TXT。"
        )
    return LoadedDocument(title=path.stem, file_type="pdf", paragraphs=paragraphs)


def load_docx(path: Path) -> LoadedDocument:
    """DOCX：段落 + 表格（表格单元格以 | 连接成段）。"""
    try:
        import docx  # python-docx（延迟导入）
    except ImportError as exc:
        raise IngestError("DOCX 解析需要 python-docx（核心依赖自带）") from exc

    paragraphs: list[Para] = []
    heading_stack: list[str] = []

    def _flush(body: list[str], level: int | None = None, heading_text: str | None = None) -> None:
        clean = _clean_line_buffer(body)
        if clean:
            if level is not None and heading_text:
                _push_heading(heading_stack, level, heading_text)
            paragraphs.append(
                Para(
                    text=clean,
                    heading_path=_path_of(heading_stack) if heading_stack else None,
                )
            )
        body.clear()

    from docx.table import Table

    body: list[str] = []
    document = docx.Document(str(path))  # python-docx 非上下文管理器，且不接受 Path 对象
    # 必须用 iter_inner_content **按文档顺序**遍历段落与表格：
    # document.paragraphs 只含 body 级段落、document.tables 是另一趟——
    # 分开遍历会把所有表格挪到全文末尾，还继承文档最后一个标题，正文顺序错乱
    # （2026-09-11 修复）
    for item in document.iter_inner_content():
        if isinstance(item, Table):
            _flush(body)  # 表格前的正文先落盘，保持顺序
            for row in item.rows:
                cells = [cell.text.strip() for cell in row.cells]
                text = " | ".join(c for c in cells if c)
                if text:
                    paragraphs.append(Para(text=text, heading_path=_path_of(heading_stack) or None))
            continue
        style = (item.style.name or "").lower() if item.style else ""
        text = item.text.strip()
        if not text:
            _flush(body)
            continue
        if style.startswith("heading") and style[-1:].isdigit():
            _flush(body)
            level = int(style[-1:])
            _push_heading(heading_stack, min(level, 3), text)
        else:
            body.append(item.text)
    _flush(body)

    return LoadedDocument(title=path.stem, file_type="docx", paragraphs=paragraphs)


# ---------------------------------------------------------------------------
# 共用工具
# ---------------------------------------------------------------------------


def _drop_page_furniture(page_texts: list[str]) -> list[str]:
    """剔除 PDF 页眉页脚噪声（行级过滤，保留段内空行与表格块标记）。

    规则（启发式，限制见 docs/limitations-and-failures.md）：
    1. 页码行按模式剔除（正文很少只有“第 1 页”/“Page 2”这类内容）；
    2. 跨页**逐字重复**的行整体剔除——页眉页脚在每一页都相同，而正文
       段落几乎不可能在多个页面逐字重复，这是比“保留首次”更干净的做法；
    3. TABLE_MARK 行豁免剔除（2026-09-10 修复）：多页各带表格块时
       【表格】标记行必然逐字重复，它是结构化标记而非页眉页脚——连同
       原页空行一起保留（load_pdf 按空行分段，吞空行会把表格块与前后
       正文并成一大段，表格形态前缀随之失效）。

    **不变量：返回列表与入参等长**（每个元素对应一页，可能是空串）。
    调用方 `load_pdf` 用 `enumerate(cleaned, start=1)` 编页码，少一个元素
    后面所有页号就前移一位。2026-09-11 修复的真实 bug：初版写成
    `if kept: cleaned.append(...)`，把"剔除后为空"的页从列表里丢掉——
    空白页（doc 71 的第 3/5/7 页就是）与"整页只有页眉页脚"的页都会触发，
    实测累积偏移达 **+4~+5 页**，文本视图的「第 N 页」标记与跳页全跟着错。
    空串在下游 `if not page_text.strip(): continue` 处照常跳过，保留占位无副作用。
    """
    if not page_texts:
        return page_texts
    from collections import Counter

    # **计数必须先按页去重、再跨页累加**：同一页内重复出现不算"页眉页脚"。
    # 直接对整篇逐行累加的话，一页里出现两次的行（跨页断表的**表头重复行**是
    # 典型）会被判定为重复，然后从**所有**页里删掉——实测 4 页 PDF 里第 1 页
    # 出现两次的表头会把第 1、2 页的表头全删光，表格列名彻底丢失，切块后只剩
    # 无表头的数据行（2026-09-11 审查实测）。docstring 声明的规则本就是
    # "跨页逐字重复"，这里让实现与之一致。
    total: Counter[str] = Counter()
    for text in page_texts:
        total.update({line.strip() for line in text.splitlines() if line.strip()})
    # 判据 = "出现在 ≥2 页" **且** "出现在 ≥60% 的页"。
    # 只用"≥2 次"太松：跨页表格的**表头行**也会在多页逐字重复（断表续排），
    # 于是被当页眉从所有页删掉，表格列名彻底丢失（2026-09-11 审查实测）。
    # 页眉页脚的特征是"几乎每页都有"，而表头只出现在它跨越的那两三页——占比
    # 能把两者分开。宁可放过一点噪声，也不能删正文（漏删只是检索时多几行噪声，
    # 误删是不可恢复的信息丢失）。
    page_count = len([t for t in page_texts if t is not None])
    page_quorum = max(2, -(-page_count * 3 // 5))  # ceil(60%)
    repeated = {
        line for line, count in total.items() if count >= page_quorum and line != TABLE_MARK
    }

    cleaned: list[str] = []
    for text in page_texts:
        kept = [
            line
            for line in text.splitlines()
            if not line.strip()
            or (line.strip() not in repeated and not _PAGE_NUMBER_ONLY.match(line.strip()))
        ]
        cleaned.append("\n".join(kept))  # 始终追加：保持"一页一元素"，页号才对得上
    return cleaned


# 超过这个大小就跳过"重复行清理"。
#
# 那一步要按行建计数器，内存与文件大小同阶：实测 10.9 MB 的 md 峰值 43.7 MB
# （约 4×）——字节缓冲 + 解码后的字符 + Counter 的每行一条 + kept 列表 + 最终
# join 同时驻留。Web 端的 upload_max_mb 允许到 500 MB，换算下来峰值接近 2 GB，
# 桌面版（单进程 pywebview）会被系统直接杀掉。**宁可不做噪声清理，也不能崩**：
# 页眉页脚残留只是检索里多几行噪声，OOM 是整篇文档进不来。
_DEDUP_MAX_BYTES = 64 * 1024 * 1024


def _read_text(path: Path, *, strip_repeats: bool = True) -> str:
    """读文本 + 保守清洗。

    strip_repeats=False 用于**结构化**格式（Markdown）：那里的"重复行"往往是
    合法内容——一篇含三个同构表格的笔记，`| --- | --- |` 分隔行会在索引里被删掉
    第 2、3 个（文件不动，但检索用的那份缺了几行）。这条启发式原本是为 PDF 转
    文本的页眉页脚写的（见 strip_repeated_lines 的 docstring），不该套在结构化
    格式上。纯文本（.txt）保留它：用户手里那份 txt 很可能是从 PDF 转来的。
    """
    size = path.stat().st_size
    raw = path.read_bytes()
    text = decode_text(raw)
    del raw  # 尽早释放字节缓冲：解码后的字符还要驻留，两者同时在场是峰值的一半
    if size > _DEDUP_MAX_BYTES:
        from mikasa.utils.logging import get_logger

        get_logger("ingest.loaders").warning(
            "文件较大（%.0f MB），跳过重复行清理以控制内存占用——页眉页脚可能残留",
            size / 1e6,
        )
        return text
    if not strip_repeats:
        return text
    # 页眉/页脚等噪声做保守清理（PDF 已做逐页处理，此处兜底）
    return strip_repeated_lines(text)


def _push_heading(stack: list[str], level: int, text: str) -> None:
    """维护标题路径栈：如 [1. 绪论] + [1.2 相关工作] -> "1. 绪论 > 1.2 相关工作"。"""
    while len(stack) >= level:
        stack.pop()
    stack.append(text)


def _path_of(stack: list[str]) -> str:
    return " > ".join(stack)


def _clean_line_buffer(lines: list[str]) -> str:
    """合并缓冲行 + 轻度清洗（原文保真：空白折叠/控制字符，标点不改）。"""
    joined = "\n".join(line.rstrip() for line in lines if line.strip())
    return normalize_text(joined)


def _para_texts(text: str) -> Iterator[str]:
    """按空行切段并清洗——三种格式的公共口径（非空行入缓冲，空行处出段）。

    txt 与 pdf 原先各写了一遍同一套"攒行—出段"（pdf 那份还逐页复制了一次
    页尾 flush），2026-09-24 合并到这里。调用方各自负责附元数据（page）与
    "这段要不要收"；markdown 不适用（它的缓冲还要在标题处断开并换 heading_path，
    见 load_markdown 的 flush_buffer）。
    """
    buffer: list[str] = []
    for raw in text.splitlines():
        if raw.strip():
            buffer.append(raw)
            continue
        if buffer:
            clean = _clean_line_buffer(buffer)
            if clean:
                yield clean
            buffer.clear()
    if buffer:
        clean = _clean_line_buffer(buffer)
        if clean:
            yield clean
