"""LangChain 对照组的**检索层**：同一份快照、同一份题库，换一套检索机器，出排名。

对照的形态（读本目录 README 的完整协议）：
- **自研侧**的排名不是在这里重算的，直接读 `export_snapshot.py` 用生产代码算好的
  `mikasa_ranked` —— 对照表里"自研"那一列必须是产品代码的输出；
- **框架侧**用 LangChain 的标准件搭同一套架构：`BM25Retriever`（rank_bm25）
  + 向量库检索 + `EnsembleRetriever`（RRF 融合，c=60）；
- 两边的评分都走本仓 `eval/metrics.py`（按文件路径加载，不引评测库也不装本仓），
  配对 bootstrap 用的也是同一份代码 —— 这才是实验，不是两套自评各说各话。

用法（在**本目录的 .venv** 下跑，见 requirements.txt）：

    experiments/langchain_baseline/.venv/Scripts/python.exe \
        experiments/langchain_baseline/run_baseline.py --out experiments/langchain_baseline/out
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
# 逐路诊断要 import 本仓的 BM25Index（指标那边走文件路径加载，不依赖这个）
sys.path.insert(0, str(REPO / "src"))


def _load_metrics():
    """按文件路径加载本仓的 metrics.py（零依赖，避免为了算几个公式装整个包）。"""
    path = REPO / "src" / "mikasa" / "eval" / "metrics.py"
    spec = importlib.util.spec_from_file_location("mikasa_eval_metrics", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # 必须先登记进 sys.modules：metrics.py 里的 @dataclass 会回头查
    # sys.modules[cls.__module__]，不登记就 AttributeError（dataclasses 的既有行为）
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


METRICS = _load_metrics()


from langchain_core.embeddings import (  # noqa: E402
    Embeddings,  # 必须继承它：FAISS / InMemoryVectorStore 都会 isinstance 校验
)


class PrecomputedEmbeddings(Embeddings):
    """把快照里算好的向量喂给 LangChain 的 Embeddings 接口。

    **为什么不在这里真的嵌一次**：同源要求两边用同一档嵌入模型，而模型的加载
    （fastembed / 云端 API）与本次对照的变量无关。快照里的向量就是在 Mikasa 侧
    用生产配置算出来的，这里按文本原样取回 —— 于是"框架拿到的向量"
    与"自研用的向量"逐字节相同，差异只剩检索机器本身。
    """

    def __init__(self, doc_vectors: dict[str, list[float]], query_vectors: dict[str, list[float]]):
        self._docs = doc_vectors
        self._queries = query_vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        missing = [t for t in texts if t not in self._docs]
        if missing:
            raise KeyError(f"快照里没有这些文本的向量（{len(missing)} 条）：{missing[0][:40]!r}…")
        return [self._docs[t] for t in texts]

    def embed_query(self, text: str) -> list[float]:
        if text not in self._queries:
            raise KeyError(f"快照里没有这个查询的向量：{text[:40]!r}…")
        return self._queries[text]


def _normalize(matrix: np.ndarray) -> np.ndarray:
    """行归一化（与 ExactVectorStore._normalize 同款，len=0 的行保持零向量）。"""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.where(norms == 0, 1.0, norms)


def _build_vectorstore(chunk_texts, chunk_metas, chunk_vecs, embeddings):
    """优先 FAISS（框架生态的默认选择），装不上则退到纯 numpy 的 InMemoryVectorStore。

    ADR-0010 记过"FAISS 在 Windows 上没有 PyPI wheel，只有 conda"，这条在
    2026-09 是否还成立由本次对照现场复核 —— 装得上就用 FAISS，装不上就用官方
    的 InMemoryVectorStore（同为精确检索，不影响"框架 vs 自研"这个变量），
    两条路都如实写进结果里。
    """
    try:
        from langchain_community.vectorstores import FAISS
        from langchain_community.vectorstores.utils import DistanceStrategy

        return FAISS.from_embeddings(
            text_embeddings=list(zip(chunk_texts, [v.tolist() for v in chunk_vecs], strict=True)),
            embedding=embeddings,
            metadatas=chunk_metas,
            # 自研侧是**归一化后的余弦**（ExactVectorStore 行归一化 + 矩阵乘），
            # 要让框架侧同一个东西：内积 + 自己先归一化（见下方 _normalize）
            distance_strategy=DistanceStrategy.MAX_INNER_PRODUCT,
        ), "faiss"
    except Exception as exc:  # noqa: BLE001 —— 装不上/建不起来都不该让整场对照停摆
        from langchain_core.vectorstores import InMemoryVectorStore

        store = InMemoryVectorStore(embedding=embeddings)
        store.add_texts(chunk_texts, metadatas=chunk_metas)
        return store, f"in-memory（FAISS 不可用：{type(exc).__name__}）"


def main() -> int:
    ap = argparse.ArgumentParser(description="跑 LangChain 检索层对照")
    ap.add_argument("--out", default=str(Path(__file__).parent / "out"), help="快照/结果目录")
    args = ap.parse_args()
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = REPO / out_dir

    snap = json.loads((out_dir / "snapshot.json").read_text(encoding="utf-8"))
    # 归一化后再交给框架：自研侧是余弦（行归一化 + 内积），FAISS 的
    # MAX_INNER_PRODUCT 只有自己归一化过才等价于余弦
    chunk_vecs = _normalize(np.load(out_dir / "chunk_vectors.npy"))
    query_vecs = _normalize(np.load(out_dir / "query_vectors.npy"))
    questions = snap["questions"]
    chunks = snap["chunks"]
    ks = tuple(snap["ks"])
    cfg = snap["retrieval"]

    import langchain
    import langchain_classic
    import langchain_core
    from langchain_classic.retrievers import EnsembleRetriever
    from langchain_community.retrievers import BM25Retriever

    # 词条走快照（两边同一个分词器），查询与语料都按原文精确取回
    token_map = {c["text"]: c["tokens"] for c in chunks}
    token_map.update({q["question"]: q["tokens"] for q in questions})
    embeddings = PrecomputedEmbeddings(
        doc_vectors={c["text"]: v.tolist() for c, v in zip(chunks, chunk_vecs, strict=True)},
        query_vectors={
            q["question"]: v.tolist() for q, v in zip(questions, query_vecs, strict=True)
        },
    )

    chunk_texts = [c["text"] for c in chunks]
    chunk_metas = [{"chunk_id": c["id"]} for c in chunks]
    store, store_kind = _build_vectorstore(chunk_texts, chunk_metas, chunk_vecs, embeddings)

    bm25 = BM25Retriever.from_texts(
        chunk_texts,
        metadatas=chunk_metas,
        preprocess_func=lambda text: token_map[text],
    )
    bm25.k = cfg["bm25_top_k"]
    dense = store.as_retriever(search_kwargs={"k": cfg["dense_top_k"]})
    # 权重等分 + c=60：与自研的 RRF（1/(k+rank)、k=60、两路等权）同一套公式，
    # 见 README「两边的差异清单」——差异要留在实现机器上，不留在融合公式上
    ensemble = EnsembleRetriever(
        retrievers=[bm25, dense],
        weights=[0.5, 0.5],
        c=cfg["fusion_k"],
        id_key="chunk_id",  # 去重按 chunk_id，不按正文（正文相同不等于同一块）
    )

    # 检索层只算可答题（不可答题没有 gold，算 recall 是无意义的 0）
    questions = [q for q in questions if q["kind"] == "answerable"]

    lc_ranked: dict[str, list[int]] = {}
    lc_ms: list[float] = []
    for q in questions:
        t0 = time.perf_counter()
        docs = ensemble.invoke(q["question"])
        lc_ms.append((time.perf_counter() - t0) * 1000.0)
        lc_ranked[q["id"]] = [d.metadata["chunk_id"] for d in docs[: cfg["fusion_top_k"]]]

    # ---- 评分：两边同一份 metrics.py ----
    arms = {"langchain": {}, "mikasa": {}}
    for name, rankings in (
        ("langchain", {qid: lc_ranked[qid] for qid in lc_ranked}),
        ("mikasa", {q["id"]: q["mikasa_ranked"] for q in questions}),
    ):
        m = METRICS.make_retrieval_metrics(ks)
        for q in questions:
            m.add_item(rankings[q["id"]], set(q["gold"]), q["difficulty"], item_id=q["id"])
        arms[name]["metrics"] = m

    def row(m) -> dict[str, float]:
        return {
            **{f"recall@{k}": METRICS.summarize(m.recall[k].values)["mean"] for k in ks},
            "mrr": METRICS.summarize(m.rr.values)["mean"],
            **{f"ndcg@{k}": METRICS.summarize(m.ndcg[k].values)["mean"] for k in ks},
        }

    # ---- 逐路诊断：融合后的差异到底来自哪一路 ----
    # 没有这一段，"哪条差异更重要"就只是猜：向量路两边本应逐字节一致（同一批
    # 向量 + 同一个余弦），BM25 那边两边是**两个不同实现**（rank_bm25.BM25Okapi
    # vs 本仓 BM25Index，IDF 公式不同），差异应当全部落在它身上。
    from rank_bm25 import BM25Okapi

    import mikasa.index.bm25 as mikasa_bm25  # 经 tokenizer→utils.logging 拉进 rich

    ours_bm25 = mikasa_bm25.BM25Index([c["tokens"] for c in chunks])
    rank_bm25 = BM25Okapi([c["tokens"] for c in chunks])
    dense_same = bm25_same = 0
    for i, q in enumerate(questions):
        ours_dense = [chunks[r]["id"] for r in np.argsort(-(chunk_vecs @ query_vecs[i]))[:20]]
        lc_dense = [
            d.metadata["chunk_id"]
            for d in store.similarity_search_by_vector(query_vecs[i].tolist(), k=20)
        ]
        if ours_dense == lc_dense:
            dense_same += 1
        ours_sparse = [chunks[r]["id"] for r, _ in ours_bm25.search(q["question"], top_k=20)]
        scores = rank_bm25.get_scores(q["tokens"])
        lc_sparse = [chunks[r]["id"] for r in np.argsort(-scores)[:20]]
        if ours_sparse == lc_sparse:
            bm25_same += 1

    mk, lc = arms["mikasa"]["metrics"], arms["langchain"]["metrics"]
    rows = {"mikasa": row(mk), "langchain": row(lc)}

    # 配对 bootstrap：同一道题上两组值的逐条差值（配对键 = 题号，顺序两边一致）
    qids = [q["id"] for q in questions]

    def per_item(m, key: str, k: int) -> list[float]:
        return [m.items[qid][key][k] for qid in qids]

    paired = {
        f"recall@{k}": METRICS.paired_bootstrap(
            per_item(mk, "recall_at", k), per_item(lc, "recall_at", k)
        )
        for k in ks
    }
    paired["mrr"] = METRICS.paired_bootstrap(
        [mk.items[qid]["rr"] for qid in qids], [lc.items[qid]["rr"] for qid in qids]
    )

    mk_ms = [q["mikasa_ms"] - q["mikasa_embed_ms"] for q in questions]
    timing = {
        "mikasa": statistics.median(mk_ms),
        "langchain": statistics.median(lc_ms),
    }

    # ---- 逐题差异：谁在哪几道题上不同（给人工复核用）----
    diffs = []
    for q in questions:
        ours, theirs = set(q["mikasa_ranked"][:10]), set(lc_ranked[q["id"]][:10])
        if ours != theirs:
            diffs.append(
                {
                    "id": q["id"],
                    "difficulty": q["difficulty"],
                    "question": q["question"],
                    "only_mikasa": sorted(ours - theirs),
                    "only_langchain": sorted(theirs - ours),
                    "gold": q["gold"],
                    "mikasa_recall@10": mk.items[q["id"]]["recall_at"][10],
                    "langchain_recall@10": lc.items[q["id"]]["recall_at"][10],
                }
            )

    payload = {
        "snapshot": {
            "profile": snap["profile"],
            "embedding_model": snap["embedding_model"],
            "corpus_sha256": snap["corpus_sha256"],
            "corpus_size": snap["corpus_size"],
            "tokenizer": snap["tokenizer"],
            "retrieval": cfg,
        },
        "versions": {
            "langchain": langchain.__version__,
            "langchain_classic": langchain_classic.__version__,
            "langchain_core": langchain_core.__version__,
            "vectorstore": store_kind,
        },
        "rows": rows,
        "paired": paired,
        "per_arm_diagnosis": {
            "n": len(questions),
            "dense_top20_identical": dense_same,
            "bm25_top20_identical": bm25_same,
        },
        "timing_ms_p50": timing,
        "diffs": diffs,
        "rankings": {
            "langchain": lc_ranked,
            "mikasa": {q["id"]: q["mikasa_ranked"] for q in questions},
        },
    }
    (out_dir / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    # ---- 落一份人读的表 ----
    lines = [
        "# LangChain 检索层对照（结果）",
        "",
        f"- 题量：{len(questions)}（可答）｜语料 {snap['corpus_size']} 块｜"
        f"嵌入 {snap['embedding_model']}｜分词 {snap['tokenizer']}",
        f"- 口径：关重排、融合窗口 {cfg['fusion_top_k']}、bm25_top_k={cfg['bm25_top_k']}、"
        f"dense_top_k={cfg['dense_top_k']}、RRF k={cfg['fusion_k']}",
        f"- 框架版本：langchain {langchain.__version__} / langchain-classic "
        f"{langchain_classic.__version__} / langchain-core {langchain_core.__version__}"
        f"｜向量库：{store_kind}",
        "",
        "| 指标 | 自研（生产代码） | LangChain | 配对差值（LC − 自研） | 95% CI | 显著 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for k in ks:
        p = paired[f"recall@{k}"]
        lines.append(
            f"| recall@{k} | {rows['mikasa'][f'recall@{k}']:.3f} | "
            f"{rows['langchain'][f'recall@{k}']:.3f} | {p['delta']:+.3f} | "
            f"[{p['lo']:+.3f}, {p['hi']:+.3f}] | {'是' if p['significant'] else '否'} |"
        )
    p = paired["mrr"]
    lines.append(
        f"| MRR | {rows['mikasa']['mrr']:.3f} | {rows['langchain']['mrr']:.3f} | "
        f"{p['delta']:+.3f} | [{p['lo']:+.3f}, {p['hi']:+.3f}] | "
        f"{'是' if p['significant'] else '否'} |"
    )
    for k in ks:
        lines.append(
            f"| nDCG@{k} | {rows['mikasa'][f'ndcg@{k}']:.3f} | "
            f"{rows['langchain'][f'ndcg@{k}']:.3f} | — | — | — |"
        )
    lines += [
        "",
        f"检索耗时（中位数，**不含查询嵌入**）：自研 {timing['mikasa']:.1f}ms ／ "
        f"LangChain {timing['langchain']:.1f}ms",
        "",
        f"前 10 名完全一致的题：{len(questions) - len(diffs)}/{len(questions)}"
        f"；不同的 {len(diffs)} 题逐条见 result.json 的 `diffs`。",
        "",
        "## 逐路诊断（差异来自哪一路）",
        "",
        f"- 稠密路 top-20 逐题一致：**{dense_same}/{len(questions)}**"
        "（同一批向量 + 同一个余弦，本应完全一致）",
        f"- BM25 路 top-20 逐题一致：**{bm25_same}/{len(questions)}**"
        "（两个不同实现：rank_bm25.BM25Okapi vs 本仓 BM25Index，IDF 公式不同）",
        "",
        "> 读法：稠密路一致、稀疏路不一致 → 融合后的差异**全部来自 BM25 实现**，"
        "而不是「框架的向量检索更差/更好」。",
        "",
    ]
    (out_dir / "result.md").write_text("\n".join(lines), encoding="utf-8")

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
