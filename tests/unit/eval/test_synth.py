"""自动出题单测：采样纪律、模型输出清洗、假 LLM 全程零网络。

为什么这个模块需要单独的测试：它把**用户自己的语料**变成评测标准答案，
出题质量直接决定评测分数有没有意义。而且它调的是一段提示词——模型可以
返回任何东西（带编号、带引号、先来解释一句、干脆空着），清洗逻辑必须
钉死，否则脏问题会一路流进报告。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mikasa.config.settings import load_settings
from mikasa.errors import EvalError
from mikasa.eval.synth import (
    MIN_CHUNK_CHARS,
    _clean_question,
    _sample_chunks,
    synthesize,
)
from mikasa.index.manager import IndexManager
from mikasa.ingest.service import IngestService
from mikasa.providers.llm import Completion

# 语料：两篇各 ~250 字，离线档分块（400/50）下每篇一块，均高于 MIN_CHUNK_CHARS
DOC_A = (
    "# 充填体力学笔记\n\n"
    "充填体的侧限压缩承载机制是矿山地表沉降控制的核心问题之一。"
    "在侧限条件下，充填体横向变形受到围岩约束，其应力应变曲线呈现明显的应变硬化特征，"
    "峰值之后仍保留较高的残余强度。实验采用分级加载方式，每级荷载保持三十分钟"
    "直至变形稳定，记录轴向位移与侧向压力的对应关系，用于反演充填体的等效弹性模量。"
    "灰砂比与料浆浓度是影响强度的两个主要因素，工程上通常取强度与管输性能的平衡点"
    "作为设计依据，并以塌落度试验控制现场料浆的流动性，避免离析与堵管。"
)
DOC_B = (
    "# 螺旋锚工作性能\n\n"
    "循环荷载作用下螺旋锚的承载力会出现不同程度的衰减，"
    "衰减幅度与荷载幅值、循环次数以及土体密实度密切相关。试验结果表明，"
    "当循环荷载幅值超过极限承载力的百分之四十时，锚杆位移累积速率显著加快，"
    "工程设计中应把这一阈值作为控制条件，并据此确定锚固段的长度与注浆压力。"
    "在密实砂土中，螺旋锚的安装扭矩与承载力之间存在稳定的相关关系，"
    "现场可依据安装扭矩的实测值估计承载力，但该方法在软黏土中误差较大。"
)
DOC_C = (
    "# 尾矿库边坡监测\n\n"
    "尾矿库坝体边坡的稳定性主要取决于堆积尾砂的密实度与浸润线位置。"
    "浸润线抬升会显著降低坝体的有效应力，进而削减其抗剪强度。"
    "现场采用测斜管与孔隙水压力计联合监测，测斜管的位移速率是判断滑坡风险最直接的指标；"
    "当位移速率连续三日增大且孔隙水压力同步上升时，应启动应急预案并降低库内水位，"
    "同时对下游居民区发布预警。日常巡查还应记录坝面裂缝的宽度变化。"
)


class _FakeLLM:
    """按调用顺序吐出预设回复的假模型（零网络、零密钥）。"""

    model = "fake-model"

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []

    def complete(self, messages, *, temperature: float, max_tokens: int) -> Completion:
        del temperature, max_tokens
        self.prompts.append(messages[-1]["content"])
        reply = self._replies.pop(0) if self._replies else "问题？"
        return Completion(text=reply)


def _seed(settings, tmp_path: Path) -> None:
    src = tmp_path / "notes"
    src.mkdir(exist_ok=True)
    (src / "a.md").write_text(DOC_A, encoding="utf-8")
    (src / "b.md").write_text(DOC_B, encoding="utf-8")
    (src / "c.md").write_text(DOC_C, encoding="utf-8")
    IngestService(settings).ingest_paths([src])


def _settings(tmp_path: Path, profile: str = "offline"):
    return load_settings(profile, data_dir=tmp_path / "data")


def _install_fake_llm(monkeypatch, replies: list[str]) -> _FakeLLM:
    fake = _FakeLLM(replies)
    import mikasa.providers as providers

    monkeypatch.setattr(providers, "get_llm", lambda config: fake)
    return fake


def test_synthesize_freezes_gold_chunk_and_hash(tmp_path, monkeypatch):
    """每道可答题的标准答案必须指向真实分块，并冻结它的内容哈希。"""
    settings = _settings(tmp_path)
    _seed(settings, tmp_path)
    fake = _install_fake_llm(monkeypatch, [f"自动问题 {i}？" for i in range(1, 20)])
    # 让 backend 不是 mock，否则会被"离线档不出题"的预检拦下
    settings = settings.model_copy(
        update={"llm": settings.llm.model_copy(update={"backend": "api"})}
    )

    golden = synthesize(settings, questions=2, unanswerable=1)
    hashes = IndexManager(settings).corpus().content_hashes()

    assert golden.source == "synthesized"
    assert golden.model == "fake-model"
    assert len(golden.answerable) == 2
    assert len(golden.unanswerable) == 1
    for item in golden.answerable:
        assert len(item.gold_chunk_ids) == len(item.gold_hashes) == 1
        assert hashes[item.gold_chunk_ids[0]] == item.gold_hashes[0]
        assert item.difficulty == "medium"  # 自动题不分层：模型自评不可信
    # 题目唯一：同一块被重复问到说明采样出了错
    assert len({i.question for i in golden.answerable}) == 2
    assert len(fake.prompts) >= 3


def test_mock_backend_is_refused_with_a_human_message(tmp_path):
    """离线档的模拟模型不会出题——必须当场说清楚，别让它跑出一堆废题。"""
    settings = _settings(tmp_path)  # offline profile → backend=mock
    _seed(settings, tmp_path)
    with pytest.raises(EvalError, match="需要真实模型"):
        synthesize(settings, questions=2, unanswerable=1)


def test_replies_that_cannot_be_used_are_skipped(tmp_path, monkeypatch):
    """模型返回空/超长内容时跳过那一道，不写进题库也不崩。"""
    settings = _settings(tmp_path)
    _seed(settings, tmp_path)
    settings = settings.model_copy(
        update={"llm": settings.llm.model_copy(update={"backend": "api"})}
    )
    # 第一条是废回复（空），后面才是正常问题：废的那次不占题数，但**消耗一个块**
    replies = ["", "可答题一？", "可答题二？", "不可答题一？"]
    _install_fake_llm(monkeypatch, replies)

    golden = synthesize(settings, questions=2, unanswerable=1)
    assert [i.question for i in golden.answerable] == ["可答题一？", "可答题二？"]


def test_all_replies_unusable_raises(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _seed(settings, tmp_path)
    settings = settings.model_copy(
        update={"llm": settings.llm.model_copy(update={"backend": "api"})}
    )
    _install_fake_llm(monkeypatch, [""] * 50)
    with pytest.raises(EvalError, match="没凑齐"):
        synthesize(settings, questions=2, unanswerable=1)


def test_short_corpus_is_refused(tmp_path):
    """全是很短的分块时不出题（一两句话的块写不出有信息量的题）。"""
    settings = _settings(tmp_path)
    # 先过"需要真实模型"那道闸，否则拦住它的是 mock 预检而不是语料太短
    settings = settings.model_copy(
        update={"llm": settings.llm.model_copy(update={"backend": "api"})}
    )
    src = tmp_path / "tiny"
    src.mkdir(exist_ok=True)
    (src / "t.md").write_text("# 小\n\n太短了。\n", encoding="utf-8")
    IngestService(settings).ingest_paths([src])
    with pytest.raises(EvalError, match="没有足够长"):
        synthesize(settings, questions=2, unanswerable=1)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("问题：什么是侧限压缩？", "什么是侧限压缩？"),
        ("Q: what is it?", "what is it?"),
        ("1. 螺旋锚的阈值是多少？", "螺旋锚的阈值是多少？"),
        ("- 「充填体如何硬化？」", "充填体如何硬化？"),
        ("先解释一句。\n\n充填体的应力应变曲线如何？", "先解释一句。"),
        ("", None),
        ("   ", None),
        ("长" * 200, None),
    ],
)
def test_clean_question_strips_model_noise(raw, expected):
    """模型爱加编号、引号、前言——清洗规则逐条钉住。"""
    assert _clean_question(raw) == expected


def test_sampling_rotates_across_documents(tmp_path):
    """跨文档轮转：否则题库全堆在最长的文档上，评出来的是那一篇的质量。"""
    settings = _settings(tmp_path)
    _seed(settings, tmp_path)
    corpus = IndexManager(settings).corpus()
    sampled = _sample_chunks(corpus, seed=0)

    assert sampled, "测试语料应当有够长的块"
    assert all(len(c.content.strip()) >= MIN_CHUNK_CHARS for c in sampled)
    # 前两个块必然来自不同文档（轮转的定义）
    if len(sampled) >= 2:
        assert sampled[0].document_id != sampled[1].document_id


def test_same_seed_gives_the_same_bank(tmp_path):
    """固定 seed 可复现：同一份语料两次生成的块顺序一致，便于对比两次改动。"""
    settings = _settings(tmp_path)
    _seed(settings, tmp_path)
    corpus = IndexManager(settings).corpus()
    first = [c.id for c in _sample_chunks(corpus, seed=7)]
    second = [c.id for c in _sample_chunks(corpus, seed=7)]
    assert first == second
