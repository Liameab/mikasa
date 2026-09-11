"""问答（非流式）与会话端点测试：提问两态、会话落库、消息解码。

MockLLM 确定性：与语料有 ≥4 字连续重叠 → 引用作答；无关问题 → 拒答。
"""

from __future__ import annotations


def test_ask_answerable_returns_citation(seeded_client):
    c, _ = seeded_client
    resp = c.post("/api/ask", json={"question": "L2 正则化为什么能防止过拟合？"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] is not None  # 新会话 id 在响应里带回
    answer = body["answer"]
    assert not answer["refused"]
    assert answer["citations"] and answer["citations"][0]["marker"] == 1
    assert set(answer["latency_ms"]) == {"retrieve", "rerank", "generate"}


def test_ask_out_of_corpus_refuses(seeded_client):
    c, _ = seeded_client
    resp = c.post("/api/ask", json={"question": "如何在一周内学会做菠萝包？"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"]["refused"] is True
    assert body["answer"]["citations"] == []
    assert "根据已有资料" in body["answer"]["text"]


def test_ask_empty_corpus_is_business_error(client):
    """空库提问：统一错误壳 400（ZhiwenError → 400），不是 500。"""
    c, _ = client
    resp = c.post("/api/ask", json={"question": "什么是线性代数？"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["type"] == "StorageError"
    assert "知识库为空" in body["error"]["message"]


def test_ask_session_roundtrip_with_history(seeded_client):
    """两轮续问：同一会话，历史摘要生效，消息全量可查。"""
    c, settings = seeded_client
    first = c.post("/api/ask", json={"question": "L2 正则化是什么？"}).json()
    session_id = first["session_id"]

    second = c.post(
        "/api/ask", json={"question": "那早停为什么也属于正则化？", "session_id": session_id}
    )
    assert second.status_code == 200
    assert second.json()["session_id"] == session_id

    history = c.get(f"/api/sessions/{session_id}/messages")
    assert history.status_code == 200
    messages = history.json()["messages"]
    assert len(messages) == 4  # 两轮 × (user + assistant)
    assert messages[0]["role"] == "user"
    assert messages[1]["citations"]  # json 列已解码成列表
    assert messages[1]["refused"] is False  # int 0 → bool
    assert "retrieve" in messages[1]["latency_ms"]  # json 列解码成 dict
    assert messages[2]["content"] == "那早停为什么也属于正则化？"


def test_ask_unknown_session_404(seeded_client):
    c, _ = seeded_client
    resp = c.post("/api/ask", json={"question": "L2 正则化是什么？", "session_id": 9999})
    assert resp.status_code == 404
    assert "会话不存在" in resp.json()["detail"]


def test_sessions_list_shows_latest_first(seeded_client):
    c, _ = seeded_client
    c.post("/api/ask", json={"question": "L2 正则化是什么？"})
    c.post("/api/ask", json={"question": "缩放因子为什么是根号 dk？"})
    sessions = c.get("/api/sessions").json()["sessions"]
    assert len(sessions) == 2
    assert sessions[0]["id"] > sessions[1]["id"]  # 新会话在前
    assert sessions[0]["message_count"] == 2
    assert sessions[0]["profile"] == "offline"


def test_blank_question_rejected(seeded_client):
    """纯空白问题：业务 400（AskService.strip 后为空）。"""
    c, _ = seeded_client
    resp = c.post("/api/ask", json={"question": "   "})
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "StorageError"


def test_ask_free_on_mock_backend_is_config_error(seeded_client):
    """offline（mock 无语义）→ mode=free：统一错误壳 400，不是 500。"""
    c, _ = seeded_client
    resp = c.post("/api/ask", json={"question": "讲讲太阳系", "mode": "free"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["type"] == "ConfigError"
    assert "api / local" in body["error"]["message"]


def test_ask_invalid_mode_is_422(seeded_client):
    """非法 mode 字面量：pydantic 声明层直接 422（先于服务层防御触发）。"""
    c, _ = seeded_client
    resp = c.post("/api/ask", json={"question": "L2 正则化是什么？", "mode": "banana"})
    assert resp.status_code == 422


def test_ask_route_passes_mode_through(seeded_client, monkeypatch):
    """路由透传：body.mode 原样到达 AskService.ask（缺省 kb + 显式 free）。"""
    c, _ = seeded_client
    svc = c.app.state.services.ask
    original = svc.ask
    calls: list[str] = []

    def spy(question, *, session_id=None, mode="kb"):  # type: ignore[no-untyped-def]
        calls.append(mode)
        return original(question, session_id=session_id, mode=mode)

    monkeypatch.setattr(svc, "ask", spy)
    kb_resp = c.post("/api/ask", json={"question": "L2 正则化是什么？"})
    assert kb_resp.status_code == 200
    free_resp = c.post("/api/ask", json={"question": "讲讲太阳系", "mode": "free"})
    assert free_resp.status_code == 400  # 透传无误后由服务层守卫兜底
    assert calls == ["kb", "free"]


def test_internal_error_returns_error_shell(seeded_client):
    """兜底 500：错误壳 JSON，不回显内部细节。

    注意：TestClient 默认 raise_server_exceptions=True 会 re-raise 服务器
    500 的原异常（有利于业务测试暴露 bug），测"错误壳本身"须显式关闭。
    """
    from fastapi.testclient import TestClient

    c, _ = seeded_client
    quiet = TestClient(c.app, raise_server_exceptions=False)

    def boom():  # type: ignore[no-untyped-def]
        raise RuntimeError("内部细节不许回显")

    original = c.app.state.services.ask.ask
    c.app.state.services.ask.ask = boom  # type: ignore[method-assign]
    try:
        resp = quiet.post("/api/ask", json={"question": "随便问"})
    finally:
        c.app.state.services.ask.ask = original
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["type"] == "internal"
    assert "内部细节" not in body["error"]["message"]
