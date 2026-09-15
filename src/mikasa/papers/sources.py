"""论文检索来源协议与统一结果模型。

两个来源（arXiv / OpenAlex）返回的字段各说各话，这里归一成一份
`PaperResult`，前端与导入链路只认这一份。协议刻意极小：每个来源
只实现 search（列表）与 fetch（按 id 取单条，导入用）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PaperResult:
    """一篇论文的归一化元数据。"""

    source: str  # "arxiv" | "openalex"
    id: str  # arxiv: "2401.12345"（已去版本后缀）；openalex: "W1234567890"
    title: str
    authors: tuple[str, ...]
    year: int | None
    venue: str  # 期刊/会议名，无则 ""
    abstract: str  # 无则 ""
    doi: str  # 裸 DOI（"10.xxxx/yyy"），无则 ""
    pdf_url: str | None  # None = 没有开放获取全文（导入端点据此 409）
    landing_url: str  # 详情页（arXiv abs 页 / doi.org 链接）
    oa: bool


class PaperSource(Protocol):
    """论文来源的最小接口。"""

    name: str

    def search(self, q: str, start: int, count: int) -> tuple[list[PaperResult], bool]:
        """检索一页。

        返回 (结果, 是否取满 count)——取满即"后面可能还有"，由服务层
        换算成 has_more（不依赖各源不可靠的 total）。
        失败抛 PaperError（中文文案）。
        """
        ...

    def fetch(self, paper_id: str) -> PaperResult:
        """按 id 取单条元数据（导入时反查，不信客户端传的标题）。"""
        ...
