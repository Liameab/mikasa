"""评测执行器单测：指纹校验、三阶段编排、拒答纪律账本、裁判注入。

语料用"锚句即问题原文"的小型笔记库（3 个 chunk），离线 mock LLM 行为
因此可预测：
  - 问题与某 chunk 原文逐字相同 → mock 引用该 chunk 作答（[1] 合法引用）；
  - 问题与全文无 ≥4 字连续重叠 → mock 判定"无据可答"而拒答；
  - 问题与全文有重叠但语义上不可答 → mock 会误答并带引用（纪律双失败样本）。

这三类样本正好覆盖生成层协议指标的三种桶（正常引用 / 可答误拒 /
不可答误答带引用），聚合断言全部确定性。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mikasa.errors import EvalError, StorageError
from mikasa.eval.golden import GoldenItem, GoldenSet
from mikasa.eval.judge import JudgeVerdict
from mikasa.eval.runner import EvalRunner, GenerationStats
from mikasa.index.manager import IndexManager
from mikasa.ingest.service import IngestService

# 语料：3 个内容块（块内句子即黄金题的锚句与逐字问题）
DOC1 = """# 深度学习笔记

## 正则化

L2 正则化在损失中加入权重的平方和惩罚项，鼓励小而分散的权重。

## 早停

验证集损失不再下降时停止训练，这是成本最低的防过拟合手段。
"""

DOC2 = """# 贝叶斯方法

## 贝叶斯公式

