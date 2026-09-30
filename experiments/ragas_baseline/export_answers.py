"""导出 RAGAS 对照用的答案/上下文/本仓指标（主 venv 跑，api 档）。

**协议（与 tech-roadmap-reply 的商定一致）**：只跑 api 档、抽 20 题（分层、种子写死）、
版本钉死。抽样与导出一次做完，落成 answers.jsonl——RAGAS 侧（独立 venv）只读它，
两边吃**逐字相同**的问题/答案/上下文，对照才成立（同 LangChain 对照组的做法）。

每题导出：
  - question / answer / refused：产品链路原样（含跨语言第二路，与评测阶段 B 一致）
  - contexts：注入生成器的片段原文（按注入顺序，= RAGAS 的 `contexts`）
  - reference：黄金集的 notes 字段（出题人写下的要点，= RAGAS 的 `reference`）
  - 本仓指标：citation gold / 越界率 / 裁判忠实性 / **claim 级支持率**（第 6 项第一半）

用法：
    MIKASA_DATA_DIR=build/eval-api/data .venv/Scripts/python.exe \
        experiments/ragas_baseline/export_answers.py --out experiments/ragas_baseline/out
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from mikasa.config.settings import load_dotenv_file, load_settings
from mikasa.eval.claims import ClaimChecker
from mikasa.eval.golden import load_golden
from mikasa.eval.judge import LLMJudge
from mikasa.index.manager import IndexManager
from mikasa.pipeline.ask import _translate_query
from mikasa.pipeline.generator import Generator, extract_markers
from mikasa.pipeline.retriever import Retriever
from mikasa.providers import get_embedding, get_llm
from mikasa.storage import repo
from mikasa.storage.db import open_db

# 抽样种子与题量写死：报告里必须能与"这张表是哪 20 题"逐条对上
SEED = 20260930
SAMPLE_SIZE = 20
REFUSAL_MARK = "根据已有资料，我无法回答这个问题。"


def stratified_sample(items: list, size: int, seed: int) -> list:
    """按难度分层抽样（与题库比例一致），种子写死 → 同一份题库抽出的题永远相同。"""
    rng = random.Random(seed)
    by_diff: dict[str, list] = {}
    for item in items:
        by_diff.setdefault(item.difficulty or "unknown", []).append(item)
    total = len(items)
    picked: list = []
    for diff in sorted(by_diff):
        bucket = sorted(by_diff[diff], key=lambda it: it.id)
        share = max(1, round(size * len(bucket) / total))
        picked.extend(rng.sample(bucket, min(share, len(bucket))))
    picked.sort(key=lambda it: it.id)
    return picked[:size]


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 RAGAS 对照的答案与上下文")
    parser.add_argument("--golden", default="evals/golden_set.json")
    parser.add_argument("--out", default="experiments/ragas_baseline/out")
    parser.add_argument("--profile", default="api")
    parser.add_argument("--size", type=int, default=SAMPLE_SIZE)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    load_dotenv_file()  # 密钥经环境变量注入（与 CLI 同一条链）
    settings = load_settings(args.profile)
    golden = load_golden(Path(args.golden))
    sample = stratified_sample(golden.answerable, args.size, args.seed)
    print(f"抽样 {len(sample)} 题（种子 {args.seed}）：" + "、".join(it.id for it in sample))

    corpus = IndexManager(settings).corpus()
    embedding = get_embedding(settings.embedding)
    llm = get_llm(settings.llm)
    retriever = Retriever(settings, corpus, embedding)
    generator = Generator(settings, llm)
    with open_db(settings.db_path) as conn:
        titles = repo.document_title_map(conn)
    judge = LLMJudge(settings.judge)
    checker = ClaimChecker(settings.judge)

    rows: list[dict] = []
    for item in sample:
        # 与评测阶段 B 逐字同轨：跨语言第二路 + 产品检索配置 + 产品生成
        second_query = _translate_query(llm, item.question) if settings.retrieval.crosslingual else None
        hits, _lat = retriever.retrieve(item.question, second_query=second_query)
        answer, _completion = generator.generate(item.question, hits, titles)
        contexts = [hit.chunk.content for hit in hits]
        by_marker = {i + 1: hit.chunk.content for i, hit in enumerate(hits)}

        # 本仓指标（同一份答案上算，供对表）
        cited_ids = [c.chunk_id for c in answer.citations]
        gold_hits = sum(1 for cid in cited_ids if cid in set(item.gold_chunk_ids))
        markers = extract_markers(answer.text)
        in_range = sum(1 for m in markers if 1 <= m <= len(hits))
        verdict = None
        if not answer.refused and answer.text.strip():
            try:
                verdict = judge.evaluate(item.question, item.notes, "\n".join(contexts)[:3000], answer.text)
            except Exception as exc:  # noqa: BLE001 - 对照导出：裁判失败不中断
                print(f"  裁判失败 {item.id}：{type(exc).__name__}")
        claim = None
        if not answer.refused and answer.text.strip():
            try:
                claim = checker.evaluate(answer.text, by_marker)
            except Exception as exc:  # noqa: BLE001
                print(f"  论断级核对失败 {item.id}：{type(exc).__name__}")

        rows.append(
            {
                "id": item.id,
                "difficulty": item.difficulty,
                "question": item.question,
                "answer": answer.text,
                "refused": answer.refused,
                "contexts": contexts,
                "reference": item.notes,
                "gold_chunk_ids": list(item.gold_chunk_ids),
                # 本仓指标（与 RAGAS 对表用）
                "ours": {
                    "citations": len(cited_ids),
                    "gold_hits": gold_hits,
                    "citation_gold": (gold_hits / len(cited_ids)) if cited_ids else None,
                    "markers": len(markers),
                    "out_of_range": len(markers) - in_range,
                    "judge_correctness": None if verdict is None else verdict.correctness,
                    "judge_faithful": None if verdict is None else verdict.faithful,
                    "judge_consistent": None if verdict is None else verdict.consistent,
                    "claim_supported": None if claim is None else claim.supported,
                    "claim_unsupported": None if claim is None else claim.unsupported,
                    "claim_uncited": None if claim is None else claim.uncited,
                    "claim_undecided": None if claim is None else claim.undecided,
                },
            }
        )
        print(f"  {item.id} 引用 {len(cited_ids)} 越界 {len(markers) - in_range} 断言 {None if claim is None else claim.supported}/{None if claim is None else claim.unsupported} 拒答 {answer.refused}")

    path = out_dir / "answers.jsonl"
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8"
    )
    print(f"已写出 {path}（{len(rows)} 题）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
