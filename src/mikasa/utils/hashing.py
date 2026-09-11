"""内容哈希：文件级（增量重建判断）与文本级（去重、语料指纹）。"""

from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_file(path: Path, chunk_bytes: int = 1 << 20) -> str:
    """流式读取计算文件 SHA-256（大文件内存友好）。"""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk_bytes):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    """计算文本 SHA-256（UTF-8 编码）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
