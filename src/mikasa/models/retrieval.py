"""检索结果数据模型（retriever → generator 的传递结构）。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from mikasa.models.document import Chunk


class RetrievedChunk(BaseModel):
    """一条检索命中的 chunk，附带各阶段分数（ablations 分析的数据来源）。"""

    model_config = ConfigDict(frozen=True)

    chunk: Chunk
    bm25_score: float | None = None  # 原始分（fusion 前归一化的可能为 None）
    dense_score: float | None = None
    fused_score: float = 0.0
    rank: int = 0  # 最终排名（1-based）
