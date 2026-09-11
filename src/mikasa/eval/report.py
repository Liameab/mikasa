"""评测报告渲染：纯函数，输入 (settings, golden, result) 输出中文 markdown。

报告是评测的"人读面"，与 metrics_json（机读快照）同源不同形：
  报告含配置披露（profile/模型/裁判策略）——复现与面试讲解都靠它；
  只列出失败/异常/不一致的逐条明细，正常条目请查 metrics_json 的 items。

渲染规约（测试与 CI 解析依赖，勿随意改标题）：
  - 一级标题 "# Mikasa评测报告"；小节标题固定为"## 阶段A/阶段B/阶段C/异常明细"；
  - 数值一律 _fmt 处理：nan/空容器渲染成 "—"，不出现裸 nan；
  - NoJudge 场次在阶段 C 固定输出"（NoJudge：仅协议层指标）"注记。
"""

from __future__ import annotations

from datetime import datetime

from mikasa.config.settings import Settings
from mikasa.eval.golden import GoldenSet
from mikasa.eval.metrics import summarize
from mikasa.eval.runner import EvalResult, ItemRecord

# 报告列宽口径：markdown 管道表不换行，数值列统一对齐
_HEADER_SEP = "---"


def render_report(
    settings: Settings,
    golden: GoldenSet,
    result: EvalResult,
    *,
    run_id: int | None = None,
    created_at: str | None = None,
) -> str:
    """渲染一份完整 markdown 评测报告。

    参数：
        settings/golden/result: 一次 run 的三要素
        run_id:     落库后的自增 id（报告头部标注，便于回溯）
        created_at: 报告时间戳（落库时间；缺省取当前本地时间）
    """
    gen, judge = result.generation, result.judge
    lines: list[str] = []
    add = lines.append
    add("# Mikasa评测报告")
    add("")
    add(_meta_table(settings, golden, result, run_id=run_id, created_at=created_at))
    add("")

    # ---------------- 阶段 A：检索层 ----------------
    add("## 阶段A 检索层（关 rerank，窗口独立锚定，见附录配置）")
    add("")
    add(_retrieval_overview_table(result))
    add("")
    diff_rows = _retrieval_difficulty_rows(result)
    if diff_rows:
        add("**按难度分层（recall 均值）**")
        add("")
        add(_table(["难度", "题数", "recall@5", "recall@8", "recall@10"], diff_rows))
        add("")

    # ---------------- 阶段 B：生成层 ----------------
    add("## 阶段B 生成层（产品配置真实链路）")
    add("")
    add(
        _table(
            ["统计项", "值"],
            [
                ["可答题数", str(gen.n_answerable)],
                ["可答题误拒（有据却拒答）", str(gen.refused_answerable)],
                ["未拒答却零引用", str(gen.no_citation)],
                [
                    "引用越界/自造编号率",
                    _mean_text(gen.out_of_range_rate),
                ],
                ["citation gold ratio（引用命中题面 gold）", _mean_text(gen.citation_gold)],
                ["不可答题数", str(gen.n_unanswerable)],
                ["拒答准确率", _rate_text(gen.refusal_clean, gen.n_unanswerable)],
                ["不可答题误答", str(gen.answered_unanswerable)],
                ["误答且带引用", str(gen.answered_with_citation)],
                ["拒答却带引用（L3 违规）", str(gen.refusal_dirty)],
                ["生成链路失败", str(gen.failed)],
            ],
        )
    )
    add("")

    # ---------------- 阶段 C：语义裁判 ----------------
    add("## 阶段C 语义裁判")
    add("")
    if judge.judge_model == "none":
        add("（NoJudge：未启用或密钥缺失 —— 本场为仅协议层指标）")
        add("")
    else:
        add(
            _table(
                ["统计项", "值"],
                [
                    ["裁判模型", judge.judge_model],
                    ["判题数（可答题非拒答）", str(judge.judged)],
                    ["双轮一致", str(judge.consistent)],
                    ["双轮不一致（含格式解析失败）", str(judge.inconsistent)],
                    ["裁判调用失败", str(judge.errors)],
                    ["正确性 1-5（一致样本均值）", _mean_text(judge.correctness)],
                    ["A/B 档占比（一致样本）", _mean_text(judge.top2_grade)],
                ],
            )
        )
        add("")

    # ---------------- 异常与不一致明细 ----------------
    add("## 异常明细（需人工复核）")
    add("")
    anomalies = _anomaly_rows(result.items)
    if not anomalies:
        add("（无异常：全部题目正常完成，无裁判不一致）")
    else:
        add(_table(["题目", "类型", "异常"], anomalies))
    add("")

    # ---------------- 附录 ----------------
    add("## 附录 评测配置与口径")
    add("")
    add(
        _table(
            ["项目", "值"],
            [
                ["黄金集名称", golden.name],
                ["语料指纹", f"{golden.corpus_sha256[:16]}…"],
                ["题量（可答/不可答）", f"{len(golden.answerable)}/{len(golden.unanswerable)}"],
                [
                    "生成模型",
                    f"{settings.llm.backend} / {settings.llm.model}"
                    + ("（mock：引用协议演示，无语义）" if settings.llm.backend == "mock" else ""),
                ],
                [
                    "重排器（阶段B）",
                    f"{settings.reranker.backend} / {settings.reranker.model}"
                    if settings.reranker.backend != "none"
                    else "关闭",
                ],
                [
                    "检索配置（阶段B）",
                    f"fusion_top_k={settings.retrieval.fusion_top_k}，"
                    f"bm25_top_k={settings.retrieval.bm25_top_k}，"
                    f"dense={settings.embedding.backend}",
                ],
                ["语义裁判策略", _judge_policy_text(settings, judge.judge_model)],
                ["数据目录", str(settings.data_dir)],
            ],
        )
    )
    add("")
    add("> 完整逐条留痕见 metrics_json 的 items；报告 markdown 落库于 eval_runs.report_md。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 表格工具
# ---------------------------------------------------------------------------


def _table(header: list[str], rows: list[list[str]]) -> str:
    """管道表格渲染：表头 + 分隔行 + 数据行。"""
    head = "| " + " | ".join(header) + " |"
    sep = "| " + " | ".join(_HEADER_SEP for _ in header) + " |"
    body = ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join([head, sep, *body])


def _fmt(value: float) -> str:
    """数值格式化：nan（空样本）渲染成 —，常规保留三位小数。"""
    return "—" if value != value else f"{value:.3f}"


def _mean_text(mean) -> str:
    """_Mean 容器 → "均值 (p50 | p95 | n)" 的紧凑文本。"""
    if not mean.values:
        return "—（无样本）"
    stats = summarize(mean.values)
    return (
        f"均值 {_fmt(float(stats['mean']))}　"
        f"(p50 {_fmt(float(stats['p50']))} / p95 {_fmt(float(stats['p95']))} / n {stats['n']})"
    )


def _rate_text(hits: int, total: int) -> str:
    """比例文本："3/5 = 60.0%"，分母为 0 → —。"""
    if total == 0:
        return "—"
    return f"{hits}/{total} = {hits / total:.1%}"


# ---------------------------------------------------------------------------
# 各节渲染件
# ---------------------------------------------------------------------------


def _meta_table(
    settings: Settings,
    golden: GoldenSet,
    result: EvalResult,
    *,
    run_id: int | None,
    created_at: str | None,
) -> str:
    """报告头部元信息表。"""
    judge_note = _judge_policy_text(settings, result.judge.judge_model)
    return _table(
        ["项目", "值"],
        [
            ["run id", str(run_id) if run_id is not None else "—（未落库）"],
            ["时间", created_at or datetime.now().isoformat(timespec="seconds")],
            ["profile", result.profile],
            ["黄金集", f"{golden.name}（题量 {len(golden.items)}）"],
            ["生成模型", f"{settings.llm.backend} / {settings.llm.model}"],
            ["语义裁判", judge_note],
            ["总耗时", f"{result.latency_sec:.1f}s"],
        ],
    )


def _judge_policy_text(settings: Settings, judge_model: str) -> str:
    """裁判策略的一句话描述（报告头部/附录共用）。"""
    if judge_model == "none":
        if settings.judge.enabled:
            return f"NoJudge（{settings.judge.model} 缺密钥）→ 仅协议层指标"
        return "NoJudge（judge 未启用）→ 仅协议层指标"
    return (
        f"LLM-as-Judge（{judge_model}，temperature={settings.judge.temperature}），"
        "双轮位置交换 + 1-5/A-D 双量尺"
    )


def _retrieval_overview_table(result: EvalResult) -> str:
    """阶段 A 总表：recall@k / MRR / nDCG@k 的描述统计。"""
    metrics = result.retrieval
    rows: list[list[str]] = []
    for k in metrics.ks:
        stats = summarize(metrics.recall[k].values)
        rows.append(
            [
                f"recall@{k}",
                _fmt(float(stats["mean"])),
                _fmt(float(stats["p50"])),
                _fmt(float(stats["p95"])),
                str(stats["n"]),
            ]
        )
    mrr = summarize(metrics.rr.values)
    rows.append(
        [
            "MRR",
            _fmt(float(mrr["mean"])),
            _fmt(float(mrr["p50"])),
            _fmt(float(mrr["p95"])),
            str(mrr["n"]),
        ]
    )
    for k in metrics.ks:
        stats = summarize(metrics.ndcg[k].values)
        rows.append(
            [
                f"nDCG@{k}",
                _fmt(float(stats["mean"])),
                _fmt(float(stats["p50"])),
                _fmt(float(stats["p95"])),
                str(stats["n"]),
            ]
        )
    return _table(["指标", "mean", "p50", "p95", "n"], rows)


def _retrieval_difficulty_rows(result: EvalResult) -> list[list[str]]:
    """按难度分层的 recall 均值行（仅输出有样本的难度）。"""
    by = result.retrieval.by_difficulty  # {难度: {k: [逐条 recall]}}
    rows: list[list[str]] = []
    for difficulty, per_k in by.items():
        means = {k: _mean_of(per_k.get(k, [])) for k in result.retrieval.ks}
        rows.append(
            [
                difficulty,
                str(len(next(iter(per_k.values()), []))),
                _fmt(means[5]),
                _fmt(means[8]),
                _fmt(means[10]),
            ]
        )
    return rows


def _mean_of(values: list[float]) -> float:
    """空列表 → nan（渲染层再转 —）。"""
    return float("nan") if not values else sum(values) / len(values)


def _anomaly_rows(items: list[ItemRecord]) -> list[list[str]]:
    """需人工复核的条目：链路失败 / 可答误拒 / 不可答误答 / 裁判不一致。

    "可答误拒"必须逐题列出：阶段 B 表格只给计数，人工复核
    （查检索素材是否真的不足、拒答是否过紧）依赖这里的题号。
    """
    rows: list[list[str]] = []
    for item in items:
        if item.error:
            rows.append([item.id, "链路/纪律", item.error])
            continue
        if item.kind == "answerable" and item.refused:
            rows.append([item.id, "可答误拒", "有据却拒答——请人工复核检索素材与拒答规则"])
            continue
        if item.kind == "unanswerable" and not item.refused:
            rows.append([item.id, "不可答误答", "应拒答却给出了回答"])
            continue
        if item.judge_consistent is False:
            rows.append([item.id, "裁判不一致", item.judge_note])
    return rows
