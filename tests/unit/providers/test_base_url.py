"""base_url 的 loopback 规范化（2026-09-20 用户报障的回归锁）。

背景：Windows 上 `localhost` 先解析到 `::1`，而 Ollama 只监听 `127.0.0.1`；
打包版里 httpx 会卡在 `::1` 上直到整个请求超时（实测每次 20.1 秒的
APITimeoutError），同一份地址在普通 Python 里却能通——这是最容易误判成
"模型服务坏了"的一类问题。规范化只动主机名恰为 `localhost` 的地址。
"""

from __future__ import annotations

import openai
import pytest

from mikasa.config.settings import EmbeddingConfig, LLMConfig, RerankerConfig
from mikasa.errors import ConfigError
from mikasa.providers.embedding import ApiEmbedding
from mikasa.providers.llm import build_openai_client, normalize_base_url
from mikasa.providers.reranker import ApiReranker


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://localhost:11434/v1", "http://127.0.0.1:11434/v1"),
        ("http://localhost/v1", "http://127.0.0.1/v1"),
        ("https://localhost:8443/api", "https://127.0.0.1:8443/api"),
        # 大小写与尾部斜杠都要原样保住（除了主机名本身）
        ("http://LocalHost:11434/v1/", "http://127.0.0.1:11434/v1/"),
    ],
)
def test_normalize_rewrites_localhost(raw: str, expected: str) -> None:
    assert normalize_base_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://api.deepseek.com/v1",  # 远程地址一个字都不碰
        "http://127.0.0.1:11434/v1",  # 已经是 IPv4
        "http://[::1]:11434/v1",  # 用户显式要 IPv6：尊重
        "http://localhost:notaport/v1",  # 端口非法 → 原样返回，不在这里报错
        "",
    ],
)
def test_normalize_keeps_everything_else(raw: str) -> None:
    assert normalize_base_url(raw) == raw


def test_build_openai_client_normalizes_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    """客户端拿到的 base_url 必须是 127.0.0.1（local 档免密钥、注入占位 key）。"""
    seen: dict[str, object] = {}

    class _FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    cfg = LLMConfig(backend="local", base_url="http://localhost:11434/v1", model="qwen3:8b")

    build_openai_client(cfg, max_retries=0)

    assert seen["base_url"] == "http://127.0.0.1:11434/v1"
    assert seen["api_key"] == "ollama"  # local 档的占位密钥（ADR-0014）
    assert seen["max_retries"] == 0


def test_build_openai_client_keeps_api_backend_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """api 档的远程地址原样透传（规范化只针对 localhost）。"""
    seen: dict[str, object] = {}

    class _FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    monkeypatch.setenv("MIKASA_TEST_KEY", "sk-test")
    cfg = LLMConfig(
        backend="api",
        base_url="https://api.deepseek.com/v1",
        api_key_env="MIKASA_TEST_KEY",
        model="deepseek-chat",
    )

    build_openai_client(cfg, max_retries=3)

    assert seen["base_url"] == "https://api.deepseek.com/v1"
    assert seen["api_key"] == "sk-test"


def test_build_openai_client_still_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """规范化不能顺手把"api 档必须有密钥"这条闸门放行（ADR-0014 的放行点只在 local）。"""
    monkeypatch.delenv("MIKASA_TEST_KEY", raising=False)
    cfg = LLMConfig(
        backend="api",
        base_url="http://localhost:9999/v1",
        api_key_env="MIKASA_TEST_KEY",
        model="x",
    )
    with pytest.raises(ConfigError, match="未配置 API 密钥"):
        build_openai_client(cfg, max_retries=0)


# ---------------------------------------------------------------------------
# 同一个坑的另外两条链路（2026-09-20 追加）
#
# 覆盖缺口是审查实测发现的：规范化最初只接在 build_openai_client 上，而
# ApiEmbedding 自己 new 一个 OpenAI、ApiReranker 自己拼端点——把嵌入/重排指向
# 本机 OpenAI 兼容服务（LM Studio / vLLM / Ollama 的 /v1）时，同一个 localhost
# 卡死在 ::1 上原样复现。
# ---------------------------------------------------------------------------


def test_embedding_client_normalizes_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class _FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    monkeypatch.setenv("MIKASA_TEST_KEY", "sk-test")
    cfg = EmbeddingConfig(
        backend="api",
        base_url="http://localhost:11434/v1",
        api_key_env="MIKASA_TEST_KEY",
        model="bge-m3",
    )

    ApiEmbedding(cfg)._get_client()

    assert seen["base_url"] == "http://127.0.0.1:11434/v1"


def test_reranker_endpoint_normalizes_localhost() -> None:
    cfg = RerankerConfig(
        backend="api",
        base_url="http://localhost:11434/v1",
        model="bge-reranker-v2-m3",
        top_n=8,
    )
    assert ApiReranker(cfg)._endpoint() == "http://127.0.0.1:11434/v1/rerank"
