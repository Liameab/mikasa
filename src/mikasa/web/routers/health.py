"""健康检查：Web 首页第一屏的数据源（版本 / 模型 / 语料规模）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from mikasa import __version__
from mikasa.config.settings import Settings
from mikasa.storage import repo
from mikasa.storage.db import open_db
from mikasa.web.deps import get_settings

router = APIRouter(tags=["health"])


@router.get("/api/health")
def health(settings: Settings = Depends(get_settings)) -> dict:
    """服务概况：前端首页加载时拉一次，语料规模实时查库（不建索引）。"""
    with open_db(settings.db_path) as conn:
        doc_count = repo.count_documents(conn)
        chunk_count = repo.count_chunks(conn)
    return {
        "name": "Mikasa",
        "version": __version__,
        "profile": settings.profile,
        "llm_model": settings.llm.model,
        "embedding_model": settings.embedding.model,
        "reranker_backend": settings.reranker.backend,
        "documents": doc_count,
        "chunks": chunk_count,
    }
