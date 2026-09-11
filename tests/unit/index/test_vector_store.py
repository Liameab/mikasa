"""ExactVectorStore 单测：精确检索的正确性与退化分支。

设计背景（ADR-0010）：Windows 无 faiss/hnswlib 预编译轮子，
个人库规模用 numpy 精确检索（<500ms），评测的 recall 因此是"真"recall。
"""

from __future__ import annotations

import numpy as np
import pytest

from mikasa.index.vector_store import ExactVectorStore


def test_add_search_returns_top_hits_by_cosine():
    store = ExactVectorStore()
    assert store.size == 0 and store.dim == 0
    store.add(np.asarray([[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]], dtype=np.float32))
    assert store.size == 3 and store.dim == 2

    hits = store.search(np.asarray([1.0, 0.0], dtype=np.float32), top_k=2)
    assert [i for i, _ in hits] == [0, 2]  # cos(1,0)=1 > cos(1,1)=0.707
    assert hits[0][1] == pytest.approx(1.0)
    assert hits[1][1] == pytest.approx(1 / np.sqrt(2))


def test_search_empty_store_or_zero_vector():
    store = ExactVectorStore()
    assert store.search(np.asarray([1.0, 0.0], dtype=np.float32), top_k=5) == []
    store.add(np.asarray([[1.0, 0.0]], dtype=np.float32))
    assert store.search(np.asarray([0.0, 0.0], dtype=np.float32), top_k=5) == []  # 零向量


def test_search_dim_mismatch_raises_storage_error():
    """查询维度 ≠ 库内维度 → StorageError（带 reindex 指引，非 numpy 裸错）。

    cli 只捕 StorageError/ConfigError：裸 ValueError 会漏成 traceback，
    且维度错配的根因是 embedding 模型切换未重嵌（见 ADR-0014）。
    """
    from mikasa.errors import StorageError

    store = ExactVectorStore(np.asarray([[1.0, 0.0, 0.0]], dtype=np.float32))  # dim 3
    with pytest.raises(StorageError, match="reindex"):
        store.search(np.asarray([1.0, 0.0], dtype=np.float32), top_k=1)  # dim 2


def test_append_add_concatenates():
    store = ExactVectorStore()
    store.add(np.asarray([[1.0, 0.0]], dtype=np.float32))
    store.add(np.asarray([[0.0, 1.0]], dtype=np.float32))
    assert store.size == 2
    hits = store.search(np.asarray([0.0, 1.0], dtype=np.float32), top_k=1)
    assert hits[0][0] == 1


def test_init_with_matrix_normalizes_and_top_k_clamp():
    store = ExactVectorStore(np.asarray([[2.0, 0.0], [0.0, 3.0]], dtype=np.float32))
    assert store.dim == 2 and store.size == 2
    # top_k 超出规模/非正 → 返回全部（降序）
    q = np.asarray([1.0, 0.0], dtype=np.float32)
    assert len(store.search(q, top_k=99)) == 2
    assert len(store.search(q, top_k=-1)) == 2
    assert store.search(q, top_k=-1)[0][1] == pytest.approx(1.0)  # 已归一化


def test_rank_order_is_similarity_descending():
    rng = np.random.default_rng(7)
    store = ExactVectorStore(rng.normal(size=(50, 8)).astype(np.float32))
    q = rng.normal(size=8).astype(np.float32)
    hits = store.search(q, top_k=10)
    scores = [s for _, s in hits]
    assert len(hits) == 10
    assert scores == sorted(scores, reverse=True)


def test_normalization_makes_scale_irrelevant():
    """归一化后 [3,0] 与 [1,0] 同余弦：tie 也返回一致的正确分数。"""
    store = ExactVectorStore()
    store.add(np.asarray([[3.0, 0.0], [1.0, 0.0], [0.0, 5.0]], dtype=np.float32))
    q = np.asarray([1.0, 0.0], dtype=np.float32)
    hits = store.search(q, top_k=2)
    assert {i for i, _ in hits} == {0, 1}
    assert all(abs(s - 1.0) < 1e-6 for _, s in hits)


def rng_noise(n: int) -> np.ndarray:
    rng = np.random.default_rng(3)
    return (rng.normal(size=(n, 3)) * 0.001).astype(np.float32)
