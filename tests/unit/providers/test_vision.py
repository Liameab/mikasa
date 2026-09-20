"""视觉 Provider 单测（M6 ②）：消息形状、失败翻译、未接入文案、魔数嗅探。

锁四条不变量：
  1. 发出去的 content 是**数组**且含 image_url + data URL——这是"图片真的
     发出去了"的唯一证据（退回成纯字符串时模型只收到提示词，识别必失败）；
  2. 上游异常翻成 ProviderError（app 层据此回 502，而不是裸 500）；
  3. 未接入时是**可照做的中文报错**，不是空文本、也不是 500；
  4. 类型判定只看魔数，不看扩展名/Content-Type（两者都是客户端说了算）。
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from mikasa.config.settings import VisionConfig
from mikasa.errors import ConfigError, ProviderError
from mikasa.providers import get_vision
from mikasa.providers.vision import (
    VISION_PROMPT,
    NoVision,
    OpenAICompatVision,
    mime_for_suffix,
    sniff_image_mime,
)

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"0" * 16


class _Msg:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _Choice:
    def __init__(self, text: str) -> None:
        self.message = _Msg(text)


class _Usage:
    prompt_tokens = 11
    completion_tokens = 7


class _Response:
    def __init__(self, text: str) -> None:
        self.choices = [_Choice(text)]
        self.usage = _Usage()


class _Completions:
    def __init__(self, sent: list[dict[str, Any]], error: Exception | None) -> None:
        self._sent = sent
        self._error = error

    def create(self, **kwargs: Any) -> _Response:
        self._sent.append(kwargs)
        if self._error is not None:
            raise self._error
        return _Response("转写结果：[标题]")


class _FakeClient:
    """假 OpenAI 客户端：记录请求、可选地抛错。"""

    def __init__(self, sent: list[dict[str, Any]], error: Exception | None = None) -> None:
        self.chat = type("Chat", (), {"completions": _Completions(sent, error)})()


def _fake_builder(sent: list[dict[str, Any]], error: Exception | None = None):
    def build(config: Any, *, max_retries: int) -> _FakeClient:
        return _FakeClient(sent, error)

    return build


def _config(**over: Any) -> VisionConfig:
    base: dict[str, Any] = {
        "backend": "api",
        "base_url": "https://example.com/v1",
        "api_key_env": "FAKE_KEY",
        "model": "vl-1",
    }
    base.update(over)
    return VisionConfig(**base)


def test_describe_sends_multimodal_message(monkeypatch):
    """content 必须是数组：退回纯字符串时图片根本没出去。"""
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr("mikasa.providers.vision.build_openai_client", _fake_builder(sent))
    out = OpenAICompatVision(_config()).describe(JPEG, mime="image/jpeg")
    assert out.text == "转写结果：[标题]"
    assert out.prompt_tokens == 11 and out.completion_tokens == 7

    (call,) = sent
    assert call["model"] == "vl-1"
    user = call["messages"][-1]
    assert isinstance(user["content"], list)
    assert [part["type"] for part in user["content"]] == ["text", "image_url"]
    assert user["content"][0]["text"] == VISION_PROMPT
    url = user["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert base64.b64encode(JPEG).decode("ascii") in url


def test_describe_translates_upstream_failure(monkeypatch):
    """上游失败 → ProviderError（含模型名与端点，排查用）。"""
    monkeypatch.setattr(
        "mikasa.providers.vision.build_openai_client",
        _fake_builder([], error=RuntimeError("connection reset")),
    )
    with pytest.raises(ProviderError) as err:
        OpenAICompatVision(_config()).describe(JPEG, mime="image/jpeg")
    assert "vl-1" in str(err.value)
    assert "connection reset" in str(err.value)


def test_no_vision_error_is_actionable():
    """未接入时给的是可照做的中文文案，不是空文本。"""
    with pytest.raises(ConfigError) as err:
        NoVision().describe(JPEG, mime="image/jpeg")
    msg = str(err.value)
    assert "视觉模型" in msg
    assert "ollama pull" in msg  # 本机那条路要写清楚命令


def test_get_vision_dispatch():
    assert isinstance(get_vision(VisionConfig(backend="none")), NoVision)
    assert isinstance(get_vision(_config()), OpenAICompatVision)


@pytest.mark.parametrize(
    ("data", "want"),
    [
        (JPEG, "image/jpeg"),
        (PNG, "image/png"),
        (WEBP, "image/webp"),
        (b"GIF89a", None),  # 合法图片但我们不收（前端也不会送）
        (b"", None),
        (b"hello world", None),
    ],
)
def test_sniff_image_mime(data: bytes, want: str | None):
    assert sniff_image_mime(data) == want


def test_mime_for_suffix_is_whitelist():
    assert mime_for_suffix(".JPG") == "image/jpeg"
    assert mime_for_suffix(".jpeg") == "image/jpeg"
    assert mime_for_suffix(".gif") is None
    assert mime_for_suffix("") is None
