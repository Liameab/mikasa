"""文生图端点（/api/images/*、/api/settings/image，ADR-0031）单元测试。

替身走 `services.image`（真出图要外部模型且按张计费，单测永远不真调）。
覆盖四类：生成与落盘、取图的文件名白名单、落库（给了 session 才落）、
设置段的校验与热生效。隔离同全项目：MIKASA_DATA_DIR 由全局夹具指到 tmp，
所以 `data_dir/generated/` 与配置覆盖层都在 tmp 里。
"""

from __future__ import annotations

from mikasa.config.settings import user_env_path
from mikasa.errors import ProviderError
from mikasa.providers.image import NO_IMAGE_HINT, GeneratedImage, NoImage
from mikasa.storage import repo
from mikasa.storage.db import open_db

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class _FakeImage:
    """出图替身：记录提示词与尺寸，可按需抛错。"""

    model = "fake-image"

    def __init__(self, data: bytes = PNG, mime: str = "image/png", error=None) -> None:
        self._data = data
        self._mime = mime
        self.error = error
        self.seen: list[dict] = []

    def generate(self, prompt: str, *, size: str | None = None) -> GeneratedImage:
        self.seen.append({"prompt": prompt, "size": size})
        if self.error is not None:
            raise self.error
        return GeneratedImage(
            data=self._data, mime=self._mime, model=self.model, size=size or "1024x1024"
        )


def _generate(c, **body):
    payload = {"prompt": "一只坐在窗台上的白猫"}
    payload.update(body)
    return c.post("/api/images/generate", json=payload)


# ---------------------------------------------------------------------------
# 生成 + 落盘 + 取图
# ---------------------------------------------------------------------------


def test_generate_saves_file_and_serves_it(client):
    """生成的图落进 data_dir/generated/，并经白名单端点取回。"""
    c, settings = client
    fake = _FakeImage()
    c.app.state.services.image = fake

    resp = _generate(c, size="1024x1024")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert fake.seen == [{"prompt": "一只坐在窗台上的白猫", "size": "1024x1024"}]
    assert body["model"] == "fake-image"
    assert body["url"] == f"/api/images/generated/{body['name']}"
    assert body["content"].startswith("![一只坐在窗台上的白猫](")
    # 没给 session → 后端**新建一个**并回传（图片不落库就等于刷新即失）
    assert body["session_id"] is not None and body["message_id"] is not None

    saved = settings.generated_dir / body["name"]
    assert saved.is_file() and saved.read_bytes() == PNG

    got = c.get(body["url"])
    assert got.status_code == 200
    assert got.headers["content-type"] == "image/png"
    assert got.headers["x-content-type-options"] == "nosniff"
    # 不写 attachment：没有 disposition 头 = 浏览器原地渲染（同 note-media）
    assert not got.headers.get("content-disposition", "").startswith("attachment")
    assert got.content == PNG


def test_same_image_twice_reuses_one_file(client):
    """内容相同 = 文件名相同（日期 + 内容哈希），不会堆出一堆副本。"""
    c, settings = client
    c.app.state.services.image = _FakeImage()
    first = _generate(c).json()
    second = _generate(c).json()
    assert first["name"] == second["name"]
    assert len(list(settings.generated_dir.iterdir())) == 1


def test_generated_name_whitelist_blocks_traversal(client):
    """取图只认服务端拼的名字形状：越界/改名/编造的路径一律 404。"""
    c, settings = client
    c.app.state.services.image = _FakeImage()
    name = _generate(c).json()["name"]
    (settings.generated_dir / "secret.txt").write_text("不该被读到", encoding="utf-8")

    for bad in (
        "secret.txt",
        "../secret.txt",
        "not-a-name.png",
        "20260921-zzzzzzzz.png",  # 非十六进制
        "20260921-1a2b3c4d.exe",  # 后缀不在白名单
        name.replace(".png", ".svg"),
    ):
        assert c.get(f"/api/images/generated/{bad}").status_code == 404, bad


