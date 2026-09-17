"""在线论文检索（M7）：来源协议 + arXiv/OpenAlex/CORE 实现 + 安全下载。

给 Web 层用的四件套：
- `search(q, sources, offset, limit, filters) -> PaperPage`：多源轮转交错
  分页、逐源降级、能力降级说明（notes）；
- `fetch_paper(source, id) -> PaperResult`：导入时按 id 反查元数据；
- `download_pdf(url, dest) -> int`：SSRF 校验 + 类型/大小/重定向防护；
- `source_catalog() -> list[dict]`：来源目录与能力声明（前端渲染筛选用）。

设计取舍见 docs/design-decisions.md ADR-0019 / ADR-0020。
"""

from mikasa.papers.errors import PaperError
from mikasa.papers.service import SOURCES, PaperPage, fetch_paper, search, source_catalog
from mikasa.papers.sources import PaperFilters, PaperResult, PaperSource, SourceCaps

__all__ = [
    "PaperError",
    "PaperFilters",
    "PaperPage",
    "PaperResult",
    "PaperSource",
    "SOURCES",
    "SourceCaps",
    "fetch_paper",
    "search",
    "source_catalog",
]
