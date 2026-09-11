"""自实现 BM25（Okapi BM25）。

为什么自实现而不是 SQLite FTS5 / Elasticsearch（详见 ADR-0009）：
- 教学与面试价值：IDF、词频饱和、长度归一化（k1/b）是信息检索的
  经典算法内容，公式在这里可见可调，评测里能跑 ablations；
- 规模匹配：个人知识库数千 chunk，全量线性打分 <10ms，无需倒排表；
- 分词语料在入库时预分词落盘（chunks.tokens），查询时只分 query。

公式：
  score(D, Q) = Σ_{q∈Q} IDF(q) · [f(q,D)·(k1+1)] / [f(q,D) + k1·(1 - b + b·|D|/avgdl)]

文档按固定顺序索引（与 DB 的 chunk_id 升序一致），检索返回 [行号, 分数]。
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence

from mikasa.index.tokenizer import Tokenizer, bigram_tokenizer


class BM25Index:
    """BM25 索引：一次性构建，支持多次查询。

    构建复杂度 O(总词条数)，查询复杂度 O(文档数 × query 词条数)，
    单次查询在数千 chunk 规模下实测 <10ms（见 docs/architecture.md）。
    """

    def __init__(
        self,
        corpus_tokens: Sequence[Sequence[str]],
        *,
        tokenize: Tokenizer = bigram_tokenizer,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        """corpus_tokens：每篇"文档"的预分词列表，顺序即行号（0-based）。"""
        self._tokenize = tokenize
        self.k1 = k1
        self.b = b
        self._docs: list[list[str]] = [list(tokens) for tokens in corpus_tokens]
        self._n = len(self._docs)
        self._avgdl = sum(len(d) for d in self._docs) / self._n if self._n else 0.0
        self._df: Counter[str] = Counter()
        for doc in self._docs:
            for term in set(doc):
                self._df[term] += 1

    @property
    def size(self) -> int:
        return self._n

    @property
    def vocabulary_size(self) -> int:
        return len(self._df)

    def _idf(self, term: str) -> float:
        """IDF 平滑公式：ln(1 + (N - df + 0.5)/(df + 0.5))，避免负分。"""
        df = self._df.get(term, 0)
        return math.log(1.0 + (self._n - df + 0.5) / (df + 0.5))

    def score_doc(self, doc_index: int, query_terms: list[str]) -> float:
        """单文档得分（供单测手算验证）。"""
        doc = self._docs[doc_index]
        if not doc or not query_terms:
            return 0.0
        freq = Counter(doc)
        dl = len(doc)
        norm = 1.0 - self.b + self.b * dl / self._avgdl if self._avgdl else 1.0
        score = 0.0
        for term in query_terms:
            f = freq.get(term, 0)
            if f == 0:
                continue
            idf = self._idf(term)
            score += idf * (f * (self.k1 + 1.0)) / (f + self.k1 * norm)
        return score

    def search(self, query: str | list[str], top_k: int = 10) -> list[tuple[int, float]]:
        """检索：返回 [(行号, BM25 分数), ...] 降序。top_k<=0 返回全部。"""
        query_terms = list(query) if isinstance(query, list) else self._tokenize(query)
        scores: list[tuple[int, float]] = []
        for idx in range(self._n):
            score = self.score_doc(idx, query_terms)
            if score > 0.0:
                scores.append((idx, score))
        scores.sort(key=lambda item: item[1], reverse=True)
        return scores[:top_k] if top_k > 0 else scores
