"""论断级忠实性（claim 级 L2）的单测：拆解/验证解析 + 核对器 + 构造开关。

替身 LLM 按队列回文（拆解 → 验证），零网络。
"""

from __future__ import annotations

import pytest

from mikasa.config.settings import JudgeConfig
from mikasa.eval.claims import (
    ClaimChecker,
    _parse_claims,
    _parse_verdicts,
)
from mikasa.providers.llm import Completion

ANSWER = (
    "L2 正则化在损失中加入权重的平方和惩罚项 [1]。\n"
    "AdamW 优化器把权重衰减从梯度更新里拆出来 [2]。\n"
    "这是目前最常用的做法。"
)
BY_MARKER = {
    1: "L2 正则化在损失中加入权重的平方和惩罚项，鼓励小而分散的权重。",
    2: "AdamW 优化器使用解耦的权重衰减，把权重衰减从梯度更新里拆出来单独施加。",
}


class _ScriptedLLM:
    """脚本替身：按队列回文，记录调用（拆解 → 验证）。"""

    def __init__(self, texts: list[str], *, boom: bool = False) -> None:
        self.texts = list(texts)
        self.calls: list[list[dict[str, str]]] = []
        self._boom = boom

    def complete(self, messages, *, temperature, max_tokens) -> Completion:
        if self._boom:
            raise RuntimeError("替身服务不可用")
        self.calls.append(messages)
        text = self.texts.pop(0) if self.texts else "（脚本耗尽）"
        return Completion(text=text, prompt_tokens=1, completion_tokens=1)


DECOMPOSED = (
    "[1] L2 正则化在损失中加入权重的平方和惩罚项\n"
    "[2] AdamW 把权重衰减从梯度更新里拆出来\n"
    "[] 这是目前最常用的做法"
)


def _checker(texts: list[str], *, boom: bool = False) -> ClaimChecker:
    checker = ClaimChecker(JudgeConfig(backend="api", model="fake-judge", api_key_env=""))
    checker._llm = _ScriptedLLM(texts, boom=boom)  # 注入替身（构造期只建客户端）
    return checker


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def test_parse_claims_extracts_markers_and_tolerates_bare_lines():
    claims, truncated = _parse_claims("[1] A 是 B\n[2,3] C 是 D\n没有方括号的断言")
    assert [(c.text, c.markers) for c in claims] == [
        ("A 是 B", [1]),
        ("C 是 D", [2, 3]),
        ("没有方括号的断言", []),
    ]
    assert not truncated


def test_parse_claims_caps_at_ten_and_flags_truncation():
    claims, truncated = _parse_claims("\n".join(f"[1] 断言{i}" for i in range(12)))
    assert len(claims) == 10 and truncated


def test_parse_verdicts_reads_both_colons_and_skips_unknown():
    assert _parse_verdicts("1: 是\n2：否\n3: 也许") == {1: True, 2: False}


def test_parse_verdicts_tolerates_list_numbering():
    """有序列表写法。2026-09-30 实测的坑：Qwen 默认把逐条判断写成 `1. 是`，
    只认冒号的解析器把 20 题里的 16 题整批记成"未判定"——看着像模型没答，
    实际是解析器没读。"""
    assert _parse_verdicts("1. 是\n2. 否\n3、是\n4）否\n5 是") == {
        1: True,
        2: False,
        3: True,
        4: False,
        5: True,
    }
    # 行首是断言文字（不是"数字 + 结论"）的行不该被当成结论
    assert _parse_verdicts("资料1 里写的是：是") == {}


# ---------------------------------------------------------------------------
# 核对器
# ---------------------------------------------------------------------------


def test_evaluate_counts_supported_unsupported_and_uncited():
    checker = _checker([DECOMPOSED, "1: 是\n2: 否"])
    report = checker.evaluate(ANSWER, BY_MARKER)
    assert report is not None
    assert [c.supported for c in report.claims] == [True, False, None]
    assert (report.supported, report.unsupported, report.uncited, report.undecided) == (1, 1, 1, 0)
    # 第二条调用（验证）：资料只给用到的引用块，断言只列带引用的那两条
    verify_user = checker._llm.calls[1][1]["content"]
    assert "【资料1】" in verify_user and "【资料2】" in verify_user
    assert "这是目前最常用的做法" not in verify_user  # 无引用断言不进验证
    assert "1. L2 正则化在损失中加入权重的平方和惩罚项" in verify_user


def test_evaluate_keeps_undecided_claims_out_of_rate_denominator():
    """验证行缺失 → 该断言 undecided（不是"不忠实"；与裁判"没测到≠不忠实"同款）。"""
    checker = _checker([DECOMPOSED, "1: 是"])
    report = checker.evaluate(ANSWER, BY_MARKER)
    assert report is not None
    assert (report.supported, report.unsupported, report.undecided) == (1, 0, 1)


def test_evaluate_skips_verification_call_when_no_citations():
    """整段回答一条引用都没有：不发验证调用，全按无引用断言披露。"""
    checker = _checker(["[ ] 第一件事\n[ ] 第二件事"])
    report = checker.evaluate("第一件事。第二件事。", BY_MARKER)
    assert report is not None
    assert len(checker._llm.calls) == 1  # 只有拆解那一次
    assert report.uncited == 2 and report.undecided == 0


def test_evaluate_returns_none_for_refusal_or_empty():
    from mikasa.pipeline.prompts import REFUSAL_TEXT

    checker = _checker([])
    assert checker.evaluate("", BY_MARKER) is None
    assert checker.evaluate(REFUSAL_TEXT, BY_MARKER) is None
    assert checker._llm.calls == []


def test_evaluate_discloses_unparseable_decomposition_instead_of_dropping_it():
    """拆解输出不像断言：**不静默丢**——按"无引用断言"如实披露（宁可见勿猜）。

    只有**空输出**才算拆解失败（下一例）。
    """
    checker = _checker(["（这是一段没有断言行的话）"])
    report = checker.evaluate(ANSWER, BY_MARKER)
    assert report is not None and report.uncited == 1


def test_evaluate_returns_none_when_decomposition_is_empty():
    checker = _checker(["", ""])
    assert checker.evaluate(ANSWER, BY_MARKER) is None


def test_evaluate_propagates_provider_error_to_caller():
    """调用异常由调用方（runner）逐题兜底——与 LLMJudge 同款分工。"""
    checker = _checker([DECOMPOSED], boom=True)
    with pytest.raises(RuntimeError):
        checker.evaluate(ANSWER, BY_MARKER)


# ---------------------------------------------------------------------------
# 构造开关
# ---------------------------------------------------------------------------


def test_build_claim_checker_respects_switches(monkeypatch):
    from mikasa.config.settings import load_settings
    from mikasa.eval.runner import build_claim_checker

    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-test")
    base = load_settings("api")

    def variant(**judge_fields):
        return base.model_copy(update={"judge": base.judge.model_copy(update=judge_fields)})

    assert build_claim_checker(variant(claims=False)) is None  # 默认关
    assert build_claim_checker(variant(claims=True, enabled=False)) is None  # 无裁判
    assert build_claim_checker(variant(claims=True, enabled=True)) is not None
    # 缺密钥：与 build_judge 同款"静默不判"
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    assert build_claim_checker(variant(claims=True, enabled=True)) is None
