"""生成器：证据注入 → 大模型生成 → 引用标记解析与校验。

三层引用校验的第一步（L1 硬校验）在这里完成：
  - 编号合法性：正文出现的 [n] 必须落在资料编号 1..N 内，
    越界/畸形标记被丢弃并计数（格式解析失败率 = 评测回归必看项）；
  - L2（语义支持度判据）与 L3（拒答不得带引用）在评测层完成
    （见 eval/judge 与 docs/architecture.md"防幻觉的三层防线"）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mikasa.config.settings import Settings
from mikasa.models.answer import Answer, Citation
from mikasa.models.retrieval import RetrievedChunk
from mikasa.pipeline.prompts import (
    JSON_OUTPUT_CONTRACT,
    REFUSAL_TEXT,
    SOURCE_CHUNK_ID_TEMPLATE,
    SYSTEM_PROMPT,
    TABLE_ENUM_OUTPUT_CONTRACT,
    build_user_message,
)
from mikasa.utils.logging import get_logger

if TYPE_CHECKING:  # 仅类型标注用：避免 providers→pipeline 的潜在循环
    from mikasa.providers.llm import Completion, LLMProvider

logger = get_logger("pipeline")

_MARKER_RE = re.compile(r"\[(\d+)\]")


def extract_markers(text: str) -> list[int]:
    """按出现顺序提取全部 [n] 标记（含重复；"[1, 2]" 等畸形形式不匹配）。

    评测的"引用格式解析失败率"以它为准：合法编号数 / 全部 [n] 数。
    """
    return [int(m) for m in _MARKER_RE.findall(text)]


@dataclass(frozen=True)
class StructuredParse:
    """结构化输出的解析结果（ADR-0037）。`ok=False` 时 reason 说明是哪一条不过。"""

    text: str  # answer 字段；解析失败时退化为原始输出（如实展示，不掩盖）
    ok: bool
    reason: str = ""
    declared: list[int] = field(default_factory=list)  # citations 里声明的编号
    text_markers: list[int] = field(default_factory=list)  # 正文里实际出现的编号


def parse_structured(raw: str, hits: list[RetrievedChunk]) -> StructuredParse:
    """解析并校验结构化输出；**任何一条不过关都如实返回 ok=False + 原因**。

    三条校验都是"协议不变"的具体化（编号是注入顺序、模型无权自造编号）：

      1. 是合法 JSON 对象，且 `answer` 是字符串、`citations` 是列表；
      2. 每条引用 `marker ∈ 1..len(hits)`，且 `chunk_id` 等于
         `hits[marker-1].chunk.id`——编号不得越界，也不得把编号指到别的块；
      3. 正文里的 `[n]` 与 `citations` 声明的编号集合一致（声明与正文不符是
         结构化路径特有的失败模式，要能被数出来，而不是被顺手抹平）。

    注意：失败时**不偷偷降级重试**——那等于把实验数据洗白（失败率正是要测的东西）。
    引用仍然从正文标记解析（marker 才是绑定证据的那个），声明数组只用于校验与计数。
    """
    text_markers: list[int] = []
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        return StructuredParse(text=raw, ok=False, reason=f"JSON 解析失败：{exc}")
    if not isinstance(payload, dict):
        return StructuredParse(text=raw, ok=False, reason="顶层不是 JSON 对象")
    answer_text = payload.get("answer")
    citations = payload.get("citations")
    if not isinstance(answer_text, str) or not isinstance(citations, list):
        return StructuredParse(text=raw, ok=False, reason="缺 answer(string) 或 citations(list)")

    text_markers = extract_markers(answer_text)
    declared: list[int] = []
    for index, entry in enumerate(citations):
        if not isinstance(entry, dict):
            return StructuredParse(
                text=answer_text, ok=False, reason=f"citations[{index}] 不是对象"
            )
        marker, chunk_id = entry.get("marker"), entry.get("chunk_id")
        if isinstance(marker, bool) or not isinstance(marker, int):
            return StructuredParse(
                text=answer_text, ok=False, reason=f"citations[{index}].marker 不是整数"
            )
        if not 1 <= marker <= len(hits):
            return StructuredParse(
                text=answer_text,
                ok=False,
                reason=f"编号越界：marker={marker}（共 {len(hits)} 条资料）",
            )
        expected = hits[marker - 1].chunk.id
        if chunk_id != expected:
            return StructuredParse(
                text=answer_text,
                ok=False,
                reason=f"chunk_id 错配：marker={marker} 声明 {chunk_id}，实际 {expected}",
            )
        declared.append(marker)

    if sorted(set(text_markers)) != sorted(set(declared)):
        return StructuredParse(
            text=answer_text,
            ok=False,
            reason=(
                f"正文与声明不符：正文 {sorted(set(text_markers))} vs 声明 {sorted(set(declared))}"
            ),
            declared=declared,
            text_markers=text_markers,
        )
    return StructuredParse(text=answer_text, ok=True, declared=declared, text_markers=text_markers)


class Generator:
    """把检索命中的 chunk 包装为带编号的资料片段，调用 LLM 并解析引用。"""

    def __init__(self, settings: Settings, llm: LLMProvider) -> None:
        self.settings = settings
        self._llm = llm
        # 结构化输出只在"真模型"下成立：mock 档的零 LLM 调用是离线承诺
        # （offline profile 的纪律，见 ADR-0014），它产不出 JSON——退回标记路径
        # 并留痕，与 crosslingual 翻译失败退回单路是同一种做法。
        self._structured = bool(settings.llm.structured_output)
        if self._structured and settings.llm.backend == "mock":
            logger.debug("结构化输出需要真实模型：mock（offline）档本次退回文本标记路径")
            self._structured = False

    # ------------------------------------------------------------------

    def generate(
        self,
        question: str,
        hits: list[RetrievedChunk],
        titles: dict[int, str],
        history_summary: str | None = None,
        context: str | None = None,
    ) -> tuple[Answer, Completion]:
        """生成一次回答。

        参数：
            hits: 检索命中（行序即注入顺序，marker = 位次 + 1）
            titles: document_id -> 文档标题（引用展示）
            context: 阅读器里用户选中的原文（可空；进提示词，不参与引用编号）
        返回：
            (Answer, Completion)：Answer 供展示/落库，Completion 供评测取用量。
        """
        messages = self._build_messages(
            question, hits, titles, history_summary=history_summary, context=context
        )
        if self._structured:
            # structured 只在**答案生成**这一次调用上传（协议层要的是 JSON 承载）；
            # 翻译/提炼那些辅助调用若也变成 JSON 模式，跨语言第二路会整场静默失效
            # （2026-09-27 实测）。条件式传参顺带保证：不开结构化时，调用签名与
            # 既有测试替身逐字一致。
            completion = self._llm.complete(
                messages,
                temperature=self.settings.llm.temperature,
                max_tokens=self.settings.llm.max_tokens,
                structured=True,
            )
        else:
            completion = self._llm.complete(
                messages,
                temperature=self.settings.llm.temperature,
                max_tokens=self.settings.llm.max_tokens,
            )
        raw = completion.text
        # 非流式路径保留真实 token 用量（成本核算依赖）；流式路径拿不到
        # usage，走 build_answer 默认 None
        structured = False
        format_ok: bool | None = None
        if self._structured:
            parsed = parse_structured(raw, hits)
            structured, format_ok = True, parsed.ok
            if not parsed.ok:
                # 失败**如实计数**（评测读 format_ok），不重试、不掩盖：拿原文当正文，
                # 其余链路照走——看到的就是"格式坏了"这个事实，而不是被洗白的数据。
                # 失败率正是这次实验要测的东西（ADR-0037）。
                logger.warning("结构化输出不合契约（%s）：本题计入格式失败", parsed.reason)
            raw = parsed.text
        answer = self.build_answer(
            raw,
            question,
            hits,
            titles,
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
            structured=structured,
            format_ok=format_ok,
        )
        return answer, completion

    # ------------------------------------------------------------------

    def _build_messages(
        self,
        question: str,
        hits: list[RetrievedChunk],
        titles: dict[int, str],
        history_summary: str | None = None,
        context: str | None = None,
        note: str | None = None,
    ) -> list[dict[str, str]]:
        """证据注入的提示词组装：generate / stream_text 共用同一构造。

        结构化路径（`llm.structured_output`）只改三处：系统提示追加 JSON 契约、
        来源行多一个 chunk_id 供模型抄写、其余逐字相同——差异必须**只有序列化层**。
        note 是「盘点表格」轮的注入说明（可空），只被流式链路传入。
        """
        sources = [
            (i + 1, self._describe(hit, titles, with_chunk_id=self._structured), hit.chunk.content)
            for i, hit in enumerate(hits)
        ]
        user_message = build_user_message(
            question, sources, history_summary=history_summary, context=context, note=note
        )
        system = SYSTEM_PROMPT + JSON_OUTPUT_CONTRACT if self._structured else SYSTEM_PROMPT
        if note is not None:
            # 盘点轮追加输出契约（与 JSON 契约同机制）：压住规则 6 的展开倾向，
            # 行首强制 [N]（服务端另有 _normalize_cite_echo_stream 兜底抄来源行）
            system += TABLE_ENUM_OUTPUT_CONTRACT
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user_message},
        ]

    def stream_text(
        self,
        question: str,
        hits: list[RetrievedChunk],
        titles: dict[int, str],
        history_summary: str | None = None,
        context: str | None = None,
        note: str | None = None,
    ) -> Iterator[str]:
        """流式补全：按 LLM 的增量逐段产出正文文本（Web SSE 的数据源）。

        代价：OpenAI 兼容流式响应默认不带 usage，本路径拿不到 token
        用量（AskService.ask_stream 的 done 事件里两字段为 None）；
        CLI / 评测的非流式 generate 不受影响。
        """
        messages = self._build_messages(
            question, hits, titles, history_summary=history_summary, context=context, note=note
        )
        yield from self._llm.stream(
            messages,
            temperature=self.settings.llm.temperature,
            max_tokens=self.settings.llm.max_tokens,
        )

    def build_answer(
        self,
        text: str,
        question: str,
        hits: list[RetrievedChunk],
        titles: dict[int, str],
        *,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        structured: bool = False,
        format_ok: bool | None = None,
    ) -> Answer:
        """把生成文本组装为 Answer（引用解析 + 拒答判定）。

        generate 与流式路径共用：同一段文本经它得到的 Answer 完全一致，
        保证"流式各 delta 拼接"与"一次性生成"的引用/拒答口径相同。
        `structured` / `format_ok` 由结构化路径传入（流式路径恒为默认值）——
        引用仍然从**正文标记**解析：编号才是绑定证据的那个，citations 数组
        是显式声明，只用于校验与计数（ADR-0037）。
        """
        return Answer(
            question=question,
            text=text,
            citations=self._resolve_citations(text, hits, titles),
            refused=REFUSAL_TEXT in text,
            model=self.settings.llm.model,
            latency_ms={},
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            structured=structured,
            format_ok=format_ok,
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _describe(
        hit: RetrievedChunk, titles: dict[int, str], *, with_chunk_id: bool = False
    ) -> str:
        """来源描述：《标题》｜章节（页码）[｜chunk_id=N]——进提示词的溯源文案。

        chunk_id 只在结构化路径注入（契约要求模型原样抄写）；标记路径不给它，
        两条路径的提示词差异因此可枚举。
        """
        chunk = hit.chunk
        doc = titles.get(chunk.document_id or -1, "未知文档")
        parts = [f"《{doc}》"]
        if chunk.heading_path:
            parts.append(chunk.heading_path)
        if chunk.page_number:
            parts.append(f"第 {chunk.page_number} 页")
        if with_chunk_id:
            parts.append(SOURCE_CHUNK_ID_TEMPLATE.format(chunk_id=chunk.id))
        return "｜".join(parts)

    @staticmethod
    def _resolve_citations(
        text: str, hits: list[RetrievedChunk], titles: dict[int, str]
    ) -> list[Citation]:
        """把正文标记解析为引用表：去重、按首次出现排序、越界丢弃。

        返回的引用即通过 L1 校验的集合；丢弃的畸形/越界计数由评测层
        通过 extract_markers 与 len(hits) 自行核算。
        """
        citations: list[Citation] = []
        seen: set[int] = set()
        for marker in extract_markers(text):
            if marker in seen or not (1 <= marker <= len(hits)):
                continue
            seen.add(marker)
            chunk = hits[marker - 1].chunk
            citations.append(
                Citation(
                    marker=marker,
                    chunk_id=chunk.id or -1,
                    document_title=titles.get(chunk.document_id, "未知文档"),
                    section=chunk.heading_path,
                    page=chunk.page_number,
                    snippet=chunk.snippet,
                )
            )
        return citations
