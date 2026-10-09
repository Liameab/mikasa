"""结构化输出（JSON 承载 [n] 协议）的解析、校验与降级（ADR-0037）。

两条硬约束在这里被钉住：
  1. **协议不变**：编号仍是注入顺序、模型无权自造编号——所以 JSON 路径多出来的
     `chunk_id` 声明只用于校验与计数，引用仍从正文标记解析；
  2. **失败如实**：解析/校验不过关就返回 ok=False 并计入格式失败，不偷偷降级重试
     （失败率正是这次 A/B 要测的东西，洗白了实验就白做）。

全程零网络：假 LLM 直接回预置文本。
"""

from __future__ import annotations

import json

from mikasa.models.document import Chunk
from mikasa.models.retrieval import RetrievedChunk
from mikasa.pipeline.generator import Generator, parse_structured
from mikasa.pipeline.prompts import JSON_OUTPUT_CONTRACT, SYSTEM_PROMPT


def _hit(chunk_id: int, content: str = "原文片段") -> RetrievedChunk:
    return RetrievedChunk(
        chunk=Chunk(
            id=chunk_id,
            document_id=1,
            seq=chunk_id,
            content=content,
            content_sha256="x" * 8,
            heading_path="一节",
            page_number=1,
        ),
        rank=chunk_id,
    )


HITS = [_hit(3118), _hit(3119)]


def _payload(answer: str, citations: list[dict[str, int]]) -> str:
    return json.dumps({"answer": answer, "citations": citations}, ensure_ascii=False)


