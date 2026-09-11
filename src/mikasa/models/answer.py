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

    @property
    def body(self) -> str:
        """无标记正文（用于评测的纯文本视图）。"""
        return self.text
