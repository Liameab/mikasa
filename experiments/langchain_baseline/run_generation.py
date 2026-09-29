"""LangChain 对照组的**生成/引用层**：框架默认的 QA 链 vs 本仓的协议层。

**比的是什么**（这条比检索层更容易比歪，所以口径先写死）：

- **上下文逐字相同**：两边都吃快照里同一批块（产品注入窗口 `product_fusion_top_k`，
  local 档 14 块），框架侧不自己检索、自研侧也不自己检索 —— 差的只有"生成这一层"。
  （为什么不用评测报告里那场：生产链路还叠了跨语言第二路，注入集与这里不同，
  实测 5 题里 14 块有 5~8 块不一样，混进来就说不清差异是谁的。见 README。）
- **框架侧用 LangChain 自己的默认链**：`RetrievalQA.from_chain_type(chain_type="stuff")`
  —— 任何一篇 "LangChain RAG 入门" 里拿到的东西：默认提示词（资料拼成一段没有编号的
  context）+ `StrOutputParser`（**没有校验**）。这是"框架默认给你什么"的实测。
- **自研侧走产品代码**：`mikasa.pipeline.generator.Generator`（同一份 SYSTEM_PROMPT、
  同一套 `[n]` 解析与拒答判定），模型/温度/上下文长度两边都是 local 档那套。
- **评分也是产品代码**：`extract_markers`、`REFUSAL_TEXT`、`Answer.refused` 都从
  `mikasa.pipeline` import，不在对照脚本里重写一套。

用法（框架 venv，且本机 Ollama 在跑、有 qwen3:8b）：

    experiments/langchain_baseline/.venv/Scripts/python.exe \
        experiments/langchain_baseline/run_generation.py --out experiments/langchain_baseline/out
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

# 复用本仓的协议层定义：编号怎么解析、什么算拒答，都以产品代码为准
from mikasa.config.settings import load_settings  # noqa: E402
from mikasa.models.document import Chunk  # noqa: E402
from mikasa.models.retrieval import RetrievedChunk  # noqa: E402
from mikasa.pipeline.generator import Generator, extract_markers  # noqa: E402
from mikasa.providers import get_llm  # noqa: E402

# 宽松拒答口径（仅用于给框架侧一个机会）：模型换了提示词就不会用我们的统一句式，
# 严格口径下它的拒答率必然是 0——那既不公平也没信息量，所以两个口径都报。
_LOOSE_REFUSAL_HINTS = ("无法回答", "没有相关", "没有提及", "未提及", "资料中没有", "不知道")


def _score(text: str, n_ctx: int, refused_strict: bool) -> dict:
    """两套链路共用的打分：只认正文文本（谁生成的、怎么生成的都不看）。"""
    markers = extract_markers(text)
    return {
        "markers": markers,
        "out_of_range": sorted({m for m in markers if not 1 <= m <= n_ctx}),
        "refused_strict": refused_strict,
        "refused_loose": refused_strict
        or (not markers and any(h in text for h in _LOOSE_REFUSAL_HINTS)),
        "chars": len(text.strip()),
        "sections": text.count("\n## "),
        "bullets": sum(1 for line in text.splitlines() if line.lstrip()[:2] in ("- ", "* ")),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="LangChain 默认 QA 链 vs 本仓协议层")
    ap.add_argument("--out", default=str(Path(__file__).parent / "out"), help="快照/结果目录")
    ap.add_argument("--model", default="qwen3:8b", help="Ollama 模型（与 local 档一致）")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题（冒烟用；0 = 全部）")
    args = ap.parse_args()
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = REPO / out_dir

    snap = json.loads((out_dir / "snapshot.json").read_text(encoding="utf-8"))
    chunks = {c["id"]: c for c in snap["chunks"]}
    questions = snap["questions"][: args.limit or len(snap["questions"])]
    # 产品注入窗口（local 档 14）= 评测阶段 B 真实喂给模型的块数，两边一致
    top_k = snap["retrieval"]["product_fusion_top_k"]

    from langchain_classic.chains import RetrievalQA
    from langchain_core.callbacks import BaseCallbackHandler, CallbackManagerForRetrieverRun
    from langchain_core.documents import Document
    from langchain_core.retrievers import BaseRetriever
    from langchain_ollama import ChatOllama

    class UsageCapture(BaseCallbackHandler):
        """把最后一次 LLM 调用的用量留下来（RetrievalQA 的返回值里没有它）。

        成本对照要的是 token 数——框架的默认链把 AIMessage 吞掉了，
        只能从回调里捞（`usage_metadata` 由 langchain-ollama 填）。
        """

        last: dict | None = None

        def on_llm_end(self, response, **kwargs) -> None:  # type: ignore[no-untyped-def]
            try:
                message = response.generations[0][0].message
                self.last = message.usage_metadata or None
            except Exception:  # noqa: BLE001
                self.last = None

    class FrozenRetriever(BaseRetriever):
        """把这一批块原样交出去（两边上下文逐字相同）。"""

        docs: dict[str, list[Document]]

        def _get_relevant_documents(
            self, query: str, *, run_manager: CallbackManagerForRetrieverRun
        ) -> list[Document]:
            return self.docs[query]

    def context_of(q: dict) -> list[int]:
        return q["mikasa_product_ranked"][:top_k]

    docs = {
        q["question"]: [
            Document(page_content=chunks[cid]["text"], metadata={"chunk_id": cid})
            for cid in context_of(q)
        ]
        for q in questions
    }

    # ---- 框架侧：默认 stuff QA 链 ----
    try:
        # think 关掉：与 local 档一致（qwen3:8b 开着思考会把预算吃光，见 profiles/local.yaml）
        llm = ChatOllama(model=args.model, temperature=0.1, num_ctx=16384, reasoning=False)
    except Exception:  # noqa: BLE001 —— 老版本 langchain-ollama 没有这个参数
        llm = ChatOllama(model=args.model, temperature=0.1, num_ctx=16384)
    chain = RetrievalQA.from_chain_type(
        llm=llm, chain_type="stuff", retriever=FrozenRetriever(docs=docs)
    )

    # ---- 自研侧：产品 Generator（同一批块，不走检索） ----
    settings = load_settings("local")  # 模型/温度/num_ctx/max_tokens 全按 local 档
    generator = Generator(settings, get_llm(settings.llm))
    hits_of: dict[str, list[RetrievedChunk]] = {}
    for q in questions:
        hits = []
        for rank, cid in enumerate(context_of(q), start=1):
            text = chunks[cid]["text"]
            chunk = Chunk(
                id=cid,
                document_id=chunks[cid]["document_id"],
                seq=rank,
                content=text,
                content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                tokens=chunks[cid]["tokens"],
            )
            hits.append(RetrievedChunk(chunk=chunk, rank=rank))
        hits_of[q["question"]] = hits

    def run_lc(q: dict) -> tuple[str, dict, dict | None]:
        capture = UsageCapture()
        out = chain.invoke({"query": q["question"]}, config={"callbacks": [capture]})
        return (out.get("result") or "").strip(), {}, capture.last

    def run_mikasa(q: dict) -> tuple[str, dict, dict | None]:
        answer, completion = generator.generate(q["question"], hits_of[q["question"]], {})
        usage = {
            "input_tokens": completion.prompt_tokens,
            "output_tokens": completion.completion_tokens,
        }
        return answer.text, {"refused": answer.refused}, usage

    rows: list[dict] = []
    for q in questions:
        n_ctx = len(docs[q["question"]])
        entry: dict = {"id": q["id"], "kind": q["kind"], "reason": q["reason"], "n_context": n_ctx}
        for arm, runner in (("langchain", run_lc), ("mikasa", run_mikasa)):
            t0 = time.perf_counter()
            try:
                text, flags, usage = runner(q)
            except Exception as exc:  # noqa: BLE001 —— 单题失败不该中断整场
                text, flags, usage = f"[链失败] {type(exc).__name__}: {exc}", {}, None
            elapsed = (time.perf_counter() - t0) * 1000.0
            scored = _score(text, n_ctx, bool(flags.get("refused")))
            scored.update(
                {
                    "answer": text,
                    "ms": elapsed,
                    "prompt_tokens": (usage or {}).get("input_tokens"),
                    "completion_tokens": (usage or {}).get("output_tokens"),
                }
            )
            entry[arm] = scored
        rows.append(entry)
        print(
            f"{q['id']} {q['kind'][:5]} 框架标记={len(entry['langchain']['markers'])}"
            f" 自研标记={len(entry['mikasa']['markers'])}"
            f" 框架{entry['langchain']['chars']}字/自研{entry['mikasa']['chars']}字"
        )

    answerable = [r for r in rows if r["kind"] == "answerable"]
    unanswerable = [r for r in rows if r["kind"] != "answerable"]

    def rate(items, arm, pred) -> float:
        return sum(1 for r in items if pred(r[arm])) / len(items) if items else float("nan")

    def mean(items, arm, key) -> float:
        return sum(r[arm][key] for r in items) / len(items) if items else float("nan")

    summary = {}
    for arm in ("langchain", "mikasa"):
        with_tokens = [r for r in rows if r[arm]["prompt_tokens"]]
        summary[arm] = {
            "cited_rate": rate(answerable, arm, lambda x: bool(x["markers"])),
            "out_of_range_rate": rate(answerable, arm, lambda x: bool(x["out_of_range"])),
            "markers_per_answer": (
                sum(len(r[arm]["markers"]) for r in answerable) / len(answerable)
                if answerable
                else 0.0
            ),
            "chars_per_answer": mean(answerable, arm, "chars"),
            "sections_per_answer": mean(answerable, arm, "sections"),
            "bullets_per_answer": mean(answerable, arm, "bullets"),
            "refusal_strict": rate(unanswerable, arm, lambda x: x["refused_strict"]),
            "refusal_loose": rate(unanswerable, arm, lambda x: x["refused_loose"]),
            "median_ms": sorted(r[arm]["ms"] for r in rows)[len(rows) // 2],
            "prompt_tokens": sum(r[arm]["prompt_tokens"] or 0 for r in with_tokens),
            "completion_tokens": sum(r[arm]["completion_tokens"] or 0 for r in with_tokens),
            "token_samples": len(with_tokens),
        }
    summary["model"] = args.model
    summary["n"] = len(rows)
    summary["answerable"] = len(answerable)
    summary["unanswerable"] = len(unanswerable)
    summary["top_k"] = top_k
    (out_dir / "generation.json").write_text(
        json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

    lc, mk = summary["langchain"], summary["mikasa"]
    lines = [
        "# LangChain 默认 QA 链 vs 本仓协议层（生成/引用层）",
        "",
        f"- 模型：{args.model}（Ollama）｜题量 {summary['n']}"
        f"（可答 {summary['answerable']} / 不可答 {summary['unanswerable']}）",
        f"- 上下文：两边逐题完全相同的 {top_k} 块（框架侧不自己检索）",
        '- 框架侧：`RetrievalQA.from_chain_type(chain_type="stuff")`'
        "（默认提示词 + 无校验的解析，即入门教程拿到的那个）",
        "- 自研侧：`mikasa.pipeline.generator.Generator`（生产提示词 + `[n]` 校验 + 拒答门）",
        "",
        "| 指标 | LangChain 默认链 | 本仓协议层 |",
        "| --- | --- | --- |",
        f"| 回答里出现 `[n]` 标记的比例（可答） | {lc['cited_rate']:.1%} | "
        f"{mk['cited_rate']:.1%} |",
        f"| 每份回答平均标记数 | {lc['markers_per_answer']:.2f} | {mk['markers_per_answer']:.2f} |",
        f"| 越界标记出现率 | {lc['out_of_range_rate']:.1%} | {mk['out_of_range_rate']:.1%} |",
        f"| 拒答率（严格口径，不可答） | {lc['refusal_strict']:.1%} | {mk['refusal_strict']:.1%} |",
        f"| 拒答率（宽松口径，不可答） | {lc['refusal_loose']:.1%} | {mk['refusal_loose']:.1%} |",
        f"| 平均字数（可答） | {lc['chars_per_answer']:.0f} | {mk['chars_per_answer']:.0f} |",
        f"| 平均分节数 / 列表项（可答） | {lc['sections_per_answer']:.2f} / "
        f"{lc['bullets_per_answer']:.2f} | {mk['sections_per_answer']:.2f} / "
        f"{mk['bullets_per_answer']:.2f} |",
        f"| 单题中位耗时 | {lc['median_ms'] / 1000:.1f}s | {mk['median_ms'] / 1000:.1f}s |",
        f"| 输入 / 输出 tokens（{lc['token_samples']} 题有记录） | {lc['prompt_tokens']:,} / "
        f"{lc['completion_tokens']:,} | {mk['prompt_tokens']:,} / {mk['completion_tokens']:,} |",
        "",
        "> 严格口径 = 回答逐字等于本仓的 `REFUSAL_TEXT`（评测的机械判据）；",
        "> 宽松口径 = 无标记且含「说不出来」类措辞。框架侧换了提示词，",
        "> 严格口径必然是 0——两个都报，读者自己判断。",
        "> token 只统计「记到用量」的题（两边都取自 Ollama 响应里的计数，同源）。",
        "",
    ]
    (out_dir / "generation.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