class _FakeLLM:
    """假生成端：回预置文本，记下收到的消息与是否被要求结构化。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.seen: list[list[dict[str, str]]] = []
        self.structured_calls: list[bool] = []

    def complete(self, messages, *, temperature, max_tokens, structured=False):  # noqa: ANN001, ANN201
        from mikasa.providers.llm import Completion

        self.seen.append(messages)
        self.structured_calls.append(structured)
        return Completion(text=self.text, prompt_tokens=11, completion_tokens=22)

    def stream(self, messages, *, temperature, max_tokens):  # noqa: ANN001, ANN201
        raise AssertionError("本文件的用例都走非流式路径")


# ---------------------------------------------------------------------------
# 解析与校验
# ---------------------------------------------------------------------------


def test_valid_payload_passes() -> None:
    parsed = parse_structured(
        _payload("答案 [1][2]", [{"marker": 1, "chunk_id": 3118}, {"marker": 2, "chunk_id": 3119}]),
        HITS,
    )
    assert parsed.ok and parsed.reason == ""
    assert parsed.text == "答案 [1][2]"
    assert parsed.declared == [1, 2]


def test_broken_payloads_are_reported_not_silenced() -> None:
    """六条失败路径，每条都要有自己的 reason（报告异常明细靠它定位）。"""
    cases = {
        "不是 JSON": "随便一段文本",
        "顶层不是对象": "[1, 2]",
        "缺字段": json.dumps({"answer": "只有正文"}),
        "marker 不是整数": _payload("答案 [1]", [{"marker": "1", "chunk_id": 3118}]),  # type: ignore[list-item]
        "编号越界": _payload("答案 [9]", [{"marker": 9, "chunk_id": 3118}]),
        "chunk_id 错配": _payload("答案 [1]", [{"marker": 1, "chunk_id": 999}]),
        "正文与声明不符": _payload("答案 [1]", []),
    }
    for label, raw in cases.items():
        parsed = parse_structured(raw, HITS)
        assert parsed.ok is False, label
        assert parsed.reason, label


def test_parse_failure_keeps_raw_text_visible() -> None:
    """解析失败时正文退化为原始输出——如实展示，不掩盖（评测也要能逐题复核）。"""
    parsed = parse_structured("这是模型忘了 JSON 的一段话", HITS)
    assert parsed.ok is False
    assert parsed.text == "这是模型忘了 JSON 的一段话"


def test_duplicate_markers_are_compared_as_sets() -> None:
    """正文重复引用同一编号是正常的（[1] 出现两次）——集合比对，不是多重集。"""
    parsed = parse_structured(
        _payload(
            "先 [1]，后 [1] 再 [2]",
            [{"marker": 1, "chunk_id": 3118}, {"marker": 2, "chunk_id": 3119}],
        ),
        HITS,
    )
    assert parsed.ok is True


# ---------------------------------------------------------------------------
# Generator 集成
# ---------------------------------------------------------------------------


def _settings(offline_settings, **llm_over):  # noqa: ANN001, ANN003
    """结构化开关打开的 settings：backend 换成 api，避免走 mock 档的退回分支。"""
    over = {"structured_output": True, "backend": "api", "model": "test-model"} | llm_over
    return offline_settings.model_copy(update={"llm": offline_settings.llm.model_copy(update=over)})


def test_structured_generate_marks_answer_and_resolves_citations(offline_settings) -> None:
    settings = _settings(offline_settings)
    llm = _FakeLLM(_payload("答案要点 [1]", [{"marker": 1, "chunk_id": 3118}]))
    answer, completion = Generator(settings, llm).generate("问", HITS, {1: "文档"})

    assert answer.structured is True and answer.format_ok is True
    assert answer.text == "答案要点 [1]"
    assert [c.chunk_id for c in answer.citations] == [3118]
    assert (answer.prompt_tokens, answer.completion_tokens) == (11, 22)
    assert completion.text.startswith("{")  # 原始输出未被就地改写


def test_structured_generate_counts_format_failure(offline_settings) -> None:
    settings = _settings(offline_settings)
    llm = _FakeLLM("模型忘了按 JSON 回")
    answer, _ = Generator(settings, llm).generate("问", HITS, {1: "文档"})

    assert answer.structured is True and answer.format_ok is False
    assert answer.text == "模型忘了按 JSON 回"  # 原文照给，不掩盖


def test_structured_prompt_differs_only_by_serialization(offline_settings) -> None:
    """两条路径的提示词差异必须可枚举：只多 JSON 契约 + 来源行的 chunk_id。"""
    llm = _FakeLLM(_payload("答案 [1]", [{"marker": 1, "chunk_id": 3118}]))
    Generator(_settings(offline_settings), llm).generate("问", HITS, {1: "文档"})
    structured_system, structured_user = llm.seen[0][0]["content"], llm.seen[0][1]["content"]

    marker_llm = _FakeLLM("答案 [1]")
    Generator(offline_settings, marker_llm).generate("问", HITS, {1: "文档"})
    marker_system, marker_user = marker_llm.seen[0][0]["content"], marker_llm.seen[0][1]["content"]

    assert structured_system == SYSTEM_PROMPT + JSON_OUTPUT_CONTRACT
    assert marker_system == SYSTEM_PROMPT
    assert "chunk_id=3118" in structured_user and "chunk_id=3118" not in marker_user
    # 除来源行外正文一字不差（差异只有序列化层，A/B 才是干净的对照）
    assert marker_user.replace("｜chunk_id=3118", "").replace("｜chunk_id=3119", "") == (
        structured_user.replace("｜chunk_id=3118", "").replace("｜chunk_id=3119", "")
    )


def test_structured_flag_is_passed_only_in_structured_mode(offline_settings) -> None:
    """`structured=True` 只出现在答案生成那一次调用上（辅助调用一律不带）。

    2026-09-27 实踩：开关挂在 provider 配置上时，查询翻译等辅助调用也被切成
    JSON 模式，整场跨语言第二路静默失效——这条钉住"只有答案生成要结构化"。
    """
    structured_llm = _FakeLLM(_payload("答案 [1]", [{"marker": 1, "chunk_id": 3118}]))
    Generator(_settings(offline_settings), structured_llm).generate("问", HITS, {1: "文档"})
    assert structured_llm.structured_calls == [True]

    marker_llm = _FakeLLM("答案 [1]")
    Generator(offline_settings, marker_llm).generate("问", HITS, {1: "文档"})
    assert marker_llm.structured_calls == [False]


def test_mock_profile_falls_back_to_marker_path(offline_settings) -> None:
    """offline（mock）档：开关打开也退回文本标记路径——零 LLM 调用的离线承诺。"""
    settings = offline_settings.model_copy(
        update={"llm": offline_settings.llm.model_copy(update={"structured_output": True})}
    )
    llm = _FakeLLM("答案 [1]")
    answer, _ = Generator(settings, llm).generate("问", HITS, {1: "文档"})

    assert answer.structured is False and answer.format_ok is None
    assert llm.seen[0][0]["content"] == SYSTEM_PROMPT  # 没带 JSON 契约
    assert "chunk_id=" not in llm.seen[0][1]["content"]


def test_stream_path_never_asks_for_json(offline_settings) -> None:
    """流式链路不解析 JSON，就**不能**要求 JSON 契约（ADR-0037 定的就是只非流式）。

    原来两条路共用 self._structured：配置打开结构化输出后走 Web 流式提问时，系统
    提示照样要求 JSON，而流式路径没有任何解析 → 用户会在聊天里看到一段裸 JSON
    （2026-10-05 审查）。
    """
    gen = Generator(offline_settings, _FakeLLM("x"))
    gen._structured = True  # 模拟"配置里打开了结构化输出"

    stream_msgs = gen._build_messages("问", HITS, {1: "文档"}, structured=False)
    assert JSON_OUTPUT_CONTRACT not in stream_msgs[0]["content"]
    assert JSON_OUTPUT_CONTRACT not in stream_msgs[1]["content"]

    # 非流式路径原样保留契约（chunk_id 声明与 JSON 要求都在）
    sync_msgs = gen._build_messages("问", HITS, {1: "文档"})
    assert JSON_OUTPUT_CONTRACT in sync_msgs[0]["content"]
    assert "chunk_id" in sync_msgs[1]["content"]
