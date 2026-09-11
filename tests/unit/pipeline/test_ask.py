"""问答门面集成测试：AskService 的编排、会话落库与历史摘要。

offline profile：MockLLM + 纯 BM25，零密钥端到端——CLI/Web/评测
共用此门面，此处测过即三处同时可信。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mikasa.errors import ConfigError, ProviderError, StorageError
from mikasa.ingest.service import IngestService
from mikasa.pipeline.ask import AskService, _translate_query
from mikasa.providers.llm import Completion
from mikasa.storage import repo
from mikasa.storage.db import open_db
from mikasa.utils.text import fold_title

NOTE = """# 机器学习笔记

## 正则化

L2 正则化在损失中加入权重的平方和惩罚项，鼓励小而分散的权重。

## 注意力机制

缩放点积注意力除以根号 dk，防止点积随维度增大而方差过大。
"""


def _seed(tmp_path: Path, offline_settings) -> None:
    md = tmp_path / "n.md"
    md.write_text(NOTE, encoding="utf-8")
    IngestService(offline_settings).ingest_paths([tmp_path])


def _count(offline_settings, table: str) -> int:
    with open_db(offline_settings.db_path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_ask_in_corpus_records_session(tmp_path, offline_settings):
    _seed(tmp_path, offline_settings)
    answer = AskService(offline_settings).ask("L2 正则化为什么能防止过拟合？")
    assert not answer.refused
    assert answer.citations and answer.citations[0].marker == 1
    assert set(answer.latency_ms) == {"retrieve", "rerank", "generate"}
    assert _count(offline_settings, "qa_sessions") == 1
    with open_db(offline_settings.db_path) as conn:
        assert len(repo.messages_by_session(conn, 1)) == 2  # user + assistant


def test_ask_out_of_corpus_refuses(tmp_path, offline_settings):
    _seed(tmp_path, offline_settings)
    answer = AskService(offline_settings).ask("如何在一周内学会做菠萝包？")
    assert answer.refused
    assert answer.citations == []
    assert _count(offline_settings, "qa_sessions") == 1  # 拒答同样留审计轨迹


def test_chat_reuses_session_and_threads_history(tmp_path, offline_settings):
    _seed(tmp_path, offline_settings)
    svc = AskService(offline_settings)
    first = svc.ask("L2 正则化是什么？")
    assert not first.refused

    # 第二轮：会话内续问（历史摘要注入，消息数 = 4）
    with open_db(offline_settings.db_path) as conn:
        session_id = int(
            conn.execute("SELECT id FROM qa_sessions ORDER BY id LIMIT 1").fetchone()[0]
        )
    second = svc.chat(session_id, "缩放因子为什么是根号 dk？")
    assert not second.refused
    assert _count(offline_settings, "qa_sessions") == 1
    with open_db(offline_settings.db_path) as conn:
        messages = repo.messages_by_session(conn, session_id)
    assert len(messages) == 4  # 两轮 × (user + assistant)
    assert messages[0]["role"] == "user"
    assert messages[3]["refused"] == 0


def test_chat_history_summary_notes_refusal(tmp_path, offline_settings):
    _seed(tmp_path, offline_settings)
    svc = AskService(offline_settings)
    with open_db(offline_settings.db_path) as conn:
        session_id = repo.create_session(conn, offline_settings.profile)
    svc.ask("如何做菠萝包？", session_id=session_id)
    summary = svc._history_summary(session_id)
    assert summary and "拒绝回答" in summary  # 拒答轮也进摘要，历史可解释


def test_empty_corpus_raises(tmp_path, offline_settings):
    offline_settings.ensure_dirs()
    with pytest.raises(StorageError, match="知识库为空"):
        AskService(offline_settings).ask("什么是线性代数？")


def test_blank_question_raises(tmp_path, offline_settings):
    _seed(tmp_path, offline_settings)
    with pytest.raises(StorageError, match="问题为空"):
        AskService(offline_settings).ask("   ")


# ---------------------------------------------------------------------------
# 流式路径（Web SSE 的数据源）：meta → delta×n → done 协议对偶
# ---------------------------------------------------------------------------


def test_ask_stream_delta_concat_equals_done(tmp_path, offline_settings):
    _seed(tmp_path, offline_settings)
    events = list(AskService(offline_settings).ask_stream("L2 正则化为什么能防止过拟合？"))

    kinds = [e.kind for e in events]
    assert kinds[0] == "meta"
    assert kinds[-1] == "done"
    assert set(kinds[1:-1]) == {"delta"}  # 中间全是正文增量
    assert events[0].session_id is not None  # meta 带会话 id（前端续问用）

    done = events[-1].answer
    assert done is not None and not done.refused
    assert "".join(e.text for e in events if e.kind == "delta") == done.text  # 拼接恒等
    assert done.citations and done.citations[0].marker == 1
    assert done.prompt_tokens is None and done.completion_tokens is None  # 流式无 usage
    assert set(done.latency_ms) == {"retrieve", "rerank", "generate"}

    # 落库与非流式同轨：user + assistant 一对消息
    assert _count(offline_settings, "qa_sessions") == 1
    with open_db(offline_settings.db_path) as conn:
        messages = repo.messages_by_session(conn, events[0].session_id)
    assert len(messages) == 2
    assert messages[1]["content"] == done.text


def test_ask_stream_refusal_matches_ask(tmp_path, offline_settings):
    _seed(tmp_path, offline_settings)
    svc = AskService(offline_settings)
    answer = svc.ask("如何在一周内学会做菠萝包？")  # 语料外问题 → 拒答
    events = list(svc.ask_stream("如何在一周内学会做菠萝包？"))
    done = events[-1].answer
    assert answer.refused and done is not None and done.refused
    assert "".join(e.text for e in events if e.kind == "delta") == done.text == answer.text


def test_ask_stream_raises_on_empty_corpus(offline_settings):
    with pytest.raises(StorageError, match="知识库为空"):
        list(AskService(offline_settings).ask_stream("什么是线性代数？"))


def test_chat_stream_reuses_session(tmp_path, offline_settings):
    _seed(tmp_path, offline_settings)
    svc = AskService(offline_settings)
    with open_db(offline_settings.db_path) as conn:
        session_id = repo.create_session(conn, offline_settings.profile)

    events = list(svc.chat_stream(session_id, "L2 正则化是什么？"))
    assert events[0].kind == "meta" and events[0].session_id == session_id
    assert events[-1].kind == "done" and not events[-1].answer.refused
    assert _count(offline_settings, "qa_sessions") == 1  # 未新建会话
    with open_db(offline_settings.db_path) as conn:
        assert len(repo.messages_by_session(conn, session_id)) == 2


# ---------------------------------------------------------------------------
# 自由问答（free）模式：kb 旁路，直连 LLM（ADR-0013）
# ---------------------------------------------------------------------------


class _FreeLLM:
    """free 替身：确定性语义模型（记录调用、按消息尾句作答、带 usage）。

    complete/stream 都吞掉提示词、以最后一条 user 消息拼回复——调用方
    从记录断言"注入了什么历史"，回复文本只求稳定。
    """

    model = "fake-free"

    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = list(replies or [])
        self.complete_calls: list[list[dict[str, str]]] = []
        self.stream_calls: list[list[dict[str, str]]] = []

    def _text(self, messages: list[dict[str, str]]) -> str:
        if self.replies:
            return self.replies.pop(0)
        return f"（自由作答）{messages[-1]['content']}"

    def complete(self, messages, *, temperature, max_tokens) -> Completion:
        self.complete_calls.append(messages)
        text = self._text(messages)
        return Completion(text=text, prompt_tokens=len(messages), completion_tokens=len(text))

    def stream(self, messages, *, temperature, max_tokens):
        self.stream_calls.append(messages)
        text = self._text(messages)
        step = max(1, len(text) // 2)
        for i in range(0, len(text), step):
            yield text[i : i + step]


def _free_ready(offline_settings):
    """offline 配置的 free 变体：仅把 LLM 后端改 api（路径/语料全不变）。

    守卫口径是 settings.llm.backend（ADR-0013）：改这一处即让 free 合法化，
    数据目录与 offline 完全隔离一致；AskService 侧注入 _FreeLLM 则零网络。
    """
    return offline_settings.model_copy(
        update={"llm": offline_settings.llm.model_copy(update={"backend": "api"})}
    )


def _session_id(offline_settings) -> int:
    with open_db(offline_settings.db_path) as conn:
        return int(conn.execute("SELECT id FROM qa_sessions ORDER BY id LIMIT 1").fetchone()[0])


def test_free_answers_without_corpus_and_records(offline_settings):
    """free 不依赖语料：空库直答（kb 在此必报"知识库为空"，形成对照）。"""
    settings = _free_ready(offline_settings)
    settings.ensure_dirs()
    svc = AskService(settings, llm=_FreeLLM())
    answer = svc.ask("讲讲太阳系有几颗行星", mode="free")

    assert not answer.refused
    assert answer.citations == []
    assert answer.model == "fake-free"
    assert answer.text == "（自由作答）讲讲太阳系有几颗行星"
    assert answer.prompt_tokens is not None and answer.completion_tokens is not None
    assert set(answer.latency_ms) == {"generate"}  # 无 retrieve 键 = 回放判据

    # 落库与 kb 同轨：citations_json="[]"（空 → 摘要话术的诚实化判据）
    with open_db(settings.db_path) as conn:
        messages = repo.messages_by_session(conn, _session_id(settings))
    assert len(messages) == 2
    assert messages[1]["citations_json"] == "[]"
    assert messages[1]["refused"] == 0
    assert messages[1]["prompt_tokens"] == 2  # [system, user] 两则消息


def test_free_chat_injects_raw_history_not_summary(offline_settings):
    """free 多轮注入原始消息（不做 kb 的摘要压缩）——指代连续性的前提。"""
    settings = _free_ready(offline_settings)
    settings.ensure_dirs()
    svc = AskService(settings, llm=_FreeLLM(replies=["甲答", "乙答"]))
    svc.ask("第一问", mode="free")  # 首问新建会话
    svc.chat(_session_id(settings), "第二问", mode="free")  # 续问复用该会话

    messages = svc._llm.complete_calls[-1]
    roles = [m["role"] for m in messages]
    assert roles == ["system", "user", "assistant", "user"]  # 交替结构完整
    assert messages[0]["role"] == "system" and "自由问答" in messages[0]["content"]
    assert messages[1]["content"] == "第一问"
    assert messages[2]["content"] == "甲答"
    assert messages[3]["content"] == "第二问"
    blob = "".join(m["content"] for m in messages)
    assert "（历史）用户" not in blob  # 非摘要行
    assert "【资料片段】" not in blob  # 非 kb 注入格式


def test_free_history_truncates_to_five_rounds(offline_settings):
    """free 历史同样限最近 5 轮：第 6 轮起最早的整轮被挤出上下文。"""
    settings = _free_ready(offline_settings)
    settings.ensure_dirs()
    svc = AskService(settings, llm=_FreeLLM())
    sid: int | None = None
    for i in range(6):
        svc.ask(f"q{i + 1}", session_id=sid, mode="free")
        if sid is None:  # 首问建会话，其后全部复用（单会话累计 6 轮）
            sid = _session_id(settings)
    svc.chat(sid, "q7", mode="free")

    messages = svc._llm.complete_calls[-1]
    assert len(messages) == 12  # system + 最近 5 轮 × 2 + q7（DB 已 7 轮 14 条）
    assert [m["role"] for m in messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert messages[1]["content"] == "q2"  # 最早幸存：q1 整轮被挤出
    assert messages[-1]["content"] == "q7"
    contents = [m["content"] for m in messages]
    assert "q1" not in contents  # q2..q6 为幸存五轮，q7 为当轮


def test_free_stream_meta_delta_done(offline_settings):
    """free 流式与 kb 四段式同构：meta → delta×n → done，拼接恒等。"""
    settings = _free_ready(offline_settings)
    settings.ensure_dirs()
    svc = AskService(settings, llm=_FreeLLM(replies=["甲乙"]))
    events = list(svc.ask_stream("讲讲太阳系", mode="free"))

    kinds = [e.kind for e in events]
    assert kinds[0] == "meta" and kinds[-1] == "done"
    assert set(kinds[1:-1]) == {"delta"}
    assert events[0].session_id is not None

    done = events[-1].answer
    assert done is not None and not done.refused
    assert done.citations == []
    assert "".join(e.text for e in events if e.kind == "delta") == done.text == "甲乙"
    assert done.prompt_tokens is None and done.completion_tokens is None  # 流式无 usage
    assert set(done.latency_ms) == {"generate"}

    # 落库与 kb 流式同轨
    with open_db(settings.db_path) as conn:
        messages = repo.messages_by_session(conn, events[0].session_id)
    assert len(messages) == 2 and messages[1]["content"] == "甲乙"
    assert len(svc._llm.stream_calls) == 1


def test_free_guarded_on_mock_backend(offline_settings):
    """mock（offline profile）无语义 → free 三入口一律 ConfigError；非法 mode 同拦。"""
    offline_settings.ensure_dirs()
    svc = AskService(offline_settings)
    with pytest.raises(ConfigError, match="api / local"):
        svc.ask("讲讲太阳系", mode="free")
    with pytest.raises(ConfigError, match="api / local"):
        list(svc.ask_stream("讲讲太阳系", mode="free"))
    # chat_stream 会话 id 是守卫的输入之一：守卫先于一切 DB 访问，假 id 亦报错
    with pytest.raises(ConfigError, match="api / local"):
        list(svc.chat_stream(999, "讲讲太阳系", mode="free"))
    with pytest.raises(ConfigError, match="未知问答模式"):  # 非法 mode 防御
        svc.ask("讲讲太阳系", mode="banana")
    # kb（缺省/显式）不受影响：mock 下仍走检索路径（空库 → 知识库为空）
    with pytest.raises(StorageError, match="知识库为空"):
        svc.ask("讲讲太阳系", mode="kb")


def test_free_mock_guard_raises_without_session_created(offline_settings):
    """守卫先于建会话：mock+free 不落任何审计轨迹（零副作用失败）。"""
    offline_settings.ensure_dirs()
    with pytest.raises(ConfigError, match="api / local"):
        AskService(offline_settings).ask("讲讲太阳系", mode="free")
    assert _count(offline_settings, "qa_sessions") == 0
    assert _count(offline_settings, "qa_messages") == 0


def test_history_summary_tells_cited_from_uncited(offline_settings, tmp_path):
    """摘要话术诚实化：kb 轮"给出引用"；free 轮如实记"未引用知识库"。"""
    _seed(tmp_path, offline_settings)
    settings = _free_ready(offline_settings)
    with open_db(offline_settings.db_path) as conn:
        session_id = repo.create_session(conn, offline_settings.profile)
    # 同会话两轮：kb（引用作答）→ free（无引用）
    kb_svc = AskService(offline_settings)
    kb_svc.ask("L2 正则化是什么？", session_id=session_id, mode="kb")
    free_svc = AskService(settings, llm=_FreeLLM(replies=["这是常识，不需要资料"]))
    free_svc.ask("太阳系有几颗行星？", session_id=session_id, mode="free")

    summary = kb_svc._history_summary(session_id)
    assert "给出引用" in summary  # 第 1 轮 kb：有引用话术不变
    assert "未引用知识库" in summary  # 第 2 轮 free：无引用话术诚实化
    assert summary.index("给出引用") < summary.index("未引用知识库")  # 顺序不乱


def test_invalidate_index_forces_rebuild(tmp_path, offline_settings, monkeypatch):
    """失效后下次提问强制重建快照；语料未变则复用缓存（行数指纹）。"""
    _seed(tmp_path, offline_settings)
    svc = AskService(offline_settings)
    manager = svc._manager
    original_rebuild = manager._rebuild
    rebuild_calls: dict[str, int] = {"n": 0}

    def counting_rebuild():  # 包装计数后交给原实现
        rebuild_calls["n"] += 1
        return original_rebuild()

    monkeypatch.setattr(manager, "_rebuild", counting_rebuild)

    svc.ask("L2 正则化是什么？")  # 首次 → 重建
    assert rebuild_calls["n"] == 1
    svc.ask("L2 正则化是什么？")  # 行数未变 → 复用快照，不重建
    assert rebuild_calls["n"] == 1
    svc.invalidate_index()
    svc.ask("L2 正则化是什么？")  # 失效后强制重建
    assert rebuild_calls["n"] == 2


# ---------------------------------------------------------------------------
# 自动标题（M4.5）：_record 的截断补名钩子（kb / 拒答 / free 三路同轨）
# ---------------------------------------------------------------------------


def test_first_ask_names_session_with_question(tmp_path, offline_settings):
    """首轮问答后自动补截断标题（首问 20 字内原样），不带手动锁。"""
    _seed(tmp_path, offline_settings)
    AskService(offline_settings).ask("L2 正则化为什么能防止过拟合？")
    with open_db(offline_settings.db_path) as conn:
        sessions = repo.list_sessions(conn)
    assert len(sessions) == 1
    assert sessions[0]["title"] == "L2 正则化为什么能防止过拟合？"
    # 自动标题不锁 manual（LLM 提炼仍可升级）——title_manual 属 get_session 契约
    with open_db(offline_settings.db_path) as conn:
        assert repo.get_session(conn, sessions[0]["id"])["title_manual"] == 0


def test_title_not_refreshed_by_later_rounds(tmp_path, offline_settings):
    """多轮会话：标题只在首轮命名，不随新轮次漂移。"""
    _seed(tmp_path, offline_settings)
    svc = AskService(offline_settings)
    svc.ask("L2 正则化是什么？")
    sid = _session_id(offline_settings)
    svc.chat(sid, "缩放因子为什么是根号 dk？")
    with open_db(offline_settings.db_path) as conn:
        assert repo.get_session(conn, sid)["title"] == "L2 正则化是什么？"


def test_refusal_round_also_names_session(tmp_path, offline_settings):
    """拒答轮同样命名（有提问就命名——标题描述话题，与是否答出无关）。"""
    _seed(tmp_path, offline_settings)
    answer = AskService(offline_settings).ask("如何在一周内学会做菠萝包？")
    assert answer.refused
    with open_db(offline_settings.db_path) as conn:
        assert repo.list_sessions(conn)[0]["title"] == "如何在一周内学会做菠萝包？"


def test_free_round_names_session_too(offline_settings):
    """free 模式同样走自动补名（kb/free 落库同轨的又一证据）。"""
    settings = _free_ready(offline_settings)
    settings.ensure_dirs()
    AskService(settings, llm=_FreeLLM()).ask("讲讲太阳系有几颗行星", mode="free")
    with open_db(settings.db_path) as conn:
        assert repo.list_sessions(conn)[0]["title"] == "讲讲太阳系有几颗行星"


def test_manual_clear_then_next_round_revives_title(tmp_path, offline_settings):
    """手动清除标题后（回 NULL 解锁），下一轮自动重新补名（闭环）。"""
    _seed(tmp_path, offline_settings)
    svc = AskService(offline_settings)
    svc.ask("L2 正则化是什么？")
    sid = _session_id(offline_settings)
    with open_db(offline_settings.db_path) as conn:
        repo.set_session_title(conn, sid, None)  # 清除 → 未命名
    svc.chat(sid, "缩放因子为什么是根号 dk？")
    with open_db(offline_settings.db_path) as conn:
        assert repo.get_session(conn, sid)["title"] == "L2 正则化是什么？"


def test_long_first_question_title_truncated(tmp_path, offline_settings):
    """超长首问：标题折叠截断到 20 字（与迁移回填同口径）。"""
    _seed(tmp_path, offline_settings)
    question = "L2 正则化为什么能防止过拟合它与权重衰减有什么区别请展开详细说说"
    AskService(offline_settings).ask(question)
    with open_db(offline_settings.db_path) as conn:
        assert repo.list_sessions(conn)[0]["title"] == fold_title(question)


# ---------------------------------------------------------------------------
# 标题提炼 suggest_title（M4.5）：mock 截断兜底 / api 走 LLM / 失败回退 / 锁定
# ---------------------------------------------------------------------------


class _TitleLLM:
    """suggest_title 专用替身：输出可编程（默认带包装，测清洗），可抛 ProviderError。"""

    model = "fake-title"

    def __init__(self, text: str = "标题：「反向传播的精髓」", *, boom: bool = False) -> None:
        self._text = text
        self._boom = boom
        self.complete_calls: list[list[dict[str, str]]] = []

    def complete(self, messages, *, temperature, max_tokens) -> Completion:
        self.complete_calls.append(messages)
        if self._boom:
            raise ProviderError("模拟鉴权失败")
        return Completion(text=self._text, prompt_tokens=1, completion_tokens=1)


def _qa_session(settings, question: str = "第一问", answer: str = "第一答") -> int:
    """直接造"一问一答"会话（不经 AskService——suggest 不依赖语料/检索）。"""
    with open_db(settings.db_path) as conn:
        sid = repo.create_session(conn, "offline")
        repo.insert_qa_message(conn, session_id=sid, role="user", content=question)
        repo.insert_qa_message(conn, session_id=sid, role="assistant", content=answer)
    return sid


def test_suggest_mock_backend_truncates_without_llm(offline_settings):
    """mock（offline）：不调 LLM、直接截断兜底并落库（截断是合法回退）。"""
    question = "L2 正则化为什么能防止过拟合它与权重衰减的区别"
    sid = _qa_session(offline_settings, question=question)
    svc = AskService(offline_settings)  # llm = MockLLM（backend=mock）
    title, applied = svc.suggest_title(sid)
    assert title == fold_title(question)
    assert applied is True
    with open_db(offline_settings.db_path) as conn:
        assert repo.get_session(conn, sid)["title"] == title


def test_suggest_api_cleans_wrapped_output_and_applies(offline_settings):
    """api 后端：LLM 输出清洗（去"标题："前缀与包裹引号）后原子落库。"""
    settings = _free_ready(offline_settings)  # backend=api（数据目录隔离）
    question = "反向传播的完整推导过程是什么？"
    sid = _qa_session(settings, question=question, answer="反向传播利用链式法则……")
    fake = _TitleLLM(text="标题：\n「反向传播的精髓」")
    title, applied = AskService(settings, llm=fake).suggest_title(sid)

    assert title == "反向传播的精髓"
    assert applied is True
    assert len(fake.complete_calls) == 1
    msgs = fake.complete_calls[0]
    assert msgs[0]["role"] == "system" and "标题" in msgs[0]["content"]
    assert question in msgs[1]["content"]  # 首问原文入料
    assert "反向传播利用链式法则" in msgs[1]["content"]  # 首答截断入料
    with open_db(settings.db_path) as conn:
        assert repo.get_session(conn, sid)["title"] == "反向传播的精髓"


def test_suggest_upgrades_fallback_title(offline_settings):
    """已有截断兜底标题（title_manual=0）时，LLM 提炼升级覆盖。"""
    settings = _free_ready(offline_settings)
    sid = _qa_session(settings)
    with open_db(settings.db_path) as conn:
        repo.auto_title_if_untitled(conn, sid, "截断兜底名")
    fake = _TitleLLM(text="更精炼的名字")
    title, applied = AskService(settings, llm=fake).suggest_title(sid)
    assert (title, applied) == ("更精炼的名字", True)
    with open_db(settings.db_path) as conn:
        assert repo.get_session(conn, sid)["title"] == "更精炼的名字"


def test_suggest_respects_manual_lock(offline_settings):
    """手动命名锁定：建议照常返回，但绝不落库覆盖。"""
    settings = _free_ready(offline_settings)
    sid = _qa_session(settings)
    with open_db(settings.db_path) as conn:
        repo.set_session_title(conn, sid, "手动标题")
    fake = _TitleLLM(text="AI 想改的名字")
    title, applied = AskService(settings, llm=fake).suggest_title(sid)
    assert title == "AI 想改的名字"  # 预览仍可用
    assert applied is False  # 但不落库
    with open_db(settings.db_path) as conn:
        assert repo.get_session(conn, sid)["title"] == "手动标题"


def test_suggest_provider_error_falls_back_to_truncation(offline_settings):
    """LLM 调用失败：截断兜底并正常落库（提炼是锦上添花，不拖挂流程）。"""
    settings = _free_ready(offline_settings)
    question = "注意力机制为什么要除以根号 dk？"
    sid = _qa_session(settings, question=question)
    fake = _TitleLLM(boom=True)
    title, applied = AskService(settings, llm=fake).suggest_title(sid)
    assert (title, applied) == (fold_title(question), True)
    with open_db(settings.db_path) as conn:
        assert repo.get_session(conn, sid)["title"] == fold_title(question)


def test_suggest_empty_llm_output_falls_back(offline_settings):
    settings = _free_ready(offline_settings)
    question = "什么是缩放点积注意力？"
    sid = _qa_session(settings, question=question)
    title, applied = AskService(settings, llm=_TitleLLM(text="  \n ")).suggest_title(sid)
    assert (title, applied) == (fold_title(question), True)


def test_suggest_apply_false_previews_without_writing(offline_settings):
    """apply=False：只算不改（预览语义），库内标题保持原样。"""
    settings = _free_ready(offline_settings)
    sid = _qa_session(settings)
    with open_db(settings.db_path) as conn:
        repo.auto_title_if_untitled(conn, sid, "截断兜底名")
    fake = _TitleLLM(text="预览名")
    title, applied = AskService(settings, llm=fake).suggest_title(sid, apply=False)
    assert (title, applied) == ("预览名", False)
    with open_db(settings.db_path) as conn:
        assert repo.get_session(conn, sid)["title"] == "截断兜底名"


def test_suggest_without_question_raises(offline_settings):
    """空会话（无任何提问）：报错而非产出无意义标题。"""
    with open_db(offline_settings.db_path) as conn:
        sid = repo.create_session(conn, "offline")
    with pytest.raises(StorageError, match="还没有提问"):
        AskService(offline_settings).suggest_title(sid)


def test_suggest_missing_session_raises(offline_settings):
    with pytest.raises(StorageError, match="会话不存在"):
        AskService(offline_settings).suggest_title(9999)


def test_clean_title_strips_prefix_quotes_and_limits():
    """LLM 输出清洗：去前缀/包裹引号/编号噪声，空白折叠，≤16 字。"""
    clean = AskService._clean_title
    assert clean("标题：「反向传播的精髓」") == "反向传播的精髓"
    assert clean("“ 反向传播的精髓 ”") == "反向传播的精髓"
    assert clean("1. 反向传播的精髓") == "反向传播的精髓"
    assert clean("反向传播的精髓与梯度消失的本质区别是什么深入探讨一下") == fold_title(
        "反向传播的精髓与梯度消失的本质区别是什么深入探讨一下", 16
    )
    assert clean("   ") == ""  # 空输出 → 调用方回退截断
    assert clean("（不加包裹的标题）") == "不加包裹的标题"


# ---------------------------------------------------------------------------
# 跨语言检索：查询翻译与双路接线（2026-09-10，retrieval.crosslingual）
# ---------------------------------------------------------------------------


class _ScriptedLLM:
    """脚本替身 LLM：按队列回文，记录 complete 的 messages 与 kwargs。

    QA/翻译两侧共用（AskService 单实例注入）：翻译调用先弹出译文，
    问答调用再弹出答案文本。
    """

    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)
        self.calls: list[list[dict[str, str]]] = []
        self.kwargs: list[dict] = []

    def complete(self, messages, *, temperature, max_tokens) -> Completion:
        self.calls.append(messages)
        self.kwargs.append({"temperature": temperature, "max_tokens": max_tokens})
        text = self.texts.pop(0) if self.texts else "（脚本耗尽）"
        return Completion(text=text, prompt_tokens=1, completion_tokens=1)


def test_translate_query_returns_english_for_cjk():
    """含汉字问题 → LLM 翻译调用（翻译专用 system，参数写死低温短输出）。"""
    from mikasa.pipeline.prompts import TRANSLATE_MAX_TOKENS, TRANSLATE_SYSTEM_PROMPT

    llm = _ScriptedLLM(["How does a screw anchor perform under cyclic loading?"])
    question = "循环荷载作用下螺旋锚的承载力怎么变化？"
    out = _translate_query(llm, question)
    assert out == "How does a screw anchor perform under cyclic loading?"
    messages, kwargs = llm.calls[0], llm.kwargs[0]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == TRANSLATE_SYSTEM_PROMPT
    assert messages[1]["content"] == question  # 翻译输入 = 问题原文
    assert kwargs == {"temperature": 0.1, "max_tokens": TRANSLATE_MAX_TOKENS}


def test_translate_query_skips_questions_without_cjk():
    """纯英文/数字问题：无翻译调用（词面可自命中，零 LLM 浪费）。"""
    llm = _ScriptedLLM(["translated"])
    assert _translate_query(llm, "What is the helix bearing resistance?") is None
    assert _translate_query(llm, "h = 2r / tan(30) for sand") is None  # 纯 ASCII 也免翻
    assert llm.calls == []  # 两次都不该发生 LLM 调用


def test_translate_query_falls_back_on_empty_output():
    llm = _ScriptedLLM(["   "])
    assert _translate_query(llm, "螺旋锚在循环荷载下的工作性能如何？") is None


def test_translate_query_falls_back_when_output_still_has_cjk():
    """防御：小模型没翻（复述原文）→ 判不可用回退，译文不得污染 BM25。"""
    llm = _ScriptedLLM(["请把这个问题翻译成英文"])
    assert _translate_query(llm, "螺旋锚在循环荷载下的工作性能如何？") is None


def test_translate_query_falls_back_on_provider_error():
    """翻译 LLM 调用异常：回退单路检索，异常不穿透问答主链。"""

    class _Broken(_ScriptedLLM):
        def complete(self, messages, *, temperature, max_tokens):
            raise ProviderError("mock 翻译服务不可用")

    assert _translate_query(_Broken([]), "螺旋锚在循环荷载下的工作性能如何？") is None


# 英文资料正文（与 test_retriever 的 EN_NOTE 同语料：量子退火）
_QA_EN_NOTE = (
    "# Quantum Notes\n\n## Annealing\n\n"
    "Quantum annealing cools a system into its lowest-energy ground state.\n"
)


def test_ask_crosslingual_route_wires_translation_into_retrieval(tmp_path, offline_settings):
    """接线验证（替身 LLM 不懂语义，验证的是路由而非回答质量）：
    crosslingual=true + 含汉字问题 → calls[0] 是翻译调用（system=翻译助手）、
    calls[1] 问答注入片段含英文块原文（第二路真的召回英文）→
    latency_ms 出现 translate 分段；默认关的对照测试见 test_settings。
    """
    md = tmp_path / "量子.md"
    md.write_text(NOTE, encoding="utf-8")
    (tmp_path / "quantum.md").write_text(_QA_EN_NOTE, encoding="utf-8")
    IngestService(offline_settings).ingest_paths([tmp_path])

    settings = offline_settings.model_copy(
        update={"retrieval": offline_settings.retrieval.model_copy(update={"crosslingual": True})}
    )
    question = "量子退火如何让系统落入基态？"
    llm = _ScriptedLLM(
        [
            "How does quantum annealing cool a system into its ground state?",  # 翻译
            "量子退火通过缓慢降温让系统落入基态 [1]。",  # 问答（替身文）
        ]
    )
    answer = AskService(settings, llm=llm).ask(question)

    assert len(llm.calls) == 2, "翻译 + 问答各一次"
    from mikasa.pipeline.prompts import TRANSLATE_SYSTEM_PROMPT

    assert llm.calls[0][0]["content"] == TRANSLATE_SYSTEM_PROMPT  # 翻译助手人格
    assert llm.calls[0][1]["content"] == question  # 待译问题原文
    qa_user = llm.calls[1][-1]["content"]  # QA 用户消息 = 资料片段 + 问题
    assert "ground state" in qa_user, "英文块经第二路进入注入片段"
    assert not answer.refused
    assert answer.citations, "替身答案带 [1]，引用解析通过"
    assert answer.text == "量子退火通过缓慢降温让系统落入基态 [1]。"
    assert set(answer.latency_ms) == {"retrieve", "rerank", "translate", "generate"}


def test_ask_crosslingual_off_by_default_keeps_three_latency_segments(tmp_path, offline_settings):
    """crosslingual 默认关（offline）：翻译零调用，延迟分段不变（回归护栏）。"""
    _seed(tmp_path, offline_settings)
    llm = _ScriptedLLM(["量子退火……"])  # 即使注入脚本 LLM，也不该被调翻译
    answer = AskService(offline_settings, llm=llm).ask("L2 正则化为什么能防止过拟合？")
    assert len(llm.calls) == 1, "只问答一次，无翻译调用"
    assert set(answer.latency_ms) == {"retrieve", "rerank", "generate"}


# ---------------------------------------------------------------------------
# 双语对照块（#8，2026-09-10）：英文块命中且被引用 → 回答附原文+翻译
# ---------------------------------------------------------------------------

# 纯英文语料（767 字符 → chunk size 400 下恰分 2 块，两块都远 ≥20 字母）：
# 命中块必为英文主导，双语判定不依赖 RRF 排名细节（中文问题经第二路命中）。
# 注意别把语料切出碎片块（如 <20 字母的残句）——会被 is_english_dominant 判非英文。
_BILINGUAL_EN_NOTE = (
    "# Helical Anchor Notes\n\n## Behaviour\n\n"
    "Under cyclic loading the capacity of helical anchors in sand degrades gradually with the "
    "number of load cycles. "
    "The degradation rate depends on the relative density of the sand and on the amplitude "
    "of the applied load. "
    "Dense sand shows less capacity loss than loose sand under identical conditions, while the "
    "post-cyclic friction angle governs the residual pullout resistance of the anchor.\n\n"
    "## Uplift\n\n"
    "The uplift behaviour of helical anchors is influenced by the helix spacing ratio and by the "
    "installation torque. "
    "Field tests show that the uplift capacity increases with the embedment depth ratio until a "
    "critical depth is reached, beyond which the failure mode changes from a shallow cone to a "
    "deep cylindrical shear surface.\n"
)

_BILINGUAL_QUESTION = "循环荷载下螺旋锚的承载力如何变化？"
_BILINGUAL_QUERY_EN = "How does helical anchor capacity change under cyclic loading?"
_BILINGUAL_QA = "循环荷载下螺旋锚的承载力逐渐下降 [1]。"
_BILINGUAL_QA_TWO = "循环荷载下螺旋锚的承载力逐渐下降 [1]，上拔行为受螺旋间距影响 [2]。"
_BILINGUAL_TRANSLATION = "【译文1】\n循环荷载下砂土中螺旋锚的承载力随加载循环次数退化。"


def _seed_bilingual(tmp_path: Path, offline_settings) -> None:
    (tmp_path / "helical.md").write_text(_BILINGUAL_EN_NOTE, encoding="utf-8")
    IngestService(offline_settings).ingest_paths([tmp_path])


def _bilingual_ready(offline_settings):
    """offline 配置的双语变体：LLM 改 api + crosslingual + bilingual 全开。

    三个开关口径与 profile yaml 一致（api/local 档全开）；数据目录与
    offline 隔离一致；AskService 侧注入 _ScriptedLLM 则零网络。
    """
    return offline_settings.model_copy(
        update={
            "llm": offline_settings.llm.model_copy(update={"backend": "api"}),
            "retrieval": offline_settings.retrieval.model_copy(update={"crosslingual": True}),
            "answer": offline_settings.answer.model_copy(update={"bilingual": True}),
        }
    )


def test_bilingual_block_appended_for_english_citation(tmp_path, offline_settings):
    """英文块被引用 → 答案末尾追加"原文+译文对照"块（参数/格式/延迟全锁）。"""
    from mikasa.pipeline.prompts import (
        TRANSLATE_TO_ZH_MAX_TOKENS,
        TRANSLATE_TO_ZH_SYSTEM_PROMPT,
    )

    _seed_bilingual(tmp_path, offline_settings)
    llm = _ScriptedLLM([_BILINGUAL_QUERY_EN, _BILINGUAL_QA, _BILINGUAL_TRANSLATION])
    answer = AskService(_bilingual_ready(offline_settings), llm=llm).ask(_BILINGUAL_QUESTION)

    assert len(llm.calls) == 3, "翻译 + 问答 + 块翻译各一次"
    messages, kwargs = llm.calls[2], llm.kwargs[2]
    assert messages[0]["content"] == TRANSLATE_TO_ZH_SYSTEM_PROMPT  # 翻译助手人格
    assert "【原文1】" in messages[1]["content"]
    assert "helical anchors" in messages[1]["content"]  # 待译原文（chunk 边界不定，断言稳定词）
    assert kwargs == {"temperature": 0.1, "max_tokens": TRANSLATE_TO_ZH_MAX_TOKENS}

    assert answer.text.startswith(_BILINGUAL_QA)  # 正文在前，块在后
    assert "> **原文与译文对照**" in answer.text
    assert "> [1] **原文**" in answer.text
    assert "> [1] **中文翻译**" in answer.text
    assert "循环荷载下砂土中螺旋锚的承载力随加载循环次数退化。" in answer.text
    # 块内标题与引用卡的 document_title 一致（[n] 用真实 marker，永不越界）
    assert f"（《{answer.citations[0].document_title}》）" in answer.text
    assert set(answer.latency_ms) == {
        "retrieve",
        "rerank",
        "translate",
        "translate_answer",
        "generate",
    }


def test_bilingual_block_neutralizes_paper_citation_numbers(tmp_path, offline_settings):
    """原文/译文里的 [12]（论文文献编号）必须转全角（2026-09-11 修复）。

    前端 renderAnswer 把全文的 [n] 当引用角标，命中不到 citations 的编号会
    渲染成红色"越界/自造"标记——用户以为服务端编造引用。双语块的正文是论文
    原样文本，学术编号极常见，故展示前统一转全角。
    """
    from mikasa.pipeline.ask import _neutralize_cite_markers

    # 纯函数：只动"方括号 + 1~3 位数字"，公式/普通方括号一律不碰
    assert _neutralize_cite_markers("见 [12] 与 [3]。") == "见 ［12］ 与 ［3］。"
    assert _neutralize_cite_markers("公式 f(x) 与 [abcd] 不变") == "公式 f(x) 与 [abcd] 不变"

    # 集成：译文带 [12] → 落进 answer.text 时已是全角
    _seed_bilingual(tmp_path, offline_settings)
    translated = "【译文1】\n循环荷载下砂土中螺旋锚承载力退化，见文献 [12]。"
    llm = _ScriptedLLM([_BILINGUAL_QUERY_EN, _BILINGUAL_QA, translated])
    answer = AskService(_bilingual_ready(offline_settings), llm=llm).ask(_BILINGUAL_QUESTION)

    assert "［12］" in answer.text
    assert "[12]" not in answer.text  # 半角形态绝不能漏给前端
    assert "[1]" in answer.text  # 真正的引用角标（marker）保持可点击


def test_bilingual_off_by_default_no_extra_call(tmp_path, offline_settings):
    """bilingual 默认关：即使命中英文块也只翻译查询、不翻译块（零额外调用）。"""
    _seed_bilingual(tmp_path, offline_settings)
    settings = offline_settings.model_copy(
        update={
            "llm": offline_settings.llm.model_copy(update={"backend": "api"}),
            "retrieval": offline_settings.retrieval.model_copy(update={"crosslingual": True}),
        }
    )
    llm = _ScriptedLLM([_BILINGUAL_QUERY_EN, _BILINGUAL_QA, "（不应被使用）"])
    answer = AskService(settings, llm=llm).ask(_BILINGUAL_QUESTION)
    assert len(llm.calls) == 2, "查询翻译 + 问答各一次，无块翻译"
    assert answer.text == _BILINGUAL_QA
    assert set(answer.latency_ms) == {"retrieve", "rerank", "translate", "generate"}


def test_bilingual_skipped_on_mock_backend(tmp_path, offline_settings):
    """mock 后端即使开 bilingual 也跳过：offline/mock 零 LLM 调用的硬保证。"""
    _seed_bilingual(tmp_path, offline_settings)
    settings = offline_settings.model_copy(
        update={
            "retrieval": offline_settings.retrieval.model_copy(update={"crosslingual": True}),
            "answer": offline_settings.answer.model_copy(update={"bilingual": True}),
        }
    )
    llm = _ScriptedLLM([_BILINGUAL_QUERY_EN, _BILINGUAL_QA, "（不应被使用）"])
    answer = AskService(settings, llm=llm).ask(_BILINGUAL_QUESTION)
    assert len(llm.calls) == 2, "mock 后端不触发块翻译"
    assert answer.text == _BILINGUAL_QA


def test_bilingual_skipped_on_refusal(tmp_path, offline_settings):
    """拒答轮不追加双语块：评测裁判对 REFUSAL_TEXT 精确匹配（judge.py:171）。"""
    from mikasa.pipeline.prompts import REFUSAL_TEXT

    _seed_bilingual(tmp_path, offline_settings)
    llm = _ScriptedLLM([_BILINGUAL_QUERY_EN, REFUSAL_TEXT])
    answer = AskService(_bilingual_ready(offline_settings), llm=llm).ask(_BILINGUAL_QUESTION)
    assert answer.refused
    assert len(llm.calls) == 2, "拒答轮零块翻译调用"
    assert answer.text == REFUSAL_TEXT
    assert "translate_answer" not in answer.latency_ms


def test_bilingual_parser_tolerates_spacing(tmp_path, offline_settings):
    """小模型输出【译文 1】（带空格）：正则容错，正常入块。"""
    _seed_bilingual(tmp_path, offline_settings)
    llm = _ScriptedLLM([_BILINGUAL_QUERY_EN, _BILINGUAL_QA, "【译文 1】\n循环荷载下承载力退化。"])
    answer = AskService(_bilingual_ready(offline_settings), llm=llm).ask(_BILINGUAL_QUESTION)
    assert "> [1] **中文翻译**" in answer.text
    assert "循环荷载下承载力退化。" in answer.text


def test_bilingual_partial_parse_keeps_available_sections(tmp_path, offline_settings):
    """两段入料只回【译文2】：块里只有 [2] 段，[1] 段静默缺位（能取几段用几段）。"""
    _seed_bilingual(tmp_path, offline_settings)
    llm = _ScriptedLLM(
        [_BILINGUAL_QUERY_EN, _BILINGUAL_QA_TWO, "【译文2】\n上拔承载力随埋深比增大而提高。"]
    )
    answer = AskService(_bilingual_ready(offline_settings), llm=llm).ask(_BILINGUAL_QUESTION)
    assert "> **原文与译文对照**" in answer.text
    assert "> [2] **原文**" in answer.text
    assert "> [2] **中文翻译**" in answer.text
    assert "> [1] **原文**" not in answer.text  # 无译文段 → 整段缺位


def test_bilingual_unparseable_output_skips_block(tmp_path, offline_settings):
    """块翻译输出无【译文N】标记：整体跳过，答案与无双语时完全一致。"""
    _seed_bilingual(tmp_path, offline_settings)
    llm = _ScriptedLLM([_BILINGUAL_QUERY_EN, _BILINGUAL_QA, "好的，以下是翻译内容。"])
    answer = AskService(_bilingual_ready(offline_settings), llm=llm).ask(_BILINGUAL_QUESTION)
    assert answer.text == _BILINGUAL_QA
    assert "translate_answer" not in answer.latency_ms


def test_bilingual_provider_error_skips_block(tmp_path, offline_settings):
    """块翻译 LLM 调用异常：跳过对照块，异常不穿透问答主链。"""

    class _BlockBroken(_ScriptedLLM):
        def complete(self, messages, *, temperature, max_tokens):
            from mikasa.pipeline.prompts import TRANSLATE_TO_ZH_SYSTEM_PROMPT

            if messages[0]["content"] == TRANSLATE_TO_ZH_SYSTEM_PROMPT:
                raise ProviderError("mock 翻译服务不可用")
            return super().complete(messages, temperature=temperature, max_tokens=max_tokens)

    _seed_bilingual(tmp_path, offline_settings)
    llm = _BlockBroken([_BILINGUAL_QUERY_EN, _BILINGUAL_QA])
    answer = AskService(_bilingual_ready(offline_settings), llm=llm).ask(_BILINGUAL_QUESTION)
    assert answer.text == _BILINGUAL_QA
    assert "translate_answer" not in answer.latency_ms


def test_ask_stream_bilingual_delta_concat_equals_done(tmp_path, offline_settings):
    """流式：双语块作为额外 delta 在 done 前流出，delta 拼接 === done.text。"""

    class _StreamingScripted(_ScriptedLLM):
        def stream(self, messages, *, temperature, max_tokens):
            text = self.texts.pop(0) if self.texts else "（脚本耗尽）"
            for i in range(0, len(text), 12):
                yield text[i : i + 12]

    _seed_bilingual(tmp_path, offline_settings)
    llm = _StreamingScripted([_BILINGUAL_QUERY_EN, _BILINGUAL_QA, _BILINGUAL_TRANSLATION])
    events = list(
        AskService(_bilingual_ready(offline_settings), llm=llm).ask_stream(_BILINGUAL_QUESTION)
    )
    kinds = [e.kind for e in events]
    assert kinds[0] == "meta" and kinds[-1] == "done"

    done = events[-1].answer
    deltas = [e for e in events if e.kind == "delta"]
    assert deltas[-1].text.startswith("\n\n> **原文与译文对照**")  # 双语块是末帧 delta
    assert "".join(e.text for e in deltas) == done.text  # 拼接恒等
    assert "translate_answer" in done.latency_ms

    # 落库 content 已是含双语块的终态（回放渲染同一份文本）
    with open_db(offline_settings.db_path) as conn:
        messages = repo.messages_by_session(conn, events[0].session_id)
    assert messages[1]["content"] == done.text


def test_rerank_indices_are_sanitized():
    """重排服务返回非法下标时不能让它穿成 500，也不能静默截断候选。

    越界 → IndexError 直通 Web 的"内部错误"；重复 → 候选被悄悄截短。
    两者都来自外部服务，响应不完全可控（2026-09-11 审查指出）。
    """
    from mikasa.pipeline.retriever import _sanitize_indices

    # 越界丢掉、顺序保持
    assert _sanitize_indices([2, 0, 99, -1], 3) == [2, 0]
    # 重复去掉（保留首次出现的位置）
    assert _sanitize_indices([1, 1, 0, 1], 2) == [1, 0]
    # 正常输入原样返回
    assert _sanitize_indices([0, 1, 2], 3) == [0, 1, 2]
    # 全非法 → 空（宁可没有候选，也不要 500）
    assert _sanitize_indices([], 3) == []
