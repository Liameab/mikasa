"""检索与生成指标：全部手写公式（不引 ragas/ir_measures 等评测库）。

公式（题面 gold 块集合 G，检索返回按 rank 排列的块序列 R）：
  recall@k   = |G ∩ R[:k]| / |G|          —— 答案素材是否被召回
  MRR        = 1 / rank(第一个 G 元素)      —— 首个正确答案有多靠前
  nDCG@k     = DCG@k / IDCG@k             —— 多金块时对排序质量的整体衡量
  DCG@k      = Σ_{i≤k} rel_i / log₂(i+1)，rel_i = 1(G) else 0
  IDCG@k     = Σ_{i=1}^{min(k,|G|)} 1 / log₂(i+1)

自定义部分（离线/无语义裁判也能跑，见 docs/evaluation.md）：
  refusal accuracy      —— 不可答题必须拒答的比例
  citation gold ratio   —— 引用块属于 G 的比例（语义 judge 开启前的
                          召回侧代理，不与 citation precision 混称）

输入契约：gold: set[int]（chunk_id），ranked: list[int]（rank 升序）。
返回带均值的 _Mean 简单容器而非裸 float：分层报告（easy/medium/hard）
只需把各层样本的逐条值拼起来求均值，口径透明。
"""

from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, field
from typing import Any

_EPS = 1e-9

# bootstrap 的默认参数。**种子写死**：同一份数据两次跑必须得到同一个区间，
# 否则报告会随运行抖动，"上次看到 0.82、这次 0.79" 就分不清是模型变了还是
# 重采样变了——评测工具最怕这种事。2000 次重采样对 63 题规模够用（区间
# 端点的抖动远小于 0.01），再多是白花时间。
_BOOTSTRAP_N = 2000
_BOOTSTRAP_SEED = 20260925
_BOOTSTRAP_CONFIDENCE = 0.95


def bootstrap_ci(
    values: list[float],
    *,
    n: int = _BOOTSTRAP_N,
    confidence: float = _BOOTSTRAP_CONFIDENCE,
    seed: int = _BOOTSTRAP_SEED,
) -> tuple[float, float] | None:
    """均值的 bootstrap 置信区间（percentile 法）；样本为空 → None。

    **为什么需要它**（2026-09-25）：63 题上的单点数字没有不确定度，改一次
    检索拿到 recall@10 从 0.98 到 0.99 时，没人答得上"这是变好了还是噪声"。
    区间回答的正是这句；`paired_bootstrap` 回答更强的那个版本。

    **口径要说清**：这里的重采样对象是**题库**，不是语料——区间读作
    "若从同一分布的题库里另抽一批题，均值大概会落在哪"。它不覆盖
    语料变动、模型版本漂移这些系统性因素（那些不在统计里，在指纹核对里）。
    """
    if not values:
        return None
    rng = random.Random(seed)
    count = len(values)
    means = []
    for _ in range(n):
        means.append(sum(values[rng.randrange(count)] for _ in range(count)) / count)
    means.sort()
    lo = means[max(0, int((1 - confidence) / 2 * n) - 1)]
    hi = means[min(n - 1, int((1 + confidence) / 2 * n))]
    return (lo, hi)


def paired_bootstrap(
    before: list[float],
    after: list[float],
    *,
    n: int = _BOOTSTRAP_N,
    confidence: float = _BOOTSTRAP_CONFIDENCE,
    seed: int = _BOOTSTRAP_SEED,
) -> dict[str, float | int | bool] | None:
    """配对 bootstrap：同一批题在两次运行下的逐条差值，均值 + 置信区间。

    调用方保证 `before[i]` 与 `after[i]` 是**同一道题**——配对是这里的全部
    价值：不配对的两次独立区间会被题间方差淹没，而配对只看"同一题变了多少"，
    在 63 题的规模上才看得出 1-2 个点的真实变化。

    返回 `{n, delta, lo, hi, significant}`：
      - delta = mean(after - before)；正的 = 变好；
      - `significant` = 区间**不跨 0**。这是"这一次改动有没有效果"的判据，
        但它只对**这一批题**成立——题库本身就是样本，别把它读成物理定律。
    """
    if len(before) != len(after):
        return None
    if not before:
        return None
    deltas = [b - a for a, b in zip(before, after, strict=True)]
    ci = bootstrap_ci(deltas, n=n, confidence=confidence, seed=seed)
    assert ci is not None  # deltas 非空（上面已判）
    lo, hi = ci
    return {
        "n": len(deltas),
        "delta": sum(deltas) / len(deltas),
        "lo": lo,
        "hi": hi,
        "significant": lo > 0 or hi < 0,
    }


