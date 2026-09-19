"""应用内更新端点（/api/update/*）单元测试。

零网络：`mikasa.update.release._open` 换成假响应；下载与安装的两个
动作（后台任务体 / 启动安装器）被整体替换——**测试里没有任何可执行
文件被启动**，也没有字节真的经网络流动。

复用 web/conftest.py 的 `client` 夹具（offline profile + 隔离 data_dir）。
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import mikasa
import mikasa.update.release as release_mod
import mikasa.web.routers.update as update_router

_SETUP_NAME = "Mikasa-Setup-9.9.9-win64.exe"
_SETUP_URL = "https://github.com/Liameab/mikasa/releases/download/v9.9.9/" + _SETUP_NAME
_SUMS_URL = "https://github.com/Liameab/mikasa/releases/download/v9.9.9/SHA256SUMS.txt"


class _FakeResp:
    def __init__(self, payload: bytes) -> None:
        self._buf = io.BytesIO(payload)

    def read(self) -> bytes:
        return self._buf.read()

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _payload(*, tag: str = "v9.9.9", with_setup: bool = True) -> bytes:
    assets = []
    if with_setup:
        assets.append({"name": _SETUP_NAME, "browser_download_url": _SETUP_URL, "size": 99})
    assets.append({"name": "SHA256SUMS.txt", "browser_download_url": _SUMS_URL, "size": 66})
    return json.dumps(
        {
            "tag_name": tag,
            "html_url": f"https://github.com/Liameab/mikasa/releases/tag/{tag}",
            "body": "## 更新内容\n\n- 修了若干毛病",
            "published_at": "2026-09-19T00:00:00Z",
            "assets": assets,
        }
    ).encode()


class _FakeOpener:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: float):
        self.calls.append(url)
        return _FakeResp(self.payload)


@pytest.fixture()
def opener(monkeypatch: pytest.MonkeyPatch) -> _FakeOpener:
    fake = _FakeOpener(_payload())
    monkeypatch.setattr(release_mod, "_open", fake)
    return fake


def _noop_download(manager, release, updates_dir) -> None:  # noqa: ANN001 - 顶掉后台任务体
    """占住槽但什么都不做：用来观察 running 状态与 409。"""


# ---------------- /api/update/check ----------------


def test_check_reports_available_update(client, opener: _FakeOpener) -> None:
    c, _ = client
    body = c.get("/api/update/check").json()
    assert body["update_available"] is True
    assert body["current"] == mikasa.__version__
    assert body["latest"] == "9.9.9"
    assert body["asset"] == {"name": _SETUP_NAME, "size": 99}
    assert body["release_url"].endswith("/v9.9.9")
    assert "更新内容" in body["notes"]
    assert isinstance(body["install_supported"], bool)
    # 响应里**不含**任何下载地址：前端拿不到 URL（客户端无从指定地址）
    assert _SETUP_URL not in json.dumps(body)


def test_check_reports_no_update_when_versions_match(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(release_mod, "_open", _FakeOpener(_payload(tag=f"v{mikasa.__version__}")))
    c, _ = client
    body = c.get("/api/update/check").json()
    assert body["update_available"] is False
    assert body["latest"] == mikasa.__version__


def test_check_caches_within_ttl_and_force_bypasses_it(client, opener: _FakeOpener) -> None:
    c, _ = client
    c.get("/api/update/check")
    c.get("/api/update/check")
    assert len(opener.calls) == 1  # TTL 内第二次不打网络（GitHub 匿名配额有限）
    c.get("/api/update/check", params={"force": True})
    assert len(opener.calls) == 2


def test_check_maps_network_failure_to_502(client, monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    def _boom(url: str, timeout: float):
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr(release_mod, "_open", _boom)
    c, _ = client
    resp = c.get("/api/update/check")
    assert resp.status_code == 502
    assert resp.json()["error"]["type"] == "ProviderError"


# ---------------- /api/update/download ----------------


def test_download_rejects_when_already_latest(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release_mod, "_open", _FakeOpener(_payload(tag=f"v{mikasa.__version__}")))
    c, _ = client
    resp = c.post("/api/update/download")
    assert resp.status_code == 400
    assert "已是最新" in resp.json()["detail"]


def test_download_rejects_release_without_setup_asset(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(release_mod, "_open", _FakeOpener(_payload(with_setup=False)))
    c, _ = client
    resp = c.post("/api/update/download")
    assert resp.status_code == 400
    assert "没有安装包" in resp.json()["detail"]


def test_download_starts_job_and_status_reports_running(
    client, opener: _FakeOpener, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(update_router, "run_download", _noop_download)
    c, _ = client
    resp = c.post("/api/update/download")
    assert resp.status_code == 202
    assert resp.json()["version"] == "9.9.9"

    status = c.get("/api/update/download/status").json()
    assert status["status"] == "running"
    assert status["asset_name"] == _SETUP_NAME


def test_download_conflicts_when_one_is_already_running(
    client, opener: _FakeOpener, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(update_router, "run_download", _noop_download)
    c, _ = client
    assert c.post("/api/update/download").status_code == 202
    resp = c.post("/api/update/download")
    assert resp.status_code == 409


def test_download_then_install_full_flow(
    client, opener: _FakeOpener, monkeypatch: pytest.MonkeyPatch
) -> None:
    """下载完成 → 安装：安装器拿到的是 updates 目录里的那一个文件。"""
    c, settings = client
    started: list[str] = []

    def _fake_download(manager, release, updates_dir) -> None:  # noqa: ANN001
        updates_dir.mkdir(parents=True, exist_ok=True)
        path = updates_dir / release.setup_asset().name
        path.write_bytes(b"MZ")
        manager.progress(3, 3)
        manager.verifying()
        manager.finish(path)

    monkeypatch.setattr(update_router, "run_download", _fake_download)
    monkeypatch.setattr(
        update_router, "launch_installer", lambda path, dir_: started.append(str(path))
    )

    assert c.post("/api/update/download").status_code == 202
    assert c.get("/api/update/download/status").json()["status"] == "done"

    resp = c.post("/api/update/install")
    assert resp.status_code == 200
    assert resp.json()["status"] == "launched"
    assert started == [str((settings.data_dir / "updates" / _SETUP_NAME).resolve())]


def test_install_requires_finished_download(client, opener: _FakeOpener) -> None:
    c, _ = client
    resp = c.post("/api/update/install")
    assert resp.status_code == 400
    assert "还没有下载完成" in resp.json()["detail"]


def test_status_is_idle_without_any_job(client) -> None:
    c, _ = client
    assert c.get("/api/update/download/status").json() == {"status": "idle"}


def test_updates_dir_lives_under_data_dir(client) -> None:
    """下载落点固定在数据目录下（不放 %TEMP%：清理工具会顺手清掉）。"""
    _, settings = client
    assert update_router._updates_dir(settings) == Path(settings.data_dir) / "updates"
