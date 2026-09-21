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
import re
from dataclasses import dataclass, field

_EPS = 1e-9


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
        return out

    def add_item(self, ranked: list[int], gold: set[int], difficulty: str) -> None:
        for k in self.ks:
            self.recall[k].add(recall_at(ranked, gold, k))
            self.ndcg[k].add(ndcg_at(ranked, gold, k))
            self.by_difficulty.setdefault(difficulty, {}).setdefault(k, []).append(
                recall_at(ranked, gold, k)
            )
        self.rr.add(reciprocal_rank(ranked, gold))


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
