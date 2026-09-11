"""把人工题本 evals/questions.yaml 冻结成可执行的黄金集 evals/golden_set.json。

为什么需要这一步（黄金集的"双重防错配"设计，见 src/mikasa/eval/golden.py）：
  - 题本只写"问题 + 锚句（期望命中的原文）"，不含任何数据库 id——人可以审；
  - 本脚本把锚句解析成真实 chunk_id 并连同 corpus_sha256 一起冻结：
      锚句零命中 → 题面与语料对不上（改了语料/抄错原文），拒绝生成；
      锚句多命中 → 分块重叠导致有歧义（多块含同一句），人工换锚句，
      拒绝生成——宁可报错也不产出"哪块都对"的错配题；
      语料指纹一并冻结 → 之后评测前再对一次，语料变了黄金集立即失效。

用法：
  python tools/build_golden.py              # 默认题本/数据目录 → evals/golden_set.json
  python tools/build_golden.py --data-dir tmp/data  # 隔离数据目录（测试用）
  顺序：先 mikasa ingest（语料入库）再跑本脚本。

幂等：输出文件整体覆盖；语料未变时重复运行产物一致。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mikasa.config.settings import load_settings
from mikasa.errors import EvalError
from mikasa.eval.golden import (
    GoldenItem,
    GoldenSet,
    load_questions_yaml,
    save_golden,
)
from mikasa.storage.db import open_db
from mikasa.storage.repo import all_chunks_ordered, corpus_digest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _find_anchor_hits(anchor: str, chunks: list) -> list[tuple[int, str]]:
    """返回命中锚句的 (chunk_id, 内容前 120 字预览) 列表（按 chunk_id 升序）。"""
    hits: list[tuple[int, str]] = []
    for chunk in chunks:
        if anchor in chunk.content:
            preview = chunk.content.replace("\n", " ")[:120]
            hits.append((chunk.id, preview))
    return hits


def _resolve_drafts(questions, chunks) -> tuple[list[GoldenItem], list[str]]:
    """把每道题的锚句解析成 gold_chunk_ids；返回 (成品条目, 错误清单)。

    零命中 / 多命中都记为错误并继续处理其余题目——一次运行报全所有
    对不上的题，而不是修一道错一次。
    """
    items: list[GoldenItem] = []
    errors: list[str] = []
    for draft in questions.items:
        if draft.kind == "unanswerable":
            items.append(
                GoldenItem(
                    id=draft.id,
                    kind="unanswerable",
                    question=draft.question,
                    notes=draft.notes,
                    reason=draft.reason,
                )
            )
            continue
        gold_ids: list[int] = []
        for anchor in draft.anchors:
            hits = _find_anchor_hits(anchor, chunks)
            if not hits:
                errors.append(
                    f"{draft.id} 锚句零命中（题面与语料对不上，或抄录与原文不一致）：\n"
                    f"    锚句：{anchor[:60]}{'…' if len(anchor) > 60 else ''}"
                )
            elif len(hits) > 1:
                ids = ", ".join(str(i) for i, _ in hits)
                errors.append(
                    f"{draft.id} 锚句命中 {len(hits)} 个分块，有歧义（换更长的锚句避开块间重叠）："
                    f" {ids}；锚句：{anchor[:60]}{'…' if len(anchor) > 60 else ''}"
                )
            else:
                hit_id = hits[0][0]
                if hit_id not in gold_ids:
                    gold_ids.append(hit_id)
        if errors and errors[-1].startswith(draft.id):
            continue  # 这道题已有锚句错误，不再产出半成品条目
        items.append(
            GoldenItem(
                id=draft.id,
                kind="answerable",
                question=draft.question,
                difficulty=draft.difficulty,
                gold_chunk_ids=sorted(gold_ids),
                notes=draft.notes,
            )
        )
    return items, errors


def build(
    questions_path: Path,
    out_path: Path,
    data_dir: Path,
    *,
    profile: str = "offline",
    config: Path | None = None,
) -> GoldenSet:
    """读取题本 + 打开数据目录语料 → 冻结黄金集。任一环节对不上即 EvalError。"""
    questions = load_questions_yaml(questions_path)
    settings = load_settings(profile, config, data_dir=data_dir)
    with open_db(settings.db_path) as conn:
        chunks = all_chunks_ordered(conn)
        if not chunks:
            raise EvalError(
                "语料库为空：请先运行 mikasa ingest sample-corpus 把语料入库，再冻结黄金集"
            )
        digest = corpus_digest(conn)

    items, errors = _resolve_drafts(questions, chunks)
    if errors:
        raise EvalError(
            f"{questions_path.name} 有 {len(errors)} 处锚句对不上当前语料"
            f"（指纹 {digest[:12]}…），全部列如下：\n" + "\n".join(errors)
        )

    golden = GoldenSet(
        name=questions.name,
        corpus_sha256=digest,
        items=items,
    )
    golden.validate_items()  # 成品语义校验（gold ids 非空 / 不可答题无 gold 等）
    save_golden(golden, out_path)
    return golden


def _summary(golden: GoldenSet, questions_path: Path, out_path: Path) -> str:
    """生成结束时的中文汇总（难度/原因分布，一眼看出覆盖是否合理）。"""
    answerable = golden.answerable
    unanswerable = golden.unanswerable
    difficulties = ("easy", "medium", "hard")
    by_diff = {d: sum(1 for i in answerable if i.difficulty == d) for d in difficulties}
    by_reason = {
        r: sum(1 for i in unanswerable if i.reason == r)
        for r in ("unrelated", "insufficient", "hallucination_bait")
    }
    gold_chunks = sorted({cid for i in answerable for cid in i.gold_chunk_ids})
    diff_text = " / ".join(f"{d} {by_diff[d]}" for d in difficulties)
    reason_text = " / ".join(f"{r} {by_reason[r]}" for r in by_reason)

    def shown(p: Path) -> Path:
        """仓库内显示相对路径，仓库外（--out 到临时目录）原样显示。

        2026-09-11 修复：此前直接 relative_to(REPO_ROOT)，--out 指到仓库外会
        ValueError 崩在打印阶段——构建明明已成功，退出码却是 1。
        """
        try:
            return p.relative_to(REPO_ROOT)
        except ValueError:
            return p

    return (
        f"黄金集已冻结：{shown(out_path)}\n"
        f"  题目总数：{len(golden.items)}（可答 {len(answerable)}：{diff_text}；"
        f"不可答 {len(unanswerable)}：{reason_text}）\n"
        f"  覆盖分块：{len(gold_chunks)} 个（chunk {gold_chunks[0]}-{gold_chunks[-1]}），"
        f"题源可答题锚句全部唯一命中\n"
        f"  语料指纹：{golden.corpus_sha256[:16]}…（语料变更后需重跑本脚本）\n"
        f"  题本来源：{shown(questions_path)}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把人工题本（evals/questions.yaml）的锚句解析成真实 chunk_id，"
        "连同语料指纹冻结为黄金集（evals/golden_set.json）。",
    )
    parser.add_argument(
        "--questions",
        type=Path,
        default=REPO_ROOT / "evals" / "questions.yaml",
        help="人工题本路径（默认 evals/questions.yaml）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "evals" / "golden_set.json",
        help="黄金集输出路径（默认 evals/golden_set.json）",
    )
    parser.add_argument(
        "--profile", default="offline", help="配置档（默认 offline：纯本地、无需密钥）"
    )
    parser.add_argument(
        "--config", type=Path, default=None, help="配置文件路径（默认取 profile 档）"
    )
    parser.add_argument(
        "--data-dir", type=Path, default=None, help="数据目录（默认取配置里的 data_dir）"
    )
    args = parser.parse_args(argv)

    try:
        golden = build(
            questions_path=args.questions,
            out_path=args.out,
            data_dir=args.data_dir or REPO_ROOT / "data",
            profile=args.profile,
            config=args.config,
        )
    except EvalError as exc:
        print(f"构建失败：{exc}", file=sys.stderr)
        return 1
    print(_summary(golden, args.questions, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
