"""Provider 工厂与错误分支单测：分派正确性 + 缺密钥/依赖的失败语义。

api/local 的网络路径不做单测（联网、密钥），但其"失败前校验"是可测的
设计点：密钥未配置时报 ConfigError（而非隐晦的网络 401），
fastembed 未安装时报 ConfigError 并给出安装指引。
"""

from __future__ import annotations

import os
import sys
import types

import pytest

from mikasa.errors import ConfigError, ProviderError
from mikasa.providers import (
    ApiEmbedding,
    ApiReranker,
    EmbeddingProvider,
    LLMProvider,
    LocalFastEmbed,
    LocalReranker,
    MockLLM,
    NoEmbedding,
    NoReranker,
    OpenAICompatLLM,
    RerankerProvider,
    get_embedding,
    get_llm,
    get_reranker,
)

# ---------------------------------------------------------------------------
# 工厂分派
# ---------------------------------------------------------------------------


def test_factory_dispatches_mock_and_none(offline_settings):
    llm = get_llm(offline_settings.llm)
    embedding = get_embedding(offline_settings.embedding)
    reranker = get_reranker(offline_settings.reranker)
    assert isinstance(llm, MockLLM)
    assert isinstance(llm, LLMProvider)  # 协议可运行时检查
    assert isinstance(embedding, NoEmbedding)
    assert isinstance(embedding, EmbeddingProvider)
    assert isinstance(reranker, NoReranker)
    assert isinstance(reranker, RerankerProvider)


def test_unknown_backend_rejected_at_config_level():
    """非法 backend 在配置构造期就被 pydantic Literal 拦下（类型即文档）。"""
    import pydantic

    from mikasa.config.settings import EmbeddingConfig, LLMConfig, RerankerConfig

    for cfg_type in (LLMConfig, EmbeddingConfig, RerankerConfig):
        with pytest.raises(pydantic.ValidationError):
            cfg_type(backend="bad-backend")


# ---------------------------------------------------------------------------
# api 提供方的"无密钥即 ConfigError"前置校验（不联网）
# ---------------------------------------------------------------------------


def test_api_llm_without_key_raises_config_error(api_settings, monkeypatch):
    llm = OpenAICompatLLM(api_settings.llm)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="API 密钥"):
        llm.complete([{"role": "user", "content": "hi"}], temperature=0.1, max_tokens=8)


def test_api_embedding_without_key_raises_config_error(api_settings, monkeypatch):
    embedding = ApiEmbedding(api_settings.embedding)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="Embedding 密钥"):
        embedding.embed_documents(["测试"])


def test_api_reranker_checks_key_before_http(api_settings, monkeypatch):
    """校验顺序：密钥检查先于一切——宁报配置错，不给隐晦空结果。"""
    reranker = ApiReranker(api_settings.reranker)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="Reranker 密钥"):
        reranker.rerank("q", ["doc"], top_n=3)
    with pytest.raises(ConfigError, match="Reranker 密钥"):
        reranker.rerank("q", [], top_n=3)  # 空候选也在密钥校验之后
    # 密钥在位 + 空候选 → 短路返回空，无需发请求
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-test-silicon-0000")
    assert ApiReranker(api_settings.reranker).rerank("q", [], top_n=3) == []


def test_api_reranker_http_error_wrapped(monkeypatch):
    """HTTP 层失败翻译为 ProviderError（不联网：直接打桩 urlopen）。"""
    import urllib.error

    import mikasa.providers.reranker as rk

    class _FakeConfig:
        api_key = "sk-test"
        base_url = "https://example.invalid/v1"
        model = "m"

    def _boom(*args, **kwargs):
        raise urllib.error.HTTPError("url", 401, "unauthorized", None, None)

    monkeypatch.setattr(rk.urllib.request, "urlopen", _boom)
    with pytest.raises(ProviderError, match="HTTP 401"):
        ApiReranker(_FakeConfig()).rerank("q", ["d"], top_n=3)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# local 提供方：分派 + 免密钥放行（ADR-0014）+ 依赖缺失的 ConfigError
