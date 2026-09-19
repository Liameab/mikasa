"""更新下载与安装（mikasa.update.install）单元测试。

零网络：网络缝是 `install._open`，换成"按 URL 查表"的假实现；DNS 校验
注入假 resolve；`os.startfile` 用注入的假 starter 顶掉（**绝不真的启动
任何可执行文件**）。资产 URL 全部用 `http://127.0.0.1:9/...`——回环是
白名单的逃生门，走真实的 getaddrinfo 也稳定判为回环，不需要 DNS。
"""

from __future__ import annotations

import hashlib
import io
import os
import urllib.error
from pathlib import Path

import pytest

import mikasa.update.install as install_mod
from mikasa.errors import ProviderError
from mikasa.update.errors import UpdateError
from mikasa.update.install import (
    UpdateManager,
    download_asset,
    expected_sha256,
    fetch_text,
    launch_installer,
    run_download,
)
from mikasa.update.release import Asset, ReleaseInfo

_SETUP_NAME = "Mikasa-Setup-0.1.2-win64.exe"
_SETUP_URL = f"http://127.0.0.1:9/{_SETUP_NAME}"
_SUMS_URL = "http://127.0.0.1:9/SHA256SUMS.txt"
_SETUP_BYTES = b"MZ-fake-installer-payload"


def _addr(ip: str):
    """假 getaddrinfo 返回值（只要第 [4] 段的第一项）。"""
    return [(2, 1, 6, "", (ip, 0))]


def _public_resolve(host, port, type=None):  # noqa: ANN001, A002 - 与 socket.getaddrinfo 同形
    return _addr("93.184.216.34")


def _loopback_resolve(host, port, type=None):  # noqa: ANN001, A002
    return _addr("127.0.0.1")


class _FakeResp:
    """urlopen 返回值替身：read + headers + 上下文管理器。"""

    def __init__(self, data: bytes, content_length: int | None = None) -> None:
        self._buf = io.BytesIO(data)
        self.headers = {} if content_length is None else {"Content-Length": str(content_length)}

    def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _RouteOpener:
    """按 URL 查表的假网络缝；表里没有 → 404（HTTPError 走真实异常路径）。"""

    def __init__(self, routes: dict[str, bytes], lengths: dict[str, int] | None = None) -> None:
        self.routes = routes
        self.lengths = lengths or {}
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: float):
        self.calls.append(url)
        if url not in self.routes:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return _FakeResp(self.routes[url], self.lengths.get(url))


def _release(*assets: Asset, version: str = "0.1.2") -> ReleaseInfo:
    return ReleaseInfo(
        version=version,
        tag=f"v{version}",
        html_url=f"https://github.com/Liameab/mikasa/releases/tag/v{version}",
        notes="说明",
        published_at="2026-09-19T00:00:00Z",
        assets=assets,
    )


def _sums_for(data: bytes, name: str = _SETUP_NAME) -> bytes:
    return f"{hashlib.sha256(data).hexdigest()}  {name}\n".encode()


# ---------------- 下载地址白名单 ----------------


def test_validate_allows_github_hosts_over_https() -> None:
    install_mod._validate_asset_url("https://github.com/a/b")  # 不抛即过
    install_mod._validate_asset_url("https://objects.githubusercontent.com/x")
    install_mod._validate_asset_url("https://release-assets.githubusercontent.com/x")
    install_mod._validate_asset_url("https://api.github.com/x")


def test_validate_rejects_public_host_outside_allowlist() -> None:
    with pytest.raises(UpdateError, match="不是 GitHub 官方域名"):
        install_mod._validate_asset_url(
            "https://evil.example.com/setup.exe", resolve=_public_resolve
        )


def test_validate_rejects_non_github_host_even_if_loopback_scheme_is_https() -> None:
    """回环逃生门只对 http 开：https + 非白名单主机一律拒。"""
    with pytest.raises(UpdateError, match="不是 GitHub 官方域名"):
        install_mod._validate_asset_url(
            "https://evil.example.com/setup.exe", resolve=_loopback_resolve
        )


def test_validate_allows_http_loopback_for_e2e_fake_sources() -> None:
    install_mod._validate_asset_url("http://127.0.0.1:9/setup.exe", resolve=_loopback_resolve)


def test_validate_rejects_http_public_host() -> None:
    with pytest.raises(UpdateError, match="不是 GitHub 官方域名"):
        install_mod._validate_asset_url("http://example.com/setup.exe", resolve=_public_resolve)


@pytest.mark.parametrize("url", ["ftp://github.com/x", "file:///C:/setup.exe", "https:///x"])
def test_validate_rejects_other_schemes_and_missing_host(url: str) -> None:
    with pytest.raises(UpdateError):
        install_mod._validate_asset_url(url, resolve=_public_resolve)


def test_validate_maps_dns_failure_to_readable_error() -> None:
    def _boom(host, port, type=None):  # noqa: ANN001, A002
        raise OSError("no such host")

    with pytest.raises(UpdateError, match="无法解析"):
        install_mod._validate_asset_url("http://fake.invalid/x", resolve=_boom)


