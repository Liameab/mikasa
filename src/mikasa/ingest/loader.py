"""格式分发入口：按扩展名路由到具体解析器（loaders 模块）。

CLI/服务层只依赖 load_document()，新增格式只需在 LOADERS 注册：
解析层与下游（分块/入库）零改动。
"""

from __future__ import annotations

from pathlib import Path

from mikasa.errors import IngestError
from mikasa.ingest.types import LoadedDocument

from .loaders import load_docx, load_markdown, load_pdf, load_txt

LOADERS: dict[str, object] = {
    ".md": load_markdown,
    ".markdown": load_markdown,
    ".txt": load_txt,
    ".pdf": load_pdf,
    ".docx": load_docx,
}


def load_document(path: Path) -> LoadedDocument:
    """按扩展名解析文档；未知类型/解析异常统一转 IngestError。"""
    suffix = path.suffix.lower()
    loader = LOADERS.get(suffix)
    if loader is None:
        raise IngestError(f"不支持的文件类型：{path.name}（支持：{'/'.join(sorted(LOADERS))}）")
    try:
        return loader(path)  # type: ignore[operator]
    except IngestError:
        raise
    except Exception as exc:
        raise IngestError(f"解析失败（{path.name}）：{type(exc).__name__}: {exc}") from exc
