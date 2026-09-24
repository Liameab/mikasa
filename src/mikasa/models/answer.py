"""问答输出数据模型（generator → UI/CLI/评测）。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Citation(BaseModel):
    """答案正文中 [n] 标记对应的可验证引用。"""

    model_config = ConfigDict(frozen=True)

    marker: int  # 正文中的编号 [n]
    chunk_id: int
    document_title: str
    section: str | None = None  # heading_path
    page: int | None = None
    snippet: str  # 引用的原文片段（UI 点击展开核对）


class ReaderSource(BaseModel):
    """阅读器「相关片段」（A 档 c）：本次检索的命中 + 是否被答案引用。

    与 `Citation` 的区别：Citation 只包含**被引用**的块且随答案落库；
    ReaderSource 是**全部命中**（含没被引用的），只在阅读器提问的 SSE 帧里
    出现，**不落库、不进 `Answer`**——那两处的契约（评测口径、qa_messages
    的载荷）一个字都不动。前端按 `current_doc` 标「本篇/另一篇」、
    按 `cited/marker` 标被引用项。
    """

    model_config = ConfigDict(frozen=True)

    chunk_id: int
    document_id: int
    document_title: str
    section: str | None = None  # heading_path
    page: int | None = None
    snippet: str  # 命中块的原文片段（Chunk.snippet，200 字 + …）
    rank: int = 0  # 检索名次（1-based）
    cited: bool = False  # 被答案引用（done 帧前才回填）
    marker: int | None = None  # 对应的 [n]；未被引用为 None
    current_doc: bool = False  # 是否来自「正在读的这篇」


class Answer(BaseModel):
    """一次问答的完整产物。"""

    model_config = ConfigDict(frozen=True)

    question: str
    text: str  # 正文（保留 [n] 标记，UI 据此渲染可点击引用）
    citations: list[Citation] = []
    refused: bool = False  # 无据拒答标记（由 REFUSAL_TEXT 判定）
    model: str | None = None
    latency_ms: dict[str, float] = {}  # {"retrieve":…, "rerank":…, "generate":…}
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