# ---------------------------------------------------------------------------


def test_factory_dispatches_local(local_settings):
    """local：LLM/embedding 分派到 OpenAICompatLLM / LocalFastEmbed。"""
    llm = get_llm(local_settings.llm)
    embedding = get_embedding(local_settings.embedding)
    assert isinstance(llm, OpenAICompatLLM)
    assert isinstance(embedding, LocalFastEmbed)
    assert llm.model == local_settings.llm.model
    assert embedding.model == local_settings.embedding.model


def test_local_fastembed_missing_dependency_raises(api_settings, monkeypatch):
    """fastembed 不可导入 → ConfigError（含安装指引）。

    不看本机安装状态：把 sys.modules 里的 fastembed 顶成 None，强制
    `from fastembed import ...` 走 ImportError（与未安装同语义）——
    装了 [local] extra 的开发机上同样能测到缺失分支，且不触发下载。
    """
    monkeypatch.setitem(sys.modules, "fastembed", None)
    embedding = LocalFastEmbed(api_settings.embedding)
    with pytest.raises(ConfigError, match="fastembed"):
        embedding.embed_documents(["测试"])


def test_local_fastembed_retries_with_mirror_on_download_failure(local_settings, monkeypatch):
    """首次下载失败 → 自动切国内镜像重试一次（成功即照常返回后端）。

    不联网：把 fastembed.TextEmbedding 换成"第一次抛、第二次成功"的替身，
    断言第二次调用时端点已切到镜像（环境变量与 huggingface_hub 常量双写——
    后者在 import 时就定值，只设环境变量对已运行的进程无效）。
    背景：模型下载发生在上传文档的请求里，直连超时会让"入库"整条挂掉。
    """
    import fastembed
    import numpy as np
    from huggingface_hub import constants as hf_constants

    monkeypatch.setenv("HF_ENDPOINT", "https://huggingface.co")  # 注册还原点
    monkeypatch.setenv("HF_HUB_DISABLE_XET", "0")
    monkeypatch.setattr(hf_constants, "ENDPOINT", "https://huggingface.co")

    seen: list[dict] = []

    class _FakeTextEmbedding:
        def __init__(self, model: str) -> None:
            seen.append(
                {
                    "model": model,
                    "endpoint": hf_constants.ENDPOINT,
                    "env": os.environ.get("HF_ENDPOINT"),
                }
            )
            if len(seen) == 1:
                raise RuntimeError("ConnectTimeout: 模拟直连 huggingface.co 失败")

        def embed(self, texts):
            return iter([np.zeros(4, dtype=np.float32) for _ in texts])

    monkeypatch.setattr(fastembed, "TextEmbedding", _FakeTextEmbedding)
    embedding = LocalFastEmbed(local_settings.embedding)
    out = embedding.embed_documents(["测试"])

    assert out.shape == (1, 4)
    assert len(seen) == 2  # 直连一次 + 镜像重试一次
    assert seen[0]["endpoint"] == "https://huggingface.co"
    assert seen[1]["endpoint"] == "https://hf-mirror.com"
    assert seen[1]["env"] == "https://hf-mirror.com"
    assert os.environ["HF_HUB_DISABLE_XET"] == "1"  # 镜像的硬要求


def test_local_fastembed_download_failure_gives_actionable_error(local_settings, monkeypatch):
    """两次都失败 → ProviderError 中文指引（而不是裸 httpx traceback 冒到前端）。"""
    import fastembed
    from huggingface_hub import constants as hf_constants

    monkeypatch.setenv("HF_ENDPOINT", "https://huggingface.co")
    monkeypatch.setattr(hf_constants, "ENDPOINT", "https://huggingface.co")

    class _AlwaysFail:
        def __init__(self, model: str) -> None:
            raise RuntimeError("ConnectTimeout: 连接尝试失败")

    monkeypatch.setattr(fastembed, "TextEmbedding", _AlwaysFail)
    embedding = LocalFastEmbed(local_settings.embedding)
    with pytest.raises(ProviderError, match="首次使用需要联网下载"):
        embedding.embed_documents(["测试"])


