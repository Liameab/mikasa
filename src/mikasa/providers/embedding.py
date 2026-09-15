"""词向量提供方：api（SiliconFlow bge-m3 免费）/ local（fastembed）/ none。

- api：OpenAI 兼容 /embeddings 端点（SiliconFlow/DashScope 等均支持）；
- local：fastembed（Qdrant 官方，onnxruntime 推理，CPU 可跑），
  query 侧加检索指令前缀（bge 系列官方建议）以提升召回；
- none：不提供向量（offline profile，配合 retrieval.dense_enabled=false）。
"""

from __future__ import annotations

import os
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


# HuggingFace 国内只读镜像：huggingface.co 在国内常直连不上（实测 ConnectTimeout），
# 而"首次使用要下 ~100MB 向量模型"发生在**上传文档**的请求里——下载失败会让
# 入库整条链路挂掉，用户看到的是一句看不懂的网络异常（2026-09-15 用户实测）。
_HF_MIRROR = "https://hf-mirror.com"


def _switch_hf_to_mirror() -> bool:
    """把 HuggingFace 端点切到国内镜像（切换成功返回 True）。

    huggingface_hub 的 ENDPOINT 在 **import 时**从 HF_ENDPOINT 读定，之后各
    模块都通过 `constants.ENDPOINT` 取用——所以既要设环境变量，也要改这个
    常量本身，只设环境变量对已经 import 过的进程无效。
    HF_HUB_DISABLE_XET=1 是镜像的硬要求（镜像对 Xet 传输回 401，见 M4 备忘）。
    """
    try:
        from huggingface_hub import constants
    except ImportError:  # 没有 huggingface_hub 就无从切换（fastembed 会带上它）
        return False
    os.environ["HF_ENDPOINT"] = _HF_MIRROR
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    constants.ENDPOINT = _HF_MIRROR
    return True


def _download_hint(model: str, exc: Exception) -> ProviderError:
    """首次下载失败 → 能照做的中文说明（而不是裸 httpx traceback 冒到前端）。"""
    return ProviderError(
        f"本地向量模型（{model}）首次使用需要联网下载（约 100MB），本次下载失败："
        f"{type(exc).__name__}: {exc}\n"
        "  请检查网络后重试（重试时程序会先直连、失败自动改用 hf-mirror.com 镜像）。"
        "若始终连不上，可手动设环境变量 HF_ENDPOINT=https://hf-mirror.com 与 "
        "HF_HUB_DISABLE_XET=1 后重启。"
    )


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
        try:
            self._backend = TextEmbedding(self.model)
        except Exception as exc:  # 首次下载失败（网络）：换国内镜像再试一次
            if not _switch_hf_to_mirror():
                raise _download_hint(self.model, exc) from exc
            logger.warning("向量模型下载失败（%s），改用镜像 %s 重试", exc, _HF_MIRROR)
            try:
                self._backend = TextEmbedding(self.model)
            except Exception as mirror_exc:
                raise _download_hint(self.model, mirror_exc) from mirror_exc
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
