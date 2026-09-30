"""证据自评 + 一次补检索（Self-RAG 最小闭包，2026-09-30）的单测。

用 offline 配置的变体 + 脚本替身 LLM：零密钥、零网络，但链路是真跑的
（BM25 检索 → 自评调用 → 补检索 → 注入 → 引用解析）。
"""

from __future__ import annotations

from pathlib import Path

from mikasa.errors import ProviderError
from mikasa.ingest.service import IngestService
from mikasa.models.document import Chunk
from mikasa.models.retrieval import RetrievedChunk
from mikasa.pipeline.ask import AskService
from mikasa.pipeline.prompts import (
    SUFFICIENCY_MAX_TOKENS,
    SUFFICIENCY_SYSTEM_PROMPT,
    TRANSLATE_SYSTEM_PROMPT,
)
from mikasa.pipeline.sufficiency import _merge, _parse_followup, assess, maybe_supplement
from mikasa.providers.llm import Completion

# 两篇文档：首轮命中靠 L2 那条，补检索的新查询只可能命中 AdamW 那条——
# "补检索到底捞回了什么"因此可断言（不用猜 RRF 排名）
NOTE_L2 = """# 优化笔记

## L2 正则化

L2 正则化在损失中加入权重的平方和惩罚项，鼓励小而分散的权重。
"""

NOTE_ADAMW = """# 优化器笔记

## AdamW 优化器

AdamW 优化器使用解耦的权重衰减，把权重衰减从梯度更新里拆出来单独施加。
"""

QUESTION = "L2 正则化是怎么实现的？"
FOLLOWUP = "AdamW 优化器 解耦的权重衰减"
QA_ANSWER = "L2 正则化在损失里加权重平方和惩罚项 [1]，权重衰减还有解耦写法 [2]。"


class _ScriptedLLM:
    """脚本替身 LLM：按队列回文（自评 → [翻译] → 问答），记录每次调用。"""

    def __init__(self, texts: list[str], *, boom_first: bool = False) -> None:
        self.texts = list(texts)
        self.calls: list[list[dict[str, str]]] = []
        self.kwargs: list[dict] = []
        self._boom_first = boom_first
        self._calls_made = 0

    def complete(self, messages, *, temperature, max_tokens) -> Completion:
        self._calls_made += 1
        if self._boom_first and self._calls_made == 1:
            raise ProviderError("自评服务不可用")
        self.calls.append(messages)
        self.kwargs.append({"temperature": temperature, "max_tokens": max_tokens})
        text = self.texts.pop(0) if self.texts else "（脚本耗尽）"
        return Completion(text=text, prompt_tokens=1, completion_tokens=1)


def _sufficiency_ready(offline_settings, **retrieval_overrides):
    """offline 配置的"开补检索"变体：backend 改 api（绕开 mock 守卫）。

    embedding 仍为 none、crosslingual 默认关，链路只走 BM25——零网络、
    零模型下载，注入的替身 LLM 是唯一的"模型"。
    **融合窗口压到 1**：两份笔记的语料小到"随便查什么都全命中"，
    首轮窗口不收紧的话补检索永远无新块可加，"补回了什么"就断言不出来。
    """
    return offline_settings.model_copy(
        update={
            "llm": offline_settings.llm.model_copy(update={"backend": "api"}),
            "retrieval": offline_settings.retrieval.model_copy(
                update={"sufficiency_retry": True, "fusion_top_k": 1, **retrieval_overrides}
            ),
        }
    )


def _seed(tmp_path: Path, offline_settings) -> None:
    (tmp_path / "l2.md").write_text(NOTE_L2, encoding="utf-8")
    (tmp_path / "adamw.md").write_text(NOTE_ADAMW, encoding="utf-8")
    IngestService(offline_settings).ingest_paths([tmp_path])


# ---------------------------------------------------------------------------
# 解析与自评（纯函数 / 单次调用）
# ---------------------------------------------------------------------------


def test_parse_followup_only_accepts_insufficient_line():
    """只认"不足"行：判足够、空输出、含"不足"但没给查询都当 None。"""
    assert _parse_followup("足够") is None
    assert _parse_followup("资料已足够，无需补检索") is None
    assert _parse_followup("") is None
    assert _parse_followup("不足") is None  # 没给查询 = 补不了
    assert _parse_followup("不足：") is None
    # 小模型漂移的三种写法都要能剥出来（前缀词、半角冒号、包裹引号）
    assert _parse_followup("不足：AdamW 的权重衰减") == "AdamW 的权重衰减"
    assert _parse_followup("资料不足: 解耦 权重衰减") == "解耦 权重衰减"
    assert _parse_followup("不足：“螺旋锚 循环荷载”") == "螺旋锚 循环荷载"
    assert _parse_followup("\n\n不足：多行输出里的第二行查询\n") == "多行输出里的第二行查询"


