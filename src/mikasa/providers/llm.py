"""LLM 提供方：Protocol + 三实现（api / local / mock）。

- api/local 共用 OpenAICompatLLM：所有国产服务与 Ollama 都兼容
  OpenAI Chat Completions 协议，一套客户端全打通（ADR-0003）；
- mock 为 MockLLM：确定性输出、零密钥，用于离线演示、单元测试与 CI，
  实现检索栈/引用协议的完整链路（回答质量由真实 LLM 保证）。

complete() 返回 Completion（文本 + token 用量）：qa_messages 的成本
核算与评测报告都依赖用量字段，因此放进协议而不是旁路回调。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

from mikasa.config.settings import LLMConfig, VisionConfig
from mikasa.errors import ConfigError, ProviderError
from mikasa.pipeline.prompts import REFUSAL_TEXT, SECTION_QUESTION
from mikasa.utils.logging import get_logger

logger = get_logger("providers.llm")


@dataclass(frozen=True)
class Completion:
    """一次完整生成的产物：文本 + 用量（供成本核算与评测统计）。"""

    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    def __str__(self) -> str:  # 保持与纯字符串无感的语义
        return self.text


@runtime_checkable
class LLMProvider(Protocol):
    """生成端模型的统一接口。"""

    model: str

    def complete(
        self, messages: list[dict[str, str]], *, temperature: float, max_tokens: int
    ) -> Completion:  # noqa: E501
        """非流式补全：返回文本与 token 用量。"""
        ...

    def stream(
        self, messages: list[dict[str, str]], *, temperature: float, max_tokens: int
    ) -> Iterator[str]:
        """流式补全：按 token 增量产出文本（用于 Web SSE）。"""
        ...


# ---------------------------------------------------------------------------
# OpenAI 兼容实现（api / local 共用）
# ---------------------------------------------------------------------------

# Ollama /v1 端点不校验 Authorization 头，只要求非空；openai SDK 构造期
# 强制 api_key 非空。local 后端因此用固定占位符（见 ADR-0014）。
_LOCAL_API_KEY = "ollama"


def normalize_base_url(url: str) -> str:
    """把 loopback 主机名 `localhost` 规范成 `127.0.0.1`（其余原样返回）。

    **为什么必须做**（2026-09-20 用户实测报障，根因在打包版里才犯）：
    Windows 上 `localhost` 先解析到 IPv6 的 `::1`，而 Ollama 默认只监听
    `127.0.0.1`；httpx 卡在 `::1` 上直到整个请求超时才报错，**不回退到 IPv4**
    ——表现是"测试连接永远 20 秒超时 / 本地模型一问就 APITimeoutError"，而
    同一个进程里 urllib 走同一个 `localhost` 只需 2.2 秒（地址选择是 httpx 这
    一层的事）。实测同一份配置：

      http://localhost:11434/v1   → 20.1s，APITimeoutError（每次复现）
      http://127.0.0.1:11434/v1   →  0.5s，ok

    只改主机名恰为 `localhost` 的地址：远程地址（api 后端）一个字都不碰。
    用户若真把服务只绑在 IPv6 上，写 `[::1]` 即可绕过这条规范化。
    """
    try:
        parts = urlsplit(url)
        if (parts.hostname or "").lower() != "localhost":
            return url
        port = parts.port  # 端口非法会抛 ValueError → 交给下面兜底
    except ValueError:
        return url
    netloc = "127.0.0.1" if port is None else f"127.0.0.1:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def build_openai_client(config: LLMConfig | VisionConfig, *, max_retries: int) -> Any:
    """按配置构造 OpenAI 兼容客户端（llm 与 vision 共用这段构造逻辑）。

    密钥语义随后端分化：api 无密钥是配置错误（早失败）；local（Ollama）
    免密钥是产品形态，放行并注入占位 key——放行点必须在 provider 层
    （配置层放行会让 api 忘填密钥也被静默放行，见 ADR-0014）。

    抽成独立函数是 M6 ② 的产物：视觉 Provider 需要同样的客户端，而复制一份
    密钥解析必然在某次改动后与这里漂移（一处改了、另一处没改，表现为
    "识图能用、问答不能用"这种最难查的分裂）。

    base_url 先过 `normalize_base_url`：这是 local 档（Ollama）与一切"本机
    服务"的必经之路，`localhost` 会在这条链路上稳定超时（见该函数说明）。
    """
    from openai import OpenAI  # 延迟导入：保持 import mikasa 轻量

    api_key = config.api_key
    if api_key is None:
        if config.backend == "local":
            api_key = _LOCAL_API_KEY  # local：免密钥放行（Ollama 不校验）
        else:
            raise ConfigError(
                "未配置 API 密钥。请在项目根目录创建 .env 文件并填入密钥"
                "（参考 .env.example），或改用 --profile offline 体验零密钥。"
                f"（期望环境变量：{config.api_key_env}）"
            )
    if not config.base_url:
        raise ConfigError("base_url 未配置：无法确定 API 端点。")

    return OpenAI(
        base_url=normalize_base_url(config.base_url),
        api_key=api_key,
        timeout=config.timeout_seconds,
        max_retries=max_retries,
    )


class OpenAICompatLLM:
    """OpenAI Chat Completions 兼容客户端（DeepSeek / SiliconFlow / Ollama 等）。

    客户端惰性创建：只有真正调用时才校验密钥并构造 HTTP 客户端，
    使 doctor / 离线流程在无密钥环境下依然可用。

    密钥语义随后端分化：api 无密钥是配置错误（早失败）；local（Ollama）
    免密钥是产品形态，放行并注入占位 key——放行点必须在 provider 层
    （配置层放行会让 api 忘填密钥也被静默放行，见 ADR-0014）。
    """

    def __init__(self, config: LLMConfig, *, max_retries: int = 3) -> None:
        """max_retries 默认 3（正式问答链路的既有行为）；设置面板的"测试连接"
        传 0——它配 20s 超时，本意是一次硬上限，3 次重试会拖成 60s 的等待。"""
        self._config = config
        self.model = config.model
        self._max_retries = max_retries
        self._client: Any = None  # openai.OpenAI，惰性构造

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = build_openai_client(self._config, max_retries=self._max_retries)
        return self._client

    def _create(
        self, messages: list[dict[str, str]], *, temperature: float, max_tokens: int, stream: bool
    ) -> Any:  # noqa: E501
        """统一入口：把 openai SDK 的异常翻译为可读的 ProviderError。"""
        client = self._get_client()
        try:
            return client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=stream,
            )
        except Exception as exc:  # openai 抛出的鉴权/限流/网络/超时异常
            raise ProviderError(
                f"LLM 调用失败（{self.model}，{self._config.base_url}）："
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def complete(
        self, messages: list[dict[str, str]], *, temperature: float, max_tokens: int
    ) -> Completion:  # noqa: E501
        response = self._create(
            messages, temperature=temperature, max_tokens=max_tokens, stream=False
        )
        usage = getattr(response, "usage", None)
        if not response.choices:
            # 上游返回空候选（网关/兼容实现在限流或截断时会这样）：
            # 明确报"没有候选"，而不是让 IndexError 冒到用户面前（2026-09-20 审查）
            raise ProviderError(
                f"LLM 未返回任何候选（{self.model}，{self._config.base_url}）——"
                "可能是上游限流或响应被截断，稍后重试"
            )
        return Completion(
            text=response.choices[0].message.content or "",
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
        )

    def stream(
        self, messages: list[dict[str, str]], *, temperature: float, max_tokens: int
    ) -> Iterator[str]:  # noqa: E501
        response = self._create(
            messages, temperature=temperature, max_tokens=max_tokens, stream=True
        )
        return self._iter_stream(response)

    def _iter_stream(self, response: Any) -> Iterator[str]:
        """把流式迭代也纳入异常翻译。

        `_create` 只罩得住"建流"那一次调用；**迭代期**的读超时/连接重置
        （长回答很常见）原先原样抛出 → 上层判不出 ZhiwenError → 用户只看到
        "服务器内部错误"，而非流式路径同样失败时却给得出"模型/地址/原因"
        （2026-09-20 审查实测）。空候选帧同理（部分网关会在末尾补一帧空 choices）。
        """
        try:
            for chunk in response:
                if not chunk.choices:
                    continue  # 心跳/收尾帧：没有候选就跳过，不算失败
                yield chunk.choices[0].delta.content or ""
        except ProviderError:
            raise
        except Exception as exc:  # openai SDK 的传输类异常
            raise ProviderError(
                f"LLM 流式生成中断（{self.model}，{self._config.base_url}）："
                f"{type(exc).__name__}: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Mock 实现（离线 / 测试 / CI）
# ---------------------------------------------------------------------------


class MockLLM:
    """确定性模拟模型：演示检索→引用→解析全链路，但不产生真实语义。

    行为：
      - 解析用户消息【资料片段】区各段与问题；
      - 问题与资料出现足够长的"连续共同片段"（见 min_run）→ 以片段首句
        组织回答并带 [n] 引用；
      - 否则输出统一拒答句式（与真实 LLM 相同的 REFUSAL_TEXT）。

    判定为什么是"最长公共连续子串 ≥ min_run"而不是字符重叠率（真实教训，
    见 docs/limitations-and-failures.md"MockLLM 的启发式判定"）：
      按字符重叠率判定时，问题里的通用字（一/学/做/在…）与任何语料都能
      凑够阈值——"如何在一周内学会做菠萝包"被误判为有据可答；
      连续 ≥4 字的相同片段只能是主题专名/实义短语（"正则化""防止过拟合"），
      通用字拼不出 4 连。它仍是启发式：真实拒答语义由系统提示词约束实现。
    """

    model = "mock-zh-1"
    # 有据判定的最短连续同现长度：问题与资料出现 ≥min_run 字完全连续的
    # 相同片段才算"资料含有答案素材"
    min_run = 4

    def __init__(self, config: LLMConfig | None = None) -> None:
        if config is not None:
            self.model = config.model

    # ---- 用户消息的轻量解析（与 prompts.build_user_message 严格对偶） ----
    @staticmethod
    def _parse_user_message(content: str) -> tuple[list[tuple[int, str]], str | None]:
        """还原 [(编号, 正文)] 与问题。仅 mock 使用，真实模型由提示词约束。"""
        sources: list[tuple[int, str]] = []
        question: str | None = None
        current_marker: int | None = None
        parts: list[str] = []

        def flush() -> None:
            nonlocal current_marker, parts
            if current_marker is not None and parts:
                sources.append((current_marker, "\n".join(parts).strip()))
            current_marker = None
            parts = []

        head, _, tail = content.rpartition(SECTION_QUESTION)
        if tail.strip():
            question = tail.strip()
        for line in head.splitlines():
            line = line.strip()
            src_match = re.match(r"^【资料(\d+)】(?:来源：[^\n]*)?$", line)
            if src_match:
                flush()
                current_marker = int(src_match.group(1))
            elif current_marker is not None:
                parts.append(line)
        flush()
        return sources, question

    def _answer(self, question: str, sources: list[tuple[int, str]]) -> str:
        if not sources:
            return REFUSAL_TEXT
        scored = sorted(
            ((self._run(question, text), -marker, text, marker) for marker, text in sources),
            key=lambda item: item[0],
            reverse=True,
        )
        best_run, _, best_text, best_marker = scored[0]
        if best_run < self.min_run:
            return REFUSAL_TEXT

        # 取命中片段的第一句组织"引用式回答"：演示引用协议，不编造语义
        first_sentence = re.split(r"(?<=[。！？!?])\s*", best_text.strip(), maxsplit=1)[0]
        snippet = first_sentence[:120]
        cited: list[int] = [best_marker]
        for run, _, _text, marker in scored[1:]:
            if run >= self.min_run and len(cited) < 3:
                cited.append(marker)
        body = f"根据资料，与“{question}”相关的说明如下：{snippet} "
        body += "".join(f"[{m}]" for m in sorted(cited))
        return body

    @staticmethod
    def _run(question: str, text: str) -> int:
        """问题与文本的最长公共连续子串长度（< min_run 一律视为 0 级）。

        从长到短探测：找到的第一个长度即最长。问题/资料都在几百字符内，
        平方级窗口探测足够快（每段 O(len(q)²) 次 C 级子串查找）。
        """
        upper = min(len(question), len(text))
        for length in range(upper, 3, -1):
            for start in range(0, len(question) - length + 1):
                if question[start : start + length] in text:
                    return length
        return 0

    def complete(
        self, messages: list[dict[str, str]], *, temperature: float, max_tokens: int
    ) -> Completion:  # noqa: E501
        del temperature, max_tokens  # mock 输出确定性，忽略采样参数
        question: str | None = None
        for message in reversed(messages):
            if message["role"] == "user":
                sources, question = self._parse_user_message(message["content"])
                break
        else:
            sources = []
        if question is None:
            return Completion(REFUSAL_TEXT)
        text = self._answer(question, sources)
        logger.debug(
            "MockLLM: %r -> %s", question[:40], "refused" if text == REFUSAL_TEXT else "answered"
        )  # noqa: E501
        return Completion(text)

    def stream(
        self, messages: list[dict[str, str]], *, temperature: float, max_tokens: int
    ) -> Iterator[str]:  # noqa: E501
        text = self.complete(messages, temperature=temperature, max_tokens=max_tokens).text
        for i in range(0, len(text), 24):
            yield text[i : i + 24]
