"""语义裁判单测：三行格式解析、位置交换一致性、拒答短路、NoJudge。

LLMJudge 只依赖 OpenAI 兼容文本接口，因此用替身 LLM 注入脚本化输出，
全程零网络；锁住的关键契约：
  - 解析三行「正确性评分: N/5」「档位: X」「忠实性: 是/否」，任一行
    解析失败 → consistent=False 并把裁判原文留痕（供人工复核）；
  - 位置交换两轮：分数不一致取 min（悲观口径），档位不一致记 "?"，
    两轮全部一致才采信（进一致性桶）；
  - 拒答样本不判语义分（evaluate 短路返回 None，不消耗裁判轮询）。
"""

from __future__ import annotations

import pytest

from mikasa.config.settings import JudgeConfig
from mikasa.eval.judge import LLMJudge, NoJudge
from mikasa.pipeline.prompts import REFUSAL_TEXT

# ---------------------------------------------------------------------------
# 替身 LLM：按脚本逐次吐出回复（每轮判题 = 一次 complete 调用）
# ---------------------------------------------------------------------------


class _ScriptedLLM:
    """替身 LLM：记录调用并依次返回脚本化文本；文本用尽后抛错（防静默吞调用）。"""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]], **kwargs: object) -> object:
        self.calls.append(messages)
        if not self.responses:
            raise AssertionError("脚本化 LLM 回复已用尽——调用次数与脚本不符")
        return _FakeCompletion(self.responses.pop(0))


class _FakeCompletion:
    def __init__(self, text: str) -> None:
        self.text = text


def _make_judge(
    monkeypatch: pytest.MonkeyPatch, responses: list[str]
) -> tuple[LLMJudge, _ScriptedLLM]:
    """把裁判的底层 LLM 换成脚本化替身（构造钩子一律返回同一个实例）。"""
    llm = _ScriptedLLM(responses)
    monkeypatch.setattr("mikasa.eval.judge.OpenAICompatLLM", lambda _config: llm)
    judge = LLMJudge(JudgeConfig(model="fake-judge", backend="api", temperature=0.0))
    return judge, llm


def _verdict_lines(score: int = 5, grade: str = "A", faithful: str = "是") -> str:
    return f"正确性评分: {score}/5\n档位: {grade}\n忠实性: {faithful}"


# ---------------------------------------------------------------------------
# 解析与一致性
# ---------------------------------------------------------------------------


def test_nojudge_always_none():
    nojudge = NoJudge()
    assert nojudge.model == "none"
    assert nojudge.evaluate("q", "notes", "ctx", "答") is None


def test_llm_judge_consistent_verdict(monkeypatch):
    both_yes = [_verdict_lines(5, "A", "是"), _verdict_lines(5, "A", "是")]
    judge, llm = _make_judge(monkeypatch, both_yes)
    verdict = judge.evaluate("问题？", "参考答案要点", "资料", "候选回答")
    assert verdict is not None
    assert verdict.correctness == 5
    assert verdict.grade == "A"
    assert verdict.faithful is True
    assert verdict.consistent is True
    assert len(llm.calls) == 2  # 位置交换两轮


def test_llm_judge_disagree_takes_pessimistic_min(monkeypatch):
    # 两轮分数不同 → 取较小值、档位置 "?"、consistent=False（不一致是信号不抹平）
    rounds = [_verdict_lines(4, "A", "是"), _verdict_lines(3, "B", "是")]
    judge, _llm = _make_judge(monkeypatch, rounds)
    verdict = judge.evaluate("问题？", "notes", "ctx", "答")
    assert verdict is not None
    assert verdict.correctness == 3
    assert verdict.grade == "?"
    assert verdict.consistent is False
    assert len(verdict.raw) == 2  # 原始输出完整留痕


def test_llm_judge_faithful_requires_both_rounds_yes(monkeypatch):
    # 任一轮说"否"→ 忠实性整体判否（只有两轮都是"是"才采信）
    rounds = [_verdict_lines(5, "A", "是"), _verdict_lines(5, "A", "否")]
    judge, _llm = _make_judge(monkeypatch, rounds)
    verdict = judge.evaluate("问题？", "notes", "ctx", "答")
    assert verdict is not None
    assert verdict.faithful is False
    assert verdict.consistent is False


def test_llm_judge_parse_failure_marks_inconsistent(monkeypatch):
    judge, _llm = _make_judge(monkeypatch, ["完全无法解析的输出", "也是乱码"])
    verdict = judge.evaluate("问题？", "notes", "ctx", "答")
    assert verdict is not None
    assert verdict.consistent is False
    assert verdict.correctness == 0  # 无分可采信，0 占位
    assert len(verdict.raw) == 2


def test_llm_judge_partial_round_failure_keeps_raw(monkeypatch):
    # 第二轮解析失败 → 同样判不一致，raw 保留两轮原文供人工复核
    judge, _llm = _make_judge(monkeypatch, [_verdict_lines(5, "A", "是"), "第二轮坏了"])
    verdict = judge.evaluate("问题？", "notes", "ctx", "答")
    assert verdict is not None
    assert verdict.consistent is False
    assert "第二轮坏了" in verdict.raw[1]


def test_llm_judge_skips_refusal_and_empty(monkeypatch):
    # 拒答/空答案短路：不判语义分也不消耗裁判轮询（正确性由拒答桶负责）
    judge, llm = _make_judge(monkeypatch, [])
    assert judge.evaluate("问题？", "notes", "ctx", REFUSAL_TEXT) is None
    assert judge.evaluate("问题？", "notes", "ctx", "   ") is None
    assert judge.evaluate("问题？", "notes", "ctx", "") is None
    assert llm.calls == []


# ---------------------------------------------------------------------------
# 提示词结构：位置交换轮次的素材顺序（防止"两轮其实一模一样"）
# ---------------------------------------------------------------------------


def test_prompt_swaps_answer_and_notes_position():
    prompt_a = LLMJudge._prompt("问题", "参考答案要点", "资料", "候选回答", answer_first=False)
    prompt_b = LLMJudge._prompt("问题", "参考答案要点", "资料", "候选回答", answer_first=True)
    assert prompt_a.index("候选回答") > prompt_a.index("参考答案要点")
    assert prompt_b.index("候选回答") < prompt_b.index("参考答案要点")


def test_system_prompt_demands_three_lines():
    # 忠实性行是"忠实性解析"能工作的前提（输出契约改动必须同步本测试）
    assert "忠实性: 是/否" in LLMJudge.SYSTEM
    assert "正确性评分: N/5" in LLMJudge.SYSTEM
    assert "档位: X" in LLMJudge.SYSTEM
