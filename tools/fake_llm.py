"""E2E 共用的假 LLM 服务：同一份文案，**两种协议都会说**（中文注释纪律）。

为什么必须两种（2026-09-20 实测踩到）：local 档从这天起走 Ollama **原生**
`/api/chat`（`think` / `num_ctx` 只有它认，见 providers/ollama.py），而 api 档
与其余链路仍走 OpenAI 兼容的 `/v1/chat/completions`。E2E 的假服务只实现后者时，
local 档的用例会以「HTTP 404」的样子集体失败——看起来像产品坏了，其实是工具
没跟上通道变化。抽成一个共用件，以后换通道只改这里。

两个工具在用：`chrome_math.py`（公式渲染）、`chrome_eval.py`（自动题库 + 评测）。

原生协议的形状（照 providers/ollama.py 的解析写）：
  请求  POST {root}/chat  {"model", "messages", "stream", "options": {...}, "think"?}
  非流式 {"message": {"content": "…"}, "prompt_eval_count", "eval_count"}
  流式   NDJSON 逐行 {"message": {"content": "…"}, "done": false} … {"done": true}
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any, Protocol


def _chunks(text: str, size: int = 40) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def _openai_payload(text: str, model: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _openai_sse(text: str, model: str) -> bytes:
    parts = []
    for piece in _chunks(text):
        chunk = {
            "id": "chunk",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
        }
        parts.append(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n")
    stop = {
        "id": "chunk",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    parts.append(f"data: {json.dumps(stop, ensure_ascii=False)}\n\n")
    parts.append("data: [DONE]\n\n")
    return "".join(parts).encode("utf-8")


def _native_ndjson(text: str, model: str) -> bytes:
    lines = [
        json.dumps(
            {"model": model, "message": {"content": piece}, "done": False}, ensure_ascii=False
        )
        for piece in _chunks(text)
    ]
    lines.append(
        json.dumps(
            {"model": model, "message": {"content": ""}, "done": True, "eval_count": len(text)},
            ensure_ascii=False,
        )
    )
    return ("\n".join(lines) + "\n").encode("utf-8")


class _ReplyFn(Protocol):
    def __call__(self, system: str, user: str) -> str: ...


def make_handler(reply: str | _ReplyFn) -> type[BaseHTTPRequestHandler]:
    """造一个假端点类：两种协议共用同一份文案。

    reply 可以是固定字符串（chrome_math）或 `(system, user) -> str` 的回调
    （chrome_eval 要按提示词分流：出题 / 作答各一条）。

    类属性 `calls: list[dict]` 记录每次请求（取证用：断言"图片真的发出去了"、
    "走的是哪条通道"）。
    """

    def _text_for(body: dict) -> str:
        if isinstance(reply, str):
            return reply
        messages = body.get("messages") or []
        system = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
        user = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
        return reply(
            system if isinstance(system, str) else "", user if isinstance(user, str) else ""
        )

    class _FakeLLM(BaseHTTPRequestHandler):
        calls: list[dict] = []

        def log_message(self, *args):  # 静音 http.server 的 stderr 噪声
            pass

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 的接口名
            path = self.path.rstrip("/")
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            model = body.get("model", "fake-llm")
            reply_text = _text_for(body)
            _FakeLLM.calls.append({"path": path, "body": body})

            if path.endswith("/api/chat"):  # Ollama 原生
                data = (
                    _native_ndjson(reply_text, model)
                    if body.get("stream")
                    else json.dumps(
                        {
                            "model": model,
                            "message": {"role": "assistant", "content": reply_text},
                            "done": True,
                            "prompt_eval_count": 1,
                            "eval_count": 1,
                        },
                        ensure_ascii=False,
                    ).encode("utf-8")
                )
                ctype = b"application/x-ndjson" if body.get("stream") else b"application/json"
            elif path.endswith("/chat/completions"):  # OpenAI 兼容
                data = (
                    _openai_sse(reply_text, model)
                    if body.get("stream")
                    else json.dumps(_openai_payload(reply_text, model), ensure_ascii=False).encode(
                        "utf-8"
                    )
                )
                ctype = b"text/event-stream" if body.get("stream") else b"application/json"
            else:
                self.send_error(404)
                return

            self.send_response(200)
            self.send_header("Content-Type", ctype.decode())
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return _FakeLLM


def serve(handler_cls: type[BaseHTTPRequestHandler], port: int) -> ThreadingHTTPServer:
    """起假服务（守护线程；调用方在 finally 里 shutdown）。

    handler_cls 由 `make_handler(文案)` 造：调用方自己留着它，就能从
    `handler_cls.calls` 读取证记录（用到第几次、走的哪条通道）。
    """
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    Thread(target=server.serve_forever, daemon=True).start()
    return server
