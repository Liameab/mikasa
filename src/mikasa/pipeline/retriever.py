"""检索器：BM25 + 稠密双路召回 → RRF 融合 → 可插拔重排。

每条命中保留各阶段分数（RetrievedChunk），这是评测 ablations
与"融合为什么优于单路"分析的数据来源（docs/evaluation.md）。
"""

from __future__ import annotations

from time import perf_counter

from mikasa.config.settings import Settings
from mikasa.index.hybrid import rrf_fuse
from mikasa.index.manager import Corpus
from mikasa.index.vector_store import ExactVectorStore
from mikasa.models.retrieval import RetrievedChunk
from mikasa.providers import get_reranker
from mikasa.providers.embedding import EmbeddingProvider
from mikasa.providers.reranker import RerankerProvider
from mikasa.utils.logging import get_logger

logger = get_logger("pipeline")


def _sanitize_indices(indices: list[int], size: int) -> list[int]:
    """过滤重排服务返回的下标：保序去重 + 丢掉越界值。

    重排是**外部服务**（SiliconFlow 等），响应不完全可控。越界下标会让
    `rows[i]` 抛 IndexError 直接穿到 Web 变成"内部错误"（provider 层其它路径
    都规整地包成 ProviderError，只有这里没有）；重复下标则让候选被静默截断
    （2026-09-11 审查指出）。
    """
    safe: list[int] = []
    seen: set[int] = set()
    for idx in indices:
        if not isinstance(idx, int) or not 0 <= idx < size:
            continue
        if idx in seen:
            continue
        seen.add(idx)
        safe.append(idx)
    if len(safe) != len(indices):
        logger.warning(
            "重排返回了非法下标（越界或重复）：收到 %d 个、可用 %d 个，已丢弃",
            len(indices),
            len(safe),
        )
    return safe


