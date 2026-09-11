"""评测黄金集：问题定义 + 语料指纹防错配 + 分级分类。

golden 的生命周期（保证"测的是当前语料"）：
  1. 人工撰写 evals/questions.yaml（问题 + 期望的锚定原文，不含数据库 id）；
  2. tools/build_golden.py 在语料入库后解析锚句子 → 真实 chunk_id，
     连同 corpus_sha256 一起冻结为 evals/golden_set.json；
  3. 评测时 runner 校验 corpus_sha256 —— 语料一旦变化（增删改）
     golden_set 立即失效报错，杜绝"题对不上库"的静默错配。

不可答题（unanswerable）不携带 gold ids：它们的评分信号是"必须拒答"。
reason 记录不可答的成因类别，方便分桶统计拒答策略的失败模式。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

Difficulty = Literal["easy", "medium", "hard"]
UnanswerReason = Literal["unrelated", "insufficient", "hallucination_bait"]


class GoldenItem(BaseModel):
    """一条黄金问题。kind 决定评分信号：可答看引用命中，不可答看拒答。"""

    model_config = ConfigDict(frozen=True)

    id: str  # q001…
    kind: Literal["answerable", "unanswerable"]
    question: str
    difficulty: Difficulty | None = None  # 可答题必填
    gold_chunk_ids: list[int] = []  # 可答题必填（≤ 预期检索命中的块）
    notes: str = ""  # 期望答到的知识点 / 不可答原因说明（评分锚点与人工复核依据）
    reason: UnanswerReason | None = None  # 不可答题必填

    @field_validator("question")
    @classmethod
    def _question_nonempty(cls, value: str) -> str:
        return value.strip() or "？"  # 空问题由 kind 语义校验兜底拦截

    def validate_kind(self) -> None:
        from mikasa.errors import EvalError

        if self.kind == "answerable":
            if self.difficulty not in ("easy", "medium", "hard"):
                raise EvalError(f"{self.id}: 可答题必须标注难度 easy/medium/hard")
            if not self.gold_chunk_ids:
                raise EvalError(f"{self.id}: 可答题必须有 gold_chunk_ids")
        else:
            if self.reason not in ("unrelated", "insufficient", "hallucination_bait"):
                raise EvalError(f"{self.id}: 不可答题必须标注 reason")
            if self.gold_chunk_ids:
                raise EvalError(f"{self.id}: 不可答题不应携带 gold_chunk_ids")


class GoldenSet(BaseModel):
    """完整黄金集：corpus_sha256 与语料快照绑定的错配防护。"""

    model_config = ConfigDict(frozen=True)

    name: str = "mikasa-golden"
    corpus_sha256: str
    created: str = Field(default_factory=lambda: date.today().isoformat())
    items: list[GoldenItem]

    @property
    def answerable(self) -> list[GoldenItem]:
        return [i for i in self.items if i.kind == "answerable"]

    @property
    def unanswerable(self) -> list[GoldenItem]:
        return [i for i in self.items if i.kind == "unanswerable"]

    def validate_items(self) -> None:
        from mikasa.errors import EvalError

        seen: set[str] = set()
        for item in self.items:
            if item.id in seen:
                raise EvalError(f"重复的问题 id：{item.id}")
            seen.add(item.id)
            item.validate_kind()
        if not self.answerable or not self.unanswerable:
            raise EvalError("黄金集必须同时包含可答题与不可答题")


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
# "题目对不对得上当前语料"由 corpus_sha256 + 锚句唯一命中双层校验兜底。


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

    @field_validator("question")
    @classmethod
    def _question_nonempty(cls, value: str) -> str:
        return value.strip() or "？"  # 空问题由 kind 语义校验兜底拦截

    def validate_kind(self) -> None:
        from mikasa.errors import EvalError

        if self.kind == "answerable":
            if self.difficulty not in ("easy", "medium", "hard"):
                raise EvalError(f"{self.id}: 可答题必须标注难度 easy/medium/hard")
            if not self.anchors:
                raise EvalError(f"{self.id}: 可答题必须给出 anchors（锚定原文句子）")
            if any(not anchor.strip() for anchor in self.anchors):
                raise EvalError(f"{self.id}: anchors 中含空句子")
        else:
            if self.reason not in ("unrelated", "insufficient", "hallucination_bait"):
                raise EvalError(f"{self.id}: 不可答题必须标注 reason")
            if self.anchors:
                raise EvalError(f"{self.id}: 不可答题不应携带 anchors")


class QuestionsYaml(BaseModel):
    """人工黄金题文件整体。corpus_dir 仅作说明（题面面向哪份语料）。"""

    model_config = ConfigDict(frozen=True)

    name: str = "mikasa-golden"
    corpus_dir: str = ""  # 说明性字段：语料目录（build 前人工确认内容对得上）
    items: list[QuestionDraft]

    def validate_items(self) -> None:
        from mikasa.errors import EvalError

        seen: set[str] = set()
        for item in self.items:
            if item.id in seen:
                raise EvalError(f"重复的问题 id：{item.id}")
            seen.add(item.id)
            item.validate_kind()
        if not any(i.kind == "answerable" for i in self.items) or not any(
            i.kind == "unanswerable" for i in self.items
        ):
            raise EvalError("黄金题必须同时包含可答题与不可答题")


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
