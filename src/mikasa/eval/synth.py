"""从用户自己的语料自动生成评测题库（2026-09-19）。

**为什么需要**：人工题库（`evals/questions.yaml`）的锚句是内置示例语料的原文，
只能评那一份语料——"评我自己的资料"这件事，人工题本天然做不到，因为没人替你的
文档写过标准答案。这里让模型读一段、出一道"只有这段能回答"的题，**那个分块就
自动成为标准答案**，于是任意语料都能评测。

**两类题，缺一不可**：
  可答题   —— 给一个分块，要一个答案就在这段里的问题；gold = 该分块。
  不可答题 —— 给几个分块，要一个"它们都答不上来"的问题；评分信号是必须拒答。
              拒答纪律是这套系统的核心承诺（见 docs/limitations-and-failures.md），
              没有不可答题就完全测不出来。

**采样的三条纪律**：
  1. **跨文档轮转**取块：同一篇最多连取一个就换下一篇。否则题库会堆在最长的
     那篇上，跑出来的其实是"那一篇的检索质量"；
  2. 太短的块（< MIN_CHUNK_CHARS）不出题：一两句话的块写不出有信息量的题，
     而且它被检索到几乎没有难度，白占分母；
  3. **固定 seed**：同一份语料生成的题库可复现，便于对比两次检索改动的效果。

**诚实边界**（写进报告）：自动题是模型照着自己看过的原文出的，比人工题**简单**
——它不会像人那样故意挑边角、挑需要跨段推理的题。所以自动题上的分数天然偏高，
只能横向比（同一份题库下比较两次改进），不能跟人工题的绝对分比。报告里以
`source: synthesized` 标注区分。
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mikasa.config.settings import Settings
from mikasa.errors import EvalError
from mikasa.eval.golden import GoldenItem, GoldenSet, save_golden
from mikasa.index.manager import Corpus, IndexManager
from mikasa.models.document import Chunk
from mikasa.providers.llm import Completion, LLMProvider

# 太短的块不出题：见模块头的采样纪律②
MIN_CHUNK_CHARS = 140
# 上限：超长块（整章）出题会让题面变成"这章讲了什么"，也给模型白烧 token
MAX_CHUNK_CHARS = 2400
# 提示词里给模型的资料截断长度：出题只需要够判断"答案在不在这里"
_EXCERPT_CHARS = 1800

DEFAULT_QUESTIONS = 24
DEFAULT_UNANSWERABLE = 8
MAX_QUESTION_CHARS = 160

_SYSTEM = "你是检索评测的出题人。只输出一行问题，不要解释、不要编号、不要引号、不要写答案。"

_ANSWERABLE_TEMPLATE = """\
下面是一份资料里的一个片段：

<片段>
{text}
</片段>

请提出一个**只能靠这个片段回答**的问题。要求：
- 问的是片段里的某个具体事实、方法、数值或结论，不是"这段讲了什么"；
- 不要问片段里没有的信息；
- 不要把答案写进问题里，也不要出现"这个片段""上文"这类指代；
- 只输出问题本身，一行，中文。"""

_UNANSWERABLE_TEMPLATE = """\
下面是某人的资料库里的几个片段：

{blocks}

