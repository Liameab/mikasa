"""词向量提供方：api（SiliconFlow bge-m3 免费）/ local（fastembed）/ none。

- api：OpenAI 兼容 /embeddings 端点（SiliconFlow/DashScope 等均支持）；
- local：fastembed（Qdrant 官方，onnxruntime 推理，CPU 可跑），
  query 侧加检索指令前缀（bge 系列官方建议）以提升召回；
- none：不提供向量（offline profile，配合 retrieval.dense_enabled=false）。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import numpy as np

from mikasa.config.settings import EmbeddingConfig
from mikasa.errors import ConfigError, ProviderError
from mikasa.utils.logging import get_logger

logger = get_logger("providers.embedding")


@runtime_checkable
class EmbeddingProvider(Protocol):
    """统一词向量接口。"""

    model: str

    @property
    def dim(self) -> int: ...

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        """批量嵌入（行序与输入一致），返回 float32 (n, dim)。"""
        ...

    def embed_query(self, text: str) -> np.ndarray:
        """嵌入单个查询（应用检索指令前缀）。"""
        ...


class ApiEmbedding:
    """OpenAI 兼容 /embeddings 实现（国内各家均兼容此协议）。"""

    def __init__(self, config: EmbeddingConfig) -> None:
        self._config = config
        self.model = config.model
        self._client: Any = None

    @property
    def dim(self) -> int:
        # bge-m3 固定 1024；由首条响应自动校正（构造时不联网）
        return 0

    def _get_client(self) -> Any:
        from openai import OpenAI

        if self._client is not None:
            return self._client
        api_key = self._config.api_key
        if api_key is None:
            raise ConfigError(
                "Embedding 密钥未配置（期望环境变量："
                f"{self._config.api_key_env}）。请在 .env 中填写（见 .env.example）。"
            )
        if not self._config.base_url:
            raise ConfigError("Embedding base_url 未配置。")
        self._client = OpenAI(
            base_url=self._config.base_url, api_key=api_key, timeout=60.0, max_retries=3
        )  # noqa: E501
        return self._client

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        client = self._get_client()
        try:
            resp = client.embeddings.create(model=self.model, input=texts)
        except Exception as exc:
            raise ProviderError(
                f"Embedding 调用失败（{self.model}）：{type(exc).__name__}: {exc}"
            ) from exc
        data = resp.data
        if len(data) != len(texts):
            raise ProviderError(
                f"Embedding 返回条数不一致：请求 {len(texts)} 条，收到 {len(data)} 条"
            )
        ordered = sorted(data, key=lambda item: item.index)
        return np.asarray([item.embedding for item in ordered], dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        full = self._config.query_prefix + text
        return self.embed_documents([full])[0]


class LocalFastEmbed:
    """fastembed 本地实现（CPU/GPU 均可，首次运行自动下载模型，之后离线）。

    fastembed 0.8 高层 API：TextEmbedding(model_name)，embed() 返回生成器。
    """

    def __init__(self, config: EmbeddingConfig) -> None:
        self._config = config
        self.model = config.model
        self._backend: Any = None

    def _get_backend(self) -> Any:
        if self._backend is not None:
            return self._backend
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise ConfigError(
                '本地嵌入需要 fastembed：pip install -e ".[local]"'
                "（首次运行会自动下载 bge-small-zh-v1.5，需联网一次）"
            ) from exc
        self._backend = TextEmbedding(self.model)
        return self._backend

    @property
    def dim(self) -> int:
        return 0  # 由首条向量形状确定（同 ApiEmbedding 语义）

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        vectors = list(self._get_backend().embed(texts))  # generator -> list
        return np.asarray([v.astype(np.float32) for v in vectors], dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        full = self._config.query_prefix + text
        return self.embed_documents([full])[0]


class NoEmbedding:
    """占位实现：embedding.backend=none 时返回空矩阵（dense 路自动关闭）。"""

    model = "none"

    @property
    def dim(self) -> int:
        return 0

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        del texts
        return np.zeros((0, 0), dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        del text
        return np.zeros((0,), dtype=np.float32)
