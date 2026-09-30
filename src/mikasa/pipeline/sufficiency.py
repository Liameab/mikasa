"""证据充分性自评 + 一次补检索（Self-RAG 的最小闭包，2026-09-30）。

链路：首轮检索 → LLM 自评"这些片段够不够回答问题" → 判不足时用它给出的
**新查询**再检索一次 → 新命中的块去重后追加在尾部（主排序与编号不动）→
照常生成。测量口径与结论见 docs/evaluation.md；开关 `retrieval.sufficiency_retry`。

三条纪律（与 _translate_query / hop_expand 一脉）：

1. **默认关**：多花 1~3 次 LLM 调用，并改变注入的片段集合——开了就平移
   评测基线，只作实验开关（profile yaml 不写，实验用覆盖层）。
2. **mock 档零调用**（ADR-0014 ③）：offline 的确定性演示不得多出 LLM 调用。
3. **任何失败都退回基线**：自评调用异常 / 解析不出 / 新查询为空 → 原命中
   原样返回。它是增强环节，绝不能把问答主链拖挂（同 _translate_query）。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from mikasa.config.settings import Settings
from mikasa.models.retrieval import RetrievedChunk
from mikasa.pipeline.prompts import (
    _SUFFICIENCY_SOURCE_CAP,
    SUFFICIENCY_MAX_TOKENS,
    build_sufficiency_messages,
)
from mikasa.pipeline.retriever import Retriever
from mikasa.utils.logging import get_logger

if TYPE_CHECKING:  # 仅类型标注：providers → pipeline 的既有分层不倒过来
    from mikasa.providers.llm import LLMProvider

logger = get_logger("pipeline")

# 补检索最多追加几块。上限的存在是为了"一次补检索"这个有限承诺：不设顶时
# 第二次检索也会带回整窗（本地档 14 块），注入量翻倍、引用编号一路排到 28。
# 3 块 ≈ 首轮窗口的两成，够把"漏掉的那一块"捞回来，又不至于改写答案的素材盘。
_EXTRA_CAP = 3


def _parse_followup(text: str) -> str | None:
    """自评输出 → 补检索查询；判"足够"或解析不出都是 None（不补检索）。

    容忍小模型漂移：只认包含"不足"的那一行（"资料不足：…"也算），取第一个
    冒号（全/半角）之后的内容并剥掉包裹引号；空串 = 没给出查询，同样当 None。
    """
    line = next((raw.strip() for raw in text.splitlines() if raw.strip()), "")
    if "不足" not in line:
        return None
    for sep in ("：", ":"):
        if sep in line:
            line = line.split(sep, 1)[1]
            break
    else:
        line = line.replace("不足", "", 1)
    line = line.strip().strip('"').strip("'").strip("“”‘’")
    return " ".join(line.split()) or None


def assess(llm: LLMProvider, question: str, hits: list[RetrievedChunk]) -> str | None:
    """自评证据是否充分：返回补检索查询，或 None（充分 / 判定不可用）。

    材质只取前若干块的 snippet（prompts._SUFFICIENCY_SOURCE_CAP）——自评是
    旁观调用，输入长度必须封顶，否则本地档 14 块全塞进去会比生成本身还贵。
    """
    sources = [
        (index + 1, hit.chunk.snippet) for index, hit in enumerate(hits[:_SUFFICIENCY_SOURCE_CAP])
    ]
    try:
        completion = llm.complete(
            build_sufficiency_messages(question, sources),
            temperature=0.0,
            max_tokens=SUFFICIENCY_MAX_TOKENS,
        )
    except Exception as exc:  # noqa: BLE001 - 自评只是增强器：**任何**失败都退回基线
        # 同 _translate_query：provider 只把"建流/建请求"包成 ProviderError，
        # 响应解析在网关返回空候选时会抛 IndexError 穿透到这里。
        logger.warning("证据自评失败（按足够处理，不补检索）：%s", type(exc).__name__)
        return None
    followup = _parse_followup(completion.text)
    if followup is not None:
        logger.debug("证据自评判不足，补检索查询：%r", followup[:60])
    return followup


def _merge(hits: list[RetrievedChunk], extra: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """补检索命中去重后追加尾部（最多 _EXTRA_CAP 块，秩接着排）。

    追加而非重排：引用编号 `[n]` 走**注入顺序**，主命中必须稳定占住靠前的
    编号，混进来会让排序随自评波动（与 hop_expand 的"追加在尾部"同款取舍）。
    """
    seen = {hit.chunk.id for hit in hits}
    merged = list(hits)
    for hit in extra:
        if hit.chunk.id is None or hit.chunk.id in seen:
            continue
        seen.add(hit.chunk.id)
        merged.append(hit.model_copy(update={"rank": len(merged) + 1}))
        if len(merged) - len(hits) >= _EXTRA_CAP:
            break
    return merged


def maybe_supplement(
    settings: Settings,
    llm: LLMProvider,
    retriever: Retriever,
    question: str,
    hits: list[RetrievedChunk],
    *,
    document_id: int | None = None,
) -> tuple[list[RetrievedChunk], dict[str, float]]:
    """按开关执行"自评 → 一次补检索 → 尾部合并"；关/不适用时原样返回。

    返回 (命中, 额外延迟分段)。延迟键只在**真的发生**时出现（供评测对照
    成本）：sufficiency（自评调用）、translate2（补检索查询的跨语言翻译）、
    retrieve2（第二次检索）。调用方（ask._retrieve / eval.runner 阶段 B）
    把这段延迟并入自己那份 latency 字典。

    document_id 原样透传给补检索：阅读器「只看本篇」的范围是用户的显式
    选择，补检索不得越界到全库（否则注入里会冒出别的文档的片段）。
    """
    if not settings.retrieval.sufficiency_retry or settings.llm.backend == "mock" or not hits:
        return hits, {}

    t0 = time.perf_counter()
    followup = assess(llm, question, hits)
    lat = {"sufficiency": round((time.perf_counter() - t0) * 1000.0, 1)}
    if not followup:
        return hits, lat

    # 跨语言第二路同样适用：补检索查询也要译（局部 import 规避
    # ask → sufficiency → ask 的循环：_translate_query 的宿主是 ask.py）
    second_query = None
    if settings.retrieval.crosslingual:
        from mikasa.pipeline.ask import _translate_query

        t1 = time.perf_counter()
        second_query = _translate_query(llm, followup)
        lat["translate2"] = round((time.perf_counter() - t1) * 1000.0, 1)

    t2 = time.perf_counter()
    extra, _inner = retriever.retrieve(followup, second_query=second_query, document_id=document_id)
    lat["retrieve2"] = round((time.perf_counter() - t2) * 1000.0, 1)
    merged = _merge(hits, extra)
    logger.info(
        "补检索：追加 %d 块（首轮 %d 块 → 注入 %d 块）",
        len(merged) - len(hits),
        len(hits),
        len(merged),
    )
    return merged, lat
