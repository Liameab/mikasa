"""文档与分块数据模型（ingest 产物，SQLite 行对象）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FileType = Literal["pdf", "md", "txt", "docx"]
IngestStatus = Literal["pending", "done", "failed"]


class Document(BaseModel):
    """一个入库文档（对应 documents 表行）。

    folder_id（v3）是"组织归属"字段：读取路径从表行回填；写入不走
    INSERT（insert_document 的 SQL 保持列不变，v1/v2 老库无此列，迁移
    测试在旧 schema 上插行不崩），一律经 repo.move_document /
    set_document_title 的 UPDATE 落值（与 move_session 同构）。

    source_ref（v4）同构：形如 "arxiv:2401.12345" / "core:72543"，记录这篇
    文档是从哪个在线来源的哪条记录导进来的（NULL=普通上传/历史导入）。
    「找论文」页用它标"已在库中"（ADR-0020）。同样只经 UPDATE 写入。
    """

    model_config = ConfigDict(frozen=True)

    id: int | None = None
    title: str
    file_path: str  # 入库副本的绝对路径（uploads/ 下）
    file_type: FileType
    file_sha256: str  # 源文件哈希：增量重建判断依据
    char_count: int = 0
    chunk_count: int = 0
    ingest_status: IngestStatus = "pending"
    error_message: str | None = None
    folder_id: int | None = None  # 所属语料文件夹（NULL=根级）
    source_ref: str | None = None  # 在线来源标识 "source:id"（NULL=非导入）
    created_at: str | None = None
    updated_at: str | None = None


class Chunk(BaseModel):
    """检索与生成的最小单元（对应 chunks 表行）。

    tokens 是入库时预分词的结果（BM25 直接复用，检索不再分词）。
    """

    model_config = ConfigDict(frozen=True)

    id: int | None = None
    document_id: int
    seq: int  # 文档内顺序（用于上下文连贯性展示）
    content: str
    content_sha256: str  # 跨文档去重
    heading_path: str | None = None  # "1. 模型 > 1.2 注意力"
    page_number: int | None = None  # PDF 页码
    tokens: list[str] = Field(default_factory=list)

    @property
    def snippet(self) -> str:
        """UI/引用的截断摘要。"""
        return self.content[:200] + ("…" if len(self.content) > 200 else "")
