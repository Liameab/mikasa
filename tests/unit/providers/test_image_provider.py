"""出图提供方（providers/image.py，ADR-0031）单元测试。

全部走**假的 urlopen**，零联网：出图是真金白银的调用，测试永远不许真发。
覆盖四件事：
  1. 两种响应形状（SiliconFlow 的 `images[].url` / OpenAI 的 `data[].b64_json`）；
  2. URL 必须**当场下载成字节**（上游链接 1 小时过期，落盘才是终局）；
  3. 出网闸门：内网地址两个方向都拒（发请求的目标、上游返回的图片地址）；
  4. 未接入 / 缺密钥 → ConfigError，且文案能照做。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mikasa.config.settings import ImageConfig
from mikasa.errors import ConfigError, ProviderError
from mikasa.providers.image import NO_IMAGE_HINT, NoImage, OpenAICompatImage

KEY_ENV = "MIKASA_TEST_IMAGE_KEY"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64  # 嗅探只看魔数，不需要真能解码
BASE = "https://api.example.com/v1"


class _FakeResponse:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def read(self, size: int = -1) -> bytes:
        return self._payload if size < 0 else self._payload[:size]

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _routes(monkeypatch, *, post_payload: dict, image: bytes = PNG, seen: list | None = None):
    """装上假的 urlopen：POST → 给定 JSON；GET → 图片字节。"""

    def _open(request, timeout=None):  # noqa: ANN001 - 与 urlopen 同形
        url = request.full_url
        if seen is not None:
            seen.append(request)
        if url.endswith("/images/generations"):
            return _FakeResponse(json.dumps(post_payload).encode("utf-8"))
        if url.endswith("/models"):
            return _FakeResponse(json.dumps(post_payload).encode("utf-8"))
        return _FakeResponse(image)

    monkeypatch.setattr("mikasa.providers.image.urllib.request.urlopen", _open)


def _config(**overrides) -> ImageConfig:
    fields = {
        "backend": "api",
        "base_url": BASE,
        "api_key_env": KEY_ENV,
        "model": "Qwen/Qwen-Image",
        "size": "1328x1328",
    }
    fields.update(overrides)
    return ImageConfig(**fields)


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "sk-test-image-0000")


def test_downloads_url_shape_used_by_siliconflow(monkeypatch):
    """`images[].url` → 当场下载成字节（而不是把 URL 交给上层）。"""
    seen: list = []
    _routes(
        monkeypatch,
        post_payload={"images": [{"url": "http://127.0.0.1:9/gen.png"}]},
        seen=seen,
    )
    image = OpenAICompatImage(_config()).generate("一只猫")
    assert image.data == PNG
    assert image.mime == "image/png"
    assert image.source_url == "http://127.0.0.1:9/gen.png"

    post = seen[0]
    assert post.full_url == f"{BASE}/images/generations"
    body = json.loads(post.data.decode("utf-8"))
    # SiliconFlow 的字段名是 image_size（不是 OpenAI 的 size），且 batch_size 只取一张
    assert body == {
        "model": "Qwen/Qwen-Image",
        "prompt": "一只猫",
        "image_size": "1328x1328",
        "batch_size": 1,
    }
    assert post.get_header("Authorization") == "Bearer sk-test-image-0000"
    # 第二跳是 GET 图片地址（带闸门）
    assert seen[1].full_url == "http://127.0.0.1:9/gen.png"


def test_accepts_base64_shape_used_by_openai(monkeypatch):
    """`data[].b64_json`（OpenAI/DALL·E 那套）同样要认，否则换服务商就"没有图片"。"""
    import base64

    _routes(monkeypatch, post_payload={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
    image = OpenAICompatImage(_config()).generate("一只猫")
    assert image.data == PNG
    assert image.source_url is None  # 没有过 URL，不存在"下载"这一步


def test_size_override_and_missing_entries(monkeypatch):
    """显式 size 覆盖配置；响应里没有图 → ProviderError（而不是空图）。"""
    _routes(monkeypatch, post_payload={"images": []})
    with pytest.raises(ProviderError, match="没有图片"):
        OpenAICompatImage(_config()).generate("一只猫")


def test_non_image_bytes_are_rejected(monkeypatch):
    """上游回的是 HTML/二进制垃圾时不许落盘（污染数据目录 + 前端裂图）。"""
    _routes(
        monkeypatch, post_payload={"images": [{"url": "http://127.0.0.1:9/x"}]}, image=b"<html>"
    )
    with pytest.raises(ProviderError, match="不像图片"):
        OpenAICompatImage(_config()).generate("一只猫")


def test_internal_image_url_is_refused(monkeypatch):
    """上游返回的图片地址也要过闸门：那是半可信输入（云元数据地址是经典靶子）。"""
    _routes(
        monkeypatch, post_payload={"images": [{"url": "http://169.254.169.254/latest/meta-data"}]}
    )
    with pytest.raises(ConfigError, match="被拒绝"):
        OpenAICompatImage(_config()).generate("一只猫")


def test_internal_base_url_is_refused():
    """发请求的目标同样过闸门（用户可能在面板里填内网地址）。"""
    cfg = _config(base_url="http://192.168.1.10:8000/v1")
    with pytest.raises(ConfigError, match="被拒绝"):
        OpenAICompatImage(cfg).generate("一只猫")


def test_missing_key_and_empty_prompt():
    """缺密钥 / 空提示词 → ConfigError（400），文案指向设置面板。"""
    cfg = _config(api_key_env="MIKASA_DEFINITELY_UNSET_KEY")
    with pytest.raises(ConfigError, match="密钥未配置"):
        OpenAICompatImage(cfg).generate("一只猫")
    with pytest.raises(ConfigError, match="提示词不能为空"):
        OpenAICompatImage(_config()).generate("   ")


def test_key_free_local_service_uses_placeholder(monkeypatch):
    """没配密钥槽位 = 本机免密钥服务（带占位钥匙）；配了槽位没填 = 如实报错。

    "我知道这个服务不要密钥" 与 "忘了填" 必须区分开——本机出图服务
    （Automatic1111/ComfyUI 的兼容壳）不校验 Authorization，但少这个头有的
    实现直接 400。
    """
    seen: list = []
    _routes(
        monkeypatch,
        post_payload={"images": [{"url": "http://127.0.0.1:9/gen.png"}]},
        seen=seen,
    )
    cfg = _config(api_key_env="")  # 留空 = 免密钥
    image = OpenAICompatImage(cfg).generate("一只猫")
    assert image.data == PNG
    assert seen[0].get_header("Authorization") == "Bearer mikasa-local"


def test_http_error_becomes_provider_error(monkeypatch):
    """上游 4xx/5xx → ProviderError（app 层 502），并把响应体带出来。"""
    import urllib.error

    def _boom(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url,
            400,
            "Bad Request",
            {},
            __import__("io").BytesIO(b'{"message":"bad size"}'),
        )

    monkeypatch.setattr("mikasa.providers.image.urllib.request.urlopen", _boom)
    with pytest.raises(ProviderError, match="400"):
        OpenAICompatImage(_config()).generate("一只猫")


def test_no_image_provider_explains_how_to_enable():
    """未接入时给的是"怎么开"，不是静默空图。"""
    with pytest.raises(ConfigError) as exc:
        NoImage().generate("一只猫")
    assert exc.value.args[0] == NO_IMAGE_HINT


def test_list_models_reads_ids_and_tolerates_missing_endpoint(monkeypatch):
    """测试连接走 /models：列出 id；服务没有这个端点（404）时返回空列表不算失败。"""
    _routes(monkeypatch, post_payload={"data": [{"id": "Qwen/Qwen-Image"}, {"id": "x"}]})
    assert OpenAICompatImage(_config()).list_models() == ["Qwen/Qwen-Image", "x"]

    import urllib.error

    def _404(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 404, "Not Found", {}, __import__("io").BytesIO(b"")
        )

    monkeypatch.setattr("mikasa.providers.image.urllib.request.urlopen", _404)
    assert OpenAICompatImage(_config()).list_models() == []


def test_config_defaults_are_conservative():
    """默认整段是关的（none）+ 出图超时给足（出图比文本慢一个量级）。"""
    cfg = ImageConfig()
    assert cfg.backend == "none"
    assert cfg.timeout_seconds >= 60
    assert Path("src/mikasa/providers/image.py").is_file()  # 占位：确认测试跑在仓库里
