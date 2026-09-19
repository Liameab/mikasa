"""论文检索来源协议与统一结果模型。

三个来源（arXiv / OpenAlex / CORE）返回的字段各说各话，这里归一成一份
`PaperResult`，前端与导入链路只认这一份。协议刻意极小：每个来源
只实现 search（列表）与 fetch（按 id 取单条，导入用）。

**能力声明（`SourceCaps`）是这一层的核心纪律**：各来源支持哪些筛选/排序
只有它自己知道，前端据此禁用或标注选项——"给了选项却不生效"比"不支持"
更坏（CORE 的 `yearFrom` 参数就是活例子：返回 200 但静默忽略）。能力值
**只以实测为准**，没测过的一律 False（见 ADR-0020）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PaperFilters:
    """一次检索的跨源筛选条件（各来源按自己的能力翻译，翻译矩阵见 service.py）。

    日期是完整 ISO 日期（"2021-03-15"）而不是年份：界面给的是日期选择器，
    用户选了哪一天就用哪一天——只取年份等于悄悄丢掉一半输入（同 ADR-0020
    的能力对齐纪律）。各来源都支持到日（arXiv 走时间戳、OpenAlex 走
    from/to_publication_date），CORE 只到年（取前四位，能力已如实声明）。
    """

    date_from: str | None = None
    date_to: str | None = None
    oa_only: bool = False
    language: str | None = None
    sort: str = "relevance"  # relevance | cited | recent


@dataclass(frozen=True)
class SourceCaps:
    """来源能力声明（前端按"所选来源的交集"决定哪些选项可点）。

    取值全部来自 2026-09-16 的本机实测：
    - `year`：年份区间能否过滤（CORE 只能走查询语法，照样算 True）；
    - `cited_sort` / `recent_sort`：能否按被引/时间排序；
    - `language`：能否按语言过滤；
    - `oa`：开放获取的可控性——`always`（天然全 OA，**选项视为已满足，
      不产生降级提示**）/ `filterable`（可按 OA 过滤）/ `never`（无 OA）。
    """

    year: bool = False
    cited_sort: bool = False
    recent_sort: bool = False
    language: bool = False
    oa: str = "filterable"


@dataclass(frozen=True)
class PaperResult:
    """一篇论文的归一化元数据。"""

    source: str  # "arxiv" | "openalex" | "core"
    id: str  # arxiv: "2401.12345"（已去版本后缀）；openalex: "W1234567890"；core: "72543"
    title: str
    authors: tuple[str, ...]
    year: int | None
    venue: str  # 期刊/会议名，无则 ""
    abstract: str  # 无则 ""
    doi: str  # 裸 DOI（"10.xxxx/yyy"），无则 ""
    pdf_url: str | None  # None = 没有开放获取全文（导入端点据此 409）
    landing_url: str  # 详情页（arXiv abs 页 / doi.org 链接）
    oa: bool
    # 被引次数：None = 该来源不提供（字段本身缺失或该条无数据）。
    # **0 与 None 是两回事**：0 = 确实没人引，None = 不知道，前端文案不同
    cited_by: int | None = None
    language: str = ""  # 语言代码（"en"/"zh"），无则 ""


class PaperSource(Protocol):
    """论文来源的最小接口。"""

    name: str  # 注册表键，同时是 source_ref 的前半段（"arxiv:2401.12345"）
    label: str  # 界面展示名（"arXiv" / "OpenAlex" / "CORE"）
    caps: SourceCaps

    def search(
        self,
        q: str,
        start: int,
        count: int,
        *,
        filters: PaperFilters | None = None,
    ) -> tuple[list[PaperResult], int | None]:
        """检索一页。

        返回 (结果, 上游命中总数)——第二项**只用于展示规模**（"命中 N 条"），
        翻页信号仍由服务层按"取满 count"判断：各源报的 total 在带筛选时常常
        虚高，拿它算 has_more 会翻出空页（2026-09-16 的教训）。拿不到总数
        就返回 None，界面不显示，不假装。
        `filters` 只按 `caps` 声明的能力翻译，**不支持的一律忽略且不得
        假装生效**（由服务层记进 notes）。
        失败抛 PaperError（中文文案）。
        """
        ...

    def fetch(self, paper_id: str) -> PaperResult:
        """按 id 取单条元数据（导入时反查，不信客户端传的标题）。"""
        ...