def test_assess_builds_prompt_and_returns_followup(offline_settings):
    """自评调用：专用 system、问题 + 【资料N】（截断 snippet）、低温短输出。"""
    llm = _ScriptedLLM([f"不足：{FOLLOWUP}"])
    chunk = Chunk(id=7, document_id=1, seq=0, content="L2 正则化的定义。" * 30, content_sha256="h")
    hits = [RetrievedChunk(chunk=chunk, rank=1)]

    assert assess(llm, QUESTION, hits) == FOLLOWUP
    messages, kwargs = llm.calls[0], llm.kwargs[0]
    assert messages[0]["content"] == SUFFICIENCY_SYSTEM_PROMPT
    assert f"【问题】{QUESTION}" in messages[1]["content"]
    assert "【资料1】" in messages[1]["content"]
    assert len(hits[0].chunk.content) > 200 and "…" in messages[1]["content"]  # 用的是 snippet
    assert kwargs == {"temperature": 0.0, "max_tokens": SUFFICIENCY_MAX_TOKENS}


def test_assess_falls_back_on_provider_error(offline_settings):
    """自评调用异常 → None（退化为"不补检索"），异常不穿透问答主链。"""
    llm = _ScriptedLLM([], boom_first=True)
    chunk = Chunk(id=7, document_id=1, seq=0, content="随便", content_sha256="h")
    assert assess(llm, QUESTION, [RetrievedChunk(chunk=chunk, rank=1)]) is None


# ---------------------------------------------------------------------------
# 开关与守卫（零调用路径）
# ---------------------------------------------------------------------------


def test_supplement_off_by_default_makes_no_llm_call(tmp_path, offline_settings):
    """默认关：即使注入了脚本 LLM 也不发生自评调用，延迟分段与旧版逐字一致。"""
    _seed(tmp_path, offline_settings)
    llm = _ScriptedLLM(["足够", QA_ANSWER])
    answer = AskService(offline_settings, llm=llm).ask(QUESTION)
    assert len(llm.calls) == 1  # 只有问答那一次
    assert set(answer.latency_ms) == {"retrieve", "rerank", "generate"}


def test_supplement_skipped_on_mock_backend(tmp_path, offline_settings):
    """开关开 + mock 档：零自评调用（ADR-0014 ③ 的确定性纪律）。"""
    _seed(tmp_path, offline_settings)
    settings = offline_settings.model_copy(
        update={
            "retrieval": offline_settings.retrieval.model_copy(update={"sufficiency_retry": True})
        }
    )
    llm = _ScriptedLLM(["不足：随便", QA_ANSWER])
    answer = AskService(settings, llm=llm).ask(QUESTION)
    assert len(llm.calls) == 1
    assert set(answer.latency_ms) == {"retrieve", "rerank", "generate"}


def test_supplement_skipped_without_hits(offline_settings):
    """空命中：不发自评调用（没有片段可判），原样返回。"""
    llm = _ScriptedLLM(["不足：随便"])
    hits, lat = maybe_supplement(_sufficiency_ready(offline_settings), llm, None, QUESTION, [])
    assert hits == [] and lat == {} and llm.calls == []


# ---------------------------------------------------------------------------
# 补检索闭环（AskService 全链路）
# ---------------------------------------------------------------------------


def test_supplement_appends_new_chunk_and_wires_latency(tmp_path, offline_settings):
    """判不足 → 补检索命中新块 → 追加尾部（编号接着排）→ 进注入与引用。"""
    _seed(tmp_path, offline_settings)
    llm = _ScriptedLLM([f"不足：{FOLLOWUP}", QA_ANSWER])
    answer = AskService(_sufficiency_ready(offline_settings), llm=llm).ask(QUESTION)

    assert len(llm.calls) == 2, "自评 + 问答各一次"
    qa_user = llm.calls[1][-1]["content"]
    assert "AdamW" in qa_user, "补检索捞回的块进了注入片段"
    assert answer.text == QA_ANSWER and not answer.refused
    # 第 2 条引用就是补检索追加的那块（编号 = 注入顺序，主命中仍占 [1]）
    assert [c.marker for c in answer.citations] == [1, 2]
    assert "AdamW" in answer.citations[1].snippet
    assert set(answer.latency_ms) == {"retrieve", "rerank", "generate", "sufficiency", "retrieve2"}


