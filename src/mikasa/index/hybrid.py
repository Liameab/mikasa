"""混合检索融合：RRF（Reciprocal Rank Fusion）。

为什么 RRF 而不是分数加权（详见 ADR-0011）：
- BM25 分与余弦分量纲/分布完全不同，线性加权需调权重且脆；
- RRF 只依赖排名：score = Σ 1/(k + rank)，无需任何标定，
  是 kaggle/搜索竞赛中稳定有效的融合基线，也容易手算讲解。

RRF 的一个已知性质：文档必须在两路都有排名才吃两路分数；
单路独有但很靠前的文档仍可能胜出——评测里能看到融合 > 单路的证据。
"""

from __future__ import annotations

from collections.abc import Sequence


def rrf_fuse(runs: Sequence[Sequence[int]], k: int = 60) -> list[tuple[int, float]]:
    """融合多路检索结果（每路为按排名升序的行号列表）。

    返回 [(行号, RRF 分数), ...] 按分数降序；k 为 RRF 常数（经典值 60）。
    """
    fused: dict[int, float] = {}
    for ranked in runs:
        for rank, doc_index in enumerate(ranked):
            fused[doc_index] = fused.get(doc_index, 0.0) + 1.0 / (k + rank + 1)
    ordered = sorted(fused.items(), key=lambda item: item[1], reverse=True)
    return ordered
