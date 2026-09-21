"""检索指标单测：手写公式的正确性（recall@k / MRR / nDCG@k）+ 聚合容器。

公式锁定（改公式必须连测试一起改，避免"指标口径漂移"）：
  recall@k  = 前 k 名金块数 / 金块总数；gold 为空时返回 0（调用方跳过）
  MRR       = 首个金块排名的倒数；未命中 0
  nDCG@k    = DCG / IDCG；gold 为空时 0
"""

from __future__ import annotations

import pytest

from mikasa.eval.metrics import (
    _Mean,
    make_retrieval_metrics,
    ndcg_at,
    recall_at,
    reciprocal_rank,
    shape_stats,
    summarize,
)

# ---------------------------------------------------------------------------
# recall_at
# ---------------------------------------------------------------------------


def test_recall_at_top_k_hits_and_misses():
    ranked = [1, 2, 3, 4, 5]
    gold = {3, 5}
    # k=3：命中 3 号块，5 号在第 4 名不在窗口内 → 1/2
    assert recall_at(ranked, gold, 3) == 0.5
    assert recall_at(ranked, gold, 5) == 1.0


def test_recall_at_gold_empty_returns_zero():
    # 空 gold = 无召回目标（不可答题不会走进检索层），返回 0 而不是除零
    assert recall_at([1, 2], set(), 5) == 0.0


def test_recall_at_no_hits():
    assert recall_at([10, 11], {1}, 5) == 0.0


# ---------------------------------------------------------------------------
# reciprocal_rank
# ---------------------------------------------------------------------------


def test_reciprocal_rank_first_hit_position():
    ranked = [7, 1, 9]
    assert reciprocal_rank(ranked, {1}) == 0.5  # 首位命中 → 1/1
    ranked = [7, 1, 9]
    assert reciprocal_rank(ranked, {9}) == 1 / 3
    ranked = [7, 1, 9]
    assert reciprocal_rank(ranked, {7, 1}) == 1.0  # 第一个金块在第 1 名


def test_reciprocal_rank_miss_is_zero():
    assert reciprocal_rank([1, 2, 3], {99}) == 0.0


# ---------------------------------------------------------------------------
# ndcg_at：金块越靠前得分越高（排序质量，不只看"有没有召回"）
# ---------------------------------------------------------------------------


def test_ndcg_rewards_earlier_gold():
    gold = {1, 2}
    early = ndcg_at([1, 2, 3, 4], gold, 4)  # 金块占前两名 → 理想排序（≈1，分母有 ε 防零）
    late = ndcg_at([9, 1, 2, 3], gold, 4)  # 金块被挤到 2、3 名 → 有折损
    assert early == pytest.approx(1.0)
    assert 0.0 < late < 1.0


def test_ndcg_partial_window():
    gold = {1, 5}
    # k=1 窗口只有第一名，命中的只有 1 号 → 完整召回放不进窗口仍得满分（口径定义）
    assert ndcg_at([1, 5, 2], gold, 1) == pytest.approx(1.0)
    assert ndcg_at([9, 1, 5], gold, 1) == 0.0  # 第一名不是金块 → 0


def test_ndcg_gold_empty_returns_zero():
    assert ndcg_at([1, 2], set(), 5) == 0.0


# ---------------------------------------------------------------------------
# _Mean / summarize：逐条容器与描述统计
# ---------------------------------------------------------------------------


def test_mean_container_accumulates_and_finalizes():
    mean = _Mean()
    assert mean.finalize() != mean.finalize()  # 空容器 → nan
    mean.add(1.0)
    mean.extend([2.0, 3.0])
    assert mean.finalize() == 2.0


def test_summarize_percentiles_edge_sizes():
    stats = summarize([0.5])
    assert stats == {"mean": 0.5, "p50": 0.5, "p95": 0.5, "min": 0.5, "max": 0.5, "n": 1}
    # 位置口径 = ceil(p/100·n)−1（nearest-rank）：n=2 时 p50 指 index 0、p95 指 index 1
    stats = summarize([0.0, 1.0])
    assert stats["p50"] == 0.0
    assert stats["p95"] == 1.0
    assert stats["mean"] == 0.5
    assert stats["n"] == 2


def test_summarize_empty():
    import math

    stats = summarize([])
    assert stats["n"] == 0 and math.isnan(float(stats["mean"]))


# ---------------------------------------------------------------------------
# RetrievalMetrics：分层聚合容器
# ---------------------------------------------------------------------------


def test_make_metrics_accumulates_by_difficulty():
    metrics = make_retrieval_metrics((5, 8, 10))
    metrics.add_item([3, 1, 2], {1}, "easy")  # gold 在 rank2 → recall@5=1, mrr=1/2
    metrics.add_item([1, 2], {2}, "medium")  # gold 在 rank2
    metrics.add_item([9, 8], {1}, "hard")  # 未命中 → 全 0

    assert metrics.recall[5].finalize() == 2 / 3
    assert metrics.rr.finalize() == (0.5 + 0.5 + 0.0) / 3
    # 分层只记 k 最大档 10 的逐条 recall
    assert metrics.by_difficulty["easy"][10] == [1.0]
    assert metrics.by_difficulty["medium"][10] == [1.0]
    assert metrics.by_difficulty["hard"][10] == [0.0]
    assert metrics.recall[10].finalize() == 2 / 3


def test_metrics_final_snapshot_shape():
    metrics = make_retrieval_metrics((5,))
    metrics.add_item([1], {1}, "easy")
    snapshot = metrics.final()
    assert set(snapshot) == {"recall_at", "mrr", "ndcg_at", "by_difficulty"}
    # 内存快照的 k 键保持 int（落库 JSON 时由 json.dumps 自动转成字符串键）
    assert snapshot["recall_at"][5]["mean"] == 1.0
    assert snapshot["by_difficulty"]["easy"][5]["n"] == 1


# ---------------------------------------------------------------------------
# 作答形态（交接单 P3）
# ---------------------------------------------------------------------------


def test_shape_stats_counts_the_shapes_that_matter():
    """分节 / 表格 / 公式 / 列表 / 字数——提示词改动的回归镜子。

    计数口径写死在这里：围栏代码块里的竖线与 $ **不算**（那是代码字面量，
    不是呈现形态），单行竖线也不算表格（≥2 行才算一张）。
    """
    text = (
        "## 结论\n\n"
        "| 模型 | 分数 |\n| --- | --- |\n| a | 1 |\n\n"
        "- 要点一 $x^2$\n- 要点二\n\n"
        "$$E = mc^2$$\n\n"
        "```python\n# | 这不是表格 |\nprint('$不是公式$')\n```\n"
        "收尾一句。"
    )
    stats = shape_stats(text)
    assert stats["sections"] == 1
    assert stats["tables"] == 1  # 代码块里的竖线不算
    assert stats["formulas"] == 2  # 一个行内 + 一个行间
    assert stats["bullets"] == 2
    assert stats["chars"] == len(text.strip())


def test_shape_stats_empty_and_plain():
    """空文本与纯段落都不炸，且全 0（回放/拒答轮会出现空文本）。"""
    assert shape_stats("")["chars"] == 0
    plain = shape_stats("就是一句话，没有任何形态。")
    assert plain == {"chars": 13, "sections": 0, "tables": 0, "formulas": 0, "bullets": 0}
