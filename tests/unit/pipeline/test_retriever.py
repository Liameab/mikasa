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
from mikasa.storage import repo
from mikasa.storage.db import open_db

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


# ---------------------------------------------------------------------------
# 文档内检索（阅读器「边看边问」的「只看本篇」，2026-09-22）
# ---------------------------------------------------------------------------

NOTE_SCOPE_A = """# 正则化长文

## L2 正则化

L2 正则化在损失函数里加入权重平方和的惩罚项，鼓励小而分散的权重，让模型
不再依赖某一个特征，从而缓和训练集噪声带来的过拟合。惩罚强度由系数控制，
系数越大，权重被压得越狠，模型越保守。

## 权重衰减

权重衰减是 L2 正则化在梯度下降里的等价实现：每一步都把权重按比例缩小一点，
缩放系数由学习率与惩罚强度共同决定，最终收敛到同样的小权重解。它实现简单，
几乎所有优化器都内置了这个选项，工程上比直接改损失函数更常用。

## L1 正则化

L1 正则化在损失函数里加入权重绝对值之和，它的梯度是常数，会把一部分权重
恰好压到零，于是得到稀疏解，常被用来做特征选择。稀疏解的可解释性更好：
被压到零的特征等于被模型主动丢弃。

## 弹性网络

弹性网络把 L1 与 L2 两个正则化项按比例混合，既能像 L1 那样产生稀疏解，
又保留 L2 的稳定性与分组效应，适合特征之间存在强相关的场景。
"""

NOTE_SCOPE_B = """# 注意力机制笔记

## 缩放点积注意力

缩放点积注意力除以根号 dk，防止点积随着维度增大而方差过大，
使 softmax 的梯度保持稳定。
"""


def _doc_id_of(settings, needle: str) -> int:
    """按块内容反查 document_id（不依赖文档标题的推断规则）。"""
    with open_db(settings.db_path) as conn:
        row = conn.execute(
            "SELECT document_id FROM chunks WHERE content LIKE ? LIMIT 1",
            (f"%{needle}%",),
        ).fetchone()
    assert row is not None, f"库里找不到含 {needle!r} 的块"
    return int(row[0])


def _scope_retriever(tmp_path, offline_settings) -> tuple[Retriever, int, int]:
    """两篇文档（A 正则化长文 / B 注意力）→ (检索器, A 的 id, B 的 id)。"""
    (tmp_path / "a.md").write_text(NOTE_SCOPE_A, encoding="utf-8")
    (tmp_path / "b.md").write_text(NOTE_SCOPE_B, encoding="utf-8")
    IngestService(offline_settings).ingest_paths([tmp_path])
    corpus = IndexManager(offline_settings).corpus()
    retriever = Retriever(offline_settings, corpus, get_embedding(offline_settings.embedding))
    return (
        retriever,
        _doc_id_of(offline_settings, "稀疏解的可解释性"),
        _doc_id_of(offline_settings, "缩放点积注意力"),
    )


def test_document_scope_restricts_hits_to_that_doc(tmp_path, offline_settings):
    retriever, _doc_a, doc_b = _scope_retriever(tmp_path, offline_settings)
    question = "缩放点积注意力除以根号 dk 是为了什么"

    hits_all, _ = retriever.retrieve(question)
    assert any(h.chunk.document_id == doc_b for h in hits_all), "全库口径应命中 B 篇"

    hits_b, latency = retriever.retrieve(question, document_id=doc_b)
    assert hits_b, "限定 B 篇后仍应有命中"
    assert all(h.chunk.document_id == doc_b for h in hits_b)
    assert [h.rank for h in hits_b] == list(range(1, len(hits_b) + 1))
    assert set(latency) == {"retrieve", "rerank"}


