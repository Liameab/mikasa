"""结构化输出与流式用量：请求体里到底带了什么（ADR-0037 / ADR-0038）。

这一层的契约只有两条，但都只能靠"看请求体"来验：
  1. **非流式**才带结构化参数——api 档 `response_format=json_object`、
     local 档 Ollama 原生 `format=<schema>`；**流式不带**（流式仍走文本标记）；
  2. 流式末帧的用量要能捞回来（`last_usage`）：捞不回来，"我花了多少 token"
     在网页问答这条主力路径上永远是 None。

全程零网络：两条缝分别是 `build_openai_client` 与 `urllib.request.urlopen`。
"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
from typing import Any

import pytest

import mikasa.providers.llm as llm_mod
import mikasa.providers.ollama as ollama_mod
from mikasa.config.settings import LLMConfig
from mikasa.errors import ProviderError
from mikasa.pipeline.prompts import STRUCTURED_SCHEMA
from mikasa.providers.llm import OpenAICompatLLM
from mikasa.providers.ollama import OllamaNativeLLM


def _config(**over: Any) -> LLMConfig:
    base: dict[str, Any] = {
        "backend": "api",
        "base_url": "https://example.com/v1",
        "api_key_env": "FAKE_KEY",
        "model": "test-model",
        "temperature": 0.1,
        "max_tokens": 64,
    }
    base.update(over)
    return LLMConfig(**base)


# ---------------------------------------------------------------------------
# api 档（OpenAI 兼容）
# ---------------------------------------------------------------------------


class _Completions:
    """假 create：记下 kwargs；stream 时返回预置帧（可让第一次带 stream_options 失败）。"""

    def __init__(self, sent: list[dict[str, Any]], frames: list[Any], fail_on_usage: bool) -> None:
        self._sent = sent
        self._frames = frames
        self._fail_on_usage = fail_on_usage

    def create(self, **kwargs: Any) -> Any:
        self._sent.append(kwargs)
        if self._fail_on_usage and "stream_options" in kwargs:
            raise ProviderError("HTTP 400：不认 stream_options")
        if kwargs.get("stream"):
            return iter(self._frames)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="非流式回答 [1]"))],
            usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3),
        )


def _fake_client(monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]], **over: Any):
    frames = over.pop("frames", [])
    fail_on_usage = over.pop("fail_on_usage", False)
    completions = _Completions(sent, frames, fail_on_usage)
    fake = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(llm_mod, "build_openai_client", lambda config, *, max_retries: fake)


def test_api_structured_only_on_demand_and_only_non_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """`response_format` 由**调用方**要求，且只在非流式那次带上。

    这里钉的是一个真实踩过的坑（2026-09-27）：把开关挂到 provider 配置上时，
    查询翻译这类辅助调用也会被切成 JSON 模式——api-json 那场 63 道题的
    跨语言第二路整场静默失效（日志里 63 条"查询翻译失败"）。所以：
    **配置只决定答案生成要不要结构化，provider 只认显式传参**。
    """
    sent: list[dict[str, Any]] = []
    _fake_client(monkeypatch, sent)
    llm = OpenAICompatLLM(_config(structured_output=True))

    # 辅助调用（不传 structured）：配置开着也不许带 response_format
    llm.complete([{"role": "user", "content": "把这句话译成英文"}], temperature=0, max_tokens=8)
    assert "response_format" not in sent[-1]

    # 答案生成（显式 structured=True）：带上，且非流式不带 stream_options
    llm.complete([{"role": "user", "content": "hi"}], temperature=0, max_tokens=8, structured=True)
    call = sent[-1]
    assert call["response_format"] == {"type": "json_object"}
    assert "stream_options" not in call


def test_api_stream_keeps_marker_path_and_asks_for_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """流式：即使开了结构化也不带 response_format（流式仍走文本标记）。"""
    sent: list[dict[str, Any]] = []
    _fake_client(
        monkeypatch,
        sent,
        frames=[
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="答"))], usage=None
            ),
            # 收尾帧：空 choices + 带 usage（include_usage 的实际形状）
            SimpleNamespace(
                choices=[], usage=SimpleNamespace(prompt_tokens=101, completion_tokens=52)
            ),
        ],
    )
    llm = OpenAICompatLLM(_config(structured_output=True))

    assert list(llm.stream([{"role": "user", "content": "hi"}], temperature=0, max_tokens=8)) == [
        "答"
    ]
    (call,) = sent
    assert "response_format" not in call
    assert call["stream_options"] == {"include_usage": True}
    assert llm.last_usage == (101, 52)


def test_api_stream_falls_back_when_upstream_rejects_stream_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上游 400 不认 stream_options：退一步不要用量，但**回答不能丢**。"""
    sent: list[dict[str, Any]] = []
    _fake_client(
        monkeypatch,
        sent,
        frames=[
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="答"))], usage=None
            )
        ],
        fail_on_usage=True,
    )
    llm = OpenAICompatLLM(_config())

    assert list(llm.stream([{"role": "user", "content": "hi"}], temperature=0, max_tokens=8)) == [
        "答"
    ]
    assert len(sent) == 2 and "stream_options" not in sent[1]
    assert llm.last_usage == (None, None)  # 没测到就是 None，不假装是 0