# ---------------- 下载 ----------------


def test_download_asset_writes_file_and_reports_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(install_mod, "_CHUNK", 4)  # 切小块才能验到多次进度回调
    opener = _RouteOpener({_SETUP_URL: _SETUP_BYTES})
    monkeypatch.setattr(install_mod, "_open", opener)
    dest = tmp_path / _SETUP_NAME
    seen: list[tuple[int, int]] = []

    written = download_asset(
        Asset(_SETUP_NAME, _SETUP_URL, len(_SETUP_BYTES)), dest, lambda d, t: seen.append((d, t))
    )

    assert written == len(_SETUP_BYTES)
    assert dest.read_bytes() == _SETUP_BYTES
    assert seen[0] == (0, len(_SETUP_BYTES))  # 首帧先给总量（前端能画进度条）
    assert seen[-1] == (len(_SETUP_BYTES), len(_SETUP_BYTES))
    assert [d for d, _ in seen] == sorted(d for d, _ in seen)


def test_download_asset_prefers_content_length_over_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """元信息里的 size 可能不准，跳转后的 Content-Length 才是真的。"""
    opener = _RouteOpener({_SETUP_URL: _SETUP_BYTES}, lengths={_SETUP_URL: len(_SETUP_BYTES)})
    monkeypatch.setattr(install_mod, "_open", opener)
    seen: list[tuple[int, int]] = []
    download_asset(
        Asset(_SETUP_NAME, _SETUP_URL, 99999),
        tmp_path / _SETUP_NAME,
        lambda d, t: seen.append((d, t)),
    )
    assert seen[0][1] == len(_SETUP_BYTES)


def test_download_asset_deletes_partial_file_on_truncated_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """声明 100 字节只给 5 字节 = 下载不完整：报错且**不留半成品**。"""
    opener = _RouteOpener({_SETUP_URL: b"short"}, lengths={_SETUP_URL: 100})
    monkeypatch.setattr(install_mod, "_open", opener)
    dest = tmp_path / _SETUP_NAME
    with pytest.raises(ProviderError, match="不完整"):
        download_asset(Asset(_SETUP_NAME, _SETUP_URL, 0), dest, lambda d, t: None)
    assert not dest.exists()


def test_download_asset_enforces_size_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install_mod, "_MAX_ASSET_BYTES", 8)
    monkeypatch.setattr(install_mod, "_CHUNK", 4)
    opener = _RouteOpener({_SETUP_URL: b"0123456789abcdef"})
    monkeypatch.setattr(install_mod, "_open", opener)
    dest = tmp_path / _SETUP_NAME
    with pytest.raises(UpdateError, match="体积上限"):
        download_asset(Asset(_SETUP_NAME, _SETUP_URL, 0), dest, lambda d, t: None)
    assert not dest.exists()


def test_download_asset_maps_http_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install_mod, "_open", _RouteOpener({}))  # 404
    with pytest.raises(ProviderError, match="HTTP 404"):
        download_asset(Asset(_SETUP_NAME, _SETUP_URL, 0), tmp_path / _SETUP_NAME, lambda d, t: None)


def test_fetch_text_rejects_oversized_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install_mod, "_open", _RouteOpener({_SUMS_URL: b"x" * 32}))
    with pytest.raises(UpdateError, match="超出预期大小"):
        fetch_text(_SUMS_URL, max_bytes=16)


# ---------------- 校验和 ----------------


def test_expected_sha256_parses_standard_lines() -> None:
    text = "abc123  a.exe\nDEF456 *b.exe\n\n# 注释行\n"
    assert expected_sha256(text, "a.exe") == "abc123"
    assert expected_sha256(text, "b.exe") == "def456"  # * 前缀（二进制模式）也要认
    assert expected_sha256(text, "c.exe") is None


# ---------------- 后台任务：下载 → 校验 → 落定 ----------------


def _ready_release(sums_bytes: bytes) -> ReleaseInfo:
    """标准"两资产"发布：安装包 + 校验和文件。"""
    return _release(
        Asset(_SETUP_NAME, _SETUP_URL, len(_SETUP_BYTES)),
        Asset("SHA256SUMS.txt", _SUMS_URL, len(sums_bytes)),
    )


def test_run_download_completes_and_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sums = _sums_for(_SETUP_BYTES)
    monkeypatch.setattr(
        install_mod, "_open", _RouteOpener({_SETUP_URL: _SETUP_BYTES, _SUMS_URL: sums})
    )
    manager = UpdateManager()
    assert manager.start("0.1.2", _SETUP_NAME) is True

    run_download(manager, _ready_release(sums), tmp_path / "updates")

    snapshot = manager.snapshot()
    assert snapshot["status"] == "done"
    assert snapshot["downloaded"] == len(_SETUP_BYTES)
    path = manager.result_path()
    assert path is not None and path.read_bytes() == _SETUP_BYTES
    assert path.parent == tmp_path / "updates"