def test_supplement_no_retrieval_when_evidence_sufficient(tmp_path, offline_settings):
    """判"足够"：只多一次自评调用，不补检索（延迟无 retrieve2 键）。"""
    _seed(tmp_path, offline_settings)
    llm = _ScriptedLLM(["足够", "L2 正则化在损失里加权重平方和惩罚项 [1]。"])
    answer = AskService(_sufficiency_ready(offline_settings), llm=llm).ask(QUESTION)
    assert len(llm.calls) == 2
    assert "AdamW" not in llm.calls[1][-1]["content"]
    assert set(answer.latency_ms) == {"retrieve", "rerank", "generate", "sufficiency"}


def test_supplement_survives_assess_failure(tmp_path, offline_settings):
    """自评异常：退回首轮命中照常作答，延迟只留 sufficiency 键。"""
    _seed(tmp_path, offline_settings)
    llm = _ScriptedLLM([QA_ANSWER], boom_first=True)
    answer = AskService(_sufficiency_ready(offline_settings), llm=llm).ask(QUESTION)
    assert len(llm.calls) == 1
    assert "AdamW" not in llm.calls[0][-1]["content"]
    assert set(answer.latency_ms) == {"retrieve", "rerank", "generate", "sufficiency"}


def test_supplement_followup_is_translated_when_crosslingual(tmp_path, offline_settings):
    """跨语言档：补检索查询也走翻译（否则新查询在英文库上白搜）。

    调用序：主问题翻译 → 自评 → 补检索查询翻译 → 问答。
    """
    _seed(tmp_path, offline_settings)
    llm = _ScriptedLLM(
        [
            "How is L2 regularization implemented?",
            f"不足：{FOLLOWUP}",
            "AdamW optimizer decoupled weight decay",
            QA_ANSWER,
        ]
    )
    settings = _sufficiency_ready(offline_settings, crosslingual=True)
    answer = AskService(settings, llm=llm).ask(QUESTION)

    assert len(llm.calls) == 4
    assert llm.calls[1][0]["content"] == SUFFICIENCY_SYSTEM_PROMPT  # 自评
    assert llm.calls[2][0]["content"] == TRANSLATE_SYSTEM_PROMPT  # 译的是补检索查询
    assert llm.calls[2][1]["content"] == FOLLOWUP
    assert set(answer.latency_ms) == {
        "retrieve",
        "rerank",
        "translate",
        "generate",
        "sufficiency",
        "translate2",
        "retrieve2",
    }


# ---------------------------------------------------------------------------
# 合并规则（纯函数）
# ---------------------------------------------------------------------------


def _hit(chunk_id: int, text: str = "正文") -> RetrievedChunk:
    chunk = Chunk(
        id=chunk_id, document_id=1, seq=chunk_id, content=text, content_sha256=f"h{chunk_id}"
    )
    return RetrievedChunk(chunk=chunk, rank=chunk_id)


def test_supplement_passes_document_scope_to_second_retrieval(offline_settings):
    """补检索继承「只看本篇」的范围（阅读器 scope=doc 不得越界到全库）。"""

    class _SpyRetriever:
        def __init__(self) -> None:
            self.document_ids: list[int | None] = []

        def retrieve(self, question, *, second_query=None, document_id=None):
            self.document_ids.append(document_id)
            return [], {}

    spy = _SpyRetriever()
    llm = _ScriptedLLM([f"不足：{FOLLOWUP}"])
    hits = [_hit(1)]
    merged, lat = maybe_supplement(
        _sufficiency_ready(offline_settings), llm, spy, QUESTION, hits, document_id=42
    )
    assert spy.document_ids == [42]
    assert merged == hits and "retrieve2" in lat


def test_merge_dedupes_caps_and_continues_ranking():
    """去重（已注入的块不重复）、封顶 3 块、秩接着主命中往下排。"""
    hits = [_hit(1), _hit(2)]
    extra = [_hit(2), _hit(3), _hit(4), _hit(5), _hit(6)]  # 2 是重复；能追加的只有 3/4/5
    merged = _merge(hits, extra)
    assert [h.chunk.id for h in merged] == [1, 2, 3, 4, 5]
    assert [h.rank for h in merged] == [1, 2, 3, 4, 5]
    assert _merge(hits, [_hit(2)]) == hits, "全是重复 → 原样"
