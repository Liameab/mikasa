"""SSE 流式问答端点测试：meta → delta×n → done 协议对偶 + 异常帧。

解析器 _parse_sse 与前端 js 的帧切分同构（空行分帧、data 行取 JSON）。
MockLLM 的流式 = 完整文本按 24 字符切片，因此 delta 通常多条。
"""

from __future__ import annotations

import json


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """SSE 文本 → [(event, data_json)]：与前端同构的帧切分。"""
    frames: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event = ""
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data_lines.append(line.removeprefix("data: "))
        frames.append((event, json.loads("\n".join(data_lines))))
    return frames


def test_stream_meta_delta_done_concat_equals_answer(seeded_client):
    c, _ = seeded_client
    resp = c.post(
        "/api/ask/stream",
        json={"question": "L2 正则化为什么能防止过拟合？"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = _parse_sse(resp.text)

    kinds = [kind for kind, _ in frames]
    assert kinds[0] == "meta"
    assert kinds[-1] == "done"
    assert set(kinds[1:-1]) == {"delta"}  # 中间全是正文增量

    meta_data = frames[0][1]
    assert meta_data["session_id"] is not None  # 前端续问依赖它

    done_data = frames[-1][1]
    answer = done_data["answer"]
    assert done_data["session_id"] == meta_data["session_id"]
    assert not answer["refused"]
    assert answer["citations"] and answer["citations"][0]["marker"] == 1
    concat = "".join(data["text"] for kind, data in frames if kind == "delta")
    assert concat == answer["text"]  # 协议对偶：拼接恒等
    assert answer["prompt_tokens"] is None  # 流式无 usage


def test_disconnect_after_meta_leaves_no_empty_session(seeded_client):
    """客户端在**已经看到 meta 帧之后**断开：会话树里不能留下空对话（2026-09-24 修）。

    原先的守卫是 `if new_session and not produced`，而 meta 帧在**检索成功后
    就发**：用户"看到回答在往外冒"时关页/刷新，produced 已为真、落库却没发生
    （落库在 done 帧之前才执行），于是每中断一次就在会话树里多一条空对话——
    正是 2026-09-11 那条修复想消灭的东西，只是漏了中断这条路径。
    `_drop_session_if_empty` 自己会查消息表，有落库消息时是空操作，所以
    这里应当**无条件调用**（非流式路径一直如此）。
    """
    from mikasa.web.routers.qa import _new_session, _to_frames

    c, settings = seeded_client
    before = len(c.get("/api/sessions").json()["sessions"])

    session_id = _new_session(settings)
    frames = _to_frames(
        c.app.state.services, settings, "L2 正则化是什么？", session_id, new_session=True
    )
    first = next(frames)
    assert first.startswith("event: meta"), first
    frames.close()  # 客户端断开：生成器被 close() → GeneratorExit 打在 yield 处

    after = c.get("/api/sessions").json()["sessions"]
    assert len(after) == before, f"中断后留下了空会话：{[s for s in after][:3]}"


def test_disconnect_after_a_full_answer_keeps_the_session(seeded_client):
    """反面对照：跑完一整轮的会话必须留下（清理不能误伤"已经有消息"的会话）。

    同一个 `finally` 分支既服务中断也服务正常结束，所以判据只能是
    "这条会话到底有没有落库消息"——这正是 `_drop_session_if_empty` 内部
    自己查消息表的原因。
    """
    from mikasa.web.routers.qa import _new_session, _to_frames

    c, settings = seeded_client
    session_id = _new_session(settings)
    frames = list(
        _to_frames(
            c.app.state.services, settings, "L2 正则化是什么？", session_id, new_session=True
        )
    )
    assert frames[-1].startswith("event: done")

    sessions = c.get("/api/sessions").json()["sessions"]
    assert any(s["id"] == session_id for s in sessions), "正常跑完的会话不该被清理掉"


def test_stream_refusal_events(seeded_client):
    c, _ = seeded_client
    resp = c.post("/api/ask/stream", json={"question": "如何在一周内学会做菠萝包？"})
    frames = _parse_sse(resp.text)
    assert frames[-1][0] == "done"
    answer = frames[-1][1]["answer"]
    assert answer["refused"] is True
    assert "".join(d["text"] for k, d in frames if k == "delta") == answer["text"]


def test_stream_unknown_session_404(seeded_client):
    c, _ = seeded_client
    resp = c.post("/api/sessions/9999/chat/stream", json={"question": "问什么都可以"})
    assert resp.status_code == 404
    assert "会话不存在" in resp.json()["detail"]


def test_chat_stream_continues_session(seeded_client):
    """续问复用会话与历史：messages 端点能看到两轮完整轨迹。"""
    c, _ = seeded_client
    first = c.post("/api/ask/stream", json={"question": "L2 正则化是什么？"})
    session_id = _parse_sse(first.text)[0][1]["session_id"]

    second = c.post(
        f"/api/sessions/{session_id}/chat/stream",
        json={"question": "缩放因子为什么是根号 dk？"},
    )
    frames = _parse_sse(second.text)
    assert frames[0][0] == "meta" and frames[0][1]["session_id"] == session_id
    assert frames[-1][0] == "done" and not frames[-1][1]["answer"]["refused"]

    history = c.get(f"/api/sessions/{session_id}/messages").json()
    assert len(history["messages"]) == 4
    # 第二轮 user 消息已落库（流结束后 record），顺序与提问一致
    assert history["messages"][2]["content"] == "缩放因子为什么是根号 dk？"


def test_stream_empty_corpus_yields_error_frame(client):
    """空库提问：SSE 以 error 帧收尾（业务错误消息可回显），HTTP 仍 200。"""
    c, _ = client
    resp = c.post("/api/ask/stream", json={"question": "什么是线性代数？"})
    assert resp.status_code == 200
    frames = _parse_sse(resp.text)
    assert frames[-1][0] == "error"
    assert "知识库为空" in frames[-1][1]["message"]


def test_stream_blank_question_error_frame(seeded_client):
    c, _ = seeded_client
    resp = c.post("/api/ask/stream", json={"question": "   "})
    frames = _parse_sse(resp.text)
    assert frames[-1][0] == "error"
    assert "问题为空" in frames[-1][1]["message"]


def test_stream_free_on_mock_yields_single_error_frame(seeded_client):
    """offline free：恰一帧 error、零 meta/done——守卫先于生成器任何产出。"""
    c, _ = seeded_client
    resp = c.post("/api/ask/stream", json={"question": "讲讲太阳系", "mode": "free"})
    assert resp.status_code == 200  # SSE 语义：业务错误进帧不进状态码
    frames = _parse_sse(resp.text)
    assert [(kind, None) for kind, _ in frames] == [("error", None)]
    assert "api / local" in frames[0][1]["message"]


def test_chat_stream_free_offline_404_before_guard(seeded_client):
    """chat/stream：会话不存在 → 404 先于 mode 守卫；存在 → 单 error 帧。"""
    c, _ = seeded_client
    missing = c.post(
        "/api/sessions/9999/chat/stream",
        json={"question": "随便问问", "mode": "free"},
    )
    assert missing.status_code == 404  # 404 优先级高于模式守卫（会话校验在前）

    created = c.post("/api/ask/stream", json={"question": "L2 正则化是什么？"})
    session_id = _parse_sse(created.text)[0][1]["session_id"]
    resp = c.post(
        f"/api/sessions/{session_id}/chat/stream",
        json={"question": "讲讲太阳系", "mode": "free"},
    )
    frames = _parse_sse(resp.text)
    assert [(kind, None) for kind, _ in frames] == [("error", None)]
    assert "api / local" in frames[0][1]["message"]


def test_free_stream_end_to_end_empty_corpus(free_client):
    """api profile（FreeLLM 替身）+ 空库：free 流式端到端成功。

    kb 在空库必报"知识库为空"——free 全程零语料依赖即旁路的直接证明。
    """
    c, _, _ = free_client
    resp = c.post("/api/ask/stream", json={"question": "讲讲太阳系", "mode": "free"})
    assert resp.status_code == 200
    frames = _parse_sse(resp.text)

    kinds = [kind for kind, _ in frames]
    assert kinds[0] == "meta" and kinds[-1] == "done"
    assert kinds[1:-1] == ["delta", "delta"]  # 替身按两段产出

    done = frames[-1][1]["answer"]
    assert not done["refused"] and done["citations"] == []
    assert done["model"] == "fake-free"
    assert "retrieve" not in done["latency_ms"]  # 仅 generate（free 回放判据）
    assert done["prompt_tokens"] is None  # 流式无 usage（与 kb 同口径）
    concat = "".join(data["text"] for kind, data in frames if kind == "delta")
    assert concat == done["text"] == "第一段第二段"  # 拼接恒等

    # 会话已落库：历史接口是回放数据源（前端据 latency 键判渲染分支）
    session_id = frames[0][1]["session_id"]
    history = c.get(f"/api/sessions/{session_id}/messages").json()["messages"]
    assert len(history) == 2
    assert history[1]["citations"] == []
    assert "retrieve" not in history[1]["latency_ms"]
