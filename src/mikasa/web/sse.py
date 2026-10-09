"""手写 SSE：事件帧序列化 + 响应封装（刻意不引 sse-starlette）。

协议（与 ask.StreamEvent / 前端 js 解析严格对偶）：
  event: meta/delta/done/error + data: JSON，帧间空行分隔。
data 一律 JSON（ensure_ascii=False）——多字节/换行/引号都安全，
前端按帧解析只需处理空行边界。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator

from fastapi.responses import StreamingResponse
from starlette.concurrency import iterate_in_threadpool

# 响应头：no-cache 防代理缓冲（SSE 语义要求）、X-Accel-Buffering
# 关 nginx 缓冲（本地直连时无副作用）
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def sse_event(event: str, data: object) -> str:
    """序列化一帧 SSE：`event: 名` 行 + `data: JSON` 行 + 空行结尾。"""
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


async def _closing_stream(frames: Iterator[str]) -> AsyncIterator[str]:
    """把同步生成器包成"断开时会被显式收尾"的异步流。

    Starlette 的 StreamingResponse 只做 `iterate_in_threadpool`——**客户端断开时
    它不会 close 那个同步生成器**（2026-10-05 审查核对了 starlette.responses 源码）。
    于是生成器里的 `finally`（收尾 SSE、关上游 HTTP 流）要等 GC 才跑，长进程里就是
    资源一直吊着。这里在取消路径上补一刀 `close()`，让 finally 立刻执行。
    """
    try:
        async for chunk in iterate_in_threadpool(frames):
            yield chunk
    finally:
        close = getattr(frames, "close", None)
        if close is not None:
            close()


def sse_response(frames: Iterator[str]) -> StreamingResponse:
    """把"逐帧已序列化的 SSE 文本流"封装为流式响应。"""
    return StreamingResponse(
        _closing_stream(frames), media_type="text/event-stream", headers=_SSE_HEADERS
    )
