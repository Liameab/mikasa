"""索引管理器的降级与缓存语义。

这里守的是一条**可用性**底线：向量与语料对不上时，应用必须还能用（BM25 单路），
而不是每次提问都报错。成因很常见——入库到一半进程被强制关闭（关窗/崩溃/断电）
就会留下"chunk 已提交、向量还没写完"的文档。
"""

from __future__ import annotations

import numpy as np

from mikasa.index.manager import IndexManager
from mikasa.models.document import Chunk, Document
from mikasa.storage import repo
from mikasa.storage.db import open_db


def _seed(settings, chunks: int = 3) -> list[int]:
    with open_db(settings.db_path) as conn:
        doc_id = repo.insert_document(
            conn,
            Document(
                title="样本", file_path="样本.md", file_type="md", file_sha256="sha", char_count=1
            ),
        )
        repo.insert_chunks(
            conn,
            [
                Chunk(
                    document_id=doc_id,
                    seq=i,
                    content=f"第{i}块 注意力机制",
                    content_sha256=f"s{i}",
                    tokens=["注意", "力", "机制"],
                )
                for i in range(chunks)
            ],
        )
        return repo.chunk_ids_of_document(conn, doc_id)


def test_vector_mismatch_degrades_to_bm25_instead_of_failing(api_settings):
    """向量行数少于 chunk（入库中途被杀）→ 降级为 BM25 单路，而不是抛异常。

    原实现 raise StorageError，于是**每一次提问都失败**，而错误里说的"等入库
    完成"永远不会发生——没有进程在跑，只能人工 reindex 才能恢复。
    """
    ids = _seed(api_settings)
    with open_db(api_settings.db_path) as conn:
        # 只写前 2 个 chunk 的向量，模拟"第 3 个还没写完就被杀"
        repo.save_embeddings(
            conn, api_settings.embedding.model, ids[:2], np.zeros((2, 4), dtype=np.float32)
        )

    corpus = IndexManager(api_settings).corpus()  # 不得抛异常

    assert corpus.matrix is None, "向量对不上时必须丢掉 dense 路"
    assert len(corpus.chunks) == 3, "BM25 路仍要能服务全部 chunk"
    assert corpus.bm25 is not None


def test_matching_vectors_keep_dense_path(api_settings):
    """反向对照：向量齐备时必须正常启用 dense 路（别把降级写成永久降级）。"""
    ids = _seed(api_settings)
    with open_db(api_settings.db_path) as conn:
        repo.save_embeddings(
            conn, api_settings.embedding.model, ids, np.zeros((3, 4), dtype=np.float32)
        )

    corpus = IndexManager(api_settings).corpus()

    assert corpus.matrix is not None
    assert corpus.embedding_model == api_settings.embedding.model
