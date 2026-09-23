"""问答门面 AskService：检索 + 生成 + 落库的编排（CLI / Web / 评测共用）。

- 单轮 ask：独立会话落库（qa_sessions 一条，消息成对），形成完整审计轨迹；
- 多轮 chat：会话内续问，历史以"摘要行"注入提示词（v1 不做查询改写，
  语义连续性依赖摘要，局限见 limitations-and-failures.md）；
- 延迟分段在 Answer.latency_ms 记录：检索/生成分开，评测统计 p95 时
  能定位瓶颈在召回还是在生成。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from mikasa.config.settings import Settings
from mikasa.errors import ConfigError, StorageError
from mikasa.index.manager import IndexManager
from mikasa.models.answer import Answer, ReaderSource
from mikasa.models.retrieval import RetrievedChunk
from mikasa.pipeline.generator import Generator
from mikasa.pipeline.prompts import (
    FREE_SYSTEM_PROMPT,
    HISTORY_TEMPLATE,
    NO_CITE_HISTORY_LINE,
    TITLE_CHAR_LIMIT,
    TRANSLATE_MAX_TOKENS,
    TRANSLATE_TO_ZH_MAX_TOKENS,
    build_title_messages,
    build_translate_messages,
    build_translate_to_zh_messages,
)
from mikasa.pipeline.retriever import Retriever
from mikasa.providers import get_embedding, get_llm
from mikasa.storage import repo
from mikasa.storage.db import open_db
from mikasa.utils.logging import get_logger
from mikasa.utils.text import fold_title, is_english_dominant

if TYPE_CHECKING:  # 仅类型标注用：避免 providers→pipeline 的潜在循环
    from mikasa.providers.llm import LLMProvider

logger = get_logger("pipeline")

# 历史摘要只保留最近对话轮数，控制提示词长度与成本
_HISTORY_ROUNDS = 5
# 单条历史摘要行的最大长度（防长回答撑爆上下文）
_HISTORY_LINE_CAP = 120

# 标题提炼的最大输出 token（≤16 汉字 + 缓冲；流式响应无 usage 限制同此）
_TITLE_MAX_TOKENS = 24

# 问答模式：kb=知识库检索（默认，现状）；free=自由问答（直连 LLM 的旁路，
# 免检索/免引用/免拒答协议，需 api/local 的真实模型，见 ADR-0013）
AnswerMode = Literal["kb", "free"]

# 跨语言检索（2026-09-10，见 retrieval.crosslingual）：含汉字的提问才需要
# 翻译；纯英文/数字问题直接走单路（英文词面 BM25 可自命中）
_CJK_RE = re.compile(r"[一-鿿]")

# 双语块翻译输出的分段标记（#8）：容忍【译文 1】带空格；标记协议一贯
# 用【】（【资料N】【原文N】【译文N】同构），不做「」变体兼容
_TRANSLATE_TAG_RE = re.compile(r"【\s*译文\s*(\d+)\s*】")

# 双语块里原文的展示上限。为什么是 500（2026-09-10 doc 71 实测教训）：
# 初版取 300，但分块器把块限制在 chunking.size（400）以内——实测库内
# 964 块最长 400、中位 353，300 的截断系统性砍掉四分之一，译文跟着断在
# 句中（"…水平应力会出现降低。由于"），读感像 bug。500 覆盖 400 并留
# 余量，截断退回成"异常配置"的兜底；上限偏小还会连带撑爆块翻译的
# TRANSLATE_TO_ZH_MAX_TOKENS（输入截短→输出也短，但多块叠加仍可能超）。
_BILINGUAL_ORIGINAL_CAP = 500

# 双语块正文里的方括号数字（论文文献编号 [12] 之类）。原文/译文都是论文原样
# 文本，而前端 renderAnswer 会把全文的 [n] 替换成引用角标——命中不到 citations
# 的编号会渲染成红色"越界/自造"标记（用户以为服务端编造引用，2026-09-11 修复）。
# 展示前统一转成全角：肉眼几无差别，且不落入 /\ [(\d{1,3})\ ]/ 的匹配。
_CITE_MARKER_RE = re.compile(r"\[(\d{1,3})\]")


def _neutralize_cite_markers(text: str) -> str:
    """[12] → ［12］：让论文自带的文献编号不被前端误当成引用角标。"""
    return _CITE_MARKER_RE.sub(lambda m: f"［{m.group(1)}］", text)


# 阅读器主查询里上下文（用户选中的原文）的长度上限。与 _BILINGUAL_ORIGINAL_CAP
# 同口径：本地 bge-small-zh-v1.5 的输入上限是 512 token，1000 字的选中段 +
# 问题会被 embedding **静默截断**（截断发生在末尾）。所以拼查询时问题放最前、
# 上下文截到这里为止——被砍掉的永远是上下文尾巴，问题永远不会被吃掉。
_READER_QUERY_CONTEXT_CAP = 500


def _reader_query(question: str, context: str | None) -> str:
    """阅读器主查询 = 问题 + 选中原文（问题在前；无上下文时逐字返回问题）。

    纯空白的 context（用户选中了空白、或前端传了空串）必须与"没有 context"
    完全等价——否则会拼出一个只有换行的尾巴，白给 BM25 一个多余词条、
    也让提示词多出空段。归一化后再判空，就是这一处的全部职责。
    """
    tail = " ".join(context.split()) if context else ""
    if not tail:
        return question
    if len(tail) > _READER_QUERY_CONTEXT_CAP:
        tail = tail[:_READER_QUERY_CONTEXT_CAP]
    return f"{question}\n{tail}"


def _translate_query(llm, question: str) -> str | None:
    """把含汉字的问题译成英文查询；失败/空/仍含汉字 → None（回退单路）。

    翻译是检索增强而非必需环节：任何异常都不得打断问答主链——LLM 调用
    失败、输出为空、输出仍含汉字（本地小模型偶发没翻/复述原文）一律
    判 None，AskService 照常走原问题单路检索。
    """
    if not _CJK_RE.search(question):
        return None
    try:
        completion = llm.complete(
            build_translate_messages(question),
            temperature=0.1,
            max_tokens=TRANSLATE_MAX_TOKENS,
        )
    except Exception as exc:  # noqa: BLE001 - 翻译只是加速器：**任何**失败都回退单路
        # 只捕 ProviderError 是不够的：provider 只把"建流/建请求"那一步包成了
        # ProviderError，响应解析（choices[0]）在网关返回空候选时会抛 IndexError
        # 穿透到这里——本该"回退单路"的小故障变成整轮问答失败（2026-09-20 审查实测）。
        logger.warning("查询翻译失败：%s（%s，回退单路检索）", question[:20], type(exc).__name__)
        return None
    # 剥掉可能包裹译文的首尾引号（中文引号簇或英文双/单引号），再折叠空白
    text = " ".join(completion.text.strip().strip('"').strip("'").strip("“”‘’").split())
    if not text or _CJK_RE.search(text):
        logger.debug("查询翻译结果不可用（空或仍含汉字），回退单路检索")
        return None
    return text


@dataclass(frozen=True)
class StreamEvent:
    """流式问答的事件契约（AskService 产出，Web SSE 原样转发）。

    顺序：meta（携带会话 id，供前端续问）→ delta×n（正文增量）→ done
    （完整 Answer：引用/拒答/延迟一次性给全；与全部 delta 拼接恒等）。
    中途失败则改为 error 收尾（此前已发出的 delta 仍有效）。
    """

    kind: Literal["meta", "delta", "done", "error"]
    session_id: int | None = None  # meta 事件携带
    text: str = ""  # delta 事件的增量文本
    answer: Answer | None = None  # done 事件携带的完整答案
    message: str = ""  # error 事件的说明
    # 阅读器「相关片段」（A 档 c）：只在阅读器提问（with_sources）时非 None，
    # meta 帧先给一遍（引用标记未回填），done 帧给回填后的版本。问答页那条
    # 链路的帧里**没有这个键**（Answer 契约与落库载荷一个字都不动）。
    sources: list[ReaderSource] | None = None


class AskService:
    """一次提问 = 取语料快照（IndexManager 缓存）→ 检索 → 生成 → 落库。"""

    def __init__(self, settings: Settings, *, llm: LLMProvider | None = None) -> None:
        """llm 注入点：测试传确定性假模型（缺省走配置工厂，行为不变）。"""
        self.settings = settings
        self._manager = IndexManager(settings)
        self._llm = llm if llm is not None else get_llm(settings.llm)
        self._embedding = get_embedding(settings.embedding)

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------

    def ask(
        self,
        question: str,
        *,
        session_id: int | None = None,
        mode: AnswerMode = "kb",
    ) -> Answer:
        """单轮提问（无历史）。session_id 缺省时新建会话并落库。

        mode=free 时跳过检索，直连 LLM 回答任意话题（需 api/local 模型）。
        """
        question = question.strip()
        if not question:
            raise StorageError("问题为空")
        self._guard_mode(mode)
        if session_id is None:
            with open_db(self.settings.db_path) as conn:
                session_id = repo.create_session(conn, self.settings.profile)

        if mode == "free":
            answer = self._complete_free(question, self._raw_history(session_id))
        else:
            history = self._history_summary(session_id)
            answer = self._answer(question, history)
        self._record(session_id, question, answer)
        return answer

    def ask_stream(
        self,
        question: str,
        *,
        session_id: int | None = None,
        mode: AnswerMode = "kb",
    ) -> Iterator[StreamEvent]:
        """单轮提问的流式版本（meta → delta×n → done，Web SSE 的数据源）。

        与非流式 ask 的唯一差异：正文逐增量吐出、token 用量为 None
        （流式响应不带 usage）；会话/落库/拒答/引用判定完全同轨。
        中途异常会从迭代中抛出——由 Web 层兜底转 error 事件。
        mode=free 时直连 LLM 流式（无检索；守卫在首个 yield 前完成，
        使 mock+free 产出恰一帧 error、零 meta——Web 层契约）。
        """
        question = question.strip()
        if not question:
            raise StorageError("问题为空")
        self._guard_mode(mode)
        if session_id is None:
            with open_db(self.settings.db_path) as conn:
                session_id = repo.create_session(conn, self.settings.profile)

        if mode == "free":
            yield from self._free_stream(question, session_id)
            return

        history = self._history_summary(session_id)
        for event in self._kb_stream(question, history, session_id=session_id):
            if event.kind == "done" and event.answer is not None:
                # 落库必须发生在 done 帧**之前**：qa 页收到 done 就刷新会话列表，
                # 晚一步会偶发缺行（前端与既有测试都依赖这个时序）。
                # 阅读器的「边看边问」走 ask_doc_stream、不经过这里——即问即散
                # 就是靠"不落库的那条路根本不进这段"实现的。
                self._record(session_id, question, event.answer)
            yield event

    def chat(
        self,
        session_id: int,
        question: str,
        *,
        mode: AnswerMode = "kb",
    ) -> Answer:
        """会话内提问：kb 带历史摘要；free 直连 LLM（历史为原始消息注入）。"""
        question = question.strip()
        if not question:
            raise StorageError("问题为空")
        self._guard_mode(mode)
        if mode == "free":
            answer = self._complete_free(question, self._raw_history(session_id))
        else:
            history = self._history_summary(session_id)
            answer = self._answer(question, history)
        self._record(session_id, question, answer)
        return answer

    def chat_stream(
        self,
        session_id: int,
        question: str,
        *,
        mode: AnswerMode = "kb",
    ) -> Iterator[StreamEvent]:
        """会话内提问的流式版本：多轮续问复用会话（历史按 mode 分路）。"""
        yield from self.ask_stream(question, session_id=session_id, mode=mode)

    def ask_doc_stream(
        self,
        document_id: int,
        question: str,
        *,
        scope: Literal["doc", "all"] = "doc",
        context: str | None = None,
    ) -> Iterator[StreamEvent]:
        """阅读器「边看边问」（A 档 c，2026-09-22）：**不建会话、不落库、不进历史**。

        与 ask_stream 的差别只有两点（其余帧序列完全同构，含"delta 拼接 ===
        done.text"与双语块末帧 delta 的契约）：
          - 走 `_kb_stream`（不落库的核心），会话相关的分支一个都不进；
          - meta/done 帧额外携带 `sources`（全部检索命中的「相关片段」）。

        scope="doc" 只在这篇文档的块里检索（默认）；"all" 是用户主动切的
        全库口径——此时 `focus_document_id` 仍是他正在读的那篇，用于给片段
        标「本篇/另一篇」。固定 kb 模式，没有 mode 参数（不做 free 开关）。
        文档不存在/无正文由路由层预检（与 chat_stream 的会话校验同款分工）。
        """
        question = question.strip()
        if not question:
            raise StorageError("问题为空")
        yield from self._kb_stream(
            question,
            None,
            document_id=document_id if scope == "doc" else None,
            context=context,
            with_sources=True,
            focus_document_id=document_id,
        )

    def suggest_title(self, session_id: int, *, apply: bool = True) -> tuple[str, bool]:
        """为会话提炼标题；返回 (标题, 是否落库)。

        输入材料 = 库内首问 + 首答（≤200 字，prompts.build_title_messages
        组装）。三态行为（Web 标题建议 / 首轮自动命名共用同一入口）：
          - mock 后端（offline）→ 不调 LLM，直接截断兜底：MockLLM 是
            启发式协议、没有语义能力，不能当提炼器用。截断是**合法回退**
            （非降级错误），调用方无需区分后端；
          - api/local → LLM 提炼（temperature 走配置、max_tokens=24）；
            任何失败/空输出 → 截断兜底 + logger 留痕——标题提炼是锦上
            添花，绝不能把问答流程拖挂；
          - apply=True 且会话未手动命名 → 原子落库（可覆盖先前的截断
            兜底标题；手动命名锁定见 repo.apply_suggested_title）；
            apply=False 只算不改（预览）。
        会话无任何非空提问 → StorageError（Web 400：没对话可提炼）。
        """
        with open_db(self.settings.db_path) as conn:
            if repo.get_session(conn, session_id) is None:
                raise StorageError(f"会话不存在：{session_id}")
            question = repo.first_user_message(conn, session_id)
            if question is None:
                raise StorageError("会话还没有提问，无法提炼标题")
            first_answer = next(
                (
                    str(m["content"])
                    for m in repo.messages_by_session(conn, session_id)
                    if m["role"] == "assistant"
                ),
                "",
            )

        fallback = fold_title(question)  # 截断兜底（20 字，与自动补名同口径）
        if self.settings.llm.backend == "mock":
            title = fallback
        else:
            try:
                completion = self._llm.complete(
                    build_title_messages(question, first_answer),
                    temperature=self.settings.llm.temperature,
                    max_tokens=_TITLE_MAX_TOKENS,
                )
                title = self._clean_title(completion.text) or fallback
            except Exception as exc:  # ProviderError 等：提炼失败不拖挂问答
                logger.warning("标题提炼失败，回退截断标题（会话 %s）：%s", session_id, exc)
                title = fallback

        applied = False
        if apply:
            with open_db(self.settings.db_path) as conn:
                applied = repo.apply_suggested_title(conn, session_id, title)
        return title, applied

    def invalidate_index(self) -> None:
        """语料变更（Web 上传/删除文档）后使索引快照失效，下次提问自动重建。"""
        self._manager.invalidate()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _kb_stream(
        self,
        question: str,
        history_summary: str | None,
        *,
        session_id: int | None = None,
        document_id: int | None = None,
        context: str | None = None,
        with_sources: bool = False,
        focus_document_id: int | None = None,
    ) -> Iterator[StreamEvent]:
        """kb 链路的流式核心（ask_stream 与阅读器 ask_doc_stream 共用）。

        **本方法不建会话、不落库**——落库是调用方 ask_stream 的职责（它在
        收到 done 事件后、放行 done 帧之前 `_record`）。阅读器那条路走这里
        而不走 ask_stream，就是"即问即散"的全部机制。

        参数：
            document_id: 限定只在该文档的块里检索（None = 全库）
            context: 阅读器里用户选中的原文（进提示词 + 并进检索查询）
            with_sources: meta/done 帧携带全部命中（ReaderSource）
            focus_document_id: 阅读器正在读的文档（给片段标 current_doc）
        帧序与旧版逐字一致：meta → delta×n（双语块是末帧 delta）→ done；
        任何异常从迭代中抛出（Web 层转 error 帧）。
        """
        hits, latency, t0 = self._retrieve(question, document_id=document_id, context=context)
        with open_db(self.settings.db_path) as conn:
            titles = repo.document_title_map(conn)

        # 相关片段在 meta 帧就发一遍：前端能"先铺片段、答案再流出来"
        # （cited 此刻全 False，done 帧给回填后的版本）
        sources = self._sources(hits, titles, focus_document_id) if with_sources else None
        generator = Generator(self.settings, self._llm)
        yield StreamEvent(kind="meta", session_id=session_id, sources=sources)

        parts: list[str] = []
        for piece in generator.stream_text(
            question, hits, titles, history_summary=history_summary, context=context
        ):
            parts.append(piece)
            yield StreamEvent(kind="delta", text=piece)

        answer = generator.build_answer("".join(parts), question, hits, titles)
        answer = answer.model_copy(update={"latency_ms": self._finalize_latency(latency, t0)})
        # 双语块作为额外 delta 在 done 前流出：保证 delta 拼接 === done.text
        # （流式契约测试锁定），且落库 content 已是含块的终态
        block = self._maybe_bilingual_block(answer, hits)
        if block:
            answer = answer.model_copy(update={"text": answer.text + block})
            yield StreamEvent(kind="delta", text=block)
        self._log(answer, question, hits)
        if sources is not None:
            sources = self._mark_cited(sources, answer)
        yield StreamEvent(kind="done", session_id=session_id, answer=answer, sources=sources)

    @staticmethod
    def _sources(
        hits: list[RetrievedChunk],
        titles: dict[int, str],
        focus_document_id: int | None,
    ) -> list[ReaderSource]:
        """检索命中 → 「相关片段」列表（阅读器专用，不落库）。

        全量给，不做服务端截断：上限本来就只有 fusion_top_k（8~14 条），
        而"答案引用了 [12]、列表里却没有第 12 张卡"看起来就像 bug。
        """
        out: list[ReaderSource] = []
        for hit in hits:
            chunk = hit.chunk
            if chunk.id is None:
                continue  # 快照里的块必然有 id；这里只是给类型收窄
            out.append(
                ReaderSource(
                    chunk_id=chunk.id,
                    document_id=chunk.document_id,
                    document_title=titles.get(chunk.document_id, "未知文档"),
                    section=chunk.heading_path,
                    page=chunk.page_number,
                    snippet=chunk.snippet,
                    rank=hit.rank,
                    current_doc=chunk.document_id == focus_document_id,
                )
            )
        return out

    @staticmethod
    def _mark_cited(sources: list[ReaderSource], answer: Answer) -> list[ReaderSource]:
        """done 前回填"哪些片段被答案引用了"（marker 与正文 [n] 对齐）。"""
        markers = {c.chunk_id: c.marker for c in answer.citations}
        out: list[ReaderSource] = []
        for source in sources:
            marker = markers.get(source.chunk_id)
            if marker is None:
                out.append(source)
            else:
                out.append(source.model_copy(update={"cited": True, "marker": marker}))
        return out

    def _answer(self, question: str, history_summary: str | None) -> Answer:
        """检索 + 生成核心（不落库，评测可直接调用）。"""
        hits, latency, t0 = self._retrieve(question)
        with open_db(self.settings.db_path) as conn:
            titles = repo.document_title_map(conn)

        generator = Generator(self.settings, self._llm)
        answer, _completion = generator.generate(
            question, hits, titles, history_summary=history_summary
        )
        answer = answer.model_copy(update={"latency_ms": self._finalize_latency(latency, t0)})
        block = self._maybe_bilingual_block(answer, hits)
        if block:
            answer = answer.model_copy(update={"text": answer.text + block})
        self._log(answer, question, hits)
        return answer

    def _retrieve(
        self,
        question: str,
        *,
        document_id: int | None = None,
        context: str | None = None,
    ) -> tuple[list[RetrievedChunk], dict[str, float], float]:
        """检索段（ask / ask_stream / 阅读器共用）：命中、延迟分段、计时起点。

        计时起点在检索前取——generate 分段口径 = 检索起全程耗时，
        与 M2 报告里的延迟定义一致。

        context（阅读器选中的原文）与问题拼成**主查询**：像"这里说的 μ
        是什么意思"这种指代型问题，光靠问题本身检索会跑空，选中段才是唯一
        的检索信号。跨语言翻译仍只翻**问题本身**（翻译器的输入协议是一个
        问题；塞整段话又贵又慢，且译文会模糊掉上下文里的术语）。
        """
        corpus = self._manager.corpus()
        if corpus.empty:
            raise StorageError("知识库为空：请先导入资料（mikasa ingest <文件或目录>）。")

        t0 = time.perf_counter()
        retriever = Retriever(self.settings, corpus, self._embedding)
        # 跨语言第二路查询（retrieval.crosslingual 开 + 问题含汉字）：
        # 翻译失败自动回退单路，任何情况下都不打断主链
        second_query = None
        translate_ms = 0.0
        if self.settings.retrieval.crosslingual:
            t1 = time.perf_counter()
            second_query = _translate_query(self._llm, question)
            translate_ms = (time.perf_counter() - t1) * 1000.0
        hits, latency = retriever.retrieve(
            _reader_query(question, context),
            second_query=second_query,
            document_id=document_id,
        )
        latency["retrieve"] = round(latency.get("retrieve", 0.0), 1)
        latency["rerank"] = round(latency.get("rerank", 0.0), 1)
        if second_query is not None:
            latency["translate"] = round(translate_ms, 1)
        return hits, latency, t0

    @staticmethod
    def _finalize_latency(latency: dict[str, float], t0: float) -> dict[str, float]:
        """补上 generate 分段并固化（拷贝后修改，不动 retriever 的字典）。"""
        latency = dict(latency)
        latency["generate"] = round((time.perf_counter() - t0) * 1000.0, 1)
        return latency

    def _maybe_bilingual_block(self, answer: Answer, hits: list[RetrievedChunk]) -> str | None:
        """组"原文+译文对照"块（#8）：被引用且英文主导的块批量翻译后拼块。

        呈现层增强，任何失败都退化为 None（不打断问答主链）：
          - 拒答轮不追加（评测裁判对 REFUSAL_TEXT 精确匹配，见 judge.py）；
          - mock 后端显式跳过（零 LLM 调用的硬保证，offline/mock 档不触发）；
          - 翻译失败 / 输出无有效【译文N】段 → 跳过。
        块内 [n] 用真实 citation marker（升序），前端渲染成可点击角标、
        永不越界。翻译耗时只在块产出时写入 latency["translate_answer"]
        （此刻 latency_ms 已是 _finalize_latency 的拷贝，就地 mutate 安全）。
        """
        if (
            answer.refused
            or not answer.citations
            or not self.settings.answer.bilingual
            or self.settings.llm.backend == "mock"
        ):
            return None

        by_id = {hit.chunk.id: hit for hit in hits if hit.chunk.id is not None}
        sections: list[tuple[int, str, str]] = []  # (marker, 标题, 原文)
        for citation in answer.citations:
            hit = by_id.get(citation.chunk_id)
            if hit is not None and is_english_dominant(hit.chunk.content):
                sections.append((citation.marker, citation.document_title, hit.chunk.content))
        if not sections:
            return None
        sections.sort(key=lambda item: item[0])  # 块内 [n] 升序，保证观感与断言

        t1 = time.perf_counter()
        try:
            completion = self._llm.complete(
                build_translate_to_zh_messages([(m, text) for m, _t, text in sections]),
                temperature=0.1,
                max_tokens=TRANSLATE_TO_ZH_MAX_TOKENS,
            )
        except Exception as exc:  # noqa: BLE001 - 对照块是呈现层增强：失败只跳过它
            # 同 _translate_query：这一块跑在**回答已生成之后**，异常逃出去会让
            # 整轮问答不落库、不进历史（前端只收到 error 帧）——代价远大于它
            # 本身的价值。任何异常都只跳过对照块（2026-09-20 审查实测）。
            logger.warning("双语块翻译失败（跳过对照块）：%s", type(exc).__name__)
            return None

        # 按【译文N】标记切片：每个标记到下一个标记之间的正文即该段译文
        matches = list(_TRANSLATE_TAG_RE.finditer(completion.text))
        translations: dict[int, str] = {}
        for i, match in enumerate(matches):
            marker = int(match.group(1))
            if marker in translations:
                continue  # 重复段取首次
            end = matches[i + 1].start() if i + 1 < len(matches) else len(completion.text)
            body = completion.text[match.end() : end]
            body = " ".join(body.strip().strip('"').strip("'").strip("“”‘’").split())
            if body:
                translations[marker] = body

        lines = ["> **原文与译文对照**"]
        rendered = 0
        for marker, title, content in sections:
            if marker not in translations:
                continue  # 该段无有效译文：跳过该段，不整体放弃
            rendered += 1
            original = " ".join(content.split())
            if len(original) > _BILINGUAL_ORIGINAL_CAP:
                original = original[:_BILINGUAL_ORIGINAL_CAP] + "…"
            original = _neutralize_cite_markers(original)
            # 标记写在粗体**外面**：renderAnswer 的替换次序是 code → bold →
            # 角标（common.js:206-209），[n] 若落在 **…** 内会被 bold 那步
            # stash 掉、渲染成死文字；放外面才成为可点击引用角标（marker
            # 取自 citations 本身，永不越界变红标）
            lines.extend(
                [
                    "> ",
                    f"> [{marker}] **原文**（《{title}》）",
                    f"> {original}",
                    "> ",
                    f"> [{marker}] **中文翻译**",
                    f"> {_neutralize_cite_markers(translations[marker])}",
                ]
            )
        if rendered == 0:
            logger.debug("双语块翻译结果不可用（无有效译文段），跳过对照块")
            return None

        answer.latency_ms["translate_answer"] = round((time.perf_counter() - t1) * 1000.0, 1)
        return "\n\n" + "\n".join(lines)

    @staticmethod
    def _log(answer: Answer, question: str, hits: list[RetrievedChunk]) -> None:
        logger.info(
            "问答完成：问题=%r 命中=%d 引用=%d 拒答=%s 延迟=%s",
            question[:30],
            len(hits),
            len(answer.citations),
            answer.refused,
            answer.latency_ms,
        )

    def _record(self, session_id: int | None, question: str, answer: Answer) -> None:
        """问答写入 qa_messages + 自动标题（截断兜底），同一次连接完成。

        自动标题规则（与 repo.py 标题三路径注释一致）：仅当会话无标题且
        未手动命名（auto_title_if_untitled 的原子 WHERE）时，取库内首条
        非空 user 消息折叠截断补名——多轮对话只有第一轮触发，标题不随
        轮次漂移；手动清除标题后下轮自动重新补名（闭环）。即时命名用
        截断版（毫秒级、零 LLM 调用）：LLM 提炼（suggest_title）是 Web
        首轮结束后的可选升级，只对 api/local 档生效。
        """
        with open_db(self.settings.db_path) as conn:
            repo.insert_qa_message(
                conn,
                session_id=session_id,
                role="user",
                content=question,
            )
            repo.insert_qa_message(
                conn,
                session_id=session_id,
                role="assistant",
                content=answer.text,
                citations_json=json.dumps(
                    [c.model_dump() for c in answer.citations], ensure_ascii=False
                ),
                refused=answer.refused,
                latency_ms_json=json.dumps(answer.latency_ms, ensure_ascii=False),
                prompt_tokens=answer.prompt_tokens,
                completion_tokens=answer.completion_tokens,
            )
            # 自动标题：条件不满足（已有标题/手动锁定/无提问）时零额外查询
            if session_id is not None:
                session = repo.get_session(conn, session_id)
                if session is not None and not session["title"] and not session["title_manual"]:
                    first_question = repo.first_user_message(conn, session_id)
                    title = fold_title(first_question) if first_question else ""
                    if title:
                        repo.auto_title_if_untitled(conn, session_id, title)
            conn.commit()

    def _guard_mode(self, mode: AnswerMode) -> None:
        """问答模式守卫：free 需真实语义模型；mock（offline）→ ConfigError。

        也拦非法 mode 字符串（对绕过 Literal 校验的直调方防御）。
        判定看 settings.llm.backend 而非 profile：profile 与后端解耦，
        YAML 可覆盖（api profile 强行配 mock 亦当 mock 处理）。
        """
        if mode not in ("kb", "free"):
            raise ConfigError(f"未知问答模式：{mode}（合法值：kb / free）")
        if mode == "free" and self.settings.llm.backend == "mock":
            raise ConfigError(
                "自由问答（free）模式需要 api / local profile"
                f"（当前 LLM 后端为 {self.settings.llm.backend}，无语义能力）。"
                "请以 --profile api 或 --profile local 运行。"
            )

    def _raw_history(self, session_id: int | None) -> list[dict[str, str]]:
        """free 直注用：最近 ≤_HISTORY_ROUNDS 轮的原始 user/assistant 消息。

        不走 _history_summary 的 RAG 语义压缩：free 多轮需要原文保留
        指代连续性（"它"、"刚才那个"）。防御：截断后首条非 user 则
        丢一条（防中途崩溃遗留单条破坏交替结构）。
        """
        if session_id is None:
            return []
        with open_db(self.settings.db_path) as conn:
            rows = repo.messages_by_session(conn, session_id)[-2 * _HISTORY_ROUNDS :]
        if rows and rows[0]["role"] != "user":
            rows = rows[1:]
        return [{"role": row["role"], "content": self._clip(str(row["content"]))} for row in rows]

    @staticmethod
    def _free_messages(question: str, raw_history: list[dict[str, str]]) -> list[dict[str, str]]:
        """free 的完整消息序列：系统人格 + 原始历史 + 当轮问题。"""
        return [
            {"role": "system", "content": FREE_SYSTEM_PROMPT},
            *raw_history,
            {"role": "user", "content": question},
        ]

    def _complete_free(self, question: str, raw_history: list[dict[str, str]]) -> Answer:
        """free 非流式核心：直连 LLM，无检索/无引用解析/无拒答判定。

        手工组装 Answer（Generator 的 build_answer 依赖 hits 的引用解析，
        free 无 hits，不复用——旁路在服务层完成是刻意设计，见 ADR-0013）。
        """
        t0 = time.perf_counter()
        completion = self._llm.complete(
            self._free_messages(question, raw_history),
            temperature=self.settings.llm.temperature,
            max_tokens=self.settings.llm.max_tokens,
        )
        answer = Answer(
            question=question,
            text=completion.text,
            citations=[],
            refused=False,
            model=self._llm.model,
            latency_ms={"generate": round((time.perf_counter() - t0) * 1000.0, 1)},
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
        )
        self._log(answer, question, [])  # hits=[] → 日志"命中=0"，与语义一致
        return answer

    def _free_stream(self, question: str, session_id: int) -> Iterator[StreamEvent]:
        """free 流式体：meta → delta×n → done（与 kb 流式四段式同构）。

        注意：本生成器只被 ask_stream 在守卫与建会话之后调用，
        首个产物即 meta 帧。流式响应无 usage → tokens 为 None
        （与 kb 流式口径一致，评测/成本统计只在非流式路径）。
        """
        raw = self._raw_history(session_id)
        yield StreamEvent(kind="meta", session_id=session_id)

        t0 = time.perf_counter()
        parts: list[str] = []
        for piece in self._llm.stream(
            self._free_messages(question, raw),
            temperature=self.settings.llm.temperature,
            max_tokens=self.settings.llm.max_tokens,
        ):
            parts.append(piece)
            yield StreamEvent(kind="delta", text=piece)

        answer = Answer(
            question=question,
            text="".join(parts),
            citations=[],
            refused=False,
            model=self._llm.model,
            latency_ms={"generate": round((time.perf_counter() - t0) * 1000.0, 1)},
        )
        self._log(answer, question, [])
        self._record(session_id, question, answer)
        yield StreamEvent(kind="done", session_id=session_id, answer=answer)

    def _history_summary(self, session_id: int | None) -> str | None:
        """把会话内最近几轮压缩成提示词注入的历史摘要（可空）。"""
        if session_id is None:
            return None
        with open_db(self.settings.db_path) as conn:
            messages = repo.messages_by_session(conn, session_id)
        pairs = [
            messages[i : i + 2]
            for i in range(0, len(messages) - 1, 2)
            if messages[i]["role"] == "user"
        ][-_HISTORY_ROUNDS:]
        if not pairs:
            return None
        lines: list[str] = []
        for user_msg, assistant_msg in pairs:
            question = self._clip(str(user_msg["content"]))
            if assistant_msg["refused"]:
                lines.append(f"（历史）用户：{question} → 助手：资料不足，已拒绝回答")
            else:
                # 无引用轮（free 轮；kb 罕见的"资料未覆盖仍作答"轮）不得谎称"给出引用"
                cited = bool(
                    assistant_msg["citations_json"] and assistant_msg["citations_json"] != "[]"
                )
                template = HISTORY_TEMPLATE if cited else NO_CITE_HISTORY_LINE
                lines.append(template.format(question=question))
        return "\n".join(lines)

    @staticmethod
    def _clip(text: str) -> str:
        text = " ".join(text.split())
        return text[:_HISTORY_LINE_CAP] + ("…" if len(text) > _HISTORY_LINE_CAP else "")

    @staticmethod
    def _clean_title(text: str) -> str:
        """LLM 标题输出的格式清洗：去包裹引号/前缀/编号，折叠空白，≤16 字。

        提示词约束了格式，这里兜底小模型漂移（输出"标题：xxx"、加引号、
        "1. xxx"编号行等）。清洗后为空（空输出/纯标点）返回空串，
        由调用方回退截断兜底标题。
        """
        text = " ".join(text.split())
        for prefix in ("标题：", "标题:", "title：", "title:"):
            lowered = text.lower()
            if lowered.startswith(prefix):
                text = text[len(prefix) :].strip()
                break
        for open_q, close_q in (("“", "”"), ("「", "」"), ("『", "』"), ("（", "）")):
            if len(text) >= 2 and text.startswith(open_q) and text.endswith(close_q):
                text = text[1:-1].strip()
        # 偶发的编号噪声行（"1. xxx" / "一、xxx"）：剥掉编号前缀
        if len(text) >= 2 and text[0].isdigit() and text[1] in (".", "、", "．"):
            text = text[2:].strip()
        return fold_title(text, TITLE_CHAR_LIMIT)
