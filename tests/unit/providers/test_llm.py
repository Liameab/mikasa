"""MockLLM 单测：解析对偶、有据判定、拒答与引用组织。

MockLLM 的"有据判定"经历过一次真实返工（字符重叠率 → 最长公共
连续子串），原因与判定语义见 providers/llm.py 的类 docstring；
这里锁死两类回归：
  1. 语料内主题问题（专名 ≥4 字连续同现）必须可答并带引用；
  2. 语料外问题（只有通用字/短片段同现）必须拒答——"如何在一周内
     学会做菠萝包"曾因与 AI 语料共享"学会/做"等通用字被误判为有据。
"""

from __future__ import annotations

from mikasa.pipeline.prompts import REFUSAL_TEXT, build_user_message
from mikasa.providers.llm import MockLLM


def _complete(question: str, sources: list[tuple[int, str]]) -> str:
    """走与生产一致的 build_user_message 组装（解析对偶的起点）。"""
    msg = build_user_message(question, [(m, "《笔记》", t) for m, t in sources])
    return (
        MockLLM().complete([{"role": "user", "content": msg}], temperature=0, max_tokens=100).text
    )


def test_in_corpus_subject_answer_with_citation():
    text = "L2 正则化（权重衰减）在损失中加入 λ·‖w‖²，鼓励小而分散的权重。"
    out = _complete("L2 正则化是如何防止过拟合的？", [(1, text), (2, "与主题无关的其他段落。")])
    assert out != REFUSAL_TEXT
    assert "[1]" in out  # 达到有据判定的片段必带引用


def test_out_of_corpus_generic_words_refused():
    """真实返工回归：通用字（学/做/在/一）凑不出 4 连 → 拒答。"""
    corpus = [
        (1, "梯度下降从初始参数出发，反复沿损失函数负梯度方向更新权重。"),
        (2, "上下文学习指模型无需更新参数，仅凭提示词里的少量示例就能学会新任务。"),
        (3, "自注意力把查询、键、值投影到低维子空间并行计算。"),
    ]
    out = _complete("如何在一周内学会做菠萝包？", corpus)
    assert out == REFUSAL_TEXT


def test_short_subject_still_answerable():
    """主题专名虽短（如 4 字"反向传播"）仍应可答。"""
    text = "反向传播用链式法则把损失对每层参数的梯度逐层回传。"
    out = _complete("反向传播是怎么工作的？", [(1, text)])
    assert out != REFUSAL_TEXT
    assert "[1]" in out


def test_answer_uses_first_sentence_and_sorted_markers():
    """两段都达判定线（8 连与 6 连）：引用齐全且按编号升序。"""
    s1 = "早停法在验证集误差不再下降时停止训练，从而防止过拟合现象出现。"
    s2 = "过拟合现象是模型在训练集上表现好、在测试集上表现差的症状。"
    out = _complete("早停法如何防止过拟合现象？", [(1, s1), (2, s2)])
    assert out.startswith("根据资料")  # 引用式回答句式
    assert "早停法在验证集误差不再下降时停止训练" in out  # 最长片段（[1]）首句
    assert out.rindex("[1]") < out.rindex("[2]")  # 引用按编号升序


def test_no_sources_always_refused():
    out = _complete("L2 正则化如何防止过拟合？", [])
    assert out == REFUSAL_TEXT
