"""在线论文检索（M7）：来源协议 + arXiv/OpenAlex 实现 + 安全下载。

给 Web 层用的三件套：
- `search(q, source, offset, limit) -> PaperPage`：双源交错分页、逐源降级；
- `fetch_paper(source, id) -> PaperResult`：导入时按 id 反查元数据；
- `download_pdf(url, dest) -> int`：SSRF 校验 + 类型/大小/重定向防护。

设计取舍见 docs/design-decisions.md ADR-0019。
"""

from mikasa.papers.errors import PaperError
from mikasa.papers.service import SOURCES, PaperPage, fetch_paper, search
from mikasa.papers.sources import PaperResult, PaperSource

__all__ = [
    "PaperError",
    "PaperPage",
    "PaperResult",
    "PaperSource",
    "SOURCES",
    "fetch_paper",
    "search",
]