# ---------------------------------------------------------------------------
# 单条指标
# ---------------------------------------------------------------------------


def recall_at(ranked: list[int], gold: set[int], k: int) -> float:
    """前 k 名中金块占比。gold 为空视为"无召回目标"，调用方跳过。"""
    if not gold:
        return 0.0
    top = ranked[:k]
    return sum(1 for cid in top if cid in gold) / len(gold)


def reciprocal_rank(ranked: list[int], gold: set[int]) -> float:
    """首个金块的倒数排名；未命中返回 0。"""
    for rank, cid in enumerate(ranked, start=1):
        if cid in gold:
            return 1.0 / rank
    return 0.0


def ndcg_at(ranked: list[int], gold: set[int], k: int) -> float:
    """归一化折损累计增益；gold 为空视为 0（调用方通常已跳过）。"""
    if not gold:
        return 0.0
    dcg = 0.0
    for rank, cid in enumerate(ranked[:k], start=1):
        if cid in gold:
            dcg += 1.0 / math.log2(rank + 1)
    ideal_count = min(k, len(gold))
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
    return dcg / (idcg + _EPS)


# ---------------------------------------------------------------------------
# 聚合容器
# ---------------------------------------------------------------------------


@dataclass
class _Mean:
    """逐条分数的可累加容器：mean / std / n 与分层聚合共用。"""

    values: list[float] = field(default_factory=list)

    def add(self, value: float) -> None:
        self.values.append(value)

    def extend(self, values: list[float]) -> None:
        self.values.extend(values)

    def finalize(self) -> float:
        if not self.values:
            return float("nan")
        return sum(self.values) / len(self.values)


def summarize(values: list[float]) -> dict[str, float | int]:
    """描述统计（报告用）：mean / p50 / p95 / min / max / n。

    空样本返回**同一组键**（值全为 nan）而不是只给 mean/n：报告渲染直接取
    `stats["p50"]`，键缺失会 KeyError 崩在渲染期（2026-09-20 审查实测：
    某档位无样本时整份报告渲染失败）。空样本的语义由调用方看 n=0 判断。
    """
    if not values:
        nan = float("nan")
        return {"mean": nan, "p50": nan, "p95": nan, "min": nan, "max": nan, "n": 0}
    ordered = sorted(values)
    n = len(ordered)

    def percentile(p: float) -> float:
        idx = min(n - 1, max(0, math.ceil(p / 100 * n) - 1))
        return ordered[idx]

    return {
        "mean": sum(ordered) / n,
        "p50": percentile(50),
        "p95": percentile(95),
        "min": ordered[0],
        "max": ordered[-1],
        "n": n,
    }


