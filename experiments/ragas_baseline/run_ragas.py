"""在对照组 venv 里跑 RAGAS 四维（faithfulness / answer relevancy /
context precision / context recall），产 out/ragas.json。

**对照，不底座**：本目录不进 src/、不进主 pyproject、不进主 CI；主仓只留结论
（docs/evaluation.md §11）。输入是主 venv 导出的 out/answers.jsonl——两边吃逐字
相同的题目/答案/上下文，对照才对得上（同 LangChain 对照组）。

裁判模型 = 与主仓评测**同一个**（SiliconFlow Qwen2.5-72B，见 config/profiles/api.yaml），
嵌入 = 同一家（bge-m3）：把"谁的裁判更严"这个变量也钉住，剩下的差异才归到**刻度**上。

用法：
    experiments/ragas_baseline/.venv/Scripts/python.exe \
        experiments/ragas_baseline/run_ragas.py --out experiments/ragas_baseline/out
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def load_env(path: Path) -> None:
    """极简 .env 读取（本环境没装 python-dotenv，只认 KEY=VALUE 行）。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    parser = argparse.ArgumentParser(description="RAGAS 四维对照")
    parser.add_argument("--answers", default="experiments/ragas_baseline/out/answers.jsonl")
    parser.add_argument("--out", default="experiments/ragas_baseline/out")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--judge-model", default="Qwen/Qwen2.5-72B-Instruct")
    parser.add_argument("--embed-model", default="BAAI/bge-m3")
    parser.add_argument("--base-url", default="https://api.siliconflow.cn/v1")
    args = parser.parse_args()

    load_env(Path(args.env))
    api_key = os.environ.get("SILICONFLOW_API_KEY")
    if not api_key:
        raise SystemExit("缺少 SILICONFLOW_API_KEY（.env 或环境变量）")

    from langchain_openai import ChatOpenAI, OpenAIEmbeddings
    from ragas import EvaluationDataset, RunConfig, SingleTurnSample, evaluate
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import (
        AnswerRelevancy,
        Faithfulness,
        LLMContextPrecisionWithReference,
        LLMContextRecall,
    )

    judge = LangchainLLMWrapper(
        ChatOpenAI(
            model=args.judge_model,
            base_url=args.base_url,
            api_key=api_key,
            temperature=0.0,
            timeout=120,
            max_retries=3,
        )
    )
    embeddings = LangchainEmbeddingsWrapper(
        OpenAIEmbeddings(model=args.embed_model, base_url=args.base_url, api_key=api_key)
    )

    rows = [
        json.loads(line)
        for line in Path(args.answers).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    samples = [
        SingleTurnSample(
            user_input=row["question"],
            response=row["answer"],
            retrieved_contexts=row["contexts"],
            reference=row["reference"] or "",
        )
        for row in rows
    ]
    print(f"待评 {len(samples)} 题；裁判 {args.judge_model}，嵌入 {args.embed_model}")

    dataset = EvaluationDataset(samples=samples)
    # 并发上限与重试是**必须**的（2026-09-30 实测）：默认并发会把 SiliconFlow 打到
    # 429，ragas 把失败记成 NaN——上一版 20 题里有 7~14 题整条指标是 NaN，
    # 而均值只在幸存样本上算（看着像"结果"，其实是有偏子样本）。max_workers 压到 2 +
    # 大 timeout + 多次重试后失败率归零；**失败数仍进结果 JSON**，不许静默。
    run_config = RunConfig(max_workers=2, timeout=180, max_retries=8)
    result = evaluate(
        dataset,
        metrics=[
            Faithfulness(),
            AnswerRelevancy(),
            LLMContextPrecisionWithReference(),
            LLMContextRecall(),
        ],
        llm=judge,
        embeddings=embeddings,
        run_config=run_config,
        show_progress=True,
        raise_exceptions=False,
    )

    frame = result.to_pandas()
    per_item = []
    for index, row in frame.iterrows():
        per_item.append(
            {
                "id": rows[index]["id"],
                "faithfulness": _num(row.get("faithfulness")),
                "answer_relevancy": _num(row.get("answer_relevancy")),
                "context_precision": _num(row.get("llm_context_precision_with_reference")),
                "context_recall": _num(row.get("context_recall")),
            }
        )

    keys = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")
    aggregates = {
        key: _mean([item[key] for item in per_item if item[key] is not None]) for key in keys
    }
    # 失败计数与均值同权披露：NaN 是"没测到"，不是 0，也不是"通过了"
    failures = {
        key: sum(1 for item in per_item if item[key] is None) for key in keys
    }
    payload = {
        "judge_model": args.judge_model,
        "embed_model": args.embed_model,
        "n": len(per_item),
        "aggregates": aggregates,
        "failures": failures,
        "items": per_item,
    }
    out_path = Path(args.out) / "ragas.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print("RAGAS 均值：", {k: (round(v, 3) if v is not None else None) for k, v in aggregates.items()})
    print("未测到（NaN）题数：", failures)
    print(f"已写出 {out_path}")
    return 0


def _num(value: object) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)  # type: ignore[arg-type]
        return None if number != number else number  # NaN → None
    except (TypeError, ValueError):
        return None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


if __name__ == "__main__":
    raise SystemExit(main())
