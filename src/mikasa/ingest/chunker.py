"""中文感知的结构化分块器（本项目检索质量的第一道关口）。

设计（详见 docs/architecture.md"分块策略"与 ADR-0012）：
- 结构优先：标题变化 = 天然块边界；块内按
  "段落 → 句末标点 → 逗号 → 硬切"的优先级切分长文本，避免中断词义；
- 有重叠（overlap）：新块从上一块同标题内容的尾部窗口续起，
  缓解"答案被拦腰截断"造成的召回漏失；
- 标题前置（title_prefix）在入库阶段完成（见 ingest/service.py）：
  块首拼入标题路径提升 BM25/向量召回，可做消融实验。

输出 ChunkSpec 只含正文；标题/页码作为溯源元数据单独携带。

长度不变量（块 ≤ size）的实现要点：
- 段落以 "\n\n" 连接，窗口预算必须计入分隔符（2 字符/次）；
- 重叠尾巴按剩余容量截短——"重叠尾 + 整段" 超过 size 时少截而非超发
  （真实越界 bug 见 tests/unit/ingest/test_chunker.py::test_overlap_respects_size）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mikasa.ingest.loaders import TABLE_MARK  # loader 表格块前缀（两处共享同一字面量）
from mikasa.ingest.types import Para

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])")
_COMMA_SPLIT_RE = re.compile(r"(?<=[，,])")


@dataclass(frozen=True)
class ChunkSpec:
    """分块器输出（未分配 id、未分词、未加标题前缀）。"""

    content: str
    heading_path: str | None = None
    page: int | None = None


def chunk_paragraphs(
    paragraphs: list[Para],
    *,
    size: int,
    overlap: int,
) -> list[ChunkSpec]:
    """把段落流切为块序列（保持文档顺序，逐段扫描单遍完成）。

    不变量：
      - 每个块 ≤ size（含段落间分隔符与续接的重叠尾巴）；
      - 标题变化处必切块；
      - 同一标题下的相邻块共享尾部 ≤overlap 字符（跨标题不共享；
        下一段接近 size 时按剩余容量截短重叠）。
      - page = 块内正文起始段所在页（重叠尾巴的页可能不同，属预期）。
    """
    if size <= 0 or overlap < 0 or overlap >= size:
        raise ValueError(f"非法的分块参数：size={size}, overlap={overlap}")

    pieces: list[str] = []
    current_head: str | None = None
    tail: str | None = None  # 上一块同标题尾部窗口（等待续接）
    window_page: int | None = None  # 窗口内正文起始段页码
    window_dirty = False  # 窗口是否已有正文段（重叠尾巴不算）
    last_emitted_head: str | None = None
    out: list[ChunkSpec] = []
    _SEP = "\n\n"

    def window_len() -> int:
        # 段落以 _SEP 连接，n 段多出 (n-1) 个分隔符，预算必须计入
        return sum(len(p) for p in pieces) + len(_SEP) * max(0, len(pieces) - 1)

    def emit() -> None:
        """把当前窗口固化为一个块，并记录可续接的重叠尾巴。"""
        nonlocal pieces, tail, window_page, window_dirty, last_emitted_head
        content = _join_pieces(pieces)
        if not content:
            pieces = []
            return
        tail = _overlap_tail(pieces, overlap, content)  # 先取尾再清窗（tail 依赖 pieces）
        pieces = []
        out.append(ChunkSpec(content=content, heading_path=current_head, page=window_page))
        last_emitted_head = current_head
        window_page = None
        window_dirty = False

    def seed_overlap_for(text_len: int) -> None:
        """同标题续接：窗口先铺上一块尾巴，按剩余容量截短。

        截短是必须的——“重叠尾 + 分隔符 + 整段”可能超过 size，
        此时宁可少重叠也不破坏“块 ≤ size”不变量。
        """
        nonlocal tail
        if tail is None or current_head != last_emitted_head:
            return
        take = min(len(tail), size - text_len - len(_SEP))
        if take > 0:
            pieces.append(tail[:take])
        tail = None

    def feed(part: str) -> None:
        """把一段（≤size，由调用方保证）放入窗口；放不下先固化再续重叠。"""
        while window_len() + len(part) > size:
            emit()
            seed_overlap_for(len(part))

    for para in paragraphs:
        if not para.text.strip():
            continue

        # —— 标题变化：结构边界 ——
        if para.heading_path != current_head:
            if pieces:
                emit()
            current_head = para.heading_path
            tail = None  # 跨标题不续重叠

        text = para.text.strip()
        if len(text) <= size:
            # 段能整体容纳：feed 内部可能 emit（会清空窗口状态），
            # 因此页码在 feed 之后、真正入窗时赋值
            feed(text)
            if not window_dirty and para.page is not None:
                window_page = para.page
            pieces.append(text)
            window_dirty = True
            continue
        # 单段超长：按句子/逗号边界切分逐片放入，每片入窗时归属本段页码
        for part in _split_text(text, size):
            feed(part)
            if not window_dirty and para.page is not None:
                window_page = para.page
            pieces.append(part)
            window_dirty = True

    emit()
    return out


def _join_pieces(pieces: list[str]) -> str:
    text = "\n\n".join(p.strip() for p in pieces)
    return text.strip()


def _overlap_tail(pieces: list[str], overlap: int, content: str) -> str | None:
    """同标题续接的重叠尾巴：起点落在表格段（【表格】块）内时整体跳过。

    为什么（2026-09-10 真实验证事故）：overlap 若取自表格块内部，会把
    表尾的“数据行”复制成下一块的开头——制造出**无表头的表尾碎片块**
    （如“更新 迭代更新 — — —\\n胶结充填体 1.83 0.38…”）。问“某行的
    参数名称”时检索恰命中这种碎片（数据行与问题词重叠最高，而含列名
    的完整表块因掺杂大量无关行被稀释、排名落后），模型看不到列名只能
    答“未明确标注”。宁少重叠也不跨表段：尾巴从表格段结束处起算。
    表段后的正文仍照常产生重叠（表 > size 的罕见超长表按行切块后，
    块间重叠可能落在行中，属可接受的降级）。
    """
    if overlap <= 0 or len(content) <= overlap:
        return None
    start = len(content) - overlap
    pos = 0
    last = len(pieces) - 1
    for i, p in enumerate(pieces):
        s = p.strip()
        end = pos + len(s)
        if s.startswith(TABLE_MARK) and start < end:
            start = end  # 尾巴起点落在表段内：整段跳过（宁少重叠不截表）
        pos = end + (2 if i < last else 0)
    if start >= len(content):
        return None
    return content[start:]


def _split_text(text: str, size: int) -> list[str]:
    """把一段文本切成 ≤size 的块：表格段按行 → 句末标点 → 逗号 → 硬切。

    表格段（以 TABLE_MARK 开头）优先按**行**打包：表行是完整语义单元
    （表头/单位行/数据行），按标点切会劈开数字行（表行无句读只能硬切，
    会把 0.50 与列名劈到两块）；行切后仍超 size 的罕见超长行走下方递归。
    """
    if len(text) <= size:
        return [text]

    if text.startswith(TABLE_MARK):
        rows = [r for r in text.splitlines() if r.strip()]
        # 行切必须带 \n 拼接：_pack 默认无分隔符（句子自带句读），
        # 表格行若直接拼接会把相邻行并成一串（2026-09-10 测试暴露）
        packed = _pack(rows, size, sep="\n")
        if packed:
            return packed

    for pattern in (_SENTENCE_SPLIT_RE, _COMMA_SPLIT_RE):
        parts = [p.strip() for p in pattern.split(text) if p.strip()]
        if len(parts) > 1:
            packed = _pack(parts, size)
            if packed:
                return packed
    return [text[i : i + size] for i in range(0, len(text), size)]


def _pack(parts: list[str], size: int, *, sep: str = "") -> list[str]:
    """贪心打包：片段拼到接近 size 即切块；单个超长片段递归切分。

    sep 控制相邻片段间的连接符：句子切分默认空串（标点句读自带），
    表格行切分传 "\\n"（行间必须保留换行，否则并成一串）。
    """
    out: list[str] = []
    current = ""
    for part in parts:
        if len(part) > size:
            if current:
                out.append(current)
                current = ""
            out.extend(_split_text(part, size))
            continue
        if current and len(current) + len(part) + (len(sep) if current else 0) > size:
            out.append(current)
            current = part
        else:
            current = part if not current else current + sep + part
    if current:
        out.append(current)
    return out
