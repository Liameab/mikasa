"""RRF 融合单测：不变式与手算值。

面试可讲的点：RRF 只依赖排名不依赖分数分布，因此
score = Σ 1/(k+rank) 的每个分数都可手算核对。
"""

from __future__ import annotations

import pytest

from mikasa.index.hybrid import rrf_fuse


def test_hand_computed_scores():
    """run1=[0,1] run2=[1,2]，k=60：
    doc1 = 1/61 + 1/62，doc0 = 1/61，doc2 = 1/62。"""
    fused = dict(rrf_fuse([[0, 1], [1, 2]], k=60))
    assert fused[1] == pytest.approx(1 / 61 + 1 / 62)
    assert fused[0] == pytest.approx(1 / 61)
    assert fused[2] == pytest.approx(1 / 62)
    # 排序：都出现在两路的 > 单路靠前的
    assert list(fused.items()) == sorted(fused.items(), key=lambda kv: kv[1], reverse=True)


def test_document_in_both_runs_beats_single_run_document():
    """两路都有排名的文档应胜过只在单路出现的文档（融合的核心价值）。"""
    fused = dict(rrf_fuse([[0], [0]], k=60))
    only_one_side = dict(rrf_fuse([[0, 1], [2]], k=60))
    # doc0 两路第 1：2/61 > doc2 只在单路第 2：1/62
    assert fused[0] > only_one_side[2]


def test_fusion_preserves_diversity():
    """两路各自独有但靠前的文档：都可能留在头部，不被一路垄断。"""
    fused = dict(rrf_fuse([[0, 1], [1, 0]], k=60))
    assert fused[0] == fused[1]  # 两路互相 1、2 名 → 平分


def test_empty_and_rank_sensitivity():
    assert rrf_fuse([]) == []
    assert rrf_fuse([[], []]) == []
    # rank 敏感：相同位置的分数随 k 变化但相对序不变
    a = dict(rrf_fuse([[0], [0]], k=10))
    b = dict(rrf_fuse([[0], [0]], k=100))
    assert a[0] > b[0]
