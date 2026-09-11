"""评测报告渲染单测：章节结构、数值口径、异常明细的"人读面"契约。

报告是评测结果对外的主要载体（落库 eval_runs.report_md），固定标题
是 CI 解析与文档引用的锚点——任何渲染变更都要先过这里。
"""

from __future__ import annotations

from mikasa.config.settings import load_settings
from mikasa.eval.golden import GoldenItem, GoldenSet
from mikasa.eval.metrics import make_retrieval_metrics
from mikasa.eval.report import render_report
from mikasa.eval.runner import EvalResult, GenerationStats, ItemRecord, JudgeStats


def _answerable() -> GoldenItem:
    return GoldenItem(
        id="a1",
        kind="answerable",
        question="L2 正则化？",
        difficulty="easy",
        gold_chunk_ids=[1],
        notes="权重向零收缩。",
    )


def _unanswerable() -> GoldenItem:
    return GoldenItem(
        id="u1",
        kind="unanswerable",
        question="推荐动画片？",
        reason="unrelated",
        notes="与语料无关。",
    )


def _golden() -> GoldenSet:
    golden = GoldenSet(
        corpus_sha256="ab" * 32, name="测试集", items=[_answerable(), _unanswerable()]
    )
    golden.validate_items()
    return golden


def _retrieval() -> object:
    metrics = make_retrieval_metrics((5, 8, 10))
    metrics.add_item([1, 2, 3], {1}, "easy")  # gold 首位
    metrics.add_item([2, 1], {1}, "medium")  # gold 次位 → recall 1、MRR 0.5
    return metrics


def _result(items: list[ItemRecord] | None = None) -> EvalResult:
    gen = GenerationStats(
        n_answerable=1,
        n_unanswerable=2,
        refused_answerable=0,
        no_citation=0,
        answered_unanswerable=1,
        answered_with_citation=1,
    )
    gen.citation_gold.add(0.8)
    gen.citation_gold.add(1.0)
    return EvalResult(
        profile="offline",
        retrieval=_retrieval(),
        generation=gen,
        judge=JudgeStats(),
        items=items
        or [
            ItemRecord(
                id="a1",
                kind="answerable",
                difficulty="easy",
                refused=False,
                markers=[1],
                in_range=1,
                citations=1,
                gold_hits=1,
            ),
            ItemRecord(
                id="u1",
                kind="unanswerable",
                refused=False,
                markers=[1],
                in_range=1,
                citations=1,
                gold_hits=0,
            ),
        ],
        latency_sec=1.2,
    )


def _render(tmp_path, settings=None, result=None, golden=None) -> str:
    settings = settings or load_settings("offline", data_dir=tmp_path / "data")
    return render_report(
        settings,
        golden or _golden(),
        result or _result(),
        run_id=7,
        created_at="2026-09-09T10:00:00",
    )


# ---------------------------------------------------------------------------
# 结构契约：固定标题 + 关键行
# ---------------------------------------------------------------------------


def test_report_fixed_section_headings(tmp_path):
    report = _render(tmp_path)
    assert report.startswith("# Mikasa评测报告")
    for heading in (
        "## 阶段A 检索层",
        "## 阶段B 生成层",
        "## 阶段C 语义裁判",
        "## 异常明细",
        "## 附录 评测配置与口径",
    ):
        assert heading in report


def test_report_meta_shows_run_id_and_time(tmp_path):
    report = _render(tmp_path)
    assert "| run id | 7 |" in report
    assert "2026-09-09T10:00:00" in report
    assert "| 总耗时 | 1.2s |" in report


# ---------------------------------------------------------------------------
# 阶段 A 表格
# ---------------------------------------------------------------------------


def test_report_retrieval_table_rows_and_difficulty(tmp_path):
    report = _render(tmp_path)
    # 总表：每档 k 一行 + MRR + nDCG@k；recall 样本全 1 → 均值 1.000
    for label in ("recall@5", "recall@8", "recall@10", "MRR", "nDCG@5"):
        assert f"| {label} |" in report
    assert "| recall@5 | 1.000 | 1.000 | 1.000 | 2 |" in report
    # MRR 样本 1.0 与 0.5 → 均值 0.750；百分位 nearest-rank（n=2：p50=0.5、p95=1.0）
    assert "| MRR | 0.750 | 0.500 | 1.000 | 2 |" in report
    # 难度分层块存在且带 easy/medium 行
    assert "按难度分层（recall 均值）" in report
    assert "| easy |" in report and "| medium |" in report


# ---------------------------------------------------------------------------
# 阶段 B / C：数值口径与渲染
# ---------------------------------------------------------------------------


