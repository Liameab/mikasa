"""模型提供方统一入口（providers package）。

get_llm / get_embedding / get_reranker 依配置分发到各实现；
外部代码只依赖 Protocol 接口，不感知具体实现（可插拔设计，见 ADR-0003）。
"""

from __future__ import annotations

from mikasa.config.settings import EmbeddingConfig, LLMConfig, RerankerConfig, VisionConfig
from mikasa.errors import ConfigError
from mikasa.providers.embedding import ApiEmbedding, EmbeddingProvider, LocalFastEmbed, NoEmbedding
from mikasa.providers.llm import Completion, LLMProvider, MockLLM, OpenAICompatLLM
from mikasa.providers.reranker import ApiReranker, LocalReranker, NoReranker, RerankerProvider
from mikasa.providers.vision import NoVision, OpenAICompatVision, VisionProvider

__all__ = [
    "ApiEmbedding",
    "ApiReranker",
    "Completion",
    "EmbeddingProvider",
    "LLMProvider",
    "LocalFastEmbed",
    "LocalReranker",
    "MockLLM",
    "NoEmbedding",
    "NoReranker",
    "NoVision",
    "OpenAICompatLLM",
    "OpenAICompatVision",
    "RerankerProvider",
    "VisionProvider",
    "get_embedding",
    "get_llm",
    "get_reranker",
    "get_vision",
]


def get_llm(config: LLMConfig) -> LLMProvider:
    """依据配置构造 LLM 提供方实例（mock / api / local）。"""
    if config.backend == "mock":
        return MockLLM(config)
    if config.backend in ("api", "local"):
        return OpenAICompatLLM(config)
    raise ConfigError(f"未知 LLM backend：{config.backend}")


def get_embedding(config: EmbeddingConfig) -> EmbeddingProvider:
    """依据配置构造词向量提供方（none / api / local）。"""
    if config.backend == "none":
        return NoEmbedding()
    if config.backend == "api":
        return ApiEmbedding(config)
    if config.backend == "local":
        return LocalFastEmbed(config)
    raise ConfigError(f"未知 Embedding backend：{config.backend}")


def get_vision(config: VisionConfig) -> VisionProvider:
    """依据配置构造视觉提供方实例（none / api / local）。"""
    if config.backend == "none":
        return NoVision()
    if config.backend in ("api", "local"):
        return OpenAICompatVision(config)
    raise ConfigError(f"未知 Vision backend：{config.backend}")


def get_reranker(config: RerankerConfig) -> RerankerProvider:
    """依据配置构造重排器（none / api / local）。"""
    if config.backend == "none":
        return NoReranker()
    if config.backend == "api":
        return ApiReranker(config)
    if config.backend == "local":
        return LocalReranker(config)
    raise ConfigError(f"未知 Reranker backend：{config.backend}")
