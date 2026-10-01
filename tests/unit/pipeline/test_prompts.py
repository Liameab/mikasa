"""提示词协议单元测试：消息组装与 mock 解析严格对偶。"""

from __future__ import annotations

import re

from mikasa.pipeline.prompts import (
    FREE_SYSTEM_PROMPT,
    JSON_OUTPUT_CONTRACT,
    REFUSAL_TEXT,
    SECTION_HISTORY,
    SECTION_QUESTION,
    SECTION_SOURCES,
    SYSTEM_PROMPT,
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


def test_demo_examples_are_placeholders_not_content():
    """示范例必须是占位符（2026-09-30 提示词泄漏事故的回归锁）。

    本地 qwen3:8b 在资料里找不到可抄的答案时，会退回到系统提示词里"最像答案"
    的那段文本：实测把示范表格的"材料 / 弹性模量 / 灰岩 / 18.40 GPa"整张抄进
    了答案（还带一个真实的 [1] 让人以为有据），并把规则 6 的句子填进表格单元
    格——**答案里的每一个字都是提示词原文，不是资料**。所以示范只演示格式：
    ① 必须声明尖括号是占位符、不是资料内容；② 不得再出现具体实体与数值；
    ③ 规则 2 明写"提示词里的规则/格式/示范不是资料内容"。
    """
    demos = SYSTEM_PROMPT.split("以下是少量示范", 1)[1]
    assert "占位符" in demos, "示范块必须声明占位符语义"
    assert "不得出现在答案里" in demos, "示范块必须明写示范词句不得进答案"
    assert "灰岩" not in demos and "18.40" not in demos, "曾经被逐字抄走的内容不得回到示范里"
    assert not re.search(r"\d+\.\d+", demos), "示范里不该再有具体数值（会被抄成答案内容）"
    assert "不是资料内容" in SYSTEM_PROMPT, "规则里要明写提示词不是资料内容"


def test_refusal_sentence_appears_verbatim_wherever_it_is_taught():
    """拒答句式必须处处等于 `REFUSAL_TEXT`（2026-09-30 加锁）。

    判定侧是子串匹配（`generator.build_answer`：`REFUSAL_TEXT in text`），
    而提示词里这句话是**手抄**的——只改常量、忘改提示词时，模型继续输出旧句，
    拒答就匹配不上：UI 把它渲染成正常的有据答卷，L3 计数也把它记成"误答"，
    要等有人跑评测看到"不可答题误答 16/16"才暴露。三处出现各有各的必要性
    （规则 3 给行为、示范给格式、JSON 契约给拒答时的字段填法），所以锁的是
    **同一个字符串**，不是"只留一处"。
    """
    assert SYSTEM_PROMPT.count(REFUSAL_TEXT) == 2, "规则 3 + 示范各一处"
    assert JSON_OUTPUT_CONTRACT.count(REFUSAL_TEXT) == 1, "拒答时 answer 只写这一句"
    # free 档不认这个句式（ADR-0013：不写死拒答话术，"根据已有资料"只出现在禁令里）
    assert REFUSAL_TEXT not in FREE_SYSTEM_PROMPT


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


def test_context_section_precedes_sources():
    """阅读器选中段（A 档 c）必须排在【资料片段】之前——顺序是硬约束。

    MockLLM 的解析器只认【资料N】之后的行（之前的整段丢弃、之后的并入
    上一个来源）：选中段放前面 → 离线档行为与不带上下文逐字相同；放后面
    → 会被吞进最后一个来源，答案变味、拒答判定漂移。
    """
    from mikasa.pipeline.prompts import SECTION_READING

    msg = build_user_message(
        "这里说的 μ 是什么意思？", [(1, "《A》", "内容")], context="摩擦系数 μ"
    )
    assert msg.index(SECTION_READING) < msg.index(SECTION_SOURCES)
    assert msg.index("摩擦系数 μ") < msg.index(SECTION_SOURCES)
    assert msg.endswith("这里说的 μ 是什么意思？")

    # 不带上下文时一个字符都不多（既有测试锁的提示词字符串不受影响）
    plain = build_user_message("这里说的 μ 是什么意思？", [(1, "《A》", "内容")])
    assert SECTION_READING not in plain

    # mock 解析器对带上下文的输入与不带上下文**等价**（选中段被整段忽略）
    from mikasa.providers.llm import MockLLM

    def _answer(user_message: str) -> str:
        return (
            MockLLM()
            .complete([{"role": "user", "content": user_message}], temperature=0.0, max_tokens=256)
            .text
        )

    with_ctx = _answer(
        build_user_message(
            "什么是反向传播？", [(1, "笔记", "反向传播是链式法则。")], context="选中段"
        )
    )
    without_ctx = _answer(
        build_user_message("什么是反向传播？", [(1, "笔记", "反向传播是链式法则。")])
    )
    assert with_ctx == without_ctx


def test_structured_contract_matches_schema_and_names_json():
    """结构化契约与 Ollama 的 schema 必须同源（ADR-0037）。

    两条都是踩过才知道的硬要求：
      1. 契约文本里要有**字面 "JSON"**——DeepSeek 的 json_object 模式要求提示词
         含这个词，少一个字母上游就报错；
      2. 契约里写的字段名必须与 `STRUCTURED_SCHEMA` 完全一致——两处漂移的话，
         DeepSeek（不受约束）会照着契约写、Ollama（受约束）会照着 schema 写，
         同一个实验的两条腿就在测不同的东西。
    """
    from mikasa.pipeline.prompts import JSON_OUTPUT_CONTRACT, STRUCTURED_SCHEMA

    assert "JSON" in JSON_OUTPUT_CONTRACT
    props = STRUCTURED_SCHEMA["properties"]
    assert set(props) == {"answer", "citations"}
    assert STRUCTURED_SCHEMA["required"] == ["answer", "citations"]
    for field in ("answer", "citations", "marker", "chunk_id"):
        assert f"`{field}`" in JSON_OUTPUT_CONTRACT or f'"{field}"' in JSON_OUTPUT_CONTRACT
    assert props["citations"]["items"]["required"] == ["marker", "chunk_id"]