def test_report_generation_rows_and_rate_text(tmp_path):
    report = _render(tmp_path)
    assert "| 可答题数 | 1 |" in report
    # refusal_clean = 2 - 1(误答) - 0(dirty) = 1 → "1/2 = 50.0%"
    assert "| 拒答准确率 | 1/2 = 50.0% |" in report
    assert "| 不可答题误答 | 1 |" in report
    assert "| 误答且带引用 | 1 |" in report
    # citation gold ratio 有样本 → 均值文本；out_of_range 无样本 → 破折号注记
    assert "均值 0.900" in report
    assert "—（无样本）" in report


def test_report_nojudge_note_when_disabled(tmp_path, offline_settings):
    report = render_report(offline_settings, _golden(), _result(), run_id=1)
    # 未启用裁判 → 阶段 C 固定注记 + 附录策略说明（关键字出现不止一次）
    assert "（NoJudge：未启用或密钥缺失 —— 本场为仅协议层指标）" in report
    assert report.count("NoJudge") >= 2


def test_report_judge_section_when_active(tmp_path, offline_settings):
    judge = JudgeStats(judge_model="stub-judge", judged=2, consistent=1, inconsistent=1)
    judge.correctness.add(5.0)
    judge.top2_grade.add(1.0)
    result = _result()
    result = EvalResult(
        profile=result.profile,
        retrieval=result.retrieval,
        generation=result.generation,
        judge=judge,
        items=result.items,
        latency_sec=result.latency_sec,
    )
    report = render_report(offline_settings, _golden(), result, run_id=2)
    assert "| 裁判模型 | stub-judge |" in report
    assert "| 双轮一致 | 1 |" in report
    assert "| 双轮不一致（含格式解析失败） | 1 |" in report
    assert "双轮位置交换 + 1-5/A-D 双量尺" in report  # 策略披露行
    assert "仅协议层指标" not in report.split("阶段C")[1].split("附录")[0]


# ---------------------------------------------------------------------------
# 异常明细
# ---------------------------------------------------------------------------


def test_report_anomaly_rows_list_unanswerable_answered(tmp_path):
    report = _render(tmp_path)
    # u1 不可答未拒答 → 明细行；a1 正常不出现在明细
    assert "| u1 | 不可答误答 | 应拒答却给出了回答 |" in report
    assert "| a1 |" not in report.split("## 异常明细")[1]


def test_report_anomaly_lists_answerable_refused(tmp_path):
    # 可答误拒必须逐题可定位（阶段 B 只给计数），这是人工复核的入口
    items = [
        ItemRecord(id="a1", kind="answerable", difficulty="easy", refused=True),
        ItemRecord(id="u1", kind="unanswerable", refused=True),
    ]
    report = _render(tmp_path, result=_result(items=items))
    assert "| a1 | 可答误拒 | 有据却拒答——请人工复核检索素材与拒答规则 |" in report
    assert "| u1 |" not in report.split("## 异常明细")[1]  # 拒答干净不上明细


def test_report_anomaly_shows_errors_and_judge_inconsistency(tmp_path):
    items = [
        ItemRecord(id="a1", kind="answerable", difficulty="easy", error="TimeoutError: 连接超时"),
        ItemRecord(id="u1", kind="unanswerable", refused=True, error=""),
        ItemRecord(
            id="a2",
            kind="answerable",
            difficulty="medium",
            judge_consistent=False,
            judge_note="位置交换两轮不一致——需人工复核。",
        ),
    ]
    report = _render(tmp_path, result=_result(items=items))
    assert "| a1 | 链路/纪律 | TimeoutError: 连接超时 |" in report
    assert "| a2 | 裁判不一致 | 位置交换两轮不一致" in report
    assert "| u1 |" not in report.split("## 异常明细")[1]  # 拒答干净不上明细


def test_report_no_anomalies_placeholder(tmp_path):
    clean_items = [
        ItemRecord(
            id="a1", kind="answerable", difficulty="easy", refused=False, citations=1, gold_hits=1
        ),
        ItemRecord(id="u1", kind="unanswerable", refused=True),
    ]
    report = _render(tmp_path, result=_result(items=clean_items))
    assert "（无异常：全部题目正常完成，无裁判不一致）" in report


# ---------------------------------------------------------------------------
# 附录
# ---------------------------------------------------------------------------


def test_report_appendix_discloses_configuration(tmp_path):
    report = _render(tmp_path)
    assert "| 黄金集名称 | 测试集 |" in report
    assert "| 语料指纹 | abababababababab… |" in report
    assert "| 题量（可答/不可答） | 1/1 |" in report  # 测试集：可答 1 + 不可答 1
    assert "mock" in report  # 附录注明生成模型是 mock（无语义，仅协议演示）
    assert "完整逐条留痕见 metrics_json 的 items" in report
