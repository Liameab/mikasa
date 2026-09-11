"""分块逆向拼接：把 chunk 序列还原成可连续阅读的全文（阅读视图用）。

为什么需要它：库内**不存解析后全文**（`Para`/`LoadedDocument` 只在 ingest
内存活一次，见 ingest/types.py），chunks 是唯一与 `citation.chunk_id` 原生
对齐的数据。但 chunker 会给**同标题**的相邻块铺一段重叠尾巴
（chunker.py:85-97 的 seed_overlap_for），直接首尾相接会让每 ~350 字重复
一小段——阅读视图里就是"同一句话读两遍"。本模块是那次重叠的逆运算。

**为什么逆运算必须与 chunker 同源**（改 chunking.overlap 时两处一起看）：

- 重叠只在相邻块 `heading_path` **相同**时才可能产生——chunker.py:110-114
  在标题变化处 `tail = None`，chunker.py:92 的守卫又要求
  `current_head == last_emitted_head`。故本模块的守卫 ① 是"跨标题必不裁"。
  实测全库 941 对相邻块中「跨标题且存在重叠」= 0 例，这条守卫零代价。
- 重叠尾巴可能被**截短**：chunker.py:94 的
  `take = min(len(tail), size - text_len - len(_SEP))` 只取尾巴的前 take 字
  （宁可少重叠也不破坏"块 ≤ size"）。截短后，下一块的开头就不再是上一块的
  后缀，而是"上一块尾部某处开始的片段"——故除后缀规则外还需要守卫 ② 的
  近尾规则。实测 PDF 类语料：后缀规则覆盖 68.8%，近尾规则再补 14.3%，
  合计 83.1%；md/docx 因标题频繁变化，本就几乎无重叠（实测 4.2% / 0%）。
- 重叠尾巴还会跳过表格段（chunker.py:143-168 的 `_overlap_tail`），
  残留的少量重复属可接受的降级。

**真机验证（2026-09-10，全库 23 篇 / 964 块）**：拼接结果与 loader 入库时
统计的 `documents.char_count` 逐篇吻合到 0.2%~2% 以内——doc 71（866 块、
281441 字原始块内容）拼后 248767 字，而 loader 当初统计 248274 字，仅差
493 字（应即 loader 加在表格块首的 `【表格】` 标记）；md/docx 差 2~10 字。
即去接缝不是"看起来合理"，而是**基本把原文还到了原长度**。全库
309499 → 275034 字（裁掉 11.1%），偏移不变量在 23 篇全部成立。

**这个模块是启发式的，不是无损还原**：拼接必然丢 md 的 `# 标题` 行、
代码围栏、blockquote 标记与 PDF 页眉页脚（它们本就没进 chunk）。阅读视图
的"原文件"视图是完整原文的出口，此处不做假还原。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from mikasa.models.document import Chunk

# 判定接缝的最小长度：更短的匹配太容易是巧合（实测真实残留 ≤7 字，视觉无感），
# 放过它换掉误裁真实重复内容的风险
MIN_SEAM = 8

# 单个接缝的最大长度上限。真实 overlap 配置上限是 50（config/profiles/*.yaml），
# 200 已远超而短到不可能巧合命中长段真实重复
MAX_SEAM = 200

# 近尾规则的窗口：匹配起点必须落在前块最后这么多字内。
# 依据 = chunker 的尾巴长度上限是 overlap（默认 50），64 留一点余量以容忍
# 配置漂移（历史语料可能用更大的 overlap 入库）。窗口越大覆盖越多、误裁风险越高。
SEAM_WINDOW = 64


@dataclass(frozen=True)
class ChunkSpan:
    """一块在拼接全文中的位置区间（半开区间 [start, end)）。"""

    chunk_id: int | None
    seq: int
    start: int
    end: int
    heading_path: str | None
    page_number: int | None


@dataclass(frozen=True)
class StitchedText:
    """拼接结果：全文 + 各块偏移 + 被裁掉的总字数（日志/测试用）。"""

    text: str
    spans: list[ChunkSpan]
    trimmed_chars: int


def _seam_length(
    prev: str,
    cur: str,
    *,
    min_seam: int,
    max_seam: int,
    seam_window: int,
) -> int:
    """求 prev 尾部与 cur 开头重叠的字数；无可靠重叠返回 0。

    两条规则按可靠性排序，命中即返回：
      ① **后缀规则**（严格）：找最大的 k 使 `prev` 以 `cur[:k]` 结尾。
         这要求重叠一直延伸到前块末尾——未截短的重叠都长这样，误裁风险最低。
      ② **近尾规则**（兜底）：找最大的 k 使 `cur[:k]` 出现在 `prev` 的最后
         `seam_window` 字内。覆盖被 chunker 截短的重叠（此时片段后面还有
         属于前块的正文，故不是后缀）。窗口约束是防误裁的关键。
    """
    limit = min(len(prev), len(cur), max_seam)

    # ① 后缀规则
    for k in range(limit, min_seam - 1, -1):
        if prev.endswith(cur[:k]):
            return k

    # ② 近尾规则
    for k in range(limit, min_seam - 1, -1):
        pos = prev.rfind(cur[:k])
        if pos != -1 and len(prev) - pos <= seam_window:
            return k

    return 0


def stitch_chunks(
    chunks: Sequence[Chunk],
    *,
    min_seam: int = MIN_SEAM,
    max_seam: int = MAX_SEAM,
    seam_window: int = SEAM_WINDOW,
) -> StitchedText:
    """把按 seq 升序的 chunk 序列拼成连续全文，并裁掉块间重叠。

    调用方保证 chunks 已按 `seq` 升序（`repo.chunks_by_document` 的 ORDER BY）。
    空序列 → 空文本空 spans。

    不变量（测试直接断言）：
      - `spans[0].start == 0`、`spans[-1].end == len(text)`；
      - 相邻 `spans[i].end == spans[i+1].start`（偏移连续无空洞）；
      - `sum(end - start) == len(text)`；
      - 绝不把某块裁空（`k >= len(cur)` 一律不裁）。
    """
    if not chunks:
        return StitchedText(text="", spans=[], trimmed_chars=0)

    parts: list[str] = []
    spans: list[ChunkSpan] = []
    trimmed = 0
    cursor = 0

    for index, chunk in enumerate(chunks):
        content = chunk.content
        if index > 0:
            prev = chunks[index - 1]
            # 守卫 ①：跨标题不可能是重叠（chunker.py:110-114 在标题变化处
            # 清空 tail），此时一律不裁
            same_heading = chunk.heading_path == prev.heading_path
            if same_heading and len(content) > 1:
                # 比较基准必须是**前块的原始 DB 内容**而不是已裁剪过的 parts[-1]：
                # 下一块的重叠尾巴是 chunker 从前块**产出时**的内容尾部取的，
                # 本函数的裁剪只改显示、不改"下一块当初是怎么切出来的"事实。
                k = _seam_length(
                    prev.content,
                    content,
                    min_seam=min_seam,
                    max_seam=max_seam,
                    seam_window=seam_window,
                )
                # 守卫 ③：绝不裁空整块
                if 0 < k < len(content):
                    content = content[k:]
                    trimmed += k

        start = cursor
        cursor += len(content)
        parts.append(content)
        spans.append(
            ChunkSpan(
                chunk_id=chunk.id,
                seq=chunk.seq,
                start=start,
                end=cursor,
                heading_path=chunk.heading_path,
                page_number=chunk.page_number,
            )
        )

    return StitchedText(text="".join(parts), spans=spans, trimmed_chars=trimmed)
