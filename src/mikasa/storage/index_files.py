"""索引快照文件（index_dir/meta.json）的读写。

数据库行（documents/chunks/embeddings）是唯一事实源；
meta.json 只是派生快照，供 doctor 一致性体检与评测运行的语料指纹使用。
"""

from __future__ import annotations

import json
import time
from typing import Any

from mikasa.config.settings import Settings
from mikasa.utils.logging import get_logger

logger = get_logger("storage")

META_FILENAME = "meta.json"
_META_SCHEMA_VERSION = 1


def index_meta_path(settings: Settings):
    return settings.index_dir / META_FILENAME


def save_index_meta(
    settings: Settings,
    *,
    chunk_count: int,
    embedding_model: str | None,
    embedding_dim: int | None,
    corpus_sha256: str,
) -> dict[str, Any]:
    """写索引快照（幂等覆盖）。返回写入的元数据。"""
    meta = {
        "schema_version": _META_SCHEMA_VERSION,
        "chunk_count": chunk_count,
        "embedding_model": embedding_model,
        "embedding_dim": embedding_dim,
        "corpus_sha256": corpus_sha256,
        "updated_at": int(time.time()),
    }
    settings.index_dir.mkdir(parents=True, exist_ok=True)
    path = index_meta_path(settings)
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("索引快照已写入：%s", path)
    return meta


def load_index_meta(settings: Settings) -> dict[str, Any] | None:
    """读索引快照；不存在（尚未 ingest）返回 None。"""
    path = index_meta_path(settings)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("索引快照损坏：%s（%s）", path, exc)
        return None
