"""检索器单测：BM25 单路（offline）命中与排序、延迟分段、空结果。

offline profile：无向量、无重排 → dense/rerank 分段为 0，
这是 CI 与离线演示的确定性基线（M2 评测会对比混合检索）。
"""

from __future__ import annotations

from pathlib import Path

from mikasa.index.manager import IndexManager
from mikasa.ingest.service import IngestService
from mikasa.pipeline.retriever import Retriever
from mikasa.providers import get_embedding

NOTE = """# 量子计算笔记

## 退火算法

量子退火通过缓慢降温度让系统落入能量最低的基态。

## 纠错码

表面码是容错量子计算最有前景的纠错方案之一。
"""


def _seed(tmp_path: Path, offline_settings) -> None:
    md = tmp_path / "量子.md"
    md.write_text(NOTE, encoding="utf-8")
    IngestService(offline_settings).ingest_paths([tmp_path])


def _retriever(tmp_path: Path, offline_settings) -> tuple[Retriever, object]:
    _seed(tmp_path, offline_settings)
    corpus = IndexManager(offline_settings).corpus()
    embedding = get_embedding(offline_settings.embedding)
    return Retriever(offline_settings, corpus, embedding), corpus


def test_bm25_hit_ordering_and_latency(tmp_path, offline_settings):
    retriever, corpus = _retriever(tmp_path, offline_settings)
    hits, latency = retriever.retrieve("量子退火如何让系统落入能量最低的基态")

    assert hits, "语料内问题应有命中"
    assert latency["retrieve"] >= 0.0
    assert latency["rerank"] == 0.0  # offline 无重排
    assert all(h.rank == i + 1 for i, h in enumerate(hits))  # rank 连续 1-based
    from itertools import pairwise

    assert all(h.rank < h2.rank for h, h2 in pairwise(hits))

    # 分数域说明：fused_score = RRF 分（>0）；单路 BM25 分携带原值
    top = hits[0]
    assert top.fused_score > 0.0
    assert top.bm25_score is not None and top.bm25_score > 0.0
    assert top.chunk.content.startswith("量子退火通过缓慢降温度")


def test_out_of_corpus_question_returns_empty(tmp_path, offline_settings):
    """零词项重叠（中文问题 vs 英文语料）：两路都无命中 → 空结果。

    设计说明：同语言里任意两句几乎必有共享词项（的/在/是…），
    少量词项重叠时的"无关"判定在生成层阈值拒答兜底（test_generator）；
    检索层只保证词项空间完全不相交时给空集（评测的 recall 分母）。
    """
    md = tmp_path / "en.md"
    md.write_text(
        "# Quantum Notes\n\n## Annealing\n\nQuantum annealing cools a system "
        "into its lowest-energy ground state.\n",
        encoding="utf-8",
    )
    IngestService(offline_settings).ingest_paths([tmp_path])
    corpus = IndexManager(offline_settings).corpus()
    embedding = get_embedding(offline_settings.embedding)
    retriever = Retriever(offline_settings, corpus, embedding)
    hits, latency = retriever.retrieve("足球比赛裁判出示红牌的规则")
    assert hits == []
    assert set(latency) == {"retrieve", "rerank"}


def test_ranks_are_fused_scores_sorted(tmp_path, offline_settings):
    retriever, _ = _retriever(tmp_path, offline_settings)
    hits, _ = retriever.retrieve("纠错码与表面码容错量子计算方案")
    fused = [h.fused_score for h in hits]
    assert fused == sorted(fused, reverse=True)


# ---------------------------------------------------------------------------
# 跨语言第二路查询（2026-09-10）：中文主路 + 译后英文查询 RRF 合流
# ---------------------------------------------------------------------------

EN_NOTE = """# Quantum Notes

## Annealing

Quantum annealing cools a system into its lowest-energy ground state.
"""


def _seed_bilingual(tmp_path, offline_settings) -> None:
    (tmp_path / "量子.md").write_text(NOTE, encoding="utf-8")
    (tmp_path / "quantum.md").write_text(EN_NOTE, encoding="utf-8")
    IngestService(offline_settings).ingest_paths([tmp_path])


def test_second_query_bridges_cross_language(tmp_path, offline_settings):
    """第二路译后英文查询召回单路跨语言必空的英文块（RRF 合流补位）。

    场景 = 2026-09-10 实况：offline 无向量路 + BM25 跨语言词面零命中，
    中文问题命中不了英文文档，加第二查询后英文块进入融合结果，且其
    bm25_score 由第二路补位（主查询分数优先的 setdefault 语义）。
    """
    _seed_bilingual(tmp_path, offline_settings)
    corpus = IndexManager(offline_settings).corpus()
    embedding = get_embedding(offline_settings.embedding)
    retriever = Retriever(offline_settings, corpus, embedding)

    question = "量子退火如何让系统落入能量最低的基态"
    en_query = "How does quantum annealing cool a system into its ground state"

    hits_zh, _ = retriever.retrieve(question)
    assert hits_zh, "主路在中文语料内应有命中"
    assert not any("ground state" in h.chunk.content for h in hits_zh), (
        "跨语言词面不相交：单路命中不了英文块"
    )

    hits_both, latency = retriever.retrieve(question, second_query=en_query)
    assert set(latency) == {"retrieve", "rerank"}, "翻译计时在 ask 层，retriever 延迟分段不变"
    en_hits = [h for h in hits_both if "ground state" in h.chunk.content]
    assert en_hits, "英文块应经第二路进入融合结果"
    assert en_hits[0].bm25_score is not None and en_hits[0].bm25_score > 0.0
    assert any("基态" in h.chunk.content for h in hits_both), "主路中文块不因合流消失"
    fused = [h.fused_score for h in hits_both]
    assert fused == sorted(fused, reverse=True)