请提出一个**这些片段都回答不了**、但读完它们的人会自然想问的问题。要求：
- 问题要贴着这些片段的主题（听起来该有答案，实际没有）；
- 不要问完全无关的领域（那是废话问题，测不出东西）；
- 只输出问题本身，一行，中文。"""


@dataclass(frozen=True)
class SynthProgress:
    """出题进度（Web 轮询用；CLI 不传回调即为无感）。"""

    done: int
    total: int
    stage: str  # "可答题" / "不可答题"

    def to_dict(self) -> dict[str, object]:
        return {"done": self.done, "total": self.total, "stage": self.stage}


def golden_path(settings: Settings) -> Path:
    """自动题库存放位置：数据目录下的 eval/（用户数据，不进仓库）。"""
    return settings.data_dir / "eval" / "golden-auto.json"


def _clean_question(raw: str) -> str | None:
    """模型的输出 → 一行干净的问题；不合规返回 None（跳过这块，换下一块）。

    模型普遍会自作主张加编号、引号、markdown 强调，甚至先来一句"问题是："
    ——逐条剥掉，比在提示词里反复叮嘱有效。
    """
    text = (raw or "").strip()
    if not text:
        return None
    # 取第一段非空行：有的模型会附上解释或答案
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    line = re.sub(r"^\s*(?:问题|题|Q|Question)\s*[:：]\s*", "", line, flags=re.IGNORECASE)
    line = re.sub(r"^\s*(?:[-*•]|\d+[.、)]|[（(]\d+[）)])\s*", "", line)
    line = line.strip().strip("\"'“”‘’`「」【】").strip()
    if not line or len(line) > MAX_QUESTION_CHARS:
        return None
    # 问号缺失多半不是问题（模型跑了题），但也可能只是没写标点：不强制，只警告
    return line


def _ask(llm: LLMProvider, prompt: str) -> str:
    completion: Completion = llm.complete(
        [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=200,
    )
    return completion.text


def _sample_chunks(corpus: Corpus, seed: int) -> list[Chunk]:
    """跨文档轮转排序后的候选块（长度不合规的直接排除）。"""
    by_doc: dict[int, list[Chunk]] = {}
    for chunk in corpus.chunks:
        text = chunk.content.strip()
        if chunk.id is None:  # 快照里的块必有 id；防御性跳过（见 Corpus.content_hashes）
            continue
        if not (MIN_CHUNK_CHARS <= len(text) <= MAX_CHUNK_CHARS):
            continue
        by_doc.setdefault(chunk.document_id, []).append(chunk)
    rng = random.Random(seed)
    for chunks in by_doc.values():
        rng.shuffle(chunks)
    # 轮转：每轮每篇各取一个，篇内顺序由 rng 决定（可复现）
    order: list[Chunk] = []
    round_no = 0
    while True:
        added = False
        for doc_id in sorted(by_doc):
            chunks = by_doc[doc_id]
            if round_no < len(chunks):
                order.append(chunks[round_no])
                added = True
        if not added:
            return order
        round_no += 1


def _require_real_llm(settings: Settings) -> None:
    """离线档的 MockLLM 不会出题——提前用人话说清楚，别让它跑出一堆废题。"""
    if settings.llm.backend == "mock":
        raise EvalError(
            "自动出题需要真实模型，当前是离线档的模拟模型（backend=mock）。"
            "请在「设置 → 模型」里接入 api 或 local 档后再试。"
        )


def _chunk_id(chunk: Chunk) -> int:
    """快照里的块必然有 id（`None` 只出现在入库前的临时对象上）。

    不用 assert：`python -O` 会把 assert 整条删掉，于是脏数据会静默变成
    "gold_chunk_ids=[None]"，直到评测时才发现（2026-09-11 同类问题修过一次）。
    """
    if chunk.id is None:  # pragma: no cover - 与 Corpus.content_hashes 同一不变量
        raise EvalError("语料快照里出现了没有 id 的分块（索引快照可能已损坏）")
    return chunk.id


def synthesize(
    settings: Settings,
    *,
    questions: int = DEFAULT_QUESTIONS,
    unanswerable: int = DEFAULT_UNANSWERABLE,
    seed: int = 0,
    on_progress: Callable[[SynthProgress], None] | None = None,
    corpus: Corpus | None = None,
) -> GoldenSet:
    """为当前语料生成一份黄金集（不落盘；落盘由调用方决定）。

    corpus 可注入（测试零索引）；不传则取当前索引快照。
    出不满目标题数不算失败——语料小、模型偶发不配合都会少题，如实少给。
    """
    from mikasa.providers import get_llm

    _require_real_llm(settings)
    snapshot = corpus if corpus is not None else IndexManager(settings).corpus()
    if snapshot.empty:
        raise EvalError("知识库为空：先导入语料（mikasa ingest <目录>）再生成题库。")
    pool = _sample_chunks(snapshot, seed)
    if not pool:
        raise EvalError(
            f"没有足够长的分块可以出题（每个至少 {MIN_CHUNK_CHARS} 字）。"
            "语料太碎时请先调整分块参数再入库。"
        )

    llm = get_llm(settings.llm)
    items: list[GoldenItem] = []
    total = questions + unanswerable
    done = 0
    used: set[int] = set()

    for chunk in pool:
        if len([i for i in items if i.kind == "answerable"]) >= questions:
            break
        used.add(_chunk_id(chunk))
        if on_progress is not None:
            on_progress(SynthProgress(done=done, total=total, stage="可答题"))
        raw = _ask(llm, _ANSWERABLE_TEMPLATE.format(text=chunk.content[:_EXCERPT_CHARS]))
        question = _clean_question(raw)
        done += 1
        if question is None:
            continue
        items.append(
            GoldenItem(
                id=f"a{len(items) + 1:03d}",
                kind="answerable",
                question=question,
                difficulty="medium",  # 自动题不给难度分层：模型自评不可信，硬编中档
                gold_chunk_ids=[_chunk_id(chunk)],
                gold_hashes=[chunk.content_sha256],
                notes=f"自动生成（{llm.model}）；标准答案分块：chunk {_chunk_id(chunk)}",
            )
        )

    # 不可答题：给几个块作背景，要一个它们都答不上来的问题。
    # 背景块数量**不设下限**（2026-09-19 修）：原先少于 3 个块就整个跳过，
    # 于是小语料（块本来就不多）永远出不了不可答题，而拒答纪律正是靠它测的。
    leftovers = [c for c in pool if _chunk_id(c) not in used] or list(pool)
    for start in range(0, len(leftovers), 3):
        if len([i for i in items if i.kind == "unanswerable"]) >= unanswerable:
            break
        batch = leftovers[start : start + 3]
        if on_progress is not None:
            on_progress(SynthProgress(done=done, total=total, stage="不可答题"))
        blocks = "\n\n".join(f"[{n}] {c.content[:600]}" for n, c in enumerate(batch, start=1))
        question = _clean_question(_ask(llm, _UNANSWERABLE_TEMPLATE.format(blocks=blocks)))
        done += 1
        if question is None:
            continue
        items.append(
            GoldenItem(
                id=f"u{len(items) + 1:03d}",
                kind="unanswerable",
                question=question,
                reason="insufficient",  # 贴着语料主题但语料答不了 → insufficient
                notes=(
                    f"自动生成（{llm.model}）；"
                    f"参考分块：{', '.join(str(_chunk_id(c)) for c in batch)}"
                ),
            )
        )

    if on_progress is not None:
        on_progress(SynthProgress(done=total, total=total, stage="完成"))

    if not any(i.kind == "answerable" for i in items) or not any(
        i.kind == "unanswerable" for i in items
    ):
        raise EvalError(
            "自动出题没凑齐可答题与不可答题（各至少一道）——"
            f"语料太小或模型没按格式回答。已生成 {len(items)} 道。"
        )
    return GoldenSet(
        name="mikasa-auto",
        corpus_sha256=snapshot.sha256,
        source="synthesized",
        model=llm.model,
        items=items,
    )


def synthesize_to_disk(
    settings: Settings,
    *,
    questions: int = DEFAULT_QUESTIONS,
    unanswerable: int = DEFAULT_UNANSWERABLE,
    seed: int = 0,
    on_progress: Callable[[SynthProgress], None] | None = None,
) -> tuple[GoldenSet, Path]:
    """生成并落盘到数据目录；返回 (题库, 路径)。"""
    golden = synthesize(
        settings,
        questions=questions,
        unanswerable=unanswerable,
        seed=seed,
        on_progress=on_progress,
    )
    path = golden_path(settings)
    save_golden(golden, path)
    return golden, path