@dataclass
class RetrievalMetrics:
    """一层检索的完整指标（k 与分层口径固定，报告渲染简单）。"""

    ks: tuple[int, ...]
    recall: dict[int, _Mean]  # per-k 逐条 recall 值
    rr: _Mean  # MRR 的逐条原料
    ndcg: dict[int, _Mean]
    # 分层（难度）口径：k 取最大档，value 为逐条值列表
    by_difficulty: dict[str, dict[int, list[float]]] = field(default_factory=dict)
    # 逐题原料：题号 → {recall_at: {k: v}, rr: v, ndcg_at: {k: v}}
    # 上面的 _Mean 只保序不保名，配不了对；比较两次运行必须认得出"同一道题"
    # （`eval compare` 与 paired_bootstrap 都读这里）。
    items: dict[str, dict[str, Any]] = field(default_factory=dict)

    def final(self) -> dict[str, object]:
        out: dict[str, object] = {
            "recall_at": {k: summarize(self.recall[k].values) for k in self.ks},
            "mrr": summarize(self.rr.values),
            "ndcg_at": {k: summarize(self.ndcg[k].values) for k in self.ks},
        }
        if self.by_difficulty:
            out["by_difficulty"] = {
                d: {k: summarize(v) for k, v in kv.items()} for d, kv in self.by_difficulty.items()
            }
        if self.items:
            out["items"] = self.items
        return out

    def add_item(
        self, ranked: list[int], gold: set[int], difficulty: str, *, item_id: str = ""
    ) -> None:
        for k in self.ks:
            self.recall[k].add(recall_at(ranked, gold, k))
            self.ndcg[k].add(ndcg_at(ranked, gold, k))
            self.by_difficulty.setdefault(difficulty, {}).setdefault(k, []).append(
                recall_at(ranked, gold, k)
            )
        self.rr.add(reciprocal_rank(ranked, gold))
        if item_id:
            self.items[item_id] = {
                "recall_at": {k: recall_at(ranked, gold, k) for k in self.ks},
                "ndcg_at": {k: ndcg_at(ranked, gold, k) for k in self.ks},
                "rr": reciprocal_rank(ranked, gold),
            }


def make_retrieval_metrics(ks: tuple[int, ...]) -> RetrievalMetrics:
    return RetrievalMetrics(
        ks=ks,
        recall={k: _Mean() for k in ks},
        ndcg={k: _Mean() for k in ks},
        rr=_Mean(),
    )


# ---------------------------------------------------------------------------
# 作答形态（2026-09-21，交接单 P3）
# ---------------------------------------------------------------------------

# 分节标题：## 起的行（h1 留给报告自身，模型的分节从 ## 算起，与提示词的
# OUTPUT_FORMAT_CONTRACT 一致）
_SECTION_RE = re.compile(r"^\s{0,3}#{2,4}\s+\S", re.MULTILINE)
# 表格：连续以 | 起头的行，≥2 行才算一张（单行是正文里的竖线，不算）
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
# 列表项：- / * / + 或 1. / 1)
_BULLET_RE = re.compile(r"^\s{0,3}(?:[-*+]\s+\S|\d{1,3}[.)]\s+\S)", re.MULTILINE)
# 公式：行间 $$...$$ 与行内 $...$（同一个 $ 计数里，行间占 2）
_DISPLAY_MATH_RE = re.compile(r"\$\$[^$]+\$\$", re.DOTALL)
_INLINE_MATH_RE = re.compile(r"(?<!\$)\$[^$\n]+\$(?!\$)")


def shape_stats(text: str) -> dict[str, int]:
    """一段回答的"形态"计数：字数 / 分节 / 表格 / 公式 / 列表项。

    为什么要有它（交接单 P3）：提示词里那份 OUTPUT_FORMAT_CONTRACT（分节、
    表格、公式、示意图）在 2026-09-20 只有**一条问题的肉眼实测**——
    改提示词没有任何回归手段，只能靠感觉。这几个计数就是那面镜子：
    同一个题库跑两轮，形态变化一眼可见（见 evaluation.md 的使用说明）。

    **只统计形态，不判断好坏**——"用了 3 张表格"不等于"答得好"，
    语义正确性仍归裁判与引用指标管。别把这两件事混成一个人造分数。
    """
    body = text or ""
    stripped = body.strip()
    # 围栏代码块里的竖线与 $ 不算形态（那是代码/公式的字面量，不是呈现）
    without_fences = re.sub(r"```.*?```", "", body, flags=re.DOTALL)
    tables = 0
    run = 0
    for line in without_fences.splitlines():
        if _TABLE_ROW_RE.match(line):
            run += 1
            continue
        if run >= 2:
            tables += 1
        run = 0
    if run >= 2:
        tables += 1
    display = len(_DISPLAY_MATH_RE.findall(without_fences))
    inline = len(_INLINE_MATH_RE.findall(without_fences))
    return {
        "chars": len(stripped),
        "sections": len(_SECTION_RE.findall(without_fences)),
        "tables": tables,
        "formulas": display + inline,
        "bullets": len(_BULLET_RE.findall(without_fences)),
    }
