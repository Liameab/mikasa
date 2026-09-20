"""模型配置端点（/api/settings/*）单元测试。

隔离说明：全局 autouse 夹具把 MIKASA_DATA_DIR 指到 tmp，因此覆盖层
（user_data_root()/config.yaml）与密钥 .env 都落在 tmp，本文件**不会**
碰到开发机的真实 data/ 与仓库根 .env。
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mikasa.config.settings import Settings, load_settings, user_config_path, user_env_path
from mikasa.errors import ProviderError
from mikasa.web.app import create_app

# 测试专用的密钥变量名：避免与本机真实环境变量撞名
KEY_ENV = "MIKASA_TEST_PROBE_KEY"


def _put_body(**overrides) -> dict:
    body = {
        "backend": "api",
        "base_url": "https://example.com/v1",
        "api_key_env": KEY_ENV,
        "model": "probe-model",
    }
    body.update(overrides)
    return body


@pytest.fixture()
def fake_probe(monkeypatch):
    """替换探测用的 LLM 客户端：记录构造参数与"调用时能看到的密钥"。"""
    created: list = []

    class FakeLLM:
        def __init__(self, config, *, max_retries: int = 3) -> None:
            self.config = config
            self.max_retries = max_retries
            self.seen_key: str | None = None
            created.append(self)

        def complete(self, messages, *, temperature: float, max_tokens: int):
            self.seen_key = self.config.api_key  # 调用时刻的密钥（临时 env 已就位）
            return SimpleNamespace(text="你好")

    monkeypatch.setattr("mikasa.web.routers.settings.OpenAICompatLLM", FakeLLM)
    return created


# ---------------------------------------------------------------------------
# GET：读当前配置
# ---------------------------------------------------------------------------


def test_get_model_settings_hides_key(client):
    c, settings = client
    resp = c.get("/api/settings/model")
    assert resp.status_code == 200
    body = resp.json()
    assert "api_key" not in body  # 密钥永不下发，只有 has_api_key
    assert body["has_api_key"] is False
    assert body["backend"] == "mock"  # offline 档
    assert body["profile"] == "offline"
    assert body["locked"] is False
    assert body["model"] == settings.llm.model


# ---------------------------------------------------------------------------
# PUT：保存 + 热生效
# ---------------------------------------------------------------------------


def test_put_saves_and_applies_hot(client):
    c, _settings = client
    old_ask = c.app.state.services.ask

    resp = c.put("/api/settings/model", json=_put_body(api_key="sk-new-0001"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["backend"] == "api"
    assert body["model"] == "probe-model"
    assert body["has_api_key"] is True

    # 内存里的配置已换新（app.state + services 同一份）
    new = c.app.state.settings
    assert isinstance(new, Settings)
    assert new.llm.model == "probe-model"
    assert new.llm.base_url == "https://example.com/v1"
    assert new.llm.api_key == "sk-new-0001"
    assert new.embedding.backend == "none"  # 只动 llm 段
    assert c.app.state.services.settings is new
    assert c.app.state.services.ask is not old_ask  # 服务已按新配置重建

    # health 同步反映（前端顶栏胶囊的取数口）
    assert c.get("/api/health").json()["llm_model"] == "probe-model"

    # 落盘：覆盖层只含 llm 段且**不含密钥**；密钥在 .env
    overlay = user_config_path().read_text(encoding="utf-8")
    assert "probe-model" in overlay
    assert "sk-new-0001" not in overlay
    assert KEY_ENV in overlay  # 变量名（不是值）可以存在
    assert "sk-new-0001" in user_env_path().read_text(encoding="utf-8")
    assert os.environ[KEY_ENV] == "sk-new-0001"


def test_put_clear_key_removes_env_line(client):
    c, _settings = client
    assert c.put("/api/settings/model", json=_put_body(api_key="sk-first")).status_code == 200
    assert "sk-first" in user_env_path().read_text(encoding="utf-8")

    assert c.put("/api/settings/model", json=_put_body(api_key="")).status_code == 200
    text = user_env_path().read_text(encoding="utf-8")
    assert "sk-first" not in text
    assert KEY_ENV not in text  # 删行而不是留空值
    assert KEY_ENV not in os.environ  # 进程内旧值也摘掉
    assert c.get("/api/settings/model").json()["has_api_key"] is False


def test_put_without_api_key_field_keeps_existing(client):
    """api_key 缺省（前端密钥框没被碰过）→ 已存密钥原样保留。"""
    c, _settings = client
    c.put("/api/settings/model", json=_put_body(api_key="sk-keep"))
    resp = c.put("/api/settings/model", json=_put_body(model="another-model"))
    assert resp.status_code == 200
    assert resp.json()["has_api_key"] is True
    assert os.environ[KEY_ENV] == "sk-keep"
    assert "sk-keep" in user_env_path().read_text(encoding="utf-8")


def test_put_validation_is_chinese_422(client):
    c, _settings = client
    resp = c.put("/api/settings/model", json=_put_body(model="   "))
    assert resp.status_code == 422
    assert "模型名不能为空" in resp.json()["detail"]

    resp = c.put("/api/settings/model", json=_put_body(base_url=""))
    assert resp.status_code == 422
    assert "base_url" in resp.json()["detail"]

    # backend 白名单（mock 是 offline 体验档的形态，不属于面板可写范围）
    resp = c.put("/api/settings/model", json=_put_body(backend="mock"))
    assert resp.status_code == 422


def test_put_local_persists_think_and_num_ctx(client):
    """本机旋钮必须落进覆盖层并能读回——面板上那两个开关不能只是装饰。

    2026-09-20 用户报障的正是"本机慢且答得少"：思考模式与上下文长度这两个旋钮
    只有在配置里真的存下来、并传进 Ollama 原生请求，才谈得上可调。
    """
    c, _settings = client
    resp = c.put(
        "/api/settings/model",
        json={
            "backend": "local",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "qwen3:8b",
            "think": False,
            "num_ctx": 8192,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["think"] is False
    assert resp.json()["num_ctx"] == 8192

    # 读回（面板打开时的取数口）
    body = c.get("/api/settings/model").json()
    assert body["think"] is False and body["num_ctx"] == 8192

    # 内存里的配置也换了（热生效），并且只动 llm 段
    new = c.app.state.settings
    assert new.llm.think is False and new.llm.num_ctx == 8192
    assert new.embedding.backend == "none"

    # 落盘可见：覆盖层里就是这两个键（下次启动仍然生效）
    overlay = user_config_path().read_text(encoding="utf-8")
    assert "think: false" in overlay
    assert "num_ctx: 8192" in overlay


def test_put_local_can_follow_model_defaults(client):
    """两个旋钮传 null = 跟随默认（"没配置"与"关掉"是两件事）。"""
    c, _settings = client
    resp = c.put(
        "/api/settings/model",
        json={
            "backend": "local",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "qwen3:8b",
            "think": None,
            "num_ctx": None,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["think"] is None and resp.json()["num_ctx"] is None


def test_put_local_backend_defaults_no_key_env(client):
    """local（Ollama）免密钥：api_key_env 归空，密钥字段被忽略。"""
    c, _settings = client
    resp = c.put(
        "/api/settings/model",
        json={
            "backend": "local",
            "base_url": "http://localhost:11434/v1",
            "model": "qwen3:8b",
            "api_key": "不该被保存",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["api_key_env"] == ""
    assert body["has_api_key"] is False
    env_path = user_env_path()  # local 档连 .env 都不该被创建
    assert not env_path.exists()


def test_put_rejected_when_config_locked(tmp_path):
    """配置来自显式文件（--config / config.yaml）时面板只读：400 + 指引。"""
    cfg = tmp_path / "custom.yaml"
    cfg.write_text("profile: offline\n", encoding="utf-8")
    settings = load_settings("offline", config_path=cfg, data_dir=tmp_path / "data")
    with TestClient(create_app(settings)) as c:
        assert c.get("/api/settings/model").json()["locked"] is True
        resp = c.put("/api/settings/model", json=_put_body(api_key="sk-x"))
        assert resp.status_code == 400
        assert "显式配置文件" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# POST /api/settings/model/test：连通性探测
# ---------------------------------------------------------------------------


def test_probe_ok_records_retries_and_cleans_env(client, fake_probe):
    c, _settings = client
    resp = c.post(
        "/api/settings/model/test",
        json={
            "backend": "api",
            "base_url": "https://example.com/v1",
            "model": "probe-model",
            "api_key": "sk-probe",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["model"] == "probe-model"
    assert body["latency_ms"] >= 0
    assert len(fake_probe) == 1
    assert fake_probe[0].max_retries == 0  # 硬上限：不排队重试
    assert fake_probe[0].seen_key == "sk-probe"  # 调用时刻临时 env 就位
    assert "MIKASA_SETTINGS_TEST_KEY" not in os.environ  # finally 摘干净


def test_probe_without_key_prechecks_without_client(client, fake_probe):
    """api 后端没密钥：预检直接给结果，不构造客户端（临时变量名不进文案）。"""
    c, _settings = client
    resp = c.post(
        "/api/settings/model/test",
        json={"backend": "api", "base_url": "https://example.com/v1", "model": "m"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "未填写 API 密钥" in resp.json()["error"]
    assert fake_probe == []
    assert "MIKASA_SETTINGS_TEST_KEY" not in os.environ


def test_probe_uses_stored_key_when_omitted(client, fake_probe):
    """api_key 缺省 → 用 api_key_env 指向的已存密钥（先用 PUT 存一份）。"""
    c, _settings = client
    c.put("/api/settings/model", json=_put_body(api_key="sk-stored"))
    resp = c.post(
        "/api/settings/model/test",
        json={
            "backend": "api",
            "base_url": "https://example.com/v1",
            "model": "probe-model",
            "api_key_env": KEY_ENV,
        },
    )
    assert resp.json()["ok"] is True
    assert fake_probe[0].seen_key == "sk-stored"


def test_probe_local_backend_uses_the_native_channel(client, monkeypatch):
    """local 探测必须走 **Ollama 原生通道**，且不需要密钥。

    2026-09-20 起 local 档的正式问答走原生接口（思考/上下文两个旋钮只有它认）。
    探测如果还走兼容面，就会给出与真实使用不符的结论——实测 qwen3:8b 在兼容面
    上光推理就 14 秒，用户的"测试连接"必然 20 秒超时（真实报障就是这个）。
    """
    c, _settings = client
    created: list = []

    class FakeNative:
        def __init__(self, config, *, timeout: float | None = None) -> None:
            self.config = config
            self.timeout = timeout
            created.append(self)

        def complete(self, messages, *, temperature: float, max_tokens: int):
            return SimpleNamespace(text="你好")

    monkeypatch.setattr("mikasa.web.routers.settings.OllamaNativeLLM", FakeNative)
    resp = c.post(
        "/api/settings/model/test",
        json={
            "backend": "local",
            "base_url": "http://localhost:11434/v1",
            "model": "qwen3:8b",
            "think": False,
            "num_ctx": 8192,
        },
    )
    assert resp.json()["ok"] is True
    assert len(created) == 1
    # 面板上的两个本机旋钮原样传到探测请求里（否则探测的就不是用户要用的那条路）
    assert created[0].config.think is False
    assert created[0].config.num_ctx == 8192
    assert created[0].config.api_key is None  # local 无密钥


def test_probe_failure_translated_to_ok_false(client, monkeypatch):
    c, _settings = client

    class BoomLLM:
        def __init__(self, config, *, max_retries: int = 3) -> None:
            pass

        def complete(self, messages, *, temperature: float, max_tokens: int):
            raise ProviderError("LLM 调用失败（m，https://x/v1）：APIConnectionError: 拒绝连接")

    monkeypatch.setattr("mikasa.web.routers.settings.OpenAICompatLLM", BoomLLM)
    resp = c.post(
        "/api/settings/model/test",
        json={
            "backend": "api",
            "base_url": "https://x/v1",
            "model": "m",
            "api_key": "sk-x",
        },
    )
    assert resp.status_code == 200  # 探测结果用 200+ok:false 表达，不是服务端错误
    assert resp.json()["ok"] is False
    assert "拒绝连接" in resp.json()["error"]
    assert "MIKASA_SETTINGS_TEST_KEY" not in os.environ


# ---------------------------------------------------------------------------
# GET /api/settings/ollama/models
# ---------------------------------------------------------------------------


def test_ollama_models_listing(client, monkeypatch):
    c, _settings = client
    seen: dict = {}

    def fake_tags(base_url: str) -> list[str]:
        seen["base_url"] = base_url
        return ["qwen3:8b", "qwen3:4b"]

    monkeypatch.setattr("mikasa.web.routers.settings.fetch_ollama_tags", fake_tags)
    resp = c.get("/api/settings/ollama/models?base_url=http://127.0.0.1:11434/v1")
    assert resp.status_code == 200
    assert resp.json()["models"] == ["qwen3:8b", "qwen3:4b"]
    assert seen["base_url"] == "http://127.0.0.1:11434/v1"  # 参数透传


def test_ollama_models_default_url_and_failure(client, monkeypatch):
    c, _settings = client
    seen: dict = {}

    def boom(base_url: str) -> list[str]:
        seen["base_url"] = base_url
        raise RuntimeError("Ollama 服务不可达（http://localhost:11434/api/tags）")

    monkeypatch.setattr("mikasa.web.routers.settings.fetch_ollama_tags", boom)
    resp = c.get("/api/settings/ollama/models")
    assert seen["base_url"] == "http://localhost:11434/v1"  # 缺省 = local 档默认端点
    assert resp.status_code == 502
    assert "Ollama 服务不可达" in resp.json()["error"]["message"]


def test_probe_rejects_lan_addresses(client):
    """面板的 base_url 过出网闸门：内网段拒绝（SSRF）。

    这些端点任何网页都能触发（`--host 0.0.0.0` 下同网段也能）——不加闸就是
    一个内网探活扫描器：请求 `?base_url=http://192.168.1.1` 会如实区分
    "拒绝连接 / 超时"，`http://169.254.169.254/v1` 也会被打（2026-09-20 审查实测）。
    策略与论文下载器共用（utils/net.py）。想指向局域网模型服务请直接改配置文件。
    """
    c, _ = client
    for bad in ("http://192.168.1.1/v1", "http://10.0.0.5:8000/v1", "http://169.254.169.254/v1"):
        resp = c.post(
            "/api/settings/model/test",
            json={"backend": "api", "base_url": bad, "model": "m", "api_key": "sk-x"},
        )
        assert resp.status_code == 422, f"{bad} 应被拒绝：{resp.text}"
        assert "安全策略" in resp.json()["detail"]
    # 回环（本机 Ollama / 假源）必须照常放行——只是探测结果由下游如实给
    ok = c.get("/api/settings/ollama/models?base_url=http://127.0.0.1:9/v1")
    assert ok.status_code in (200, 502), ok.text