def test_document_scope_bypasses_global_top_k_truncation(tmp_path, offline_settings):
    """bm25_top_k=1 时全库只能取到 1 条，文档内必须仍取到该文档的全部命中。

    这条守的是"先全库 top-k 再过滤"的错法：截断发生在 search 内部，
    本篇的块在全局排名里可能都在截断线之外，过滤后就成空结果（静默变空，
    用户只会看到"这篇里没有相关内容"）。
    """
    retriever, doc_a, _doc_b = _scope_retriever(tmp_path, offline_settings)
    clipped = offline_settings.model_copy(
        update={"retrieval": offline_settings.retrieval.model_copy(update={"bm25_top_k": 1})}
    )
    corpus = IndexManager(offline_settings).corpus()
    narrow = Retriever(clipped, corpus, get_embedding(offline_settings.embedding))

    question = "正则化"
    assert len(narrow.retrieve(question)[0]) == 1, "全库口径被 top_k=1 截断"

    hits_a, _ = narrow.retrieve(question, document_id=doc_a)
    assert len(hits_a) >= 2, f"文档内检索不该被全局 top_k 截断，实得 {len(hits_a)}"
    assert all(h.chunk.document_id == doc_a for h in hits_a)


def test_document_scope_never_leaks_other_documents(tmp_path, offline_settings):
    """B 篇的主题，限定在 A 篇问：结果里绝不能出现 B 篇的块。

    注意**不断言"结果必为空"**：BM25 在中文上按词与字匹配，A 篇偶然共享
    几个常用词是正常的（实测 "缩放点积注意力除以根号 dk" 会命中 A 篇里
    "缩小一点"之类的词）。要守的不变量是"跨文档不泄漏"。
    """
    retriever, doc_a, doc_b = _scope_retriever(tmp_path, offline_settings)
    hits, latency = retriever.retrieve("缩放点积注意力除以根号 dk 是为了什么", document_id=doc_a)
    assert all(h.chunk.document_id == doc_a for h in hits)
    assert not any(h.chunk.document_id == doc_b for h in hits)
    assert set(latency) == {"retrieve", "rerank"}


def test_document_scope_zero_token_overlap_returns_empty(tmp_path, offline_settings):
    """与 A 篇词面零交集的查询：限定文档后如实为空，且延迟键给全。

    查询特意用**单个不存在的词**（不带空格）：分词器把空格也当成一个词条
    （实测 query tokens = ['spiral', ' ', 'anchor', …]），而每个块的 tokens
    里都有空格——任何多词查询都会靠这个空格词条拿到非零 BM25 分，"零交集"
    反而造不出来。这是分词器的既有行为，不是本功能的坑。
    """
    retriever, doc_a, _doc_b = _scope_retriever(tmp_path, offline_settings)
    hits, latency = retriever.retrieve("zzzznonexistenttoken", document_id=doc_a)
    assert hits == []
    assert set(latency) == {"retrieve", "rerank"}, "空结果的延迟键也要给全"


def test_document_scope_unknown_id_returns_empty(tmp_path, offline_settings):
    retriever, _doc_a, _doc_b = _scope_retriever(tmp_path, offline_settings)
    hits, latency = retriever.retrieve("正则化", document_id=9999)
    assert hits == [], "文档不在快照里（已删/未入库）→ 空，不许退化成全库检索"
    assert set(latency) == {"retrieve", "rerank"}


def test_document_scope_applies_to_second_query(tmp_path, offline_settings):
    """限定文档时第二路（跨语言）同样被过滤——否则别的文档会从英文路漏进来。"""
    retriever, doc_a, doc_b = _scope_retriever(tmp_path, offline_settings)
    hits, _ = retriever.retrieve(
        "缩放点积注意力除以根号 dk",
        second_query="scaled dot product attention divided by square root dk",
        document_id=doc_a,
    )
    assert all(h.chunk.document_id == doc_a for h in hits)
    assert not any(h.chunk.document_id == doc_b for h in hits), "第二路不许把 B 篇带进来"


# ---------------------------------------------------------------------------
# 一跳跨文档扩展（2026-09-25；默认关，见 RetrievalConfig.hop_expand）
# ---------------------------------------------------------------------------

DOC_A = """# 甲篇

## 锚固

预应力锚索的锚固段长度按规范取。
"""

DOC_B = """# 乙篇

## 注浆

注浆体的抗压强度决定了预应力锚索的承载上限。
"""


