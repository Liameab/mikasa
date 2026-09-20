"""视觉模型提供方：图片 → 文本（M6 ② 拍照/图片转笔记，ADR-0027）。

**为什么单独一层、不复用 llm 的协议**：两者输入不是一回事（一个收文本消息、
一个收图片 + 提示词），而 `LLMProvider.complete()` 的 messages 标注是
`list[dict[str, str]]`——多模态的 content 数组过不了静态检查，硬塞还会撞上
MockLLM 对数组内容做 `.rpartition` 的崩溃点（offline 档的守卫）。这里用一个
小协议把"看图说话"隔离出来，ask 链路一行不动。

三条纪律：

  1. **未接入时必须如实报错**，且文案要能直接照做（去设置面板 / 装本机视觉
     模型）——绝不静默返回空文本让用户以为识别成功了；
  2. 图片以 **data URL** 内联发送（base64），**不落盘**：识图是一次性的，
     数据目录里不该留下用户没要求保存的照片（要留存是"原图随笔记保存"
     那条路的事，见 web/routers/documents.py 的 notes media 端点）；
  3. 上游失败一律翻成 `ProviderError`（app 层已有 502 处理器），超时用
     `vision.timeout_seconds` 单独可调——视觉推理比文本慢得多，借 ask 的
     默认值会在手机拍的大图上一片超时。
"""

from __future__ import annotations

import base64
from typing import Any, Protocol, runtime_checkable

from mikasa.config.settings import VisionConfig
from mikasa.errors import ConfigError, ProviderError
from mikasa.providers.llm import Completion, build_openai_client

# 支持的图片格式 → MIME（前端只会送压缩后的 JPEG，但接口不假设这一点）
_SUFFIX_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

VISION_SYSTEM = (
    "你是文档识别助手。把图片里的内容原样转写为 Markdown，"
    "不要解释、不要总结、不要添加图片里没有的内容。"
)

VISION_PROMPT = """\
请把这张图片里的内容转写为 Markdown 文本。要求：
- 逐字转写，保留标题层级、列表、编号与表格结构（表格用 Markdown 表格）；
- 数学公式用 LaTeX（行内 $...$、独立成行 $$...$$）；
- 看不清的字用「□」占位，**不要猜写**；
- 只输出转写结果本身，不要任何说明或结语。"""

# 未接入时的可操作提示（两个调用点共用同一份文案，避免说法漂移）
NO_VISION_HINT = (
    "还没有接入视觉模型（图片识别）。三种开法：① 打开右上角 ⚙ 设置 →「视觉模型」"
    "选一个（SiliconFlow 或本机 Ollama）；② 本机识图：先 `ollama pull qwen2.5vl:7b`，"
    "再在设置里选「本机」；③ 切到 api 档（config/profiles/api.yaml 已配好 SiliconFlow）。"
)


def sniff_image_mime(data: bytes) -> str | None:
    """按**魔数**判图片类型，认不出返回 None。

    不看扩展名也不看 Content-Type：两者都由客户端说了算，而模型要的是真的
    MIME（认错了要么被上游拒，要么把一段二进制当图片发出去）。覆盖本项目
    收的三种：JPEG / PNG / WebP。
    """
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def mime_for_suffix(suffix: str) -> str | None:
    """扩展名 → 允许的 MIME（不在白名单返回 None，调用方据此 415）。"""
    return _SUFFIX_MIME.get(suffix.lower())


# MIME → 规范扩展名（原图存档用：以**嗅探结果**为准，不信用户给的后缀）
_MIME_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def ext_for_mime(mime: str) -> str:
    """嗅探出的 MIME → 规范扩展名（认不出返回 .bin，正常流程到不了）。"""
    return _MIME_EXT.get(mime, ".bin")


@runtime_checkable
class VisionProvider(Protocol):
    """视觉模型的统一接口：一张图 + 一条提示词 → 一段文本。"""

    model: str

    def describe(self, image: bytes, *, mime: str, prompt: str = VISION_PROMPT) -> Completion:  # noqa: E501
        """识别图片内容。失败抛 ProviderError（未接入抛 ConfigError）。"""
        ...


class NoVision:
    """未接入视觉模型（backend: none）。

    存在的意义是把"没有"变成**一句能照做的话**：散在各个调用点自己判断
    "配置是不是空的"，漏判一处就是静默返回空文本——用户看到"识别完成"，
    笔记里却什么都没有。
    """

    model = ""

    def describe(self, image: bytes, *, mime: str, prompt: str = VISION_PROMPT) -> Completion:
        raise ConfigError(NO_VISION_HINT)


class OpenAICompatVision:
    """OpenAI 兼容的多模态端点（SiliconFlow Qwen-VL / Ollama 视觉模型）。

    客户端惰性创建（与 OpenAICompatLLM 同款理由：无密钥环境也要能构造对象、
    跑 doctor），复用 `build_openai_client` 的密钥解析。
    """

    def __init__(self, config: VisionConfig, *, max_retries: int = 1) -> None:
        """max_retries 默认 1（不是 llm 的 3）：单次识图是秒级到十几秒的
        大请求，重试三次会把用户按在"识别中"里等半分钟；失败一次再试一次
        已经覆盖了偶发抖动。"""
        self._config = config
        self.model = config.model
        self._max_retries = max_retries
        self._client: Any = None  # openai.OpenAI，惰性构造

    def describe(self, image: bytes, *, mime: str, prompt: str = VISION_PROMPT) -> Completion:
        if self._client is None:
            self._client = build_openai_client(self._config, max_retries=self._max_retries)
        data_url = f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}"
        # content 用数组形式：文本在前、图片在后（Qwen-VL 官方推荐顺序，
        # 也让"提示词约束转写格式"先于图片进入上下文）
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": VISION_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.0,  # 转写要的是稳定复现，不是文采
                max_tokens=self._config.max_tokens,
            )
        except Exception as exc:  # openai SDK 的鉴权/限流/网络/超时异常
            raise ProviderError(
                f"图片识别失败（{self.model}，{self._config.base_url}）："
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if not resp.choices:
            # 同 llm.complete：空候选要明确报出来，别让 IndexError 变成 500
            raise ProviderError(
                f"图片识别未返回任何候选（{self.model}，{self._config.base_url}）——"
                "可能是上游限流或响应被截断，稍后重试"
            )
        text = (resp.choices[0].message.content or "").strip()
        usage = getattr(resp, "usage", None)
        return Completion(
            text=text,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
        )
