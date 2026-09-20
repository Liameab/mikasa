"""Ollama 原生 API：模型列表探测 + **本地档的生成端**。

与 providers/llm.py 的分工：api 档走 OpenAI 兼容端点（云端服务商）；
local 档**走原生** `/api/chat`（见下方 OllamaNativeLLM 的取舍说明），
而"本机有哪些模型"只有原生 API 能答（`GET {根}/api/tags`），两套端点不同根。

这里原本住在 cli/__init__.py（doctor 用）；Web 设置面板也要列模型，
按"被 web 依赖的代码不能住在 cli 里"的口径搬来 providers——
cli 侧保留同名下划线别名，既有 monkeypatch 单测零改动。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

from mikasa.config.settings import LLMConfig
from mikasa.errors import ConfigError, ProviderError
from mikasa.providers.llm import Completion, normalize_base_url
from mikasa.utils.logging import get_logger

logger = get_logger("providers.ollama")

# 答案为空但推理有内容时的提示（2026-09-20 实测：qwen3:8b + max_tokens=400 时
# 617 字推理把预算吃光，content 返回空串——用户看到的是"转了半天什么都没有"）
_EMPTY_ANSWER_HINT = (
    "Ollama 没有返回答案：推理（thinking）把输出预算用光了。"
    "到右上角 ⚙ →「模型」把「思考模式」关掉，或把 max_tokens 调大。"
)


def ollama_api_root(base_url: str) -> str:
    """OpenAI 兼容 base_url（…/v1）→ Ollama 原生 API 根（…/api）。

    单测关注点：/v1 去除、容忍尾斜杠、非 /v1 结尾直接追加。
    """
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    return f"{root}/api"


def fetch_ollama_tags(base_url: str) -> list[str]:
    """GET {根}/api/tags → 本机已拉取模型名列表。

    失败抛 RuntimeError：文案带可执行下一步——Windows 下 Ollama 若只绑定
    IPv6 回环（localhost→::1 连不上），给 OLLAMA_BASE_URL 逃生口指引
    （http://127.0.0.1:11434/v1）。3s 超时：本地服务探测不等网络。
    """
    import json
    import urllib.error
    import urllib.request

    url = f"{ollama_api_root(base_url)}/tags"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        # 连接拒绝/域名解析失败等（HTTPError 是 URLError 子类，一并覆盖）
        raise RuntimeError(
            f"Ollama 服务不可达（{url}）：{exc.reason}。\n"
            "  请先启动 Ollama（退出系统托盘图标后重启应用）再试；若 Windows 下\n"
            "  localhost 连不上（服务仅绑 IPv6 回环），可设环境变量\n"
            "  OLLAMA_BASE_URL=http://127.0.0.1:11434/v1 后重跑"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - 读超时/HTTP/JSON 解析等统一翻译为可执行文案
        raise RuntimeError(f"Ollama 服务响应异常（{url}）：{exc}") from exc
    return [str(model["name"]) for model in data.get("models", [])]


class OllamaNativeLLM:
    """local 档的生成端：走 Ollama **原生** `/api/chat`（2026-09-20 起）。

    **为什么不再走 OpenAI 兼容面**：实测（Ollama 0.34.2 + qwen3:8b，同一个问题）
    `/v1/chat/completions` **完全不认** `think` 与 `num_ctx` —— 而这两个恰好是本地档
    最要紧的旋钮：

      - **思考模式**：`think: true` 14.2s、617 字推理、**答案 0 字**（400 token 的
        输出预算被推理吃光）；`think: false` **1.4s** 给出 45 字答案——快 10 倍。
        兼容面忽略该参数，所以本地档此前永远在"先想半天"，而且想完可能没答案。
      - **上下文长度**：`options.num_ctx` 只在原生接口生效（实测 8192 生效）。

    **为什么用 stdlib urllib 而不是 openai SDK**：原生接口的返回形状 SDK 不认
    （流式是 NDJSON、思考内容在 `message.thinking`）；本地服务无密钥无代理，
    urllib 足够，也顺带避开 httpx 那套地址选择的老坑（见 normalize_base_url）。
    """

    def __init__(self, config: LLMConfig, *, timeout: float | None = None) -> None:
        self._config = config
        self.model = config.model
        self._timeout = timeout if timeout is not None else config.timeout_seconds

    # ---- 内部：请求组装与发送 ----

    def _root(self) -> str:
        base = normalize_base_url(self._config.base_url or "")
        if not base:
            raise ConfigError("base_url 未配置：无法确定 Ollama 端点。")
        return ollama_api_root(base)

    def _body(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        stream: bool,
    ) -> dict[str, Any]:
        """请求体：思考模式与上下文长度只在**显式配置**时才带上。

        不写 `think` 时由模型自己决定（qwen3 默认思考）；不写 `num_ctx` 时用
        Ollama 的默认上下文（本机实测 4096）——"没配置"与"关掉"是两件事。
        """
        options: dict[str, Any] = {"temperature": temperature, "num_predict": max_tokens}
        if self._config.num_ctx:
            options["num_ctx"] = self._config.num_ctx
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "options": options,
        }
        if self._config.think is not None:
            body["think"] = self._config.think
        return body

    def _open(self, body: dict[str, Any]):
        url = f"{self._root()}/chat"
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mikasa/0.1 (ollama-native)",
            },
        )
        try:
            return urllib.request.urlopen(request, timeout=self._timeout)
        except urllib.error.HTTPError as exc:
            detail = f"HTTP {exc.code} {exc.reason}"
            raise ProviderError(
                f"LLM 调用失败（{self.model}，{self._config.base_url}）：{detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ProviderError(
                f"LLM 调用失败（{self.model}，{self._config.base_url}）：{reason}"
            ) from exc

    @staticmethod
    def _empty_answer_message(thinking: str) -> str:
        return f"{_EMPTY_ANSWER_HINT}（本次推理 {len(thinking)} 字）"

    # ---- 协议实现 ----

    def complete(
        self, messages: list[dict[str, str]], *, temperature: float, max_tokens: int
    ) -> Completion:
        body = self._body(messages, temperature=temperature, max_tokens=max_tokens, stream=False)
        with self._open(body) as resp:
            try:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            except json.JSONDecodeError as exc:
                raise ProviderError(f"Ollama 返回了无法解析的响应：{exc}") from exc
        message = data.get("message") or {}
        text = (message.get("content") or "").strip()
        thinking = (message.get("thinking") or "").strip()
        if not text and thinking:
            # 思考把预算吃光：**如实报错**，别返回空串让界面显示"答完了但什么都没有"
            raise ProviderError(self._empty_answer_message(thinking))
        return Completion(
            text=text,
            prompt_tokens=data.get("prompt_eval_count"),
            completion_tokens=data.get("eval_count"),
        )

    def stream(
        self, messages: list[dict[str, str]], *, temperature: float, max_tokens: int
    ) -> Iterator[str]:
        """NDJSON 流：只把 `message.content` 当答案增量；`message.thinking` 计数留证。"""
        body = self._body(messages, temperature=temperature, max_tokens=max_tokens, stream=True)
        thinking_chars = 0
        answered = 0
        with self._open(body) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:  # 半行/噪声：跳过而不是中断整段回答
                    continue
                if chunk.get("error"):
                    raise ProviderError(
                        f"LLM 调用失败（{self.model}，{self._config.base_url}）：{chunk['error']}"
                    )
                message = chunk.get("message") or {}
                thinking_chars += len(message.get("thinking") or "")
                piece = message.get("content") or ""
                if piece:
                    answered += len(piece)
                    yield piece
                if chunk.get("done"):
                    break
        if answered or not thinking_chars:
            return
        # 流走完了却一个字都没产出、而推理有内容 → 同一句可照做的提示
        # （生成器末尾抛错会让 SSE 收尾成 error 帧，与"答不出来"的既有契约一致）
        raise ProviderError(self._empty_answer_message("x" * thinking_chars))
