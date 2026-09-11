"""BM25 单测：公式手算对照 + 检索性质。

设计理由（面试可讲）：检索层的每个公式都可独立验证——
手工算一遍 idf / 词频饱和 / 长度归一化，再断言与实现一致，
这正是"自实现而非调库"的底气来源。
"""

from __future__ import annotations

import math

import pytest

from mikasa.index.bm25 import BM25Index
from mikasa.index.tokenizer import bigram_tokenizer

# ---------------------------------------------------------------------------
# 公式手算（k1=1.5, b=0.75，3 篇文档）
# ---------------------------------------------------------------------------

CORPUS = [["猫", "吃", "鱼"], ["狗", "吃", "肉"], ["猫", "猫", "吃", "鱼"]]
N = 3
AVGDL = (3 + 3 + 4) / 3  # 10/3


def _idf(df: int) -> float:
    return math.log(1.0 + (N - df + 0.5) / (df + 0.5))


def _norm(dl: int) -> float:
    return 1.0 - 0.75 + 0.75 * dl / AVGDL


def _term_score(f: int, dl: int, df: int) -> float:
    return _idf(df) * (f * 2.5) / (f + 1.5 * _norm(dl))


def test_score_doc_matches_hand_computation():
    idx = BM25Index(CORPUS, tokenize=bigram_tokenizer, k1=1.5, b=0.75)
    # doc0「猫 吃 鱼」查「猫 吃」
    expected = _term_score(f=1, dl=3, df=2) + _term_score(f=1, dl=3, df=3)
    assert idx.score_doc(0, ["猫", "吃"]) == pytest.approx(expected, rel=1e-12)


def test_term_frequency_saturation_is_sublinear():
    """f=2 的得分必须小于 f=1 的两倍（k1 的饱和效应）。"""
    idx = BM25Index(CORPUS, tokenize=bigram_tokenizer)
    twice = idx.score_doc(2, ["猫"])  # doc2 出现 2 次猫
    once = idx.score_doc(0, ["猫"])  # doc0 出现 1 次猫
    assert 0 < twice < 2 * once


def test_length_normalization_penalizes_long_docs():
    """b=0.75：同样只出现 1 次词，长文档得分更低。"""
    long_corpus = [["猫", "吃", "鱼"], ["猫"] + ["杂"] * 99]  # dl=3 vs dl=100
    idx = BM25Index(long_corpus, tokenize=bigram_tokenizer, k1=1.5, b=0.75)
    short, long_doc = idx.score_doc(0, ["猫"]), idx.score_doc(1, ["猫"])
    assert short > long_doc > 0


def test_idf_never_negative():
    """idf 平滑 ln(1+(N-df+0.5)/(df+0.5))：所有词都出现时仍为正（不惩罚常见词）。"""
    idx = BM25Index(CORPUS, tokenize=bigram_tokenizer)
    for term in ("猫", "吃"):
        assert idx._idf(term) > 0  # noqa: SLF001 —— 白盒单测直接验证平滑项


def test_search_ordering_and_filtering():
    idx = BM25Index(CORPUS, tokenize=bigram_tokenizer)
    hits = idx.search(["猫", "吃"], top_k=10)
    rows = [row for row, _ in hits]
    assert rows == [2, 0, 1]  # doc2 双词频 + doc0 双词命中 + doc1 只有“吃”
    assert all(score > 0 for _, score in hits)


def test_search_top_k_and_query_string():
    idx = BM25Index(CORPUS, tokenize=bigram_tokenizer)
    hits = idx.search("猫 吃", top_k=1)  # 字符串 query 也走分词器
    assert [row for row, _ in hits] == [2]
    assert idx.search("猫 吃", top_k=0) != []  # top_k<=0 返回全部


def test_vocabulary_and_size():
    idx = BM25Index(CORPUS, tokenize=bigram_tokenizer)
    assert idx.size == 3
    assert idx.vocabulary_size == 5  # 猫 吃 鱼 狗 肉


def test_empty_corpus_and_no_overlap_query():
    empty = BM25Index([], tokenize=bigram_tokenizer)
    assert empty.search("随便") == []
    assert empty.size == 0 and empty.vocabulary_size == 0
    idx = BM25Index(CORPUS, tokenize=bigram_tokenizer)
    assert idx.search("量子引力", top_k=10) == []  # 无任何词重叠 → 空结果
