"""生成器：证据注入 → 大模型生成 → 引用标记解析与校验。

三层引用校验的第一步（L1 硬校验）在这里完成：
  - 编号合法性：正文出现的 [n] 必须落在资料编号 1..N 内，
    越界/畸形标记被丢弃并计数（格式解析失败率 = 评测回归必看项）；
  - L2（语义支持度判据）与 L3（拒答不得带引用）在评测层完成
    （见 eval/judge 与 docs/architecture.md"防幻觉的三层防线"）。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import TYPE_CHECKING

from mikasa.config.settings import Settings
from mikasa.models.answer import Answer, Citation
from mikasa.models.retrieval import RetrievedChunk
from mikasa.pipeline.prompts import REFUSAL_TEXT, SYSTEM_PROMPT, build_user_message
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


class Generator:
    """把检索命中的 chunk 包装为带编号的资料片段，调用 LLM 并解析引用。"""

    def __init__(self, settings: Settings, llm: LLMProvider) -> None:
        self.settings = settings
        self._llm = llm

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
        completion = self._llm.complete(
            messages,
            temperature=self.settings.llm.temperature,
            max_tokens=self.settings.llm.max_tokens,
        )
        # 非流式路径保留真实 token 用量（成本核算依赖）；流式路径拿不到
        # usage，走 build_answer 默认 None
        answer = self.build_answer(
            completion.text,
            question,
            hits,
            titles,
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
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
    ) -> list[dict[str, str]]:
        """证据注入的提示词组装：generate / stream_text 共用同一构造。"""
        sources = [
            (i + 1, self._describe(hit, titles), hit.chunk.content) for i, hit in enumerate(hits)
        ]
        user_message = build_user_message(
            question, sources, history_summary=history_summary, context=context
        )
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]

    def stream_text(
        self,
        question: str,
        hits: list[RetrievedChunk],
        titles: dict[int, str],
        history_summary: str | None = None,
        context: str | None = None,
    ) -> Iterator[str]:
        """流式补全：按 LLM 的增量逐段产出正文文本（Web SSE 的数据源）。

        代价：OpenAI 兼容流式响应默认不带 usage，本路径拿不到 token
        用量（AskService.ask_stream 的 done 事件里两字段为 None）；
        CLI / 评测的非流式 generate 不受影响。
        """
        messages = self._build_messages(
            question, hits, titles, history_summary=history_summary, context=context
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
    ) -> Answer:
        """把生成文本组装为 Answer（引用解析 + 拒答判定）。

        generate 与流式路径共用：同一段文本经它得到的 Answer 完全一致，
        保证"流式各 delta 拼接"与"一次性生成"的引用/拒答口径相同。
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
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _describe(hit: RetrievedChunk, titles: dict[int, str]) -> str:
        """来源描述：《标题》｜章节（页码）——进提示词的溯源文案。"""
        chunk = hit.chunk
        doc = titles.get(chunk.document_id or -1, "未知文档")
        parts = [f"《{doc}》"]
        if chunk.heading_path:
            parts.append(chunk.heading_path)
        if chunk.page_number:
            parts.append(f"第 {chunk.page_number} 页")
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
