"""稠密向量存储：numpy 精确（暴力）检索实现。

为什么不直接用 FAISS / hnswlib（详见 ADR-0010）：
- 二者在 Windows 上均无预编译 wheel（faiss-cpu 仅 conda；hnswlib 仅源码），
  与"零编译安装"约束冲突；
- 个人知识库规模（<10 万 chunk）numpy 矩阵乘实测 <500ms，精确检索无近似误差，
  评测里的 recall 是"真"recall，实验结论干净。
"""

from __future__ import annotations

import numpy as np

from mikasa.errors import StorageError


class ExactVectorStore:
    """精确余弦检索：行归一化矩阵 + 矩阵乘。

    add/search 均把向量按行归一化（L2），dot 即余弦相似度。
    内存：n × dim × 4 字节；1 万 chunk × 512 维 ≈ 20MB，个人库无压力。
    """

    def __init__(self, matrix: np.ndarray | None = None) -> None:
        self._matrix: np.ndarray | None = None
        if matrix is not None and matrix.shape[0] > 0:
            self._matrix = _normalize(matrix)
        self._dim = int(matrix.shape[1]) if matrix is not None and matrix.shape[0] > 0 else 0

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def size(self) -> int:
        return 0 if self._matrix is None else int(self._matrix.shape[0])

    def add(self, vectors: np.ndarray) -> None:
        if self._matrix is None:
            self._matrix = _normalize(vectors)
        else:
            self._matrix = np.concatenate([self._matrix, _normalize(vectors)], axis=0)
        self._dim = int(vectors.shape[1])

    def search(self, query: np.ndarray, top_k: int) -> list[tuple[int, float]]:
        """余弦相似度检索：返回 [(行号, 相似度), ...] 降序。"""
        if self._matrix is None or self._matrix.shape[0] == 0:
            return []
        q = query.astype(np.float32, copy=False)
        if q.shape != (self._dim,):
            # 维度不匹配是 embedding 模型切换/数据迁移事故，不是普通检索错：
            # 显式 StorageError（带 reindex 指引），不把 numpy 裸 ValueError
            # 漏到 CLI traceback（cli 只捕 StorageError/ConfigError）
            raise StorageError(
                f"查询向量维度 {q.shape[0]} ≠ 库内向量维度 {self._dim}：embedding 模型"
                "已切换？不同模型的维度不可混用——请执行 mikasa ingest --reindex"
            )
        norm = np.linalg.norm(q)
        if norm == 0.0:
            return []
        q = q / norm
        scores = self._matrix @ q  # (n,) 已归一化，dot 即余弦
        if top_k <= 0 or top_k >= scores.size:
            top_k = int(scores.size)
        # argpartition 前 top_k（无序）后按分数降序排，O(n + k log k)
        idx = np.argpartition(scores, -top_k)[-top_k:]
        idx = idx[np.argsort(scores[idx])[::-1]]
        return [(int(i), float(scores[i])) for i in idx]


def _normalize(vectors: np.ndarray) -> np.ndarray:
    """按行 L2 归一化（float32，行全零向量置零向量，避免除零）。"""
    matrix = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return matrix / norms
