"""黄金集模型单测：题本/成品校验规则、JSON 冻结与读回。

校验规则是"题库与语料不错配"的第一道防线（第二道是 runner 的
corpus_sha256 指纹），每条语义规则都要锁死：
  可答题必带 difficulty + anchors（题本）/ gold_chunk_ids（成品）；
  不可答题必带 reason 且不得携带 gold/anchors；题本与成品都必须
  同时含可答与不可答两类题。
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from mikasa.errors import EvalError
from mikasa.eval.golden import (
    GoldenItem,
    GoldenSet,
    QuestionDraft,
    load_golden,
    load_questions_yaml,
    save_golden,
)

GOOD_YAML = """\
name: test-golden
corpus_dir: sample-corpus
items:
  - id: q001
    kind: answerable
    question: L2 正则化是怎么防止过拟合的？
    difficulty: easy
    anchors: ["L2 正则化在损失中加入权重的平方和惩罚项，鼓励小而分散的权重。"]
    notes: L2 惩罚项 → 权重向零收缩。
  - id: q002
    kind: unanswerable
    question: 推荐一部动画片。
    reason: unrelated
    notes: 语料无关，必须拒答。
"""


# ---------------------------------------------------------------------------
# QuestionDraft / QuestionsYaml：题本校验
# ---------------------------------------------------------------------------


def test_load_questions_yaml_valid(tmp_path):
    questions = load_questions_yaml(_write_yaml(GOOD_YAML, tmp_path))
    assert questions.name == "test-golden"
    assert questions.items[0].anchors[0].startswith("L2 正则化")
    assert questions.items[1].reason == "unrelated"


def test_questions_yaml_missing_difficulty_rejected(tmp_path):
    # 删整行（含 4 空格缩进），否则残留空格会把下一行顶深一层、破坏 YAML 结构
    text = GOOD_YAML.replace("    difficulty: easy\n", "")
    with pytest.raises(EvalError, match="可答题必须标注难度"):
        load_questions_yaml(_write_yaml(text, tmp_path))


def test_questions_yaml_answerable_requires_anchors(tmp_path):
    anchor_line = '    anchors: ["L2 正则化在损失中加入权重的平方和惩罚项，鼓励小而分散的权重。"]\n'
    text = GOOD_YAML.replace(anchor_line, "")
    with pytest.raises(EvalError, match="必须给出 anchors"):
        load_questions_yaml(_write_yaml(text, tmp_path))


def test_questions_yaml_unanswerable_cannot_carry_anchors(tmp_path):
    text = GOOD_YAML.replace(
        "    notes: 语料无关，必须拒答。",
        '    anchors: ["任意锚句"]\n    notes: 语料无关，必须拒答。',
    )
    with pytest.raises(EvalError, match="不应携带 anchors"):
        load_questions_yaml(_write_yaml(text, tmp_path))


def test_questions_yaml_duplicate_ids_rejected(tmp_path):
    text = GOOD_YAML.replace("id: q002", "id: q001")
    with pytest.raises(EvalError, match="重复的问题 id"):
        load_questions_yaml(_write_yaml(text, tmp_path))


def test_questions_yaml_must_contain_both_kinds(tmp_path):
    # 删掉整条不可答题（从它的 id 行到文件尾）→ 只剩可答题，整本无效
    lines = GOOD_YAML.splitlines()
    start = next(i for i, line in enumerate(lines) if "id: q002" in line)
    text = "\n".join(lines[:start]).rstrip() + "\n"
    with pytest.raises(EvalError, match="必须同时包含可答题与不可答题"):
        load_questions_yaml(_write_yaml(text, tmp_path))


def test_questions_yaml_missing_file_raises(tmp_path):
    with pytest.raises(EvalError, match="题本不存在"):
        load_questions_yaml(tmp_path / "nope.yaml")


# ---------------------------------------------------------------------------
# GoldenItem / GoldenSet：成品校验
# ---------------------------------------------------------------------------


def _answerable_item() -> GoldenItem:
    return GoldenItem(
        id="q001",
        kind="answerable",
        question="Q？",
        difficulty="easy",
        gold_chunk_ids=[1],
        notes="要点",
    )


def _golden(items: list[GoldenItem]) -> GoldenSet:
    golden = GoldenSet(corpus_sha256="abc123", items=items)
    golden.validate_items()
    return golden


def test_golden_item_kind_validation():
    item = _answerable_item()
    assert item.validate_kind() is None  # 合法
    bad = item.model_copy(update={"difficulty": None})
    with pytest.raises(EvalError, match="必须标注难度"):
        bad.validate_kind()
    bad = item.model_copy(update={"gold_chunk_ids": []})
    with pytest.raises(EvalError, match="必须有 gold_chunk_ids"):
        bad.validate_kind()


def test_golden_unanswerable_reason_rules():
    item = GoldenItem(id="q002", kind="unanswerable", question="Q？", reason="unrelated")
    assert item.validate_kind() is None
    with pytest.raises(EvalError, match="必须标注 reason"):
        item.model_copy(update={"reason": None}).validate_kind()
    with pytest.raises(EvalError, match="不应携带 gold_chunk_ids"):
        item.model_copy(update={"gold_chunk_ids": [1]}).validate_kind()


def test_golden_set_properties_and_validation():
    answerable = _answerable_item()
    unanswerable = GoldenItem(id="q002", kind="unanswerable", question="Q？", reason="unrelated")
    golden = _golden([answerable, unanswerable])
    assert [i.id for i in golden.answerable] == ["q001"]
    assert [i.id for i in golden.unanswerable] == ["q002"]
    with pytest.raises(EvalError, match="重复的问题 id"):
        _golden([answerable, answerable, unanswerable])
    with pytest.raises(EvalError, match="必须同时包含"):
        _golden([answerable])


# ---------------------------------------------------------------------------
# 冻结 / 读回：save_golden → load_golden 往返（corpus_sha256 一并落盘）
# ---------------------------------------------------------------------------


def test_save_load_golden_roundtrip(tmp_path):
    golden = _golden(
        [
            _answerable_item(),
            GoldenItem(id="q002", kind="unanswerable", question="Q？", reason="unrelated"),
        ]
    )
    path = tmp_path / "golden_set.json"
    save_golden(golden, path)
    loaded = load_golden(path)
    assert loaded.model_dump() == golden.model_dump()
    assert loaded.corpus_sha256 == "abc123"
    assert loaded.created == golden.created


def test_load_golden_missing_file_raises(tmp_path):
    with pytest.raises(EvalError, match="黄金集不存在"):
        load_golden(tmp_path / "nope.json")


def test_load_golden_rejects_foreign_json(tmp_path):
    # 缺 items 这种结构字段 → pydantic 校验失败（结构错配即拒绝，不留静默默认值）
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"name": "x"}), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_golden(path)


def test_load_golden_rejects_a_bank_with_only_one_kind(tmp_path):
    # 只有可答题（或只有不可答题）不算黄金集：召回质量与拒答纪律各占一半评分信号。
    # corpus_sha256 自 2026-09-19 起是可选元信息（逐题校验取代了全库指纹），
    # 所以这条只能靠 kind 覆盖面来拦，不能靠"必填字段"。
    path = tmp_path / "half.json"
    path.write_text(json.dumps({"name": "x", "items": []}), encoding="utf-8")
    with pytest.raises(EvalError):
        load_golden(path)


def test_questions_are_frozen_models():
    # frozen 语义：跑测评期间题目不可变，杜绝"评测中题本被改"的竞态
    item = _answerable_item()
    with pytest.raises(ValidationError):
        item.question = "被篡改的问题"
    draft = QuestionDraft(
        id="q001", kind="answerable", question="Q？", difficulty="easy", anchors=["锚句"]
    )
    with pytest.raises(ValidationError):
        draft.difficulty = "hard"


def _write_yaml(text: str, tmp_path):
    path = tmp_path / "test_questions.yaml"
    path.write_text(text, encoding="utf-8")
    return path
