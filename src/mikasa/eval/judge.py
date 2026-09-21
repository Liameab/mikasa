"""LLM-as-Judge：回答质量的裁判（双轮位置交换 + 双量尺 + 跨厂商）。

Judge 的三个偏差修正机制（judge 轮询是评测开销的大头，机制设计见
docs/evaluation.md"裁判偏差修正"）：
  1. 位置交换：同一判题把【参考答案/资料块】与【候选回答块】先后颠倒
     问两轮，两轮给分一致才采信，否则 inconsistent——不一致本身是
     "生成不稳定/裁判敏感"信号，报告单独披露而非抹平；
  2. 量尺置换：同轮同时要求 1-5 分数制与 A-D 档位制两把量尺，
     报告披露量尺间分级一致率（不一致 = 边界题，人工复核线索）；
  3. 跨厂商：裁判与生成模型不同服务商（api profile 默认 DeepSeek 生成 /
     Qwen 裁判），配置即约束，报告开头披露当次配置。

解析不依赖 JSON mode（Ollama 小模型不稳；沿用项目"固定格式行"哲学）：
要求输出「正确性评分: N/5」「档位: X」两行。解析失败计 raw 并披露
"裁判格式解析失败率"（对标引用解析失败率的回归看板项）。

offline/CI 无裁判密钥：NoJudge 恒 None，语义分缺席，报告注明
"仅协议层指标"（拒答准确率、引用块金块命中率）。显式 NoJudge 实现
使三端行为一致、仅配置不同，而不是 None 分支散落调用点。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from mikasa.config.settings import JudgeConfig, LLMConfig
from mikasa.pipeline.prompts import REFUSAL_TEXT
from mikasa.providers.llm import OpenAICompatLLM
from mikasa.providers.ollama import OllamaNativeLLM
from mikasa.utils.logging import get_logger

logger = get_logger("eval.judge")

_LINE_PATTERN = re.compile(r"^\s*([^\s:：]+)\s*[:：]\s*(.+?)\s*$")
_SCORE_PATTERN = re.compile(r"^(\d)\s*/\s*5$")


@dataclass(frozen=True)
class JudgeVerdict:
    """一次判题结果；consistent=False 时分数仅供参考（位置交换不一致）。"""

    correctness: int  # 1-5
    grade: str  # A-D（第二量尺）
    # 回答论断是否都由资料支撑。**三态**：None = 裁判两轮里缺「忠实性」这一行
    # （未解析到 ≠ 判为不忠实——真值化会把"没测到"写成"不忠实"，2026-09-20 审查实测）
    faithful: bool | None = None
    consistent: bool = True
    raw: list[str] = field(default_factory=list)  # 裁判原始输出，人工复核留痕


class Judge(Protocol):
    """裁判协议。evaluate 输入一次判题的完整素材，None = 裁判不可用。"""

    model: str

    def evaluate(
        self,
        question: str,
        notes: str,
        context: str,
        answer: str,
    ) -> JudgeVerdict | None: ...


class NoJudge:
    """零密钥裁判：恒 None。语义分缺席由报告显式注明，行为与真实裁判同构。"""

    model = "none"

    def evaluate(self, *args: object) -> None:
        del args  # 统一签名，占位
        return None


class LLMJudge:
    """真实裁判：OpenAI 兼容端点（api 的 SiliconFlow Qwen / local 的 Ollama）。

    Judge 复用 OpenAICompatLLM 客户端——与生成端同协议不同配置，
    跨厂商约束在 config 层落实（生成 DeepSeek / 裁判 Qwen）。
    """

    SYSTEM = (
        "你是严谨的中文评测裁判。根据问题与参考答案要点评估候选回答。规则：\n"
        "1. 只依据「参考答案要点」与「事实资料」判断，不引入外部知识；\n"
        "2. 候选回答加入资料外的实质性事实视为不忠实；\n"
        "3. 忠实但要点不全给低分不判错——只要已给出的部分都正确。\n"
        "只输出三行：\n"
        "正确性评分: N/5   （5=正确且完整，4=正确但缺要点，3=部分正确有遗漏，"
        "2=错误与正确混杂，1=基本错误）\n"
        "档位: X            （A=优秀 B=良好 C=及格 D=不及格，与分数独立判断）\n"
        "忠实性: 是/否      （候选回答是否加入了资料之外的事实）"
    )

    def __init__(self, config: JudgeConfig) -> None:
        self.model = config.model
        self._temperature = config.temperature
        llm_config = LLMConfig(
            backend="api" if config.backend == "api" else "local",
            base_url=config.base_url,
            api_key_env=config.api_key_env,
            model=config.model,
            temperature=config.temperature,
            # 判题只要三行结论，256 token 的预算经不起思考：
            # 实测（2026-09-21）同一条译文，兼容面 10.5s、原生 think=false 0.4s
            # ——思考既拖时间又可能把预算吃光（空输出是踩过的坑，见 ADR-0029）。
            think=False if config.backend == "local" else None,
            max_tokens=256,
            timeout_seconds=120.0,
        )
        # 本地档走**原生通道**：`think` 只有它认，兼容面会静默忽略
        # （ADR-0029 的教训——同一个模型、同一个提示词，差 26 倍）。
        self._llm = (
            OllamaNativeLLM(llm_config)
            if config.backend == "local"
            else OpenAICompatLLM(llm_config)
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _prompt(question: str, notes: str, context: str, answer: str, *, answer_first: bool) -> str:
        """组装判题提示词。answer_first 控制位置交换轮次的素材顺序。"""
        blocks = [
            f"问题：{question}",
            f"参考答案要点：{notes}",
            f"事实资料（模型可引用的片段）：\n{context}",
            f"候选回答：{answer}",
        ]
        if answer_first:
            blocks[1], blocks[3] = blocks[3], blocks[1]  # 候选回答先行
        return "\n\n".join(blocks) + "\n\n请按系统规则输出两行评分。"

    def _round(
        self,
        question: str,
        notes: str,
        context: str,
        answer: str,
        *,
        answer_first: bool,
    ) -> tuple[int | None, str | None, bool | None, str]:
        message = self._prompt(question, notes, context, answer, answer_first=answer_first)
        completion = self._llm.complete(
            [
                {"role": "system", "content": self.SYSTEM},
                {"role": "user", "content": message},
            ],
            temperature=self._temperature,
            max_tokens=256,
        )
        raw = completion.text.strip()
        correctness: int | None = None
        grade: str | None = None
        faithful: bool | None = None
        for line in raw.splitlines():
            match = _LINE_PATTERN.match(line)
            if not match:
                continue
            label, value = match.group(1), match.group(2)
            if label.startswith("正确性"):
                num = _SCORE_PATTERN.match(value)
                if num:
                    correctness = int(num.group(1))
            elif label == "档位":
                candidate = value.upper()[:1]
                if candidate in "ABCD":
                    grade = candidate
            elif label.startswith("忠实"):
                faithful = value.startswith("是")
        if correctness is None or grade is None:
            logger.warning("裁判输出解析失败：%r", raw[:200])
        return correctness, grade, faithful, raw

    def evaluate(
        self,
        question: str,
        notes: str,
        context: str,
        answer: str,
    ) -> JudgeVerdict | None:
        if not answer.strip() or answer.strip() == REFUSAL_TEXT:
            return None  # 拒答不判语义分，正确性由拒答桶负责

        c1, g1, f1, raw1 = self._round(question, notes, context, answer, answer_first=False)
        c2, g2, f2, raw2 = self._round(question, notes, context, answer, answer_first=True)
        if c1 is None or c2 is None or g1 is None or g2 is None:
            # 任一轮解析失败 → 不可信，标记不一致并把原文留给人工
            return JudgeVerdict(0, "?", None, consistent=False, raw=[raw1, raw2])
        # 忠实性三态：两轮都读到才下结论；缺行保持 None（"没测到"≠"不忠实"）
        faithful = None if f1 is None or f2 is None else bool(f1 and f2)
        # 一致性判据：**只比能读到的量**。缺「忠实性」行不该把每一题都打成
        # "不一致"（那会把复核清单灌满噪声），但读到了就必须两轮相同。
        consistent = c1 == c2 and g1 == g2 and (f1 is None or f2 is None or f1 == f2)
        return JudgeVerdict(
            correctness=c1 if c1 == c2 else min(c1, c2),
            grade=g1 if g1 == g2 else "?",
            faithful=faithful,
            consistent=consistent,
            raw=[raw1, raw2],
        )
