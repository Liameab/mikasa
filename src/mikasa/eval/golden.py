"""评测黄金集：问题定义 + 逐题防错配 + 分级分类。

golden 的生命周期（保证"测的是当前语料"）：
  1. 人工撰写 evals/questions.yaml（问题 + 期望的锚定原文，不含数据库 id）；
  2. tools/build_golden.py 在语料入库后解析锚句子 → 真实 chunk_id，
    连同 corpus_sha256 一起冻结为 evals/golden_set.json；
  3. 评测时 runner **逐题校验**标准答案分块还在不在、内容变没变（见下），
    受影响的题跳过并写进报告，杜绝"题对不上库"的静默错配。

另有**自动生成**的题库（src/mikasa/eval/synth.py，2026-09-19）：从用户自己的
语料里抽样分块，让模型出一道"只有这段能回答"的题，那个分块即标准答案。
人工题库只能覆盖它写作时的那份语料（锚句是原文），所以"评我自己的资料"
这条路只能靠自动生成。

**防错配改成逐题校验**（2026-09-19）：原先比较的是**全库**指纹——往库里加
任意一篇文档都会让整份题库失效，而真正的风险只涉及题目引用的那几个分块。
现在 `GoldenItem.gold_hashes` 冻结每个标准答案分块的内容哈希，评测时逐题核对
"这一块还在、内容没变"：不相干的文档随便加，只有真的动了标准答案所在的分块，
那一道题才被跳过（并在报告里写明跳了几道）。全题被跳过才是硬错误。

不可答题（unanswerable）不携带 gold ids：它们的评分信号是"必须拒答"。
reason 记录不可答的成因类别，方便分桶统计拒答策略的失败模式。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

Difficulty = Literal["easy", "medium", "hard"]
UnanswerReason = Literal["unrelated", "insufficient", "hallucination_bait"]


# 成品题（GoldenItem）与草稿题（QuestionDraft）共享的校验。两套模型一字不差地
# 各写一遍是 2026-09-20 审查点名的"该抽没抽"——改一处漏另一处，报错文案还会悄悄分叉。
def _check_common(
    item_id: str,
    question: str,
    kind: str,
    difficulty: Difficulty | None,
    reason: UnanswerReason | None,
) -> None:
    """题面非空 / 可答题标了难度 / 不可答题标了原因（载荷差异各查各的）。"""
    from mikasa.errors import EvalError

    if not question.strip():
        raise EvalError(f"{item_id}: 题目不能为空（question 漏写或缩进错了吧？）")
    if kind == "answerable":
        if difficulty not in ("easy", "medium", "hard"):
            raise EvalError(f"{item_id}: 可答题必须标注难度 easy/medium/hard")
    elif reason not in ("unrelated", "insufficient", "hallucination_bait"):
        raise EvalError(f"{item_id}: 不可答题必须标注 reason")


def _check_item_list(items: list[Any], what: str) -> None:
    """去重 id → 逐题校验 → 两类都得有（`what` 只影响报错里的名词）。"""
    from mikasa.errors import EvalError

    seen: set[str] = set()
    for item in items:
        if item.id in seen:
            raise EvalError(f"重复的问题 id：{item.id}")
        seen.add(item.id)
        item.validate_kind()
    if not any(i.kind == "answerable" for i in items) or not any(
        i.kind == "unanswerable" for i in items
    ):
        raise EvalError(f"{what}必须同时包含可答题与不可答题")


def _strip_question(value: str) -> str:
    """question 字段校验器：只做去空白归一。

    **空问题不在字段层替换成"？"**——那会把"YAML 里漏写 question"变成一个
    看起来正常的题目：评测时阶段 A 必然零命中、阶段 B 记一次可答误拒，
    全程无报错，只是分数悄悄变差（2026-09-20 审查实测）。
    真正的拦截在 validate_kind（抛 EvalError、带题号）。
    """
    return value.strip()


class GoldenItem(BaseModel):
    """一条黄金问题。kind 决定评分信号：可答看引用命中，不可答看拒答。"""

    model_config = ConfigDict(frozen=True)

    id: str  # q001…
    kind: Literal["answerable", "unanswerable"]
    question: str
    difficulty: Difficulty | None = None  # 可答题必填
    gold_chunk_ids: list[int] = []  # 可答题必填（≤ 预期检索命中的块）
    # 与 gold_chunk_ids **逐位对应**的内容哈希：评测前的逐题校验据此判断
    # "这一块还是当初那一块"。空列表 = 老题库没冻结哈希，退化成只校验 id 存在。
    gold_hashes: list[str] = []
    notes: str = ""  # 期望答到的知识点 / 不可答原因说明（评分锚点与人工复核依据）
    reason: UnanswerReason | None = None  # 不可答题必填

    _validate_question = field_validator("question")(_strip_question)

    def validate_kind(self) -> None:
        from mikasa.errors import EvalError

        _check_common(self.id, self.question, self.kind, self.difficulty, self.reason)
        if self.kind == "answerable":
            if not self.gold_chunk_ids:
                raise EvalError(f"{self.id}: 可答题必须有 gold_chunk_ids")
            if self.gold_hashes and len(self.gold_hashes) != len(self.gold_chunk_ids):
                raise EvalError(
                    f"{self.id}: gold_hashes 与 gold_chunk_ids 必须逐位对应"
                    f"（{len(self.gold_hashes)} vs {len(self.gold_chunk_ids)}）"
                )
        elif self.gold_chunk_ids:
            raise EvalError(f"{self.id}: 不可答题不应携带 gold_chunk_ids")


class GoldenSet(BaseModel):
    """完整黄金集。

    corpus_sha256 自 2026-09-19 起**只是元信息**（报告首部展示"这套题是为哪份
    语料写的"），不再是评测门槛——门槛改成逐题校验（见 check_against_corpus）。
    自动生成的题库压根没有唯一语料快照可言（用户随时会往库里加东西）。
    """

    model_config = ConfigDict(frozen=True)

    name: str = "mikasa-golden"
    corpus_sha256: str = ""  # 生成时的全库指纹（元信息，不参与判定）
    source: Literal["builtin", "synthesized"] = "builtin"
    model: str = ""  # 自动出题用的模型名（人工题库为空）
    created: str = Field(default_factory=lambda: date.today().isoformat())
    items: list[GoldenItem]

    @property
    def answerable(self) -> list[GoldenItem]:
        return [i for i in self.items if i.kind == "answerable"]

    @property
    def unanswerable(self) -> list[GoldenItem]:
        return [i for i in self.items if i.kind == "unanswerable"]

    def validate_items(self) -> None:
        _check_item_list(self.items, "黄金集")


@dataclass(frozen=True)
class GoldenCheck:
    """逐题核对的结果：仍可评的题库 + 被跳过的题（id, 中文原因）。"""

    golden: GoldenSet
    skipped: list[tuple[str, str]]


def check_against_corpus(golden: GoldenSet, hashes: Mapping[int, str]) -> GoldenCheck:
    """逐题核对标准答案分块是否还在、内容是否未变。

    **为什么不是全库指纹**（2026-09-19 改）：原先比较整库的 (id, 内容哈希) 序列，
    于是用户往库里加任意一篇文档、改个标题，整份题库就作废报错——而真正的风险
    只涉及题目引用的那几个分块。现在按题核对，不相干的增删一律不影响评测。

    仍拦得住它该拦的：`--reindex` 让 chunk id 整体平移（旧 id 查不到 → 跳过），
    或某个分块的内容被改写（哈希不符 → 跳过）。挡住的是"拿旧标准答案对新语料"
    这种静默错配，只是把爆炸半径从"整份题库"缩到"确实被改动的那几道题"。

    不可答题没有标准答案分块，永远保留。老题库（gold_hashes 为空）退化成
    只校验 id 是否存在。
    """
    kept: list[GoldenItem] = []
    skipped: list[tuple[str, str]] = []
    for item in golden.items:
        if item.kind != "answerable":
            kept.append(item)
            continue
        pairs: Iterable[tuple[int, str | None]]
        if len(item.gold_hashes) == len(item.gold_chunk_ids):
            pairs = zip(item.gold_chunk_ids, item.gold_hashes, strict=True)
        else:  # 老题库：只有 id 可查
            pairs = ((cid, None) for cid in item.gold_chunk_ids)
        missing = [
            cid for cid, want in pairs if cid not in hashes or (want and hashes[cid] != want)
        ]
        if missing:
            skipped.append(
                (
                    item.id,
                    f"标准答案所在分块已变动（chunk {', '.join(str(m) for m in missing[:3])}），"
                    "题目与当前语料对不上",
                )
            )
            continue
        kept.append(item)
    return GoldenCheck(
        golden=golden.model_copy(update={"items": kept}),
        skipped=skipped,
    )


def load_golden(path: Path) -> GoldenSet:
    from mikasa.errors import EvalError

    if not path.is_file():
        raise EvalError(f"黄金集不存在：{path}（先运行 tools/build_golden.py）")
    data = json.loads(path.read_text(encoding="utf-8"))
    golden = GoldenSet.model_validate(data)
    golden.validate_items()
    return golden


def save_golden(golden: GoldenSet, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(golden.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 人工题本（evals/questions.yaml）—— golden 的"源头文件"
# ---------------------------------------------------------------------------
# 与 GoldenItem 的差别：人工版本携带 anchors（锚定原文的句子，不含数据库
# id），由 tools/build_golden.py 解析成真实 chunk_id 后冻结为 GoldenSet JSON。
# 锚句在成品中不保留，因此"题目内容"以 questions.yaml 为准（可读可审），
# "题目对不对得上当前语料"由两层兜底：冻结时的锚句唯一命中（build_golden
# 拒绝零命中/多命中）与评测时的逐题 gold_hashes（见 check_against_corpus）。


class QuestionDraft(BaseModel):
    """questions.yaml 的一条题目草稿。kind 语义与 GoldenItem 完全一致。"""

    model_config = ConfigDict(frozen=True)

    id: str  # q001…
    kind: Literal["answerable", "unanswerable"]
    question: str
    difficulty: Difficulty | None = None  # 可答题必填
    anchors: list[str] = []  # 可答题必填：从语料中逐字摘出的原文句子（≥1 条）
    notes: str = ""  # 期望答到的知识点 / 不可答原因（评分锚点与人工复核依据）
    reason: UnanswerReason | None = None  # 不可答题必填

    _validate_question = field_validator("question")(_strip_question)

    def validate_kind(self) -> None:
        from mikasa.errors import EvalError

        _check_common(self.id, self.question, self.kind, self.difficulty, self.reason)
        if self.kind == "answerable":
            if not self.anchors:
                raise EvalError(f"{self.id}: 可答题必须给出 anchors（锚定原文句子）")
            if any(not anchor.strip() for anchor in self.anchors):
                raise EvalError(f"{self.id}: anchors 中含空句子")
        elif self.anchors:
            raise EvalError(f"{self.id}: 不可答题不应携带 anchors")


class QuestionsYaml(BaseModel):
    """人工黄金题文件整体。corpus_dir 仅作说明（题面面向哪份语料）。"""

    model_config = ConfigDict(frozen=True)

    name: str = "mikasa-golden"
    corpus_dir: str = ""  # 说明性字段：语料目录（build 前人工确认内容对得上）
    items: list[QuestionDraft]

    def validate_items(self) -> None:
        _check_item_list(self.items, "黄金题")


def load_questions_yaml(path: Path) -> QuestionsYaml:
    """读取并校验人工题本（UTF-8 YAML）。校验失败一律翻译成 EvalError。"""
    from mikasa.errors import EvalError

    if not path.is_file():
        raise EvalError(f"题本不存在：{path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        questions = QuestionsYaml.model_validate(data)
    except ValidationError as exc:
        raise EvalError(f"{path} 题本格式校验失败：{str(exc).splitlines()[0][:200]}") from exc
    questions.validate_items()
    return questions
