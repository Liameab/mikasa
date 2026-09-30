"""论断级忠实性（claim 级 L2）：把回答拆成原子断言，逐条核对引用是否支持。

**为什么要它**（2026-09-30，评估清单第 6 项）：L2「语义支持度」此前只有**整段**
粒度——裁判回答"这段回答忠不忠实"给一个 0/1。整段判定的盲区是**一条越界断言
藏在三条正确断言里**：均值仍是 0.83，报告上看不见它。RAGAS 的 faithfulness
把刻度做到了 claim 级（拆断言 → 逐条验证），那是它真正值钱的地方，本项目把
这个刻度**自己实现**（不引 ragas，见 docs/evaluation.md §3 的口径纪律）。

**口径（与 RAGAS 的差异是刻意的）**：RAGAS 用**检索到的全部上下文**验证断言；
本仓的 L2 定义是"**引用**是否真的支持所声称的陈述"——所以证据面 = 该断言自己
引用的那些块（`[n]` → 第 n 条注入片段），更严格，也更贴本仓的引用协议。
两类特殊断言单独计数，不混进支持率：

  - **无引用断言**（`uncited`）：行首没有 `[n]` 的事实陈述。引用协议要求 kb 回答
    逐条带引用，所以它们既不能算被支持、也不该算被证伪——单独披露，"答案里有多少
    事实陈述根本没挂引用"本身就是纪律信号；
  - **未判定**（`undecided`）：解析不出结论的断言。与 judge 的"没测到 ≠ 不忠实"
    同款纪律：宁缺勿猜。

协议沿用本仓的"固定格式行"哲学（不依赖 JSON mode，小模型上更稳），两次调用：
拆断言（`[n] 断言` 每行一条）→ 逐条验证（`序号: 是/否` 每行一条）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from mikasa.config.settings import JudgeConfig, LLMConfig
from mikasa.pipeline.prompts import REFUSAL_TEXT
from mikasa.providers.llm import OpenAICompatLLM
from mikasa.providers.ollama import OllamaNativeLLM
from mikasa.utils.logging import get_logger

logger = get_logger("eval.claims")

# 单次调用上限（拆断言 / 逐条验证都是 ≤10 行结论；思考模型的预算留足，见 judge.py）
_MAX_TOKENS = 1024
# 断言条数上限：超过就截断（截断本身进报告，不静默丢）
_CLAIM_CAP = 10
# 证据面字符上限：引用块通常 ≤400 字，多块拼接时封顶，防超长回答把验证调用撑爆
_EVIDENCE_CAP = 3000

# 拆断言行：`[1] xxx` / `[2,3] xxx`（方括号可空）；容忍没有方括号的裸行（编号记空）
_CLAIM_LINE_RE = re.compile(r"^\s*(?:\[([\d\s,，]*)\]\s*)?(.+?)\s*$")
# 验证行：`1: 是` / `2：否`，**也认列表写法** `1. 是` / `3、否` / `4) 是`。
# 为什么要容忍（2026-09-30 实测）：Qwen 默认把逐条判断写成有序列表（`1. 是`），
# 只认冒号的解析器会把 20 题里的 16 题整批判成"未判定"——**看起来像模型没答，
# 实际是解析器没读**。分隔符可选（`1 是` 也收），但行首必须是"数字 + 是/否"，
# 断言原文里的数字不会被误认成结论。
_VERDICT_LINE_RE = re.compile(r"^\s*(\d+)\s*[.、)）。:：]?\s*(是|否)")

_DECOMPOSE_SYSTEM = (
    "你是严谨的中文事实核对员。把「候选回答」拆成逐条独立的断言，供逐条核对。"
    "规则：\n"
    "1. 每条断言单独一行，行首方括号内写该断言引用到的资料编号（多个用逗号分隔；"
    "没有引用写 []）。例：\n"
    "[1] L2 正则化在损失中加入权重的平方和惩罚项\n"
    "[2,3] 权重衰减在 AdamW 中被解耦\n"
    "2. 只拆事实性陈述：不要问候语、过渡句、纯观点（如“综上所述”），不要小标题；\n"
    "3. 一条断言只讲一个事实：原文“既…又…”要拆成两条；\n"
    "4. 最多 10 行；用原文措辞，不改写、不解释、不加序号以外的编号。"
)

_VERIFY_SYSTEM = (
    "你是严谨的中文事实核对员。逐条判断「资料」是否支持「断言」。"
    "规则：\n"
    "1. 支持 = 资料里能找到该断言的事实依据（允许同义改写与概括表述）；\n"
    "2. 资料里找不到依据、或只有常识性关联 → 判否；不要引入资料之外的知识；\n"
    "3. 输出格式：每个断言一行 `序号: 是/否`，序号与输入的断言序号一致；"
    "不要解释、不要其他内容。"
)


@dataclass(frozen=True)
class ClaimCheck:
    """一条断言及其核对结果。supported=None = 未判定（解析失败）。"""

    text: str
    markers: list[int]
    supported: bool | None


@dataclass(frozen=True)
class ClaimReport:
    """一题的论断级核对结果。uncited / undecided 由 claims 现算，不重复存储。"""

    claims: list[ClaimCheck] = field(default_factory=list)
    truncated: bool = False  # 断言超过 _CLAIM_CAP 被截断（该题支持率只覆盖前 N 条）

    @property
    def supported(self) -> int:
        return sum(1 for c in self.claims if c.supported is True)

    @property
    def unsupported(self) -> int:
        return sum(1 for c in self.claims if c.supported is False)

    @property
    def uncited(self) -> int:
        return sum(1 for c in self.claims if not c.markers)

    @property
    def undecided(self) -> int:
        return sum(1 for c in self.claims if c.supported is None and c.markers)


def _parse_claims(text: str) -> tuple[list[ClaimCheck], bool]:
    """拆断言输出 → 断言列表。markers 解析成去重升序的整数；空行/模板行丢弃。"""
    claims: list[ClaimCheck] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _CLAIM_LINE_RE.match(line)
        if not match:
            continue
        body = match.group(2).strip()
        if not body or body.startswith("候选回答"):  # 模型复述输入时丢弃
            continue
        markers = sorted({int(n) for n in re.findall(r"\d+", match.group(1) or "")})
        claims.append(ClaimCheck(text=body, markers=markers, supported=None))
    truncated = len(claims) > _CLAIM_CAP
    return claims[:_CLAIM_CAP], truncated


def _parse_verdicts(text: str) -> dict[int, bool]:
    """验证输出 → {序号: 是否支持}；解析不出的序号缺席（调用方记 undecided）。"""
    out: dict[int, bool] = {}
    for raw in text.splitlines():
        match = _VERDICT_LINE_RE.match(raw)
        if not match:
            continue
        index = int(match.group(1))
        if index not in out:
            out[index] = match.group(2) == "是"
    return out


class ClaimChecker:
    """论断级核对器：复用裁判的模型配置（同一端点/模型，口径见模块 docstring）。"""

    def __init__(self, config: JudgeConfig) -> None:
        self.model = config.model
        self._temperature = config.temperature
        llm_config = LLMConfig(
            backend="api" if config.backend == "api" else "local",
            base_url=config.base_url,
            api_key_env=config.api_key_env,
            model=config.model,
            temperature=config.temperature,
            think=False if config.backend == "local" else None,
            max_tokens=_MAX_TOKENS,
            timeout_seconds=120.0,
        )
        self._llm = (
            OllamaNativeLLM(llm_config)
            if config.backend == "local"
            else OpenAICompatLLM(llm_config)
        )

    # ------------------------------------------------------------------

    def _evidence(self, markers: set[int], by_marker: dict[int, str]) -> str:
        """把本次用到的引用块拼成证据段（【资料N】，与问答注入协议同构）。"""
        parts: list[str] = []
        budget = _EVIDENCE_CAP
        for marker in sorted(markers):
            content = by_marker.get(marker)
            if not content:
                continue
            piece = content[:budget]
            parts.append(f"【资料{marker}】{piece}")
            budget -= len(piece)
            if budget <= 0:
                break
        return "\n\n".join(parts)

    def evaluate(self, answer_text: str, by_marker: dict[int, str]) -> ClaimReport | None:
        """核对一段回答：返回 ClaimReport；空/拒答/异常 → None（不判、不猜）。

        by_marker: {注入编号: 该片段原文}——`[n]` 的证据面就是它。
        """
        answer = answer_text.strip()
        if not answer or answer == REFUSAL_TEXT:
            return None

        completion = self._llm.complete(
            [
                {"role": "system", "content": _DECOMPOSE_SYSTEM},
                {"role": "user", "content": f"候选回答：\n{answer}"},
            ],
            temperature=self._temperature,
            max_tokens=_MAX_TOKENS,
        )
        claims, truncated = _parse_claims(completion.text)
        if not claims:
            logger.warning("断言拆解失败（无有效行）：%r", completion.text[:120])
            return None

        used = {m for claim in claims for m in claim.markers}
        evidence = self._evidence(used, by_marker)
        if not evidence:
            # 一条引用都没有：全局无引用——不需要验证调用，全部按 uncited 披露
            return ClaimReport(claims=claims, truncated=truncated)

        # 只把**带引用**的断言送进验证：无引用断言没有证据面可核，送进去只会骗出
        # 一句"否"（它该记的是 uncited，不是 unsupported）。编号在验证提示里
        # **从 1 连续重排**——模型面对"1,3,4"这种跳号极易自作主张写回 1..N，
        # 于是映射靠这份列表的位置、不靠原始序号（见下面的 pos 计数）。
        cited = [claim for claim in claims if claim.markers]
        completion = self._llm.complete(
            [
                {"role": "system", "content": _VERIFY_SYSTEM},
                {
                    "role": "user",
                    "content": "资料：\n"
                    + evidence
                    + "\n\n断言：\n"
                    + "\n".join(f"{j}. {claim.text}" for j, claim in enumerate(cited, start=1)),
                },
            ],
            temperature=self._temperature,
            max_tokens=_MAX_TOKENS,
        )
        verdicts = _parse_verdicts(completion.text)
        checked: list[ClaimCheck] = []
        pos = 0
        for claim in claims:
            if not claim.markers:
                checked.append(claim)  # uncited：保持 supported=None
                continue
            pos += 1
            verdict = verdicts.get(pos)
            if verdict is None:
                checked.append(claim)  # undecided：解析不出，不猜
            else:
                checked.append(
                    ClaimCheck(text=claim.text, markers=claim.markers, supported=verdict)
                )
        return ClaimReport(claims=checked, truncated=truncated)