def test_generate_persists_message_only_with_session(client):
    """给了 session 就用一条助手消息把图钉进会话（刷新后仍在）。"""
    c, settings = client
    c.app.state.services.image = _FakeImage()
    with open_db(settings.db_path) as conn:
        session_id = repo.create_session(conn, settings.profile)

    resp = _generate(c, session_id=session_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["session_id"] == session_id
    assert resp.json()["message_id"] is not None

    messages = c.get(f"/api/sessions/{session_id}/messages").json()["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    assert f"/api/images/generated/{resp.json()['name']}" in messages[0]["content"]


def test_generate_with_unknown_session_is_404(client):
    """会话不存在 → 404（不静默丢弃用户刚生成的图）。"""
    c, settings = client
    c.app.state.services.image = _FakeImage()
    assert _generate(c, session_id=999999).status_code == 404
    # 图片本身已经落盘（先生成后落库），但这里 404 是诚实的：调用方要的
    # "生成并落进会话"没做到
    assert list(settings.generated_dir.iterdir())


def test_generate_reports_not_configured_and_upstream_failure(client):
    """未接入 → 400（文案能照做）；上游失败 → 502（走 app 层处理器）。"""
    c, _ = client
    c.app.state.services.image = NoImage()
    resp = _generate(c)
    assert resp.status_code == 400
    assert NO_IMAGE_HINT in resp.json()["error"]["message"]

    c.app.state.services.image = _FakeImage(error=ProviderError("上游炸了"))
    assert _generate(c).status_code == 502


def test_generate_rejects_empty_prompt(client):
    """空提示词在 schema 层就挡掉（不浪费一次出网调用）。"""
    c, _ = client
    c.app.state.services.image = _FakeImage()
    assert c.post("/api/images/generate", json={"prompt": ""}).status_code == 422
    assert c.post("/api/images/generate", json={"prompt": "   "}).status_code == 422


# ---------------------------------------------------------------------------
# 设置段
# ---------------------------------------------------------------------------


def _put_body(**overrides) -> dict:
    body = {
        "backend": "api",
        "base_url": "https://api.example.com/v1",
        "model": "Qwen/Qwen-Image",
        "size": "1328x1328",
        "api_key_env": "MIKASA_TEST_IMAGE_SETTING_KEY",
    }
    body.update(overrides)
    return body


def test_image_settings_roundtrip_and_none_collapse(client):
    """保存后立刻可读；backend=none 时整段塌缩（不留一串用不上的字段）。"""
    c, _ = client
    assert c.get("/api/settings/image").json()["backend"] == "none"

    saved = c.put("/api/settings/image", json=_put_body()).json()
    assert saved["backend"] == "api"
    assert saved["model"] == "Qwen/Qwen-Image"
    assert saved["size"] == "1328x1328"
    assert saved["has_api_key"] is False

    again = c.get("/api/settings/image").json()
    assert again["model"] == "Qwen/Qwen-Image"

    off = c.put("/api/settings/image", json={"backend": "none"}).json()
    assert off["backend"] == "none"
    assert off["model"] == ""  # 塌缩成 none 之后不再回显旧模型名

    # 而且热生效：服务容器里的提供方跟着换回未接入
    assert isinstance(c.app.state.services.image, NoImage)


def test_image_settings_validates_size_and_required_fields(client):
    """尺寸形状、模型名、地址、空串密钥四类都在 422 里说清楚。"""
    c, _ = client
    cases = [
        (_put_body(size="128"), "宽x高"),
        (_put_body(size="1x1"), "宽x高"),
        (_put_body(model=" "), "模型名不能为空"),
        (_put_body(base_url=" "), "base_url"),
        (_put_body(api_key=""), "不能清空"),
    ]
    for body, needle in cases:
        resp = c.put("/api/settings/image", json=body)
        assert resp.status_code == 422, body
        assert needle in resp.text, body


def test_image_settings_writes_key_without_exposing_it(client):
    """密钥写进去后只回 has_api_key，GET 永不下发值。"""
    c, settings = client
    put = c.put("/api/settings/image", json=_put_body(api_key="sk-secret-123")).json()
    assert put["has_api_key"] is True
    assert "sk-secret-123" not in c.get("/api/settings/image").text
    assert user_env_path().is_file()


def test_image_test_endpoint_checks_model_list(client, monkeypatch):
    """「测试连接」走 /models：连通 + 模型名核对；恒 200。"""
    c, _ = client
    seen: list[dict] = []

    def _fake_list(self, timeout: float = 20.0) -> list[str]:
        seen.append({"base_url": self._config.base_url})
        return ["Qwen/Qwen-Image", "Kwai-Kolors/Kolors"]

    monkeypatch.setattr("mikasa.web.routers.settings.OpenAICompatImage.list_models", _fake_list)
    ok = c.post(
        "/api/settings/image/test",
        json={
            "base_url": "https://api.example.com/v1",
            "model": "Qwen/Qwen-Image",
            "api_key": "sk-test-0000",
        },
    )
    assert ok.status_code == 200
    assert ok.json()["ok"] is True and ok.json()["model_available"] is True

    miss = c.post(
        "/api/settings/image/test",
        json={
            "base_url": "https://api.example.com/v1",
            "model": "不存在/的模型",
            "api_key": "sk-test-0000",
        },
    )
    assert miss.json()["ok"] is True and miss.json()["model_available"] is False
    assert "名字写错" in miss.json()["error"]

    # 没填密钥 → ok=False（不发起真实请求）
    nokey = c.post(
        "/api/settings/image/test",
        json={"base_url": "https://api.example.com/v1", "model": "m", "api_key_env": ""},
    )
    assert nokey.status_code == 200 and nokey.json()["ok"] is False