贝叶斯公式把先验、似然与后验联系起来：后验正比于似然乘以先验。
"""

A1_QUESTION = "L2 正则化在损失中加入权重的平方和惩罚项，鼓励小而分散的权重。"
A2_QUESTION = "验证集损失不再下降时停止训练，这是成本最低的防过拟合手段。"
A3_QUESTION = "先验与后验如何通过数据更新？"
U1_QUESTION = "推荐一首适合睡前听的纯音乐。"
U2_QUESTION = "L2 正则化会把哪些权重直接压成零？"


def _seed(settings, tmp_path: Path) -> Path:
    """导入语料并返回笔记目录（重复调用会因指纹已存在而跳过，幂等）。"""
    src = tmp_path / "notes"
    src.mkdir(exist_ok=True)
    (src / "dl.md").write_text(DOC1, encoding="utf-8")
    (src / "bayes.md").write_text(DOC2, encoding="utf-8")
    IngestService(settings).ingest_paths([src])
    return src


def _resolve_one(anchor: str, chunks) -> int:
    """锚句必须唯一命中一个 chunk（与 tools/build_golden.py 同口径）。"""
    hits = [c.id for c in chunks if anchor in c.content]
    assert len(hits) == 1, f"锚句命中 {len(hits)} 个块：{anchor[:30]}…"
    return hits[0]


def _make_golden(settings, tmp_path: Path, with_a3: bool = True) -> GoldenSet:
    """语料入库后用真实指纹冻结小型黄金集（gold ids 取自锚句解析）。"""
    corpus = IndexManager(settings).corpus()
    assert not corpus.empty
    chunks = corpus.chunks
    items = [
        GoldenItem(
            id="a1",
            kind="answerable",
            question=A1_QUESTION,
            difficulty="easy",
            gold_chunk_ids=[_resolve_one(A1_QUESTION, chunks)],
            notes="L2 惩罚项→权重收缩。",
        ),
        GoldenItem(
            id="a2",
            kind="answerable",
            question=A2_QUESTION,
            difficulty="medium",
            gold_chunk_ids=[_resolve_one(A2_QUESTION, chunks)],
            notes="早停=成本最低的正则化。",
        ),
        GoldenItem(
            id="u1",
            kind="unanswerable",
            question=U1_QUESTION,
            reason="unrelated",
            notes="与语料无关。",
        ),
        GoldenItem(
            id="u2",
            kind="unanswerable",
            question=U2_QUESTION,
            reason="hallucination_bait",
            notes="L1 才压零，语料只讲了 L2——陷阱题。",
        ),
    ]
    if with_a3:
        items.append(
            GoldenItem(
                id="a3",
                kind="answerable",
                question=A3_QUESTION,
                difficulty="hard",
                gold_chunk_ids=[_resolve_one("贝叶斯公式把先验、似然与后验联系起来", chunks)],
                notes="后验正比于似然乘以先验。",
            )
        )
    golden = GoldenSet(corpus_sha256=corpus.sha256, items=items)
    golden.validate_items()
    return golden


def _runner(settings, golden: GoldenSet) -> EvalRunner:
    return EvalRunner(settings, golden)


def _find(items, item_id: str):
    return next(i for i in items if i.id == item_id)


# ---------------------------------------------------------------------------
# 防御路径：空库 / 指纹错配
# ---------------------------------------------------------------------------


def test_run_empty_corpus_raises_storage_error(tmp_path, offline_settings):
    # 黄金集在"另一份已入库的语料"上冻结，run 指向空库 → 先报空库错误
    from mikasa.config.settings import load_settings

    seeded = load_settings("offline", data_dir=tmp_path / "seeded")
    _seed(seeded, tmp_path)
    golden = _make_golden(seeded, tmp_path)
    with pytest.raises(StorageError, match="知识库为空"):
        _runner(offline_settings, golden).run()


def test_unrelated_corpus_change_keeps_the_bank_usable(tmp_path, offline_settings):
    """往库里加不相干的文档不再让题库失效（2026-09-19 起逐题校验）。

    这条原先是 `test_run_fingerprint_mismatch_raises_and_hints_rebuild`：那时
    加一篇笔记就触发全库指纹失配、整场拒绝执行——守卫范围远大于风险范围。
    现在只有**标准答案所在分块确实变了**的那几道题会被跳过，并在报告里写明。
    """
    _seed(offline_settings, tmp_path)
    golden = _make_golden(offline_settings, tmp_path)
    extra = tmp_path / "notes" / "extra.md"
    extra.write_text("# 新文档\n\n新增一段完全不同的内容。\n", encoding="utf-8")
    IngestService(offline_settings).ingest_paths([extra.parent])

    result = _runner(offline_settings, golden).run()
    assert result.skipped_items == []
    assert len(result.items) == len(golden.items)


def test_run_rejects_when_every_gold_chunk_is_gone(tmp_path, offline_settings):
    """标准答案分块**全部**对不上才是硬错误，且必须给出重建指引。"""
    _seed(offline_settings, tmp_path)
    golden = _make_golden(offline_settings, tmp_path)
    stale = golden.model_copy(
        update={
            "items": [
                item.model_copy(update={"gold_chunk_ids": [90001], "gold_hashes": ["0" * 64]})
                if item.kind == "answerable"
                else item
                for item in golden.items
            ]
        }
    )
    with pytest.raises(EvalError) as excinfo:
        _runner(offline_settings, stale).run()
    assert "完全对不上" in str(excinfo.value)
    assert "build_golden" in str(excinfo.value)


def test_only_the_changed_gold_chunk_is_skipped(tmp_path, offline_settings):
    """只跳过"标准答案被改动"的那一道，并在结果里留痕（静默缩分母最危险）。"""
    _seed(offline_settings, tmp_path)
    golden = _make_golden(offline_settings, tmp_path)
    broken = golden.model_copy(
        update={
            "items": [
                item.model_copy(update={"gold_hashes": ["0" * 64]}) if item.id == "a2" else item
                for item in golden.items
            ]
        }
    )
    result = _runner(offline_settings, broken).run()
    assert [item_id for item_id, _ in result.skipped_items] == ["a2"]
    assert all(record.id != "a2" for record in result.items)


# ---------------------------------------------------------------------------
# 正常整场：offline（NoJudge）协议层指标
# ---------------------------------------------------------------------------


def test_run_full_offline_protocol_metrics(tmp_path, offline_settings):
    _seed(offline_settings, tmp_path)
    golden = _make_golden(offline_settings, tmp_path)
    result = _runner(offline_settings, golden).run()

    # ---- 阶段 A：检索层（锚句逐字 → a1/a2 样本 recall 必为 1） ----
    recall_mean = result.retrieval.recall[5].finalize()
    assert recall_mean >= 2 / 3 - 1e-9  # a1/a2 顶格，a3 未知 → 下界 2/3
    assert set(result.retrieval.by_difficulty) == {"easy", "medium", "hard"}

    # ---- 阶段 B：生成层账本（三类样本各归其桶） ----
    gen = result.generation
    assert gen.n_answerable == 3
    assert gen.n_unanswerable == 2
    assert gen.refused_answerable == 1  # a3：问题与全库无 ≥4 字重叠 → mock 拒答
    assert gen.no_citation == 0
    assert gen.refusal_clean == 1  # u1 拒答干净；u2 误答
    assert gen.answered_unanswerable == 1
    assert gen.answered_with_citation == 1  # u2 误答且带引用（纪律双重失败）
    assert gen.refusal_dirty == 0
    assert gen.failed == 0
    assert gen.refusal_accuracy() == 0.5
    assert gen.citation_gold.finalize() == 1.0  # a1/a2 引用块=gold 块

    # ---- 逐条留痕 ----
    assert len(result.items) == 5
    a1 = _find(result.items, "a1")
    assert a1.refused is False and a1.error == ""
    assert a1.citations >= 1 and a1.gold_hits == a1.citations  # 引用全中 gold
    a3 = _find(result.items, "a3")
    assert a3.refused is True and a3.markers == []
    u2 = _find(result.items, "u2")
    assert u2.refused is False and u2.citations >= 1
    assert u2.gold_hits == 0  # 不可答题无 gold，命中数恒 0

    # ---- 阶段 C：NoJudge（offline 默认关裁判） ----
    assert result.judge.judge_model == "none"
    assert result.judge.judged == 0 and result.judge.errors == 0

    assert result.latency_sec > 0
    snapshot = result.to_metrics_json()
    assert set(snapshot) == {
        "profile",
        "latency_sec",
        "retrieval",
        "generation",
        "judge",
        "items",
        "skipped_items",  # 逐题校验跳过的题（2026-09-19 起）；静默缩分母最危险
        "shape",  # 作答形态（2026-09-21 起）；改提示词的回归镜子
    }
    assert snapshot["skipped_items"] == []
    assert snapshot["generation"]["refusal_accuracy"] == 0.5
    # 形态只统计"可答题且未拒答"的样本：a1/a2 作答、a3 拒答、u1/u2 不可答
    # → 分母 2。MockLLM 只会平铺直叙，所以分节/表格/公式都是 0，字数 > 0。
    shape = snapshot["shape"]
    assert shape["answers"] == 2
    assert shape["chars"]["mean"] > 0
    assert shape["sections"]["mean"] == 0 and shape["tables"]["mean"] == 0


def test_run_item_to_json_shape(tmp_path, offline_settings):
    _seed(offline_settings, tmp_path)
    result = _runner(offline_settings, _make_golden(offline_settings, tmp_path)).run()
    record = _find(result.items, "a1").to_json()
    assert set(record) == {
        "id",
        "kind",
        "difficulty",
        "refused",
        "error",
        "markers",
        "in_range",
        "citations",
        "gold_hits",
        "judge_score",
        "judge_grade",
        "judge_consistent",
        "judge_note",
        "latency_ms",
    }
    assert record["id"] == "a1" and record["gold_hits"] >= 1


# ---------------------------------------------------------------------------
# 阶段 C：裁判替身注入（judged / consistent / inconsistent / errors 四桶）
# ---------------------------------------------------------------------------


class _StubJudge:
    """evaluate 行为可脚本化的裁判替身（model 非 "none" 才会被调用）。"""

    def __init__(
        self, verdicts: dict[str, JudgeVerdict] | None = None, raise_error: bool = False
    ) -> None:
        self.verdicts = verdicts or {}
        self.raise_error = raise_error
        self.model = "stub-judge"

    def evaluate(self, question: str, notes: str, context: str, answer: str) -> JudgeVerdict | None:
        if self.raise_error:
            raise RuntimeError("裁判服务不可达")
        verdict = self.verdicts.get(question)
        return verdict if verdict is not None else JudgeVerdict(5, "A", True)


def _patch_judge(monkeypatch: pytest.MonkeyPatch, judge: _StubJudge) -> None:
    import mikasa.eval.runner as runner_module

    monkeypatch.setattr(runner_module, "build_judge", lambda _settings: judge)


def test_run_with_judge_counts_consistent(tmp_path, offline_settings, monkeypatch):
    _seed(offline_settings, tmp_path)
    golden = _make_golden(offline_settings, tmp_path)
    judge = _StubJudge()
    _patch_judge(monkeypatch, judge)
    result = _runner(offline_settings, golden).run()

    stats = result.judge
    assert stats.judge_model == "stub-judge"
    assert stats.judged == 2  # 仅 a1/a2（a3 拒答不判）
    assert stats.consistent == 2 and stats.inconsistent == 0 and stats.errors == 0
    assert stats.correctness.finalize() == 5.0
    assert stats.top2_grade.finalize() == 1.0
    a1 = _find(result.items, "a1")
    assert a1.judge_score == 5 and a1.judge_grade == "A" and a1.judge_consistent is True


def test_run_judge_inconsistent_exposed_not_smoothed(tmp_path, offline_settings, monkeypatch):
    _seed(offline_settings, tmp_path)
    golden = _make_golden(offline_settings, tmp_path)
    flaky = _StubJudge(
        verdicts={A1_QUESTION: JudgeVerdict(3, "C", True, consistent=False, raw=["轮次原文…"])}
    )
    _patch_judge(monkeypatch, flaky)
    result = _runner(offline_settings, golden).run()

    assert result.judge.inconsistent == 1 and result.judge.consistent == 1
    assert result.judge.correctness.finalize() == 5.0  # 只统计一致样本
    a1 = _find(result.items, "a1")
    assert a1.judge_consistent is False and a1.judge_note  # 留痕供人工复核


def test_run_judge_error_does_not_fail_the_item(tmp_path, offline_settings, monkeypatch):
    _seed(offline_settings, tmp_path)
    golden = _make_golden(offline_settings, tmp_path)
    _patch_judge(monkeypatch, _StubJudge(raise_error=True))
    result = _runner(offline_settings, golden).run()

    assert result.judge.errors == 2  # 两条可答题都调裁判都失败
    assert result.judge.judged == 0 and result.generation.failed == 0  # 协议层不受连累
    assert all("裁判调用失败" in i.judge_note for i in result.items if i.id in ("a1", "a2"))


def test_run_on_item_callback_fires_per_item_in_order(tmp_path, offline_settings):
    """on_item：阶段 B/C 每题回调一次（进度推进点），顺序与题本一致。"""
    _seed(offline_settings, tmp_path)
    golden = _make_golden(offline_settings, tmp_path)
    seen: list[str] = []
    result = _runner(offline_settings, golden).run(on_item=lambda rec: seen.append(rec.id))
    assert seen == [i.id for i in golden.items]  # 每题恰好一次、不重不漏
    assert len(result.items) == len(seen)


# ---------------------------------------------------------------------------
# 拒答准确率的两条口径（2026-09-20 全量审查发现并修复）
# ---------------------------------------------------------------------------


def test_failed_unanswerable_is_not_a_clean_refusal():
    """链路失败的不可答题**不能**算成"干净拒答"。

    失败的那几道什么也没测到：既没验出误答，也没验出正确拒答。旧口径下
    "16 道里 5 道超时失败"会报 16/16 = 100% 拒答准确率，而分母里只有 11 道
    真跑过——数字看着更好、其实什么也没测（审查实测）。
    """
    gen = GenerationStats(n_answerable=47, n_unanswerable=16, failed=5, failed_unanswerable=5)
    assert gen.refusal_clean == 11
    assert gen.refusal_accuracy() == 11 / 16


def test_refusal_accuracy_uses_total_unanswerable_as_denominator():
    """分母是**不可答题总数**——与报告/CLI 打出的分数同源。

    旧口径 (clean + 误答) 把 L3 违规（拒答句却带引用）漏出分母：同一次 run
    里 CLI 打 "12/16"、markdown 报告算 75%、百分数却算成 80%（审查实测）。
    三个数字必须来自同一个比值。
    """
    gen = GenerationStats(n_unanswerable=16, answered_unanswerable=3, refusal_dirty=1)
    assert gen.refusal_clean == 12
    assert gen.refusal_accuracy() == 12 / 16  # 不是 12/15


def test_refusal_accuracy_is_none_without_unanswerable():
    assert GenerationStats(n_answerable=3).refusal_accuracy() is None
