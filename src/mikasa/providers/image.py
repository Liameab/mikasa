"""图像生成提供方：文字 → 图片（文生图）。

**为什么单独一层**（与 vision.py 同一个理由）：输入输出形状都不一样——
LLM 出文本、视觉收图、这里出图字节。塞进 LLMProvider 的 Protocol 会让
"离线档零外部调用"的守卫少一个明确的开关点，MockLLM 那些假实现也全得改。

三条纪律：

  1. **未接入时如实报错**（同 vision）：文案要能直接照做，绝不静默返回空图；
  2. **拿到的是字节，不是 URL**：上游给的图片链接**只有 1 小时有效期**
     （SiliconFlow 文档明说），必须当场下载落盘——把 URL 原样写进回答，
     用户一小时后回来看到的就是一张裂图；
  3. 上游失败一律翻成 `ProviderError`（app 层 502）；本地配置问题（未接入、
     密钥缺失、主机闸门拒绝）是 `ConfigError`（400）。

**出网两条路都要过闸门**（utils/net.py 的 reject_reason，与论文下载器共用）：
发请求的目标（用户填的 base_url）与**下载图片的目标**（上游返回的 URL，
算半可信输入）——后者尤其不能省：上游被攻破或返回内网地址时，那是一个
现成的内网探测原语。
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from mikasa.config.settings import ImageConfig
from mikasa.errors import ConfigError, ProviderError
from mikasa.providers.llm import normalize_base_url
from mikasa.providers.vision import ext_for_mime, sniff_image_mime  # noqa: F401 - ext 供调用方用
from mikasa.utils.logging import get_logger
from mikasa.utils.net import reject_reason

logger = get_logger("providers.image")

# 未接入时的可操作提示（端点与面板共用一份文案，避免说法漂移）
NO_IMAGE_HINT = (
    "还没有接入图像生成（文生图）。开法：打开右上角 ⚙ 设置 →「图像生成」"
    "选一个来源（SiliconFlow / 自定义）→ 填好模型名 → 保存即可。"
    "（云端出图按张计费或走免费额度，提示词会发到该服务商。）"
)

# 单张图上限：上游返回 HTML 错误页/巨物时不要把内存与磁盘吃掉
_MAX_IMAGE_BYTES = 20 * 1024 * 1024

# 指向本机出图服务（Automatic1111/ComfyUI 的 OpenAI 兼容壳等）时的占位钥匙：
# 这类服务不校验 Authorization，但**不能不带这个头**（有的实现直接 400）。
# 与 llm.py 的 _LOCAL_API_KEY 同款做法。
_LOCAL_PLACEHOLDER_KEY = "mikasa-local"


@dataclass(frozen=True)
class GeneratedImage:
    """一次出图的结果：**字节**（不是 URL，见模块头第 2 条）。"""

    data: bytes
    mime: str
    model: str
    size: str
    source_url: str | None = None


@runtime_checkable
class ImageProvider(Protocol):
    """统一出图接口：一条提示词 → 一张图的字节。"""

    model: str

    def generate(self, prompt: str, *, size: str | None = None) -> GeneratedImage:
        """生成图片。失败抛 ProviderError（未接入抛 ConfigError）。"""
        ...


class NoImage:
    """未接入图像生成（backend: none）。

    与 NoVision 的存在意义相同：把"没有"变成**一句能照做的话**，
    而不是让每个调用点各自判断"配置是不是空的"（漏判一处就是静默空图）。
    """

    model = ""

    def generate(self, prompt: str, *, size: str | None = None) -> GeneratedImage:
        del prompt, size
        raise ConfigError(NO_IMAGE_HINT)


def _guard_url(url: str, what: str) -> None:
    """出网闸门：内网/保留地址一律拒绝（回环放行——本机服务是合法用途）。"""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"{what}不是一个可用的 http(s) 地址：{url}")
    reason = reject_reason(
        parts.hostname,
        parts.port or (443 if parts.scheme == "https" else 80),
        allow_loopback=True,
        strict_resolution=False,
    )
    if reason:
        raise ConfigError(f"{what}被拒绝：{reason}")


def _post_json(url: str, payload: dict[str, Any], api_key: str, timeout: float) -> dict[str, Any]:
    """POST JSON 并解析响应（stdlib urllib：出图端点的形状不是 OpenAI 的，
    走 openai SDK 反而要跟它的参数校验打架，同 ollama.py 的取舍）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise ProviderError(f"图像生成调用失败（HTTP {exc.code}）：{detail}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ProviderError(f"图像生成调用失败：{type(exc).__name__}: {exc}") from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError(f"图像生成返回的不是 JSON（前 120 字节：{raw[:120]!r}）") from exc
    if not isinstance(parsed, dict):
        raise ProviderError(f"图像生成返回的 JSON 形状不认识：{type(parsed).__name__}")
    return parsed


def _first_image_entry(payload: dict[str, Any]) -> dict[str, Any] | None:
    """从响应里取第一张图。

    **两种形状都要认**：SiliconFlow 是 `{"images": [{"url": …}]}`，OpenAI /
    DALL·E 那套是 `{"data": [{"url"|"b64_json": …}]}`。只认一种的话，
    换一家服务商就是"调用成功但没有图片"。
    """
    for key in ("images", "data"):
        items = payload.get(key)
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
    return None


def _fetch_image_bytes(url: str, timeout: float) -> bytes:
    """下载上游给的图片链接（**带闸门**，见模块头）。"""
    _guard_url(url, "上游返回的图片地址")
    request = urllib.request.Request(url, headers={"User-Agent": "Mikasa"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = resp.read(_MAX_IMAGE_BYTES + 1)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ProviderError(f"下载生成的图片失败：{type(exc).__name__}: {exc}") from exc
    if len(data) > _MAX_IMAGE_BYTES:
        raise ProviderError(f"生成的图片超过 {_MAX_IMAGE_BYTES // 1048576} MB 上限，已丢弃。")
    return data


class OpenAICompatImage:
    """OpenAI 兼容风格的出图端点（实测 SiliconFlow；同形状的服务通用）。

    请求体按 SiliconFlow 的规矩来：`image_size`（**必填**）+ `batch_size`
    （固定 1；它只对 Kolors 系列生效）。响应两种形状都认（见
    `_first_image_entry`）。
    """

    def __init__(self, config: ImageConfig) -> None:
        self._config = config
        self.model = config.model

    def _effective_key(self) -> str:
        """本次请求用的密钥：配了就用；**没配槽位**=本机免密钥服务；配了槽位没值=报错。

        "没配槽位"与"配了但没填"是两件事——前者是"我知道这个服务不要密钥"
        （配置文件里写 `api_key_env:` 空着），后者是"忘了填"，必须如实报错。
        """
        key = self._config.api_key
        if key:
            return key
        if not (self._config.api_key_env or "").strip():
            return _LOCAL_PLACEHOLDER_KEY
        raise ConfigError(
            f"图像生成的密钥未配置（期望环境变量：{self._config.api_key_env}）。"
            "在设置面板「图像生成」段填入密钥即可；本机免密钥服务请把 api_key_env 留空。"
        )

    def _base_url(self) -> str:
        if not self._config.base_url:
            raise ConfigError("图像生成的 base_url 未配置。")
        # 与 llm/embedding 同一处坑：`localhost` 在 Windows 上先解析到 ::1，
        # 而本机服务多半只听 127.0.0.1（同 llm.normalize_base_url）
        return normalize_base_url(self._config.base_url).rstrip("/")

    def generate(self, prompt: str, *, size: str | None = None) -> GeneratedImage:
        prompt = prompt.strip()
        if not prompt:
            raise ConfigError("提示词不能为空。")
        api_key = self._effective_key()
        base = self._base_url()
        _guard_url(base, "图像生成地址")
        use_size = (size or self._config.size).strip() or self._config.size
        payload = {
            "model": self.model,
            "prompt": prompt,
            "image_size": use_size,
            "batch_size": 1,
        }
        body = _post_json(
            f"{base}/images/generations", payload, api_key, self._config.timeout_seconds
        )
        entry = _first_image_entry(body)
        if entry is None:
            raise ProviderError(
                f"图像生成返回里没有图片：{json.dumps(body, ensure_ascii=False)[:200]}"
            )

        source_url: str | None = None
        raw: bytes | None = None
        b64 = entry.get("b64_json") or entry.get("image_base64")
        url = entry.get("url")
        if isinstance(b64, str) and b64:
            try:
                raw = base64.b64decode(b64, validate=True)
            except (ValueError, TypeError) as exc:
                raise ProviderError(f"图像生成返回的 base64 无法解码：{exc}") from exc
        elif isinstance(url, str) and url:
            source_url = url
            raw = _fetch_image_bytes(url, self._config.timeout_seconds)
        if not raw:
            raise ProviderError("图像生成返回里既没有 url 也没有 base64 图片。")

        mime = sniff_image_mime(raw)
        if mime is None:
            # 认不出格式 = 上游给了段二进制/HTML，落盘只会污染数据目录
            raise ProviderError("图像生成返回的字节不像图片（认不出格式），已丢弃。")
        return GeneratedImage(
            data=raw, mime=mime, model=self.model, size=use_size, source_url=source_url
        )

    def list_models(self, timeout: float = 20.0) -> list[str]:
        """读 `/models`（设置面板的"测试连接"用：几百字节、不烧出图额度）。

        出图是真金白银的调用，拿它当连通性测试不合适；列出模型足以证明
        "地址通、密钥对"。返回空列表表示该服务没有这个端点（不影响出图）。
        """
        api_key = self._effective_key()
        base = self._base_url()
        _guard_url(base, "图像生成地址")
        request = urllib.request.Request(
            f"{base}/models",
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": "Mikasa"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                parsed = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 405):
                logger.info("该服务没有 /models 端点（HTTP %s），跳过模型列表检查", exc.code)
                return []
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise ProviderError(f"读取模型列表失败（HTTP {exc.code}）：{detail}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ProviderError(f"读取模型列表失败：{type(exc).__name__}: {exc}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError(f"模型列表返回的不是 JSON：{exc}") from exc
        items = parsed.get("data") if isinstance(parsed, dict) else None
        if not isinstance(items, list):
            return []
        names: list[str] = []
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                names.append(item["id"])
        return names