def test_local_reranker_missing_dependency_raises(api_settings, monkeypatch):
    monkeypatch.setitem(sys.modules, "fastembed", None)
    reranker = LocalReranker(api_settings.reranker)
    with pytest.raises(ConfigError, match="fastembed"):
        reranker.rerank("q", ["doc"], top_n=3)


def test_local_llm_without_key_injects_placeholder(local_settings, monkeypatch):
    """local（Ollama）免密钥：构造客户端时注入占位 key（ADR-0014）。

    不联网：用假 openai 模块接管 _get_client 的延迟导入，捕获构造参数
    断言注入值。前提由配置档保证：local 的 api_key_env="" → api_key 恒
    None——豁免点选在 provider 层（若在 settings 层放行，api 档忘填
    密钥会被静默放行，见 ADR-0014）。
    """
    captured: dict[str, object] = {}

    class _FakeResponse:
        choices = [types.SimpleNamespace(message=types.SimpleNamespace(content="你好"))]
        usage = None

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        @property
        def chat(self):
            return self

        @property
        def completions(self):
            return self

        def create(self, **kwargs):
            del kwargs
            return _FakeResponse()

    fake_module = types.ModuleType("openai")
    fake_module.OpenAI = _FakeOpenAI  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake_module)

    llm = get_llm(local_settings.llm)
    assert local_settings.llm.api_key is None  # 前提：local 无密钥可用
    out = llm.complete([{"role": "user", "content": "hi"}], temperature=0.1, max_tokens=8)
    assert out.text == "你好"
    assert captured["api_key"] == "ollama"  # 占位 key：Ollama /v1 只要求非空
    assert str(captured["base_url"]).endswith("/v1")


# ---------------------------------------------------------------------------
# none 提供方的退化语义（offline 基线）
# ---------------------------------------------------------------------------


def test_no_embedding_returns_empty_matrix():
    embedding = NoEmbedding()
    assert embedding.dim == 0
    assert embedding.embed_documents(["任意文本"]).shape == (0, 0)
    assert embedding.embed_query("任意文本").shape == (0,)


def test_no_reranker_passthrough_order():
    reranker = NoReranker()
    assert reranker.rerank("q", ["a", "b", "c"], top_n=2) == [0, 1]
    assert reranker.rerank("q", ["a", "b"], top_n=0) == [0, 1]  # 全量
    assert reranker.rerank("q", [], top_n=2) == []


def test_api_embedding_empty_texts_short_circuit(api_settings, monkeypatch):
    """空列表不发请求（_get_client 前短路 → 无密钥也不报错）。"""
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    embedding = ApiEmbedding(api_settings.embedding)
    assert embedding.embed_documents([]).shape == (0, 0)


def test_mock_llm_stream_chunks_cover_text(offline_settings):
    """MockLLM 的 stream 与 complete 输出一致（Web SSE 依赖）。"""
    import mikasa.pipeline.prompts as prompts
    from mikasa.providers.llm import MockLLM

    llm = MockLLM()
    msg = prompts.build_user_message("什么是正则化？", [(1, "《n》", "正则化是防止过拟合的手段。")])
    complete = llm.complete([{"role": "user", "content": msg}], temperature=0, max_tokens=10)
    streamed = "".join(llm.stream([{"role": "user", "content": msg}], temperature=0, max_tokens=10))
    assert streamed == complete.text


def test_mock_llm_empty_input_refuses():
    import mikasa.pipeline.prompts as prompts
    from mikasa.providers.llm import MockLLM

    out = MockLLM().complete(
        [{"role": "user", "content": "没有标记的文本"}], temperature=0, max_tokens=8
    )
    assert out.text == prompts.REFUSAL_TEXT