class Retriever:
    """一次检索 = 语料快照 + 配置；由 AskService 每次提问时构造。

    dense 路可用条件（三选一不可用即退化为单路，评测分别记录）：
      1. retrieval.dense_enabled 为 true
      2. embedding.backend != "none"
      3. 语料快照携带该模型的向量矩阵
    """

    def __init__(
        self,
        settings: Settings,
        corpus: Corpus,
        embedding: EmbeddingProvider,
    ) -> None:
        self.settings = settings
        self._corpus = corpus
        self._embedding = embedding
        self._reranker: RerankerProvider = get_reranker(settings.reranker)
        self._dense_store = ExactVectorStore(corpus.matrix) if corpus.matrix is not None else None

    # ------------------------------------------------------------------

    def retrieve(
        self,
        question: str,
        *,
        second_query: str | None = None,
        document_id: int | None = None,
    ) -> tuple[list[RetrievedChunk], dict[str, float]]:
        """检索主流程；返回 (命中列表, 分段延迟毫秒)。延迟分段供评测统计 p95。

        second_query（跨语言第二路，2026-09-10）：中文问题译成的英文查询
        与主查询各跑一套 bm25+dense → 两路融合结果再 RRF 合流。英文路
        只在英文块上有分（中文块对它零命中），不会挤占主路中文结果；
        语义上天然按文档语言分流。None = 纯单路，行为与旧版一致。

        document_id（阅读器「边看边问」的「只看本篇」，2026-09-22）：
        限定只在某一篇文档的块里检索；None = 全库（默认，与旧版逐字节一致）。
        做法是**行号 allow-list**：先按 `Chunk.document_id` 算出允许的行集合，
        两条路都全取（`top_k=0`）后过滤，再走原来的 RRF/重排。

        为什么是 allow-list 而不是 `Corpus.scoped(document_id)`：后者要重建
        BM25，IDF/avgdl 变成"只在这篇里统计"——同一个问题在「本篇」与「全库」
        两种范围下就不是"同一套检索的窄化"而是换了一套打分口径，用户切范围
        看到的排序会莫名跳变；且还要复制向量子矩阵。allow-list 只改候选集、
        不改任何分数，其余链路（RRF/重排/引用解析）原封不动。
        """
        lat: dict[str, float] = {}

        t0 = perf_counter()
        cfg = self.settings.retrieval
        corpus = self._corpus
        store = self._dense_store
        dense_on = cfg.dense_enabled and store is not None

        # 限定文档时的允许行集合（一次算好，两条路共用）
        allowed_rows: set[int] | None = None
        if document_id is not None:
            allowed_rows = {
                row for row, chunk in enumerate(corpus.chunks) if chunk.document_id == document_id
            }
            if not allowed_rows:
                # 该文档不在快照里（已删/未入库）或没有块：空手而归。
                # 不能退化成全库检索（那不是用户要的），也不让 dense 白跑一次全排序。
                # 延迟键仍给全（调用方按固定键集取用，见 test_retriever 的断言）。
                return [], {"retrieve": 0.0, "rerank": 0.0}

        def oneside(
            query: str,
        ) -> tuple[list[tuple[int, float]], list[tuple[int, float]], list[tuple[int, float]]]:
            """单查询双路召回 + RRF：返回 (bm25_hits, dense_hits, fused)。

            限定文档时必须**全取再过滤**：截断发生在 search 内部
            （bm25_top_k/dense_top_k），先取全库 top-20 再过滤会把"本篇里
            排名靠后的块"整批丢掉，小文档直接空结果。`top_k<=0` = 返回全部
            是两个引擎**既有**的语义（bm25.py 的 `scores[:top_k] if top_k > 0
            else scores`、vector_store.py 的 `top_k <= 0 or top_k >= scores.size`）。

            过滤还必须发生在 `rrf_fuse` **之前**：RRF 只吃名次，若用全库名次
            融合、再过滤，两路之间的区分度会被 k=60 压平（偏移几名几乎无差别），
            排序质量静默变差——不报错，只是答案变味。
            """
            bm25_hits = corpus.bm25.search(
                query, top_k=cfg.bm25_top_k if allowed_rows is None else 0
            )
            if allowed_rows is not None:
                bm25_hits = [hit for hit in bm25_hits if hit[0] in allowed_rows]
            runs: list[list[int]] = [[row for row, _ in bm25_hits]]
            dense_hits: list[tuple[int, float]] = []
            if store is not None and dense_on:
                query_vec = self._embedding.embed_query(query)
                if query_vec.shape[0] > 0:
                    dense_hits = store.search(
                        query_vec, top_k=cfg.dense_top_k if allowed_rows is None else 0
                    )
                    if allowed_rows is not None:
                        dense_hits = [hit for hit in dense_hits if hit[0] in allowed_rows]
                    runs.append([row for row, _ in dense_hits])
            return bm25_hits, dense_hits, rrf_fuse(runs, k=cfg.fusion_k)

        bm25_hits, dense_hits, fused = oneside(question)
        # (bm25, dense) 分数字典：主查询先塞（setdefault 保主路优先），
        # 第二查询只补主路未命中的块（跨语言路的英文块）
        row_scores: dict[int, list[float | None]] = {}
        for row, score in bm25_hits:
            row_scores.setdefault(row, [None, None])[0] = score
        for row, score in dense_hits:
            row_scores.setdefault(row, [None, None])[1] = score

        if second_query:
            bm25_alt, dense_alt, fused_alt = oneside(second_query)
            # 只补主路没有的分数（setdefault 返回的是**已存在**的列表，直接
            # 赋值会把主路分数覆盖成英文路的——2026-09-11 修正，与上面的注释同义）
            for row, score in bm25_alt:
                slot = row_scores.setdefault(row, [None, None])
                if slot[0] is None:
                    slot[0] = score
            for row, score in dense_alt:
                slot = row_scores.setdefault(row, [None, None])
                if slot[1] is None:
                    slot[1] = score
            # 主副两路 RRF 合流（ADR-0011 同款：只依赖排名）
            fused = rrf_fuse(
                [[row for row, _ in fused], [row for row, _ in fused_alt]],
                k=cfg.fusion_k,
            )
        fused = fused[: cfg.fusion_top_k]
        lat["retrieve"] = (perf_counter() - t0) * 1000.0
        fused_scores = dict(fused)

        # ---- 可插拔重排（api 交叉编码 / local / none） ----
        reranked_rows: list[int] = [row for row, _ in fused]
        if self._reranker.model != "none" and reranked_rows:
            t1 = perf_counter()
            documents = [self._corpus.chunks[row].content for row in reranked_rows]
            top_n = self.settings.reranker.top_n
            indices = self._reranker.rerank(
                question, documents, top_n=top_n if top_n > 0 else len(documents)
            )
            # 校验上游返回的下标（见 _sanitize_indices 的说明）。**必须真的调用
            # 它**：此前这里把同一段过滤内联复制了一份，而单测 import 的是那个
            # 函数——于是测试守着一份没人执行的副本，改坏真正生效的内联版也不会
            # 变红（2026-09-11 复查发现）。
            safe = _sanitize_indices(indices, len(reranked_rows))
            reranked_rows = [reranked_rows[i] for i in safe]
            lat["rerank"] = (perf_counter() - t1) * 1000.0
        else:
            lat["rerank"] = 0.0

        hits: list[RetrievedChunk] = []
        for rank, row in enumerate(reranked_rows, start=1):
            # 主查询分数优先（setdefault 时先塞主路），second 路只补缺
            scores = row_scores.get(row, [None, None])
            bm25_score, dense_score = scores[0], scores[1]
            hits.append(
                RetrievedChunk(
                    chunk=corpus.chunks[row],
                    bm25_score=bm25_score,
                    dense_score=dense_score,
                    fused_score=fused_scores.get(row, 0.0),
                    rank=rank,
                )
            )
        logger.debug(
            "检索：问题=%r bm25=%d dense=%d fused=%d → 命中 %d（dense=%s）",
            question[:30],
            len(bm25_hits),
            len(dense_hits),
            len(fused),
            len(hits),
            dense_on,
        )
        return hits, lat
