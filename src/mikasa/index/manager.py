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
from mikasa.errors import StorageError
from mikasa.index.bm25 import BM25Index
from mikasa.index.tokenizer import get_tokenizer
from mikasa.models.document import Chunk
from mikasa.storage.db import open_db
from mikasa.storage.repo import all_chunks_ordered, count_chunks, load_embedding_matrix
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


class IndexManager:
    """懒重建的索引访问门面。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._corpus: Corpus | None = None
        self._count_seen: int = -1

    def invalidate(self) -> None:
        """数据变更后调用（ingest/删除文档后）。"""
        self._corpus = None
        self._count_seen = -1

    def corpus(self) -> Corpus:
        """当前快照：行数未变则复用缓存，否则重建。"""
        with open_db(self._settings.db_path) as conn:
            count = count_chunks(conn)
        if self._corpus is not None and count == self._count_seen:
            return self._corpus
        self._corpus = self._rebuild()
        self._count_seen = count
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
                        raise StorageError(
                            "向量矩阵与 chunk 表不一致（可能是嵌入中断/模型切换残留）。"
                            "请运行 mikasa ingest --reindex 重建。"
                        )
                    matrix = loaded_matrix
                    embedding_model = settings.embedding.model
            logger.debug(
                "索引重建完成：chunks=%d bm25 词表=%d dense=%s",
                len(chunks),
                bm25.vocabulary_size,
                "on" if matrix is not None else "off",
            )
            return Corpus(chunks=chunks, bm25=bm25, matrix=matrix, embedding_model=embedding_model)