def test_run_download_rejects_checksum_mismatch_and_removes_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sums = _sums_for(b"another-payload")  # 与下载到的字节不符
    monkeypatch.setattr(
        install_mod, "_open", _RouteOpener({_SETUP_URL: _SETUP_BYTES, _SUMS_URL: sums})
    )
    manager = UpdateManager()
    manager.start("0.1.2", _SETUP_NAME)

    run_download(manager, _ready_release(sums), tmp_path / "updates")

    snapshot = manager.snapshot()
    assert snapshot["status"] == "error"
    assert "校验和不符" in snapshot["error"]
    assert not (tmp_path / "updates" / _SETUP_NAME).exists()
    assert manager.result_path() is None


def test_run_download_reports_missing_checksums_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有 SHA256SUMS.txt 就不自动更新（宁可让用户手动下）。"""
    monkeypatch.setattr(install_mod, "_open", _RouteOpener({}))
    manager = UpdateManager()
    manager.start("0.1.2", _SETUP_NAME)

    run_download(manager, _release(Asset(_SETUP_NAME, _SETUP_URL, 1)), tmp_path / "updates")

    assert "没有校验和文件" in manager.snapshot()["error"]


def test_run_download_reports_missing_setup_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(install_mod, "_open", _RouteOpener({}))
    manager = UpdateManager()
    manager.start("0.1.2", "")
    run_download(manager, _release(), tmp_path / "updates")
    assert "没有安装包" in manager.snapshot()["error"]


def test_run_download_cleans_previous_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sums = _sums_for(_SETUP_BYTES)
    monkeypatch.setattr(
        install_mod, "_open", _RouteOpener({_SETUP_URL: _SETUP_BYTES, _SUMS_URL: sums})
    )
    updates = tmp_path / "updates"
    updates.mkdir(parents=True)
    stale = updates / "Mikasa-Setup-0.1.1-win64.exe"
    stale.write_bytes(b"old")
    manager = UpdateManager()
    manager.start("0.1.2", _SETUP_NAME)

    run_download(manager, _ready_release(sums), updates)

    assert manager.snapshot()["status"] == "done"
    assert not stale.exists()  # 旧版本残留被清掉，退出前只留本次这一个


# ---------------- 启动安装器（全部注入假 starter，绝不真的启动） ----------------


def test_launch_installer_calls_starter_with_resolved_path(tmp_path: Path) -> None:
    updates = tmp_path / "updates"
    updates.mkdir()
    exe = updates / _SETUP_NAME
    exe.write_bytes(b"MZ")
    started: list[str] = []

    launch_installer(exe, updates, start=started.append)

    assert started == [str(exe.resolve())]


def test_launch_installer_rejects_file_outside_updates_dir(tmp_path: Path) -> None:
    updates = tmp_path / "updates"
    updates.mkdir()
    outside = tmp_path / _SETUP_NAME
    outside.write_bytes(b"MZ")
    with pytest.raises(UpdateError, match="不在更新目录"):
        launch_installer(outside, updates, start=lambda _: None)


def test_launch_installer_rejects_non_setup_name(tmp_path: Path) -> None:
    updates = tmp_path / "updates"
    updates.mkdir()
    evil = updates / "evil.exe"
    evil.write_bytes(b"MZ")
    with pytest.raises(UpdateError, match="不是 Mikasa 安装包"):
        launch_installer(evil, updates, start=lambda _: None)


def test_launch_installer_rejects_missing_file(tmp_path: Path) -> None:
    updates = tmp_path / "updates"
    updates.mkdir()
    with pytest.raises(UpdateError, match="不在磁盘上"):
        launch_installer(updates / _SETUP_NAME, updates, start=lambda _: None)


@pytest.mark.skipif(os.name == "nt", reason="Windows 上 os.startfile 存在，这条只验非 Windows")
def test_launch_installer_refuses_without_startfile(tmp_path: Path) -> None:
    updates = tmp_path / "updates"
    updates.mkdir()
    exe = updates / _SETUP_NAME
    exe.write_bytes(b"MZ")
    with pytest.raises(UpdateError, match="仅支持 Windows"):
        launch_installer(exe, updates)


# ---------------- 任务状态机 ----------------


def test_manager_is_single_slot() -> None:
    manager = UpdateManager()
    assert manager.snapshot() is None
    assert manager.start("0.1.2", _SETUP_NAME) is True
    assert manager.start("0.1.2", _SETUP_NAME) is False  # 已有任务在跑

    manager.progress(10, 100)
    manager.verifying()
    assert manager.snapshot()["status"] == "verifying"
    assert manager.result_path() is None  # 校验中还不能安装

    manager.fail("网络断了")
    assert manager.snapshot()["status"] == "error"
    # error 后允许重占槽（重下一次）
    assert manager.start("0.1.2", _SETUP_NAME) is True


def test_manager_progress_ignored_after_terminal_state() -> None:
    """状态机收尾后迟到的进度回调不该把状态改回去。"""
    manager = UpdateManager()
    manager.start("0.1.2", _SETUP_NAME)
    manager.finish(Path("x"))
    manager.progress(1, 2)
    assert manager.snapshot()["status"] == "done"
