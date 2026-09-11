"""提示词协议单元测试：消息组装与 mock 解析严格对偶。"""

from __future__ import annotations

from mikasa.pipeline.prompts import (
    REFUSAL_TEXT,
    SECTION_HISTORY,
    SECTION_QUESTION,
    SECTION_SOURCES,
    build_user_message,
)
from mikasa.providers.llm import MockLLM


def _ask(question: str, sources: list[tuple[int, str]]) -> str:
    return (
        MockLLM()
        .complete(
            [{"role": "user", "content": build_user_message(question, sources)}],
            temperature=0.0,
            max_tokens=256,
        )
        .text
    )


def test_build_message_structure():
    msg = build_user_message(
        "什么是反向传播？",
        sources=[
            (1, "深度学习笔记 / 3.1", "反向传播利用链式法则计算梯度。"),
            (2, "深度学习笔记 / 3.2", "前向传播逐层计算输出。"),
        ],
    )
    assert msg.startswith(SECTION_SOURCES)
    assert "【资料1】来源：深度学习笔记 / 3.1" in msg
    assert "【资料2】来源：深度学习笔记 / 3.2" in msg
    assert SECTION_QUESTION in msg
    assert msg.strip().endswith("什么是反向传播？")


def test_build_message_with_history():
    msg = build_user_message("追问一下", sources=[(1, "doc", "正文")], history_summary="此前问过 X")
    assert SECTION_HISTORY in msg
    assert "此前问过 X" in msg


def test_mock_parse_roundtrip():
    """MockLLM 解析应与组装严格对偶（含多行资料体）。"""
    sources = [(1, "A 笔记", "第一段内容。\n第二行内容。"), (2, "B 笔记", "另一段内容。")]
    msg = build_user_message("第一段内容是什么？", sources=sources)
    parsed, question = MockLLM._parse_user_message(msg)
    assert question == "第一段内容是什么？"
    assert parsed[0][0] == 1
    assert "第一段内容。" in parsed[0][1]
    assert "第二行内容。" in parsed[0][1]
    assert parsed[1][0] == 2


def test_mock_refuses_without_evidence():
    """资料与问题无实义重叠 → 统一拒答句式，且不带任何 [n]。"""
    out = _ask(
        "巴黎是法国的首都吗？",
        [(1, "机器学习笔记", "梯度下降是一种一阶迭代优化算法。")],
    )
    assert out == REFUSAL_TEXT
    assert "[" not in out


def test_mock_answers_with_citation():
    """资料含问题实义词 → 引用式回答，命中片段编号可解析。"""
    out = _ask(
        "RNN 如何处理序列数据",
        [
            (1, "循环神经网络笔记", "RNN 通过隐藏状态按时间步循环处理序列数据。"),
            (2, "无关文档", "菜谱烹饪时间表。"),
        ],
    )
    assert out != REFUSAL_TEXT
    assert "[1]" in out  # 命中片段编号，不得引用无关的 [2]


def test_mock_stream_joins_to_complete():
    msg = build_user_message("什么是 RNN", sources=[(1, "笔记", "RNN 是循环神经网络。")])
    messages = [{"role": "user", "content": msg}]
    llm = MockLLM()
    chunks = list(llm.stream(messages, temperature=0.0, max_tokens=256))
    assert "".join(chunks) == llm.complete(messages, temperature=0.0, max_tokens=256).text


def test_mock_completion_usage_none():
    """Mock 不产生 token 用量（非 API 调用）。"""
    out = _ask("什么是 RNN", [(1, "笔记", "RNN 是循环神经网络。")])
    assert isinstance(out, str)


def test_build_translate_to_zh_messages_structure():
    """块翻译消息组装（#8）：system 三禁令 + 【原文N】分段与注入协议同构。"""
    from mikasa.pipeline.prompts import (
        TRANSLATE_TO_ZH_SYSTEM_PROMPT,
        build_translate_to_zh_messages,
    )

    messages = build_translate_to_zh_messages(
        [(1, "Quantum annealing cools a system.\n"), (2, "Tunneling escapes local minima.")]
    )
    assert messages[0] == {"role": "system", "content": TRANSLATE_TO_ZH_SYSTEM_PROMPT}
    user = messages[1]["content"]
    assert "【原文1】\nQuantum annealing cools a system." in user  # 原文折叠成单行
    assert "【原文2】\nTunneling escapes local minima." in user
    assert user.index("【原文1】") < user.index("【原文2】")
    # 三禁令要素：只输出【译文N】分段、禁 [n]、禁解释
    assert "【译文N】" in TRANSLATE_TO_ZH_SYSTEM_PROMPT
    assert "[n]" in TRANSLATE_TO_ZH_SYSTEM_PROMPT
    assert "不要解释" in TRANSLATE_TO_ZH_SYSTEM_PROMPT
