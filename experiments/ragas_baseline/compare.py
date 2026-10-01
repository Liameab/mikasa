"""把两边的数字摆到一张表上（主 venv 跑）：out/result.md。

输入：
  out/answers.jsonl —— 主 venv 导出（题目/答案/上下文 + 本仓指标）
  out/ragas.json    —— 对照组 venv 跑出（RAGAS 四维）
输出：
  out/result.md     —— 聚合对照表 + 逐题对照表（分析写在 README.md，图省事分开）

用法：
    .venv/Scripts/python.exe experiments/ragas_baseline/compare.py
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path("experiments/ragas_baseline/out")


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"


def main() -> int:
    answers = [
        json.loads(line)
        for line in (OUT / "answers.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ragas = json.loads((OUT / "ragas.json").read_text(encoding="utf-8"))
    ragas_by_id = {item["id"]: item for item in ragas["items"]}

    ours = [row["ours"] for row in answers]
    claim_supported = sum(o["claim_supported"] or 0 for o in ours)
    claim_unsupported = sum(o["claim_unsupported"] or 0 for o in ours)
    claim_uncited = sum(o["claim_uncited"] or 0 for o in ours)
    claim_decided = claim_supported + claim_unsupported
    citations = sum(o["citations"] for o in ours)
    gold_hits = sum(o["gold_hits"] for o in ours)
    faithful_flags = [o["judge_faithful"] for o in ours if o["judge_faithful"] is not None]
    correctness_vals = [o["judge_correctness"] for o in ours if o["judge_correctness"] is not None]

    aggregates = ragas["aggregates"]
    lines: list[str] = []
    add = lines.append

    add("# RAGAS 对照结果（同 20 题、同答案、同上下文）")
    add("")
    add("- 抽样：种子 20260930、分层 20 题（可答题 47 道里），逐题见下表")
    add(f"- RAGAS 侧：{ragas['judge_model']}（与主仓裁判同款）+ {ragas['embed_model']}")
    add("- 我方指标与 RAGAS 全部跑在**同一份答案**上（out/answers.jsonl）")
    add("")
    add("## 1. 聚合对照")
    add("")
    add("| 我方口径 | 值 | RAGAS 口径 | 值 |")
    add("| --- | --- | --- | --- |")
    add(
        f"| claim 级支持率 {claim_supported}/{claim_decided} | "
        f"{fmt(claim_supported / claim_decided if claim_decided else None)} | "
        f"faithfulness | {fmt(aggregates['faithfulness'])} |"
    )
    add(
        f"| 整段忠实性（裁判 0/1 均值） | "
        f"{fmt(mean([1.0 if f else 0.0 for f in faithful_flags]))} | "
        f"— | — |"
    )
    add(
        f"| citation gold ratio（引用块命中 gold） | "
        f"{fmt(gold_hits / citations if citations else None)} | "
        f"context precision | {fmt(aggregates['context_precision'])} |"
    )
    add(f"| 引用越界/自造编号 | {sum(o['out_of_range'] for o in ours)} | — | — |")
    add(
        f"| 裁判 correctness 1-5 | "
        f"{fmt(mean(correctness_vals))} | "
        f"answer relevancy | {fmt(aggregates['answer_relevancy'])} |"
    )
    add(f"| 无引用断言（没挂 [n] 的事实陈述） | {claim_uncited} | — | — |")
    add(
        f"| 检索召回（gold 块在不在注入窗口，本题集） | 见评测报告 stage A | "
        f"context recall | {fmt(aggregates['context_recall'])} |"
    )
    add("")
    add("## 2. 逐题对照")
    add("")
    add(
        "| 题 | 难度 | 引用数 | 引用命中 gold | 断言 支持/未支持 | RAGAS faithfulness | "
        "RAGAS ctx precision | RAGAS ctx recall | 裁判忠实性 |"
    )
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in answers:
        item = ragas_by_id.get(row["id"], {})
        o = row["ours"]
        add(
            f"| {row['id']} | {row['difficulty']} | {o['citations']} | "
            f"{o['gold_hits']}/{o['citations']} | "
            f"{o['claim_supported']}/{o['claim_unsupported']} | "
            f"{fmt(item.get('faithfulness'))} | {fmt(item.get('context_precision'))} | "
            f"{fmt(item.get('context_recall'))} | "
            f"{'是' if o['judge_faithful'] else ('否' if o['judge_faithful'] is False else '—')} |"
        )
    add("")
    add("> 口径差异（写进报告时不可省）：RAGAS 的 faithfulness 用**全部检索上下文**验证断言，")
    add("> 我方的 claim 级支持率只认**该断言自己引用的块**（更严）；context precision/recall 是")
    add(
        "> **句/段级对参考答案**（黄金集的 notes 要点），"
        "我方的 citation gold 是**块级对 gold_chunk_id**。"
    )
    add("")

    path = OUT / "result.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"已写出 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
