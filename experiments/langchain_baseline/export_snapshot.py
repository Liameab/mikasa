"""导出评测快照：把 Mikasa 的语料、向量、题库固化成一个文件，供 LangChain 对照组读取。

**为什么要有这一步**（对照协议的硬要求，见本目录 README）：
对照组必须跑在**同一份语料 + 同一份题库 + 同一档嵌入模型**上，否则差异里混进了
"换了语料/换了模型"这个更大的变量。Mikasa 这边的语料、分块、向量都在它的数据目录里，
而 LangChain 那侧不该（也不能）去读它的 SQLite —— 于是中间放一份快照：本脚本用
**Mikasa 自己的生产代码**（IndexManager 建快照、phase-A 口径的 Retriever 检索）把
一切都算好并落盘，对照组只做"读快照 → 换一套检索机器 → 出排名"。

有意的两件事：

1. **自研侧的排名也在这里算**，走的是真实的 `Retriever.retrieve`（不是对照脚本里
   重写一遍）。对照表里"自研"那一列因此是产品代码的输出，不是复刻品。
2. **分词的词条一并导出**（`tokens`）：两边用**同一个分词器**是"同源"的一部分，
   而对照组那边不一定装得上 jieba（也不需要装）——把词条当数据发过去，
   比让它在对面猜一套分词规则更公平。

用法（在**主 venv** 下跑，需要 fastembed 与本仓依赖）：

    .venv/Scripts/python.exe experiments/langchain_baseline/export_snapshot.py \
        --data-dir build/eval-local/data --profile local --out experiments/langchain_baseline/out
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

# 脚本在 experiments/ 下，仓库根是上两级；把 src 挂上 sys.path 才能 import mikasa
# （对照组那侧同样这么干，两边都不依赖 pip install -e .）
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from mikasa.config.settings import load_settings  # noqa: E402
from mikasa.eval.runner import RETRIEVAL_FUSION_TOP_K, RETRIEVAL_KS  # noqa: E402
from mikasa.index.manager import IndexManager  # noqa: E402
from mikasa.index.tokenizer import get_tokenizer, get_tokenizer_name  # noqa: E402
from mikasa.pipeline.retriever import Retriever  # noqa: E402
from mikasa.providers import get_embedding  # noqa: E402


def _phase_a_settings(settings):  # type: ignore[no-untyped-def]
    """复刻评测阶段 A 的口径：关重排 + 融合窗口锚定常量（与 runner 逐字一致）。

    不直接调 runner 的私有方法：那是为了避免"对照组悄悄跑在另一个口径下"，
    这里显式抄一遍并把常量从 runner import 过来——常量改了这边跟着改，
    口径漂移会在快照的 config 段里露出来。
    """
    return settings.model_copy(
        update={
            "retrieval": settings.retrieval.model_copy(
                update={"fusion_top_k": RETRIEVAL_FUSION_TOP_K}
            ),
            "reranker": settings.reranker.model_copy(update={"backend": "none"}),
        }
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="导出 LangChain 对照用的评测快照")
    ap.add_argument("--data-dir", required=True, help="Mikasa 数据目录（如 build/eval-local/data）")
    ap.add_argument("--profile", default="local", help="运行档位（决定嵌入模型）")
    ap.add_argument("--golden", default="evals/golden_set.json", help="黄金集路径（仓库相对）")
    ap.add_argument("--out", required=True, help="输出目录")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = REPO / data_dir
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = REPO / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    golden_path = Path(args.golden)
    if not golden_path.is_absolute():
        golden_path = REPO / golden_path
    golden = json.loads(golden_path.read_text(encoding="utf-8"))

    settings = load_settings(args.profile, data_dir=data_dir)
    corpus = IndexManager(settings).corpus()
    if corpus.matrix is None:
        print("语料快照没有向量（dense 路不可用）——对照组需要它，先 ingest --reindex")
        return 1
    embedding = get_embedding(settings.embedding)
    tokenizer = get_tokenizer()

    cfg = settings.retrieval
    print(
        f"语料：{len(corpus.chunks)} 块 / 嵌入 {corpus.embedding_model} / "
        f"分词 {get_tokenizer_name()} / 指纹 {corpus.sha256[:16]}…"
    )

    # ---- 自研侧：走真实 Retriever，逐题记排名与耗时 ----
    # 两套口径都要：检索层对照用阶段 A 的窗口（10，与评测的检索指标同源），
    # 生成层对照要用**产品配置的注入窗口**（local 档是 14）——否则框架侧看到的
    # 上下文比自研少几块，token 与回答长度的对比就全歪了
    retriever = Retriever(_phase_a_settings(settings), corpus, embedding)
    product_retriever = Retriever(settings, corpus, embedding)
    ids = [c.id for c in corpus.chunks]
    if any(cid is None for cid in ids):
        print("快照里有 id 为空的块，无法对照")
        return 1
    id_to_row = {cid: row for row, cid in enumerate(ids)}  # type: ignore[misc]

    questions: list[dict] = []
    query_vectors: list[np.ndarray] = []
    skipped: list[str] = []
    # 可答 + 不可答**都要**：检索层只算可答题，但生成层的拒答纪律只在不可答题上
    # 才看得出来——只导可答题的话，对照组就没法回答"框架默认链会不会乱答"
    for item in golden["items"]:
        gold = [cid for cid in item.get("gold_chunk_ids") or [] if cid in id_to_row]
        if len(gold) != len(item.get("gold_chunk_ids") or []):
            # 题面 gold 块不在当前快照里（语料变过）：跳过并留痕，不用错位的答案算分
            skipped.append(item["id"])
            continue
        q = item["question"]
        t0 = time.perf_counter()
        vec = embedding.embed_query(q)
        embed_ms = (time.perf_counter() - t0) * 1000.0
        hits, lat = retriever.retrieve(q)
        ranked = [h.chunk.id for h in hits]
        product_hits, _ = product_retriever.retrieve(q)
        questions.append(
            {
                "id": item["id"],
                "kind": item.get("kind") or "answerable",
                "reason": item.get("reason"),  # 不可答题的失守类型（unrelated 等）
                "difficulty": item.get("difficulty") or "unknown",
                "question": q,
                "gold": gold,
                "tokens": tokenizer(q),
                # mikasa_ranked：阶段 A 口径（融合窗口 10）——检索层对照用
                # mikasa_product_ranked：产品配置口径（local 档 14）——生成层对照用
                "mikasa_ranked": ranked,
                "mikasa_product_ranked": [h.chunk.id for h in product_hits],
                # 分段：检索总耗时里含查询嵌入，单列出来，好让两边的"纯检索"
                # 耗时可比（对照组用的是预计算好的查询向量）
                "mikasa_ms": lat["retrieve"],
                "mikasa_embed_ms": embed_ms,
            }
        )
        query_vectors.append(np.asarray(vec, dtype=np.float32))

    if skipped:
        print(f"跳过 {len(skipped)} 题（gold 块不在快照里）：{', '.join(skipped)}")
    if not questions:
        print("没有可对照的题目")
        return 1

    snapshot = {
        "profile": args.profile,
        "embedding_model": corpus.embedding_model,
        "corpus_sha256": corpus.sha256,
        "corpus_size": len(corpus.chunks),
        "tokenizer": get_tokenizer_name(),
        "ks": list(RETRIEVAL_KS),
        "retrieval": {
            "bm25_top_k": cfg.bm25_top_k,
            "dense_top_k": cfg.dense_top_k,
            "fusion_top_k": RETRIEVAL_FUSION_TOP_K,  # 阶段 A 锚定值（检索层对照）
            "product_fusion_top_k": cfg.fusion_top_k,  # 产品注入窗口（生成层对照）
            "fusion_k": cfg.fusion_k,
        },
        "chunks": [
            {"id": c.id, "document_id": c.document_id, "text": c.content, "tokens": c.tokens}
            for c in corpus.chunks
        ],
        "questions": questions,
    }
    (out_dir / "snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    np.save(out_dir / "chunk_vectors.npy", np.asarray(corpus.matrix, dtype=np.float32))
    np.save(out_dir / "query_vectors.npy", np.stack(query_vectors))

    mean_ms = sum(q["mikasa_ms"] for q in questions) / len(questions)
    mean_embed = sum(q["mikasa_embed_ms"] for q in questions) / len(questions)
    print(
        f"已写出 {out_dir}：{len(questions)} 题 / 自研侧检索均值 {mean_ms:.1f}ms"
        f"（其中查询嵌入 {mean_embed:.1f}ms）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
