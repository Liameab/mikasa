"""文档加载的数据结构（loaders 与 chunker 之间的传递物）。"""

from __future__ import annotations

from dataclasses import dataclass

from mikasa.models.document import FileType


@dataclass(frozen=True)
class Para:
    """一个逻辑段落：附带其所属标题路径与页码（供溯源与结构分块）。"""

    text: str
    heading_path: str | None = None  # "1. 模型 > 1.2 注意力机制"
    page: int | None = None  # PDF 页码（1-based）；其他格式为 None


@dataclass(frozen=True)
class LoadedDocument:
    """一种格式解析完成后的统一中间表示。"""

    title: str
    file_type: FileType
    paragraphs: list[Para]

    @property
    def char_count(self) -> int:
        return sum(len(p.text) for p in self.paragraphs)