def test_api_last_usage_resets_between_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    """上一轮的用量不许串到下一轮（否则"这次花了多少"是假的）。

    注意：provider 会把 client 缓存在实例上，所以这里的假客户端要**按调用次序**
    给出不同的帧（第二次没有用量帧），换 monkeypatch 是无效的。
    """

    def _text_frame(text: str) -> Any:
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=text))], usage=None
        )

    calls: list[list[Any]] = [
        [
            _text_frame("a"),
            SimpleNamespace(
                choices=[], usage=SimpleNamespace(prompt_tokens=5, completion_tokens=1)
            ),
        ],
        [_text_frame("b")],  # 第二次：上游没给用量
    ]

    class _Sequenced:
        def create(self, **kwargs: Any) -> Any:
            frames = calls.pop(0) if kwargs.get("stream") else []
            return iter(frames)

    fake = SimpleNamespace(chat=SimpleNamespace(completions=_Sequenced()))
    monkeypatch.setattr(llm_mod, "build_openai_client", lambda config, *, max_retries: fake)

    llm = OpenAICompatLLM(_config())
    list(llm.stream([{"role": "user", "content": "hi"}], temperature=0, max_tokens=8))
    assert llm.last_usage == (5, 1)

    list(llm.stream([{"role": "user", "content": "hi"}], temperature=0, max_tokens=8))
    assert llm.last_usage == (None, None)


# ---------------------------------------------------------------------------
# local 档（Ollama 原生）
# ---------------------------------------------------------------------------


class _FakeHTTP:
    def __init__(self, *, body: bytes = b"", lines: list[bytes] | None = None) -> None:
        self.body = body
        self.lines = lines or []
        self.seen: dict[str, Any] = {}

    def __call__(self, request, timeout=None):  # noqa: ANN001 - 与 urlopen 同形
        self.seen["body"] = json.loads((request.data or b"{}").decode("utf-8"))
        if self.lines:
            return io.BytesIO(b"".join(self.lines))
        return io.BytesIO(self.body)


OLLAMA_CFG = {
    "backend": "local",
    "base_url": "http://localhost:11434/v1",
    "model": "qwen3:8b",
}


def test_ollama_format_schema_only_on_demand_and_only_non_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeHTTP(body=json.dumps({"message": {"content": "{}"}}).encode())
    monkeypatch.setattr(ollama_mod.urllib.request, "urlopen", fake)
    llm = OllamaNativeLLM(LLMConfig(**{**OLLAMA_CFG, "structured_output": True}))  # type: ignore[arg-type]

    # 辅助调用（不传 structured）：即使是同一个 provider，也不许带 format
    llm.complete([{"role": "user", "content": "译成英文"}], temperature=0, max_tokens=8)
    assert "format" not in fake.seen["body"]

    # 答案生成（显式要求）：带上 schema（受约束解码）
    llm.complete([{"role": "user", "content": "hi"}], temperature=0, max_tokens=8, structured=True)
    assert fake.seen["body"]["format"] == STRUCTURED_SCHEMA

    # 流式：无论如何都不带（流式仍走文本标记）
    line = json.dumps({"message": {"content": "答"}, "done": True}).encode() + b"\n"
    stream_fake = _FakeHTTP(lines=[line])
    monkeypatch.setattr(ollama_mod.urllib.request, "urlopen", stream_fake)
    assert list(llm.stream([{"role": "user", "content": "hi"}], temperature=0, max_tokens=8)) == [
        "答"
    ]
    assert "format" not in stream_fake.seen["body"]


def test_ollama_structured_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认（不开结构化）时请求体不带 format——新能力默认不生效。"""
    fake = _FakeHTTP(body=json.dumps({"message": {"content": "x"}}).encode())
    monkeypatch.setattr(ollama_mod.urllib.request, "urlopen", fake)
    OllamaNativeLLM(LLMConfig(**OLLAMA_CFG)).complete(  # type: ignore[arg-type]
        [{"role": "user", "content": "hi"}], temperature=0, max_tokens=8
    )
    assert "format" not in fake.seen["body"]


def test_ollama_stream_captures_usage_from_final_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    """NDJSON 收尾帧的 prompt_eval_count / eval_count 就是这次流式的用量。"""
    lines = [
        json.dumps({"message": {"content": "答"}, "done": False}).encode() + b"\n",
        json.dumps(
            {"message": {"content": ""}, "done": True, "prompt_eval_count": 900, "eval_count": 40}
        ).encode()
        + b"\n",
    ]
    monkeypatch.setattr(ollama_mod.urllib.request, "urlopen", _FakeHTTP(lines=lines))
    llm = OllamaNativeLLM(LLMConfig(**OLLAMA_CFG))  # type: ignore[arg-type]

    assert list(llm.stream([{"role": "user", "content": "hi"}], temperature=0, max_tokens=8)) == [
        "答"
    ]
    assert llm.last_usage == (900, 40)
