"""索引管理器：内存快照的生命周期（重建 / 缓存 / 失效）。

快照 = 全量 chunk（行序 = chunk_id 升序）+ BM25 索引 + 向量矩阵。
数据量级数千 chunk，全量重建 <50ms，因此快照采用"懒重建 + 行数指纹"：
每次请求先做一次 COUNT（微秒级），语料变化才真正重建。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import numpy as np

from mikasa.config.settings import Settings
from mikasa.index.bm25 import BM25Index
from mikasa.index.tokenizer import get_tokenizer
from mikasa.models.document import Chunk
from mikasa.storage.db import open_db
from mikasa.storage.repo import all_chunks_ordered, corpus_fingerprint, load_embedding_matrix
from mikasa.utils.logging import get_logger

logger = get_logger("index.manager")


@dataclass(frozen=True)
class Corpus:
    """一次索引快照：检索与评测的唯一数据源。"""

    chunks: list[Chunk]  # 行序 = chunk_id 升序（BM25 行号 / 矩阵行号都对齐它）
    row_of: dict[int, int] = field(init=False)  # chunk_id -> 行号
    bm25: BM25Index
    matrix: np.ndarray | None = None  # (n, dim)，行序与 chunks 一致
    embedding_model: str | None = None
    sha256: str = ""  # 语料指纹：(id, content_sha256) 序列的哈希，评测防错配

    def __post_init__(self) -> None:
        object.__setattr__(self, "row_of", {c.id: i for i, c in enumerate(self.chunks)})
        if not self.sha256:
            # 与 repo.corpus_digest 逐字节一致：混入 id，重建导致的 id 平移
            # 也会触发指纹失配（否则黄金集 gold_chunk_ids 会静默错位）
            digest = hashlib.sha256()
            for c in self.chunks:  # 行序 = chunk_id 升序
                digest.update(f"{c.id}:".encode())
                digest.update(c.content_sha256.encode("utf-8"))
            object.__setattr__(self, "sha256", digest.hexdigest())

    @property
    def empty(self) -> bool:
        return not self.chunks

    def chunk_by_id(self, chunk_id: int) -> Chunk | None:
        row = self.row_of.get(chunk_id)
        return self.chunks[row] if row is not None else None

    def content_hashes(self) -> dict[int, str]:
        """{chunk_id: content_sha256} —— 评测的逐题校验用（见 eval.golden）。

        `Chunk.id` 类型是可空的（入库前的临时对象没有 id），快照里的块则必然有；
        这里统一过滤掉 None，免得每个调用点各写一遍 `if c.id is not None`。
        """
        return {c.id: c.content_sha256 for c in self.chunks if c.id is not None}


class IndexManager:
    """懒重建的索引访问门面。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._corpus: Corpus | None = None
        self._fingerprint: tuple[int, int] | None = None

    def invalidate(self) -> None:
        """数据变更后调用（ingest/删除文档后）。"""
        self._corpus = None
        self._fingerprint = None

    def corpus(self) -> Corpus:
        """当前快照：语料指纹未变则复用缓存，否则重建。

        指纹是 (chunk 数, 最大 id)：只比数量识别不出**另一个进程 reindex 后
        id 整体平移而总数不变**的情况，详见 repo.corpus_fingerprint。
        """
        with open_db(self._settings.db_path) as conn:
            fingerprint = corpus_fingerprint(conn)
        if self._corpus is not None and fingerprint == self._fingerprint:
            return self._corpus
        self._corpus = self._rebuild()
        self._fingerprint = fingerprint
        return self._corpus

    def _rebuild(self) -> Corpus:
        settings = self._settings
        with open_db(settings.db_path) as conn:
            chunks = all_chunks_ordered(conn)
            bm25 = BM25Index([c.tokens for c in chunks], tokenize=get_tokenizer())

            matrix: np.ndarray | None = None
            embedding_model: str | None = None
            if settings.embedding.backend != "none":
                loaded = load_embedding_matrix(conn, settings.embedding.model)
                if loaded is None:
                    logger.warning(
                        "索引中没有 %s 的向量（%d 个 chunk）——dense 路不可用。"
                        "请运行 mikasa ingest --reindex 重建向量。",
                        settings.embedding.model,
                        len(chunks),
                    )
                else:
                    ids, loaded_matrix = loaded
                    expected = [c.id for c in chunks]
                    if ids != expected:
                        # 向量行与 chunk 对不上时**降级运行，不抛异常**。
                        #
                        # 这里原来 raise StorageError，代价是：入库中途进程被杀
                        # （关窗、崩溃、断电）会留下"chunk 已提交、向量还没写完"
                        # 的文档，此后**每一次提问都失败**，而提示里说的"等入库
                        # 完成"永远不会发生——没有进程在跑，只能人工 reindex 才
                        # 能恢复（2026-09-11 审查实测）。
                        #
                        # 对桌面应用来说"整体不可用"比"检索质量下降"严重得多：
                        # 这里改为丢掉 dense 路、用 BM25 继续服务，同时打一条
                        # ERROR 让用户知道该做什么。
                        logger.error(
                            "向量与语料不一致（%d 条向量 vs %d 个 chunk）——"
                            "本次启动降级为 BM25 单路检索（关键词可用、语义检索不可用）。"
                            "常见成因：入库中途被强制关闭，或切换过嵌入模型。"
                            "修复：运行 mikasa ingest --reindex 重建向量。",
                            len(ids),
                            len(expected),
                        )
                    else:
                        matrix = loaded_matrix
                        embedding_model = settings.embedding.model
            logger.debug(
                "索引重建完成：chunks=%d bm25 词表=%d dense=%s",
                len(chunks),
                bm25.vocabulary_size,
                "on" if matrix is not None else "off",
            )
            return Corpus(chunks=chunks, bm25=bm25, matrix=matrix, embedding_model=embedding_model)