def _linked_retriever(
    tmp_path, offline_settings, *, hop: int, fusion_top_k: int = 8, bm25_top_k: int = 20
):
    """两篇文档入库 + 甲篇 `[[乙篇]]` 建边，返回按参数配好的检索器与文档 id。"""
    from mikasa.config.settings import load_settings
    from mikasa.ingest import wikilinks

    (tmp_path / "甲.md").write_text(DOC_A, encoding="utf-8")
    (tmp_path / "乙.md").write_text(DOC_B, encoding="utf-8")
    IngestService(offline_settings).ingest_paths([tmp_path])

    with open_db(offline_settings.db_path) as conn:
        ids = {doc.title: doc.id for doc in repo.list_documents(conn)}
    wikilinks.sync_doc_links(offline_settings, ids["甲篇"], "见 [[乙篇]]")

    tuned = load_settings("offline", data_dir=offline_settings.data_dir).model_copy(
        update={
            "retrieval": offline_settings.retrieval.model_copy(
                update={
                    "hop_expand": hop,
                    "fusion_top_k": fusion_top_k,
                    "bm25_top_k": bm25_top_k,
                    "dense_top_k": bm25_top_k,
                }
            ),
        }
    )
    corpus = IndexManager(tuned).corpus()
    return Retriever(tuned, corpus, get_embedding(tuned.embedding)), ids


def test_hop_expand_off_by_default_keeps_baseline(tmp_path, offline_settings):
    """默认关：注入的片段集合与不配这个旋钮时逐字节一致（基线不动的保证）。"""
    retriever, ids = _linked_retriever(tmp_path, offline_settings, hop=0, fusion_top_k=1)
    hits, latency = retriever.retrieve("预应力锚索的锚固段长度怎么取")
    assert [hit.chunk.document_id for hit in hits] == [ids["甲篇"]]
    assert "hop" not in latency  # 关着时连延迟段都不记


def test_hop_expand_pulls_the_linked_document(tmp_path, offline_settings):
    """打开后：被互链的乙篇那一块**已进融合候选**，于是被追加到尾部。

    fusion_top_k=1 是为了造出"乙篇进了候选、但没进最终命中"的窗口——那正是
    这个功能要覆盖的场景（主命中不够，但关联文档里正好有料）。
    """
    retriever, ids = _linked_retriever(tmp_path, offline_settings, hop=1, fusion_top_k=1)
    hits, latency = retriever.retrieve("预应力锚索的锚固段长度怎么取")
    docs = [hit.chunk.document_id for hit in hits]

    assert docs[0] == ids["甲篇"], "主命中必须仍排第一"
    assert docs[-1] == ids["乙篇"], "邻居文档只能追加在尾部"
    assert "hop" in latency
    # 序号接着主命中排（展示用），不出现 0
    assert [hit.rank for hit in hits] == list(range(1, len(hits) + 1))
    # 块 id 唯一，不会重复注入
    assert len({hit.chunk.id for hit in hits}) == len(hits)


def test_hop_expand_ignores_neighbours_with_no_candidate(tmp_path, offline_settings):
    """邻居这次**一块都没进候选** → 不扩展。

    这才是这个功能真正的边界：扩展的前提是"邻居的块本来就出现在这次查询的
    结果里"。早先那版是"链过去就取邻居第 1 名"，而 BM25 对零词面命中的块也会
    返回（`top_k<=0` 的既有语义）——那等于无条件灌一块无关内容进注入，而且
    引用协议拦不住它（它确实是一条编号资料）。

    这里把 `bm25_top_k` 压到 1：候选里只剩最相关的那一块，另一篇（无论它是
    种子还是邻居）自然出局，扩展必须老老实实什么都不加。
    """
    retriever, _ids = _linked_retriever(tmp_path, offline_settings, hop=1, bm25_top_k=1)
    hits, _ = retriever.retrieve("预应力锚索的锚固段长度怎么取")
    assert len(hits) == 1, "候选里只剩一块时，邻居没有「顺带一提」的资格"
