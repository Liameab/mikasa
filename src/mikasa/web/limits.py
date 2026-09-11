"""请求体上限：在**读取过程中**拦截，而不是读完再判。

**为什么必须有这一层**：Starlette 的 multipart 解析会把 file part 写进临时文件
（它的 `max_part_size` 只作用于非文件字段，文件部分没有上限）。所以"先
`await request.form()`，再 seek 量大小判 413"的写法，整个 body 早已落满磁盘——
413 是**事后**返回的，磁盘已经交出去了。2026-09-11 审查实测：`--host 0.0.0.0`
（无鉴权）下一个 `curl -F "file=@20GB.bin"` 就能写满系统盘，随后 SQLite WAL、
日志、web-tmp 全写不进去，整机应用不可用。

这里在 ASGI 层计数：一超限就向下游投递 `http.disconnect`，让 Starlette 的
`request.form()` 抛 `ClientDisconnect` 停下来——磁盘上最多只留下已读的那一小段。
端点侧把 `ClientDisconnect` 转成 413（见 app.py 的异常处理器）。
"""

from __future__ import annotations

from typing import Any

# 表单边界、字段名、其他小字段要占掉一点字节，给上限留的余量
_FORM_OVERHEAD = 8 * 1024 * 1024


class BodySizeLimit:
    """ASGI 中间件：请求体超限即中断读取。

    先看 `Content-Length`（能直接判就一个字节都不读，这是浏览器与 curl 的正常
    路径）；没有该头（分块传输）或它说谎时，再靠流式计数兜底。
    """

    def __init__(self, app: Any, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    break
                if declared > self.max_bytes:
                    # 还没读一个字节就拒掉——这是最主要的一条防线
                    await self._reject(send)
                    return
                break

        received = 0

        async def limited_receive() -> dict:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # 投递 disconnect 让下游停止读取。不抛异常：抛了会穿过
                    # Starlette 的解析栈变成 500，而这里要的是干净的 413。
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, limited_receive, send)

    @staticmethod
    async def _reject(send: Any) -> None:
        body = b'{"error":{"type":"too_large","message":"\\u8bf7\\u6c42\\u4f53\\u8fc7\\u5927"}}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def body_limit_bytes(upload_max_mb: int) -> int:
    """按配置的上传上限算出请求体上限（含表单开销余量）。"""
    return upload_max_mb * 1024 * 1024 + _FORM_OVERHEAD
