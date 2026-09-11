"""重排器提供方：api（SiliconFlow bge-reranker-v2-m3 免费）/ local（fastembed）/ none。

重排 = 用交叉编码器（query 与每个候选拼接打分）对召回结果精排，
相比双塔向量检索更准但更慢，因此只对 fusion 后的少量候选做
（本项目 top_n=3；评测里对比 rerank 开/关 的指标差异）。

SiliconFlow rerank 端点（非 Chat 兼容）用标准库 urllib 实现，
避免为单个端点引入 HTTP 客户端依赖。
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from typing import Any, Protocol, runtime_checkable

from mikasa.config.settings import RerankerConfig
from mikasa.errors import ConfigError, ProviderError
from mikasa.utils.logging import get_logger

logger = get_logger("providers.reranker")


@runtime_checkable
class RerankerProvider(Protocol):
    """统一重排接口：对候选按相关度降序返回其下标。"""

    model: str

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[int]:
        """返回 documents 中相关度最高的 top_n 个下标（降序）。top_n<=0 返回全部。"""
        ...


class NoReranker:
    """关闭重排：原样返回前 top_n 个候选（作为基线/离线）。"""

    model = "none"

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[int]:
        del query
        if top_n <= 0 or top_n >= len(documents):
            return list(range(len(documents)))
        return list(range(top_n))


class ApiReranker:
    """SiliconFlow rerank API（免费 bge-reranker-v2-m3）。"""

    def __init__(self, config: RerankerConfig) -> None:
        self._config = config
        self.model = config.model

    def _endpoint(self) -> str:
        base = (self._config.base_url or "").rstrip("/")
        if not base:
            raise ConfigError("Reranker base_url 未配置。")
        # base 可能是 …/v1，端点固定为 …/v1/rerank
        return f"{base}/rerank"

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[int]:
        api_key = self._config.api_key
        if api_key is None:
            raise ConfigError(
                "Reranker 密钥未配置（期望环境变量："
                f"{self._config.api_key_env}）。请在 .env 中填写。"
            )
        if not documents:
            return []
        body = json.dumps(
            {
                "model": self.model,
                "query": query,
                "documents": documents,
                "top_n": top_n if top_n > 0 else len(documents),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self._endpoint(),
            data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60.0) as resp:  # noqa: S310 - 仅连接配置的国内 API 端点
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise ProviderError(f"Reranker HTTP {exc.code}（{self.model}）：{detail}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"Reranker 网络错误（{self.model}）：{exc.reason}") from exc
        except (
            OSError,
            http.client.HTTPException,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ) as exc:
            # 读超时与坏响应体（网关 HTML 错误页）**不是** URLError 的子类：
            # urllib 只把"请求阶段"的 OSError 包成 URLError，`resp.read()` 阶段
            # 的 TimeoutError 与 json 解析失败会裸穿到上层（CLI 变 traceback，
            # Web 只剩"内部错误"）。2026-09-11 修复。
            raise ProviderError(
                f"Reranker 响应异常（{self.model}）：{type(exc).__name__}: {exc}"
            ) from exc
        results = data.get("results") or []
        # results 已是降序；只保留下标（回填排序在调用方完成）
        return [int(item["index"]) for item in results]


class LocalReranker:
    """fastembed 交叉编码器本地重排（CPU/GPU）。

    按 fastembed 早期公开 API（TextCrossEncoder + predict(sentence_pairs)）
    编写；**M4 实装验证：fastembed 0.8.0 已移除全部重排 API**（顶层无
    TextCrossEncoder/TextReranker，连 rerank 子模块都不存在）——本实现
    在 0.8 下不可用。M4 决策：reranker backend=none（ADR-0014 ②），代码
    保留为启用时的起点；届时按选定版本（回落 0.7.x 或换独立重排包）改
    import 与调用，不可直接改配置开启。
    """

    def __init__(self, config: RerankerConfig) -> None:
        self._config = config
        self.model = config.model
        self._encoder: Any = None

    def _get_encoder(self) -> Any:
        if self._encoder is not None:
            return self._encoder
        try:
            # fastembed ≥0.8 已移除重排 API，此导入在 0.8 下必然 ImportError
            # （启用本地重排前需先选型适配，见类 docstring 与 ADR-0014 ②）
            from fastembed import TextCrossEncoder  # type: ignore[attr-defined]
        except ImportError as exc:
            raise ConfigError(
                '本地重排不可用：fastembed 未安装（pip install -e ".[local]"）'
                "或其版本已移除重排 API（≥0.8 无 TextCrossEncoder，见 ADR-0014 ②）"
            ) from exc
        self._encoder = TextCrossEncoder(self.model)
        return self._encoder

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[int]:
        if not documents:
            return []
        encoder = self._get_encoder()
        try:
            scores = list(encoder.predict([(query, doc) for doc in documents]))
        except Exception as exc:
            raise ProviderError(f"本地重排失败：{type(exc).__name__}: {exc}") from exc
        ordered = sorted(range(len(documents)), key=lambda i: scores[i], reverse=True)
        return ordered if top_n <= 0 else ordered[:top_n]
