"""评测执行器：黄金集 → 两阶段指标（检索层 / 生成层）→ 语义裁判。

一次 run 的三阶段口径差异是设计核心（详见 docs/evaluation.md）：
  阶段 A 检索层评测 —— 关闭重排、融合窗口锚定 RETRIEVAL_FUSION_TOP_K：
                   只测"融合召回有没有把题面 gold 块带回来"，
                   不被重排器掩盖；指标 recall@5/8/10 + MRR + nDCG，按难度分层；
  阶段 B 生成层评测 —— 产品配置原样走真实链路：测"答案引用是否命中
                   gold 块（citation gold ratio）""引用格式解析失败率"
                   （越界/自造编号）与拒答纪律（可答误拒 / 不可答误答）；
  阶段 C 语义裁判 —— judge 启用且可用时对可答题判 1-5 与 A-D 双量尺
                   （双轮位置交换取一致）；无密钥/未启用时 NoJudge，
                   报告显式注明"仅协议层指标"。

runner 保持纯计算：不写数据库、不写文件——落库与报告由 CLI 负责，
便于测试直接断言各类聚合值。每个问题逐条留痕（ItemRecord），
失败与裁判不一致的条目会在报告里单独列出供人工复核。

防御顺序：先做**逐题校验**（2026-09-19 起：核对每道题的标准答案分块还在不在、
内容变没变；变了的那几道跳过并在报告里写明，全题被跳过才是硬错误。原先比的是
全库指纹——加一篇文档就让整份题库作废，范围远大于风险）；
检索层任一条失败立即中止
（说明召回链路整体不可用，逐条吞掉只会污染均值）；
生成层逐条容错——网络/鉴权偶发失败记 error 跳过，整场继续。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from time import perf_counter
from typing import TYPE_CHECKING

from mikasa.config.settings import Settings
from mikasa.errors import EvalError, StorageError
from mikasa.eval.golden import GoldenItem, GoldenSet, check_against_corpus
from mikasa.eval.judge import Judge, LLMJudge, NoJudge
from mikasa.eval.metrics import (
    RetrievalMetrics,
    _Mean,
    make_retrieval_metrics,
    summarize,
)
from mikasa.index.manager import Corpus, IndexManager
from mikasa.models.retrieval import RetrievedChunk

# 跨语言第二路的翻译与产品链路**复用同一个函数**（不复制实现，避免两处口径漂移）
from mikasa.pipeline.ask import _translate_query
from mikasa.pipeline.generator import Generator, extract_markers
from mikasa.pipeline.retriever import Retriever
from mikasa.providers import get_embedding
from mikasa.storage import repo
from mikasa.storage.db import open_db
from mikasa.utils.logging import get_logger

if TYPE_CHECKING:
    from mikasa.models.answer import Answer
    from mikasa.providers.embedding import EmbeddingProvider
    from mikasa.providers.llm import LLMProvider

logger = get_logger("eval.runner")

# 检索层评测的召回深度与融合窗口。窗口锚定 10（与产品当前值同）：
# 63 题真实验收实证（2026-09-09 run #4）——融合窗口 @8 会漏 hard 综合题的
# 第二金块（q036/q037 实测第 9-10 名），@10 时 47 题 recall@10=1.000。
# 阶段 A 独立于产品调参锚定此值，产品将来调小也不动摇 A 口径（报告标题
# 只写"关 rerank"即此意）。
RETRIEVAL_KS = (5, 8, 10)
RETRIEVAL_FUSION_TOP_K = 10

# 裁判可见"事实资料"的总长度上限：控制判题成本，也让各题裁判成本公平
_JUDGE_CONTEXT_CAP = 3000


def build_judge(settings: Settings) -> Judge:
    """按配置构造裁判：未启用 / api 缺密钥 → NoJudge（协议层照常跑）。

    api 裁判需要独立密钥（默认 SiliconFlow，与生成端 DeepSeek 不同厂商，
    抵消自偏好偏差——跨厂商约束在配置层落实，见 docs/evaluation.md）；
    local 裁判（Ollama）不验密钥，不可达时由调用点记 judge_error 逐题跳过。
    """
    cfg = settings.judge
    if not cfg.enabled:
        return NoJudge()
    if cfg.backend == "api" and cfg.api_key is None:
        logger.warning(
            "judge.enabled=true 但 %s 未配置 → 本场评测仅协议层指标",
            cfg.api_key_env,
        )
        return NoJudge()
    if cfg.backend == "local":
        logger.warning("judge=local：裁判走 Ollama，不可达时对应题目将记 judge_error")
    return LLMJudge(cfg)


# ---------------------------------------------------------------------------
# 聚合容器（逐条 _Mean → 报告期 summarize）
# ---------------------------------------------------------------------------


@dataclass
class GenerationStats:
    """生成层协议指标（不依赖语义裁判即可完整计算）。

    它们是"引用格式纪律"和"拒答纪律"的可观测信号：
    citation gold ratio 衡量引用是否真的指到题面素材。
    """

    n_answerable: int = 0  # 可答题总数（分母）
    n_unanswerable: int = 0
    failed: int = 0  # 生成链路失败的题数（网络/鉴权等，逐条留痕）
    refused_answerable: int = 0  # 可答题误拒：有据却拒答（召回/生成失败信号）
    no_citation: int = 0  # 未拒答却零引用（引用纪律问题）
    out_of_range_rate: _Mean = field(default_factory=_Mean)  # 越界/自造编号占比（逐条）
    citation_gold: _Mean = field(default_factory=_Mean)  # 有引用样本的 gold 命中比例
    refusal_dirty: int = 0  # 拒答句却带 [n] 标记（L3 纪律违规，理论上不该发生）
    answered_unanswerable: int = 0  # 不可答题误答（最严重的失败）
    answered_with_citation: int = 0  # 不可答题误答且带引用（纪律双重失败）

    @property
    def refusal_clean(self) -> int:
        """正确拒答数 = 不可答题总数 − 误答数 − 拒答却带引用的违规数。"""
        return self.n_unanswerable - self.answered_unanswerable - self.refusal_dirty

    def refusal_accuracy(self) -> float | None:
        """不可答题的拒答准确率；没有不可答题时返回 None（无定义）。"""
        total = self.refusal_clean + self.answered_unanswerable
        return None if total == 0 else self.refusal_clean / total


@dataclass
class JudgeStats:
    """语义裁判层聚合。judge_model == "none" 时其余字段无定义（报告注明）。"""

    judge_model: str = "none"
    judged: int = 0  # 实际判题数（可答题且未拒答）
    correctness: _Mean = field(default_factory=_Mean)  # 双轮一致的 1-5 分
    top2_grade: _Mean = field(default_factory=_Mean)  # 双轮一致的 A/B 档（逐条 0/1）
    consistent: int = 0  # 双轮位置交换一致数
    inconsistent: int = 0  # 不一致数（含格式解析失败）——单独披露不抹平
    errors: int = 0  # 裁判调用失败数（网络/密钥等，逐题记 judge_note）


@dataclass
class ItemRecord:
    """一条题目的评测留痕（报告异常明细 + 落库 items 的原料）。

    可答题的信号是 citations/gold_hits（引用纪律）；不可答题的信号是
    refused 与否（拒答纪律）。judge_* 仅可答题非拒答样本被裁判判过。
    """

    id: str
    kind: str
    difficulty: str | None = None
    refused: bool = False
    markers: list[int] = field(default_factory=list)  # 正文全部 [n]（含越界）
    in_range: int = 0  # 落在注入片段编号 1..N 内的标记数
    citations: int = 0  # 通过 L1 硬校验的引用条数（去重）
    gold_hits: int = 0  # 引用块中命中题面 gold 的条数（可答题）
    judge_score: int | None = None  # 双轮一致时的 1-5
    judge_grade: str | None = None  # 双轮一致时的 A-D
    judge_consistent: bool | None = None
    judge_note: str = ""  # 不一致/裁判错误的留痕（人工复核线索）
    error: str = ""
    latency_ms: float = 0.0

    def to_json(self) -> dict[str, object]:
        """压平为可落库的 dict（List[dict] 直接进 metrics_json）。"""
        return {
            "id": self.id,
            "kind": self.kind,
            "difficulty": self.difficulty,
            "refused": self.refused,
            "error": self.error,
            "markers": self.markers,
            "in_range": self.in_range,
            "citations": self.citations,
            "gold_hits": self.gold_hits,
            "judge_score": self.judge_score,
            "judge_grade": self.judge_grade,
            "judge_consistent": self.judge_consistent,
            "judge_note": self.judge_note[:200],
            "latency_ms": round(self.latency_ms, 1),
        }


@dataclass
class EvalResult:
    """一次评测的全部产物（纯内存；CLI 负责落库 eval_runs 与出报告）。"""

    profile: str
    retrieval: RetrievalMetrics
    generation: GenerationStats
    judge: JudgeStats
    items: list[ItemRecord]
    latency_sec: float
    # 逐题校验被跳过的题（id, 原因）：语料变动后标准答案所在分块没了/内容变了。
    # 报告里必须显式写出来——静默缩小分母会让分数看着变好。
    skipped_items: list[tuple[str, str]] = field(default_factory=list)

    def to_metrics_json(self) -> dict[str, object]:
        """metrics_json 快照：报告渲染与 DB 落库共用同一序列化口径。"""
        gen, judge = self.generation, self.judge
        return {
            "profile": self.profile,
            "latency_sec": round(self.latency_sec, 1),
            "retrieval": self.retrieval.final(),
            "generation": {
                "n_answerable": gen.n_answerable,
                "n_unanswerable": gen.n_unanswerable,
                "failed": gen.failed,
                "refused_answerable": gen.refused_answerable,
                "no_citation": gen.no_citation,
                "out_of_range_rate": _summ_or_none(gen.out_of_range_rate),
                "citation_gold_ratio": _summ_or_none(gen.citation_gold),
                "refusal_clean": gen.refusal_clean,
                "refusal_dirty": gen.refusal_dirty,
                "answered_unanswerable": gen.answered_unanswerable,
                "answered_with_citation": gen.answered_with_citation,
                "refusal_accuracy": gen.refusal_accuracy(),
            },
            "judge": {
                "judge_model": judge.judge_model,
                "judged": judge.judged,
                "correctness": _summ_or_none(judge.correctness),
                "top2_grade_rate": _summ_or_none(judge.top2_grade),
                "consistent": judge.consistent,
                "inconsistent": judge.inconsistent,
                "errors": judge.errors,
            },
            "items": [r.to_json() for r in self.items],
            "skipped_items": [{"id": i, "reason": r} for i, r in self.skipped_items],
        }


def _summ_or_none(mean: _Mean) -> dict[str, float | int] | None:
    """空容器 → None（报告渲染成"—"，避免 nan 进入 JSON 快照）。"""
    return None if not mean.values else summarize(mean.values)


# ---------------------------------------------------------------------------
# 执行器
# ---------------------------------------------------------------------------


class EvalRunner:
    """执行一次完整评测。构造一次、run() 一次；settings/golden 全程只读。"""

    def __init__(self, settings: Settings, golden: GoldenSet) -> None:
        self._settings = settings
        self._golden = golden

    # ------------------------------------------------------------------

    def run(
        self,
        on_item: Callable[[ItemRecord], None] | None = None,
    ) -> EvalResult:
        """主流程：指纹校验 → 阶段 A（检索）→ 阶段 B+ C（生成 + 裁判）。

        on_item：阶段 B/C 每完成一题回调一次（逐条留痕，含失败题）——
        Web 后台任务用它推进进度；CLI 不传，行为与默认零差异。
        """
        settings = self._settings
        golden = self._golden
        t0 = perf_counter()

        # ---- 0. 语料快照 + 逐题校验（题库与库错配是评测第一杀手） ----
        corpus = IndexManager(settings).corpus()
        if corpus.empty:
            raise StorageError("知识库为空：评测前先导入语料（mikasa ingest <目录>）")
        # 逐题校验替代原先的全库指纹：不相干的文档增删不再让整份题库作废，
        # 只有"标准答案所在分块确实变了"的那几道被跳过（原因进报告）。
        check = check_against_corpus(golden, corpus.content_hashes())
        golden = check.golden
        if not golden.answerable:
            first = check.skipped[0][1] if check.skipped else "语料为空"
            raise EvalError(
                f"题库与当前语料完全对不上：{len(check.skipped)} 道可答题的标准答案分块"
                f"都不在了（首个原因：{first}）。\n"
                "语料被整体重灌之后题库需要重建——CLI 用 tools/build_golden.py，"
                "Web 用评测页的「为我的资料生成题库」。"
            )
        if check.skipped:
            logger.warning(
                "逐题校验跳过 %d 题（标准答案分块已变动）：%s",
                len(check.skipped),
                "、".join(item_id for item_id, _ in check.skipped[:5]),
            )
        embedding = get_embedding(settings.embedding)
        with open_db(settings.db_path) as conn:
            titles = repo.document_title_map(conn)

        # ---- 阶段 A：检索层（关重排 + 放宽融合窗口，见模块 docstring） ----
        retrieval_metrics = self._run_retrieval_layer(golden, corpus, embedding)
        logger.info(
            "阶段 A 完成：recall@5 均值=%.3f（%d 题）",
            retrieval_metrics.recall[5].finalize(),
            len(golden.answerable),
        )

        # ---- 阶段 B + C：生成层（产品配置）+ 语义裁判 ----
        gen = GenerationStats()
        judge_stats = JudgeStats()
        items: list[ItemRecord] = []
        llm = self._llm(settings)
        generator = Generator(settings, llm)
        retriever = Retriever(settings, corpus, embedding)  # 产品配置原样
        judge = build_judge(settings)
        judge_stats.judge_model = judge.model
        for item in golden.items:
            record = self._run_one_item(
                retriever, generator, judge, titles, corpus, item, gen, judge_stats, llm
            )
            items.append(record)
            if on_item is not None:
                on_item(record)
        if judge_stats.judge_model != "none" and judge_stats.judged:
            logger.info(
                "阶段 C 完成：判题 %d，一致 %d / 不一致 %d",
                judge_stats.judged,
                judge_stats.consistent,
                judge_stats.inconsistent,
            )

        return EvalResult(
            profile=settings.profile,
            retrieval=retrieval_metrics,
            generation=gen,
            judge=judge_stats,
            items=items,
            latency_sec=perf_counter() - t0,
            skipped_items=check.skipped,
        )

    @staticmethod
    def _llm(settings: Settings) -> LLMProvider:
        """生成端 LLM 构造（独立小函数便于测试注入替身）。"""
        from mikasa.providers import get_llm

        return get_llm(settings.llm)

    # ------------------------------------------------------------------
    # 阶段 A
    # ------------------------------------------------------------------

    def _run_retrieval_layer(
        self, golden: GoldenSet, corpus: Corpus, embedding: EmbeddingProvider
    ) -> RetrievalMetrics:
        """检索层：只测召回，不生成。任一题失败即中止（链路整体不可用）。

        golden 由调用方传入（**已过逐题校验**的那份）：用 self._golden 会把
        被跳过的题又算回分母，分数与报告里的题数就对不上了。
        """
        settings = self._settings
        # 覆写为"检索层口径"：关 rerank、融合窗口锚定常量。配置对象是 frozen，
        # 逐层 model_copy 出新对象，绝不污染调用方的 settings
        retrieval_settings = settings.model_copy(
            update={
                "retrieval": settings.retrieval.model_copy(
                    update={"fusion_top_k": RETRIEVAL_FUSION_TOP_K}
                ),
                "reranker": settings.reranker.model_copy(update={"backend": "none"}),
            }
        )
        retriever = Retriever(retrieval_settings, corpus, embedding)
        metrics = make_retrieval_metrics(RETRIEVAL_KS)
        for item in golden.answerable:
            hits, _lat = retriever.retrieve(item.question)
            ranked = [self._require_chunk_id(hit) for hit in hits]
            # 可答题必然带难度（题本校验保证），此处兜底仅满足类型收窄
            metrics.add_item(ranked, set(item.gold_chunk_ids), item.difficulty or "unknown")
        return metrics

    # ------------------------------------------------------------------
    # 阶段 B + C（单题）
    # ------------------------------------------------------------------

    def _run_one_item(
        self,
        retriever: Retriever,
        generator: Generator,
        judge: Judge,
        titles: dict[int, str],
        corpus: Corpus,
        item: GoldenItem,
        gen: GenerationStats,
        judge_stats: JudgeStats,
        llm: LLMProvider,
    ) -> ItemRecord:
        """跑一题并就地更新聚合容器，返回逐条留痕。

        阶段 B 逐条容错：检索/生成异常记 error 跳过（整场继续）；
        阶段 C 裁判异常记 judge_note，不算题目失败（裁判不可用
        ≠ 系统回答不可用）。
        """
        if item.kind == "answerable":
            gen.n_answerable += 1
        else:
            gen.n_unanswerable += 1

        record = ItemRecord(id=item.id, kind=item.kind, difficulty=item.difficulty)
        t_item = perf_counter()
        try:
            # 与产品链路（ask.py）一致：crosslingual 打开时先译成英文作第二路查询。
            # 阶段 A 刻意零 LLM 调用故不含此路（检索层指标只反映主路），阶段 B
            # 本就调 LLM，口径必须与线上一致——否则评测分数与真实问答能力脱节
            # （2026-09-11 修复：此前 runner 全程单路，跨语言能力在评测里不可见）。
            second_query = (
                _translate_query(llm, item.question)
                if self._settings.retrieval.crosslingual
                else None
            )
            hits, _lat = retriever.retrieve(item.question, second_query=second_query)
            answer, _completion = generator.generate(item.question, hits, titles)
        except Exception as exc:  # noqa: BLE001 - 逐条容错：任何链路失败都要留痕
            gen.failed += 1
            record.error = f"{type(exc).__name__}: {exc}"
            record.latency_ms = (perf_counter() - t_item) * 1000.0
            return record
        record.latency_ms = (perf_counter() - t_item) * 1000.0
        # 引用标记统计：全部 [n] 与"落在注入片段编号 1..N 内"的合法数
        # （越界 = 模型自造编号，是引用格式纪律的核心信号）
        record.markers = extract_markers(answer.text)
        record.in_range = sum(1 for m in record.markers if 1 <= m <= len(hits))
        record.refused = answer.refused
        record.citations = len(answer.citations)

        if item.kind == "answerable":
            # 引用块中命中题面 gold 的比例（record.gold_hits 供逐条留痕）
            record.gold_hits = sum(1 for c in answer.citations if c.chunk_id in item.gold_chunk_ids)
            if answer.refused:
                gen.refused_answerable += 1
            elif not answer.citations:
                gen.no_citation += 1
            else:
                total = len(record.markers)
                # 越界标记占比（含自造编号）；无标记时视为 0
                gen.out_of_range_rate.add((total - record.in_range) / total if total else 0.0)
                # gold 命中率只统计"有引用"的样本（此时 citations > 0）
                gen.citation_gold.add(record.gold_hits / len(answer.citations))
        else:
            if answer.refused:
                if record.markers:
                    # L3 违规：拒答句里出现 [n]（系统提示禁止，理论不可达）
                    gen.refusal_dirty += 1
                    record.error = "拒答句却携带引用标记（L3 纪律违规）"
            else:
                gen.answered_unanswerable += 1
                if answer.citations:
                    gen.answered_with_citation += 1

        # ---- 阶段 C：语义裁判（仅可答题非拒答样本值得判） ----
        if item.kind == "answerable" and not answer.refused:
            self._judge_item(record, judge, item, answer, corpus, judge_stats)
        return record

    # ------------------------------------------------------------------
    # 阶段 C 内部件
    # ------------------------------------------------------------------

    def _judge_item(
        self,
        record: ItemRecord,
        judge: Judge,
        item: GoldenItem,
        answer: Answer,
        corpus: Corpus,
        judge_stats: JudgeStats,
    ) -> None:
        """调裁判判一题；NoJudge / 裁判错误 → 记 note 跳过，不影响协议层。"""
        if judge.model == "none":
            return
        # 资料块 = 题面 gold 块的原文拼接（裁判判断忠实性的唯一事实源）
        context = self._gold_context(corpus, item)
        try:
            verdict = judge.evaluate(item.question, item.notes, context, answer.text)
        except Exception as exc:  # noqa: BLE001 - 网络/鉴权/超时：逐题容错
            judge_stats.errors += 1
            record.judge_note = f"裁判调用失败：{type(exc).__name__}: {exc}"
            return
        if verdict is None:
            return  # 拒答/空答案不判语义分（正确性由拒答桶负责）
        record.judge_score = verdict.correctness
        record.judge_grade = verdict.grade
        record.judge_consistent = verdict.consistent
        judge_stats.judged += 1
        if verdict.consistent:
            judge_stats.consistent += 1
            # 双轮一致才采信：单轮分数不可信，只披露不抹平（judge 模块设计）
            judge_stats.correctness.add(verdict.correctness)
            judge_stats.top2_grade.add(1.0 if verdict.grade in ("A", "B") else 0.0)
        else:
            judge_stats.inconsistent += 1
            preview = verdict.raw[0][:100] if verdict.raw else ""
            record.judge_note = (
                "位置交换两轮不一致——模型输出不稳定或处于判分边界，需人工复核。"
                f"裁判原文摘录：{preview}"
            )

    def _gold_context(self, corpus: Corpus, item: GoldenItem) -> str:
        """题面 gold 块的原文拼接（供裁判核对忠实性），总量有上限。"""
        parts: list[str] = []
        budget = _JUDGE_CONTEXT_CAP
        for cid in item.gold_chunk_ids:
            chunk = corpus.chunk_by_id(cid)
            if chunk is None:
                continue  # 指纹校验已保证错配不可能，此处仅防御
            piece = chunk.content[:budget]
            parts.append(piece)
            budget -= len(piece)
            if budget <= 0:
                break
        return "\n".join(parts)

    @staticmethod
    def _require_chunk_id(hit: RetrievedChunk) -> int:
        """检索命中的 chunk 必须有落库 id（BM25 行序对齐 chunk_id 的底层不变量）。"""
        cid = hit.chunk.id
        if cid is None:
            raise EvalError(
                f"检索返回未落库的 chunk（文档 {hit.chunk.document_id}）——语料状态异常，"
                "请 mikasa ingest --reindex 重建"
            )
        return cid
