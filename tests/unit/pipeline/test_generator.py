"""生成器单测：引用标记解析（L1 校验）+ 引用表构建 + 拒答判定。

extract_markers 是"引用格式解析失败率"的分子分母来源，
边界用例（[1][2] 连写 / [1, 2] 畸形 / 越界 / 嵌套括号）必须锁死。
"""

from __future__ import annotations

from pathlib import Path

from mikasa.index.manager import IndexManager
from mikasa.ingest.service import IngestService
from mikasa.models.document import Chunk
from mikasa.models.retrieval import RetrievedChunk
from mikasa.pipeline.generator import Generator, extract_markers
from mikasa.pipeline.prompts import REFUSAL_TEXT
from mikasa.pipeline.retriever import Retriever
from mikasa.providers import get_embedding, get_llm
from mikasa.storage import repo
from mikasa.storage.db import open_db

NOTE = """# 深度笔记

## 正则化

L2 正则化在损失中加入权重的平方和惩罚项。

## 早停

验证集误差不再下降时停止训练，防止过拟合。
"""


# ---------------------------------------------------------------------------
# extract_markers：格式解析（评测的引用格式解析失败率以它为准）
# ---------------------------------------------------------------------------


def test_extract_markers_plain_and_adjacent():
    assert extract_markers("见[1]与[2]，结论在[1][2]处。") == [1, 2, 1, 2]


def test_extract_markers_rejects_malformed():
    # [1, 2] 逗号分隔与 [1-2] 区间不算合法编号；独立的 [1][2] 正常提取
    assert extract_markers("参考[1, 2]与[1-2]，以及[1]和[2]。") == [1, 2]


def test_extract_markers_nested_and_out_of_range():
    assert extract_markers("[[3]]与[99]皆可提取") == [3, 99]


def test_extract_markers_empty_text():
    assert extract_markers("") == []
    assert extract_markers("没有引用标记的普通回答。") == []


def test_extract_markers_keeps_order_and_duplicates():
    assert extract_markers("[5][3][5][1]") == [5, 3, 5, 1]


# ---------------------------------------------------------------------------
# 引用解析：去重 / 越界丢弃 / 首次出现序
# ---------------------------------------------------------------------------


def _chunk(id_: int, content: str) -> Chunk:
    return Chunk(
        id=id_,
        document_id=7,
        seq=id_,
        content=content,
        content_sha256=f"sha-{id_}",
        tokens=[],
    )


def _hits() -> list[RetrievedChunk]:
    # 内容 210 字符：超过 snippet 截断线 200，供截断用例验证
    return [
        RetrievedChunk(chunk=_chunk(11, "甲内容" * 70), bm25_score=2.0, fused_score=1.0, rank=1),
        RetrievedChunk(chunk=_chunk(12, "乙内容" * 70), bm25_score=1.0, fused_score=0.5, rank=2),
    ]


TITLES = {7: "测试笔记"}


def test_resolve_citations_dedup_filter_order():
    text = "[2]与[2]重复，[1]后出现，[9]越界、[0]非法被丢弃"
    citations = Generator._resolve_citations(text, _hits(), TITLES)
    assert [c.marker for c in citations] == [2, 1]  # 去重 + 首次出现序
    assert [c.chunk_id for c in citations] == [12, 11]
    assert citations[0].document_title == "测试笔记"


def test_resolve_citations_no_markers_gives_empty():
    assert Generator._resolve_citations("普通文本没有引用。", _hits(), TITLES) == []


def test_citation_snippet_truncated_at_200():
    citations = Generator._resolve_citations("[1]", _hits(), TITLES)
    assert len(citations[0].snippet) == 201  # 200 字符 + 省略号
    assert citations[0].snippet.endswith("…")


# ---------------------------------------------------------------------------
# generate 全流程：mock LLM（离线零密钥）
# ---------------------------------------------------------------------------


def test_generate_returns_answer_and_citations(tmp_path: Path, offline_settings):
    md = tmp_path / "n.md"
    md.write_text(NOTE, encoding="utf-8")
    IngestService(offline_settings).ingest_paths([tmp_path])
    corpus = IndexManager(offline_settings).corpus()
    embedding = get_embedding(offline_settings.embedding)
    hits, _ = Retriever(offline_settings, corpus, embedding).retrieve("L2 正则化防止过拟合的原理")
    assert hits

    with open_db(offline_settings.db_path) as conn:
        titles = repo.document_title_map(conn)

    answer, completion = Generator(offline_settings, get_llm(offline_settings.llm)).generate(
        "L2 正则化防止过拟合的原理", hits, titles
    )
    assert answer.question == "L2 正则化防止过拟合的原理"
    assert answer.text
    assert answer.model == offline_settings.llm.model
    assert completion.prompt_tokens is None  # mock 不计量
    # L1 硬校验：正文引用的编号必须落在命中范围内
    assert all(1 <= c.marker <= len(hits) for c in answer.citations)
    # 命中片段一定带引用（mock 达到阈值即答，引用协议成立）
    assert not answer.refused


def test_refusal_when_sources_empty(tmp_path: Path, offline_settings):
    """空资料 → 一律拒答（无据不答的底线语义）。"""
    llm = get_llm(offline_settings.llm)
    answer, _ = Generator(offline_settings, llm).generate("怎么造曲速引擎？", [], {})
    assert answer.refused is True
    assert answer.text == REFUSAL_TEXT
    assert answer.citations == []
