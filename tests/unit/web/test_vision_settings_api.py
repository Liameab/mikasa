"""视觉模型设置端点测试（M6 ②）：/api/settings/vision 的读写与两条红线。

两条红线各有测试钉住：
  1）**写视觉段不许伤 llm 段**——覆盖层是一份 YAML，整段覆写会让"保存视觉
     设置"顺手把已配好的生成模型抹掉（用户看到的是"模型自己变回去了"）；
  2）**共用密钥槽位不许清空**——SILICONFLOW_API_KEY 被 embedding/reranker/
     judge 共用，在这里清掉会打挂整条检索链，故障表现为"搜索结果变差"。
"""

from __future__ import annotations

import os

from fastapi.testclient import TestClient

from mikasa.config.settings import load_settings, user_config_path
from mikasa.providers.vision import NoVision, OpenAICompatVision
from mikasa.web.app import create_app

VISION_BODY = {
    "backend": "api",
    "base_url": "https://api.siliconflow.cn/v1",
    "model": "Qwen/Qwen2.5-VL-32B-Instruct",
    "api_key_env": "SILICONFLOW_API_KEY",
}


def _vision(c, body):
    return c.put("/api/settings/vision", json=body)


def test_get_defaults_to_not_connected(client):
    """默认（offline 档）：未接入、无密钥、非共用槽位、未锁定。"""
    c, _ = client
    body = c.get("/api/settings/vision").json()
    assert body["backend"] == "none"
    assert body["has_api_key"] is False
    assert body["key_shared_with_retrieval"] is False
    assert body["locked"] is False
    assert isinstance(c.app.state.services.vision, NoVision)


def test_put_writes_overlay_and_hot_applies(client, monkeypatch):
    """保存 → 覆盖层落盘 + 密钥进 .env + 服务容器里的提供方当场换掉。"""
    c, settings = client
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    resp = _vision(c, {**VISION_BODY, "api_key": "sk-test-vision"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["backend"] == "api"
    assert body["model"] == VISION_BODY["model"]
    assert body["has_api_key"] is True
    # offline 档检索侧没有密钥槽位（embedding/reranker/judge 全关）→ 标记为假；
    # 同一个槽位在 api 档里是共用的，见下一条测试
    assert body["key_shared_with_retrieval"] is False
    # 热生效：容器里换成了真实现，且 settings 是新的
    assert isinstance(c.app.state.services.vision, OpenAICompatVision)
    assert c.app.state.settings.vision.backend == "api"
    # 密钥进了 .env 与进程环境（面板 PUT 的三段语义里"写入"那一支）
    assert os.environ.get("SILICONFLOW_API_KEY") == "sk-test-vision"
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)  # 别漏给别的测试
    assert user_config_path().is_file()


def test_key_shared_flag_follows_the_profile(tmp_path):
    """共用标记按档位算：api 档里 SILICONFLOW_API_KEY 同时供检索侧使用。

    前端据此常显"与检索侧共用同一密钥"的提示——在共用槽位上清空密钥会
    打挂 embedding/reranker/judge，而故障表现是"检索结果变差"。
    """
    settings = load_settings("api", data_dir=tmp_path / "data")
    with TestClient(create_app(settings)) as c:
        body = _vision(c, VISION_BODY).json()
    assert body["key_shared_with_retrieval"] is True


def test_put_vision_keeps_llm_section(client):
    """★ 先配模型、再配视觉：模型段必须原样还在（覆盖层读-改-写守卫）。"""
    c, _ = client
    first = c.put(
        "/api/settings/model",
        json={
            "backend": "api",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-chat",
            "api_key_env": "DEEPSEEK_API_KEY",
        },
    )
    assert first.status_code == 200, first.text

    assert _vision(c, VISION_BODY).status_code == 200
    model = c.get("/api/settings/model").json()
    assert model["model"] == "deepseek-chat"  # 视觉段没把它顶掉
    assert model["base_url"] == "https://api.deepseek.com"
    # 反向再来一次：先视觉后模型
    assert (
        c.put(
            "/api/settings/model",
            json={
                "backend": "api",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-reasoner",
                "api_key_env": "DEEPSEEK_API_KEY",
            },
        ).status_code
        == 200
    )
    assert c.get("/api/settings/vision").json()["model"] == VISION_BODY["model"]


def test_put_rejects_clearing_shared_key(client):
    """空串密钥 = 拒绝（共用槽位），且**什么都没写**（校验先于写入）。"""
    c, _ = client
    resp = _vision(c, {**VISION_BODY, "api_key": ""})
    assert resp.status_code == 422, resp.text
    assert "模型" in resp.json()["detail"]  # 指路到「模型」段
    # 校验先于写入：视觉段没被改动（还是默认的未接入）
    assert c.get("/api/settings/vision").json()["backend"] == "none"
    overlay = user_config_path()
    assert not overlay.is_file() or "vision" not in overlay.read_text(encoding="utf-8")


def test_put_none_stops_using_vision(client):
    """backend=none = 停止使用（不是删密钥）：提供方换回 NoVision。"""
    c, _ = client
    assert _vision(c, VISION_BODY).status_code == 200
    assert isinstance(c.app.state.services.vision, OpenAICompatVision)

    resp = _vision(c, {"backend": "none"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["backend"] == "none"
    assert isinstance(c.app.state.services.vision, NoVision)


def test_put_is_locked_with_explicit_config(tmp_path):
    """--config 起服务时面板只读（与模型段同语义）。"""
    cfg = tmp_path / "explicit.yaml"
    cfg.write_text("profile: offline\nvision:\n  backend: none\n", encoding="utf-8")
    settings = load_settings("offline", config_path=cfg, data_dir=tmp_path / "data")
    with TestClient(create_app(settings)) as c:
        assert c.get("/api/settings/vision").json()["locked"] is True
        resp = c.put("/api/settings/vision", json=VISION_BODY)
        assert resp.status_code == 400
        assert "显式配置文件" in resp.json()["detail"]


def test_probe_needs_a_key(client):
    """探测端点：api 后端没密钥 → 200 + ok=False（不当成服务端错误）。"""
    c, _ = client
    body = c.post(
        "/api/settings/vision/test",
        json={
            "backend": "api",
            "base_url": "https://api.siliconflow.cn/v1",
            "model": "Qwen/Qwen2.5-VL-32B-Instruct",
            "api_key_env": "MIKASA_TEST_NOPE_KEY",
        },
    ).json()
    assert body["ok"] is False
    assert "密钥" in body["error"]
