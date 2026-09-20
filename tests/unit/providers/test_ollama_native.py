"""Ollama 原生通道（local 档的生成端）单测。

为什么单独立这份：local 档 2026-09-20 从 OpenAI 兼容面改到原生 `/api/chat`，
只为一个实测结论——兼容面**静默忽略** `think` 与 `num_ctx`：

    think=true   14.2s、617 字推理、**答案 0 字**（400 token 预算被推理吃光）
    think=false   1.4s、直接给出答案

所以"请求体里真的带上了这两个参数"是本文件的第一等公民：带不上，用户面板上
那个开关就是个装饰。全程零网络——网络缝是 `urllib.request.urlopen`。
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

import mikasa.providers.ollama as ollama_mod
from mikasa.config.settings import LLMConfig
from mikasa.errors import ProviderError
from mikasa.providers.ollama import OllamaNativeLLM


def _config(**over: object) -> LLMConfig:
    base: dict[str, object] = {
        "backend": "local",
        "base_url": "http://localhost:11434/v1",
        "model": "qwen3:8b",
        "temperature": 0.1,
        "max_tokens": 1024,
    }
    base.update(over)
    return LLMConfig(**base)  # type: ignore[arg-type]


class _FakeHTTP:
    """urlopen 替身：记下请求体，按需返回一次性 JSON 或 NDJSON 流。"""

    def __init__(self, *, body: bytes = b"", lines: list[bytes] | None = None) -> None:
        self.body = body
        self.lines = lines or []
        self.seen: dict[str, object] = {}

    def __call__(self, request, timeout=None):  # noqa: ANN001 - 与 urlopen 同形
        self.seen["url"] = request.full_url
        self.seen["body"] = json.loads((request.data or b"{}").decode("utf-8"))
        self.seen["timeout"] = timeout
        if self.lines:
            return io.BytesIO(b"".join(self.lines))
        return io.BytesIO(self.body)


def test_request_body_carries_think_and_num_ctx(monkeypatch: pytest.MonkeyPatch) -> None:
    """两个本机旋钮必须原样落到原生请求体里（options.num_ctx / 顶层 think）。"""
    fake = _FakeHTTP(body=json.dumps({"message": {"content": "好的"}}).encode())
    monkeypatch.setattr(ollama_mod.urllib.request, "urlopen", fake)
    llm = OllamaNativeLLM(_config(think=False, num_ctx=8192))

    out = llm.complete([{"role": "user", "content": "hi"}], temperature=0.2, max_tokens=64)

    assert out.text == "好的"
    assert fake.seen["url"] == "http://127.0.0.1:11434/api/chat"  # localhost 已规范化
    body = fake.seen["body"]
    assert body["think"] is False
    assert body["options"]["num_ctx"] == 8192
    assert body["options"]["num_predict"] == 64
    assert body["stream"] is False


def test_unset_knobs_are_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """ "没配置"与"关掉"是两件事：None 时干脆不带这两个键（跟随模型/Ollama 默认）。"""
    fake = _FakeHTTP(body=json.dumps({"message": {"content": "x"}}).encode())
    monkeypatch.setattr(ollama_mod.urllib.request, "urlopen", fake)
    OllamaNativeLLM(_config()).complete(
        [{"role": "user", "content": "hi"}], temperature=0, max_tokens=8
    )
    body = fake.seen["body"]
    assert "think" not in body
    assert "num_ctx" not in body["options"]


def test_thinking_ate_the_budget_reports_actionably(monkeypatch: pytest.MonkeyPatch) -> None:
    """答案为空但推理有内容 → 报可照做的错，而不是返回空串让界面显示"答完了"。"""
    payload = {"message": {"content": "", "thinking": "先想…" * 50}}
    monkeypatch.setattr(
        ollama_mod.urllib.request, "urlopen", _FakeHTTP(body=json.dumps(payload).encode())
    )
    llm = OllamaNativeLLM(_config(think=True, max_tokens=400))
    with pytest.raises(ProviderError, match="思考模式"):
        llm.complete([{"role": "user", "content": "hi"}], temperature=0, max_tokens=400)


def test_stream_reads_ndjson_and_ignores_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    """流式：只把 message.content 当答案；thinking 增量不能混进正文。"""
    lines = [
        json.dumps({"message": {"content": "", "thinking": "推理中"}, "done": False}) + "\n",
        json.dumps({"message": {"content": "泰"}, "done": False}) + "\n",
        json.dumps({"message": {"content": "勒展开"}, "done": False}) + "\n",
        json.dumps({"message": {"content": ""}, "done": True}) + "\n",
    ]
    monkeypatch.setattr(
        ollama_mod.urllib.request, "urlopen", _FakeHTTP(lines=[ln.encode() for ln in lines])
    )
    llm = OllamaNativeLLM(_config(think=False))
    pieces = list(llm.stream([{"role": "user", "content": "hi"}], temperature=0, max_tokens=64))
    assert "".join(pieces) == "泰勒展开"
    assert all("推理" not in p for p in pieces)


def test_stream_with_only_thinking_raises_at_the_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """流走完了却一个字没产出、推理却有内容 → 收尾报同一句可照做的提示。"""
    lines = [
        json.dumps({"message": {"content": "", "thinking": "想很久"}, "done": False}) + "\n",
        json.dumps({"message": {"content": ""}, "done": True}) + "\n",
    ]
    monkeypatch.setattr(
        ollama_mod.urllib.request, "urlopen", _FakeHTTP(lines=[ln.encode() for ln in lines])
    )
    llm = OllamaNativeLLM(_config(think=True))
    with pytest.raises(ProviderError, match="思考模式"):
        list(llm.stream([{"role": "user", "content": "hi"}], temperature=0, max_tokens=64))


def test_http_error_becomes_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 层错误翻译成人话（含模型名与地址，前端原样展示）。"""

    def boom(request, timeout=None):  # noqa: ANN001, ARG001
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(ollama_mod.urllib.request, "urlopen", boom)
    llm = OllamaNativeLLM(_config())
    with pytest.raises(ProviderError, match="HTTP 404"):
        llm.complete([{"role": "user", "content": "hi"}], temperature=0, max_tokens=8)


def test_missing_base_url_is_config_error() -> None:
    llm = OllamaNativeLLM(_config(base_url=""))
    with pytest.raises(Exception, match="base_url"):
        llm.complete([{"role": "user", "content": "hi"}], temperature=0, max_tokens=8)
