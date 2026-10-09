"""SSE 帧序列化纯函数测试：event/data 行格式与 JSON 转义安全。

协议对偶的另一端（qa 路由端到端）由 test_ask_stream 覆盖，
这里只锁帧格式本身——格式变了两个测试文件会同时报警。
"""

from __future__ import annotations

import json

from mikasa.web.sse import sse_event, sse_response


def test_sse_event_frame_shape():
    frame = sse_event("delta", {"text": "你好"})
    assert frame == 'event: delta\ndata: {"text": "你好"}\n\n'


def test_sse_event_escapes_newlines_and_quotes():
    """正文含换行/引号：JSON 转义后不破坏帧结构（data 行不被截断）。"""
    frame = sse_event("done", {"answer": {"text": '行1\n行2"引号'}})
    lines = frame.split("\n")
    # 帧 = event 行 + 单条 data 行 + 空行；正文换行已被 JSON 转义为 \n
    assert lines[0].startswith("event: ")
    assert lines[1].startswith("data: {")
    assert lines[2] == ""
    payload = json.loads(lines[1].removeprefix("data: "))
    assert payload["answer"]["text"] == '行1\n行2"引号'


def test_sse_event_str_payload_passthrough():
    """data 传字符串时不二次 JSON（error 帧常用纯文本）。"""
    assert sse_event("error", "出错了") == "event: error\ndata: 出错了\n\n"


def test_sse_response_headers():
    resp = sse_response(iter([]))
    assert resp.media_type == "text/event-stream"
    assert resp.headers["cache-control"] == "no-cache"


def test_disconnect_closes_the_sync_iterator():
    """客户端断开时要**显式** close 同步生成器（2026-10-05 审查）。

    Starlette 的 StreamingResponse 只做 iterate_in_threadpool，断开时不会 close
    那个同步生成器——它的 finally（收尾、关上游流）要等 GC 才跑。
    """
    import asyncio

    from mikasa.web.sse import _closing_stream

    closed: list[str] = []

    def frames():
        try:
            yield "a"
            yield "b"
        finally:
            closed.append("closed")

    async def consume_then_abort() -> None:
        agen = _closing_stream(frames())
        assert await agen.__anext__() == "a"
        await agen.aclose()  # 模拟客户端断开

    asyncio.run(consume_then_abort())
    assert closed == ["closed"]
