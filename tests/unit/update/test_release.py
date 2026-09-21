"""更新检查（mikasa.update.release）单元测试：版本比较与 Release 解析。

零网络：网络缝是 `release._open`，换成"按脚本应答"的假实现。
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

import mikasa.update.release as release_mod
from mikasa.errors import ProviderError
from mikasa.update.release import (
    NOTES_CAP,
    fetch_latest_release,
    is_newer,
    parse_version,
    release_page_url,
)

_SETUP_URL = (
    "https://github.com/Liameab/mikasa/releases/download/v0.1.2/Mikasa-Setup-0.1.2-win64.exe"
)
_SUMS_URL = "https://github.com/Liameab/mikasa/releases/download/v0.1.2/SHA256SUMS.txt"


class _FakeResp:
    """urlopen 返回值的最小替身：只有 read 与上下文管理器。"""

    def __init__(self, payload: bytes) -> None:
        self._buf = io.BytesIO(payload)

    def read(self) -> bytes:
        return self._buf.read()

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _payload(**over) -> bytes:
    data = {
        "tag_name": "v0.1.2",
        "html_url": "https://github.com/Liameab/mikasa/releases/tag/v0.1.2",
        "body": "更新说明",
        "published_at": "2026-09-19T00:00:00Z",
        "assets": [
            {
                "name": "Mikasa-Setup-0.1.2-win64.exe",
                "browser_download_url": _SETUP_URL,
                "size": 123,
            },
            {"name": "SHA256SUMS.txt", "browser_download_url": _SUMS_URL, "size": 66},
        ],
    }
    data.update(over)
    return json.dumps(data).encode()


class _FakeOpener:
    """可脚本化的假网络缝：默认回一份正常 release，可改 payload 或注入异常。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.payload: bytes = _payload()
        self.error: Exception | None = None

    def __call__(self, url: str, timeout: float):
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return _FakeResp(self.payload)


@pytest.fixture()
def opener(monkeypatch: pytest.MonkeyPatch) -> _FakeOpener:
    fake = _FakeOpener()
    monkeypatch.setattr(release_mod, "_open", fake)
    return fake


# ---------------- 版本解析与比较 ----------------


def test_parse_version_accepts_tag_prefix_and_partial_versions() -> None:
    assert parse_version("v0.1.2") == (0, 1, 2)
    assert parse_version("0.1.2") == (0, 1, 2)
    assert parse_version(" v2 ") == (2,)
    assert parse_version("1.2.3.4") == (1, 2, 3, 4)


@pytest.mark.parametrize("text", ["", "v", "latest", "v1.2.0-beta", "v1.2.0+build", "1.2.x"])
def test_parse_version_rejects_anything_not_pure_numbers(text: str) -> None:
    """预发布标签一律判 None：宁可不提示，也不把 beta 推给普通用户。"""
    assert parse_version(text) is None


def test_is_newer_compares_numerically_not_lexically() -> None:
    assert is_newer("0.1.10", "0.1.9") is True  # 字符串比较会答 False
    assert is_newer("0.2", "0.1.9") is True  # 位数不同也能比（元组按位补零）
    assert is_newer("v0.1.1", "0.1.1") is False
    assert is_newer("0.1.0", "0.1.1") is False


def test_is_newer_is_conservative_when_either_side_unparsable() -> None:
    assert is_newer("v0.1.2-beta", "0.1.1") is False
    assert is_newer("0.1.2", "weird") is False


# ---------------- Release 解析 ----------------


def test_fetch_latest_release_parses_fields_and_assets(opener: _FakeOpener) -> None:
    info = fetch_latest_release()
    assert info.version == "0.1.2"  # 归一化：去掉 v 前缀
    assert info.tag == "v0.1.2"
    assert info.html_url.endswith("/v0.1.2")
    assert info.notes == "更新说明"
    assert info.setup_asset() is not None
    assert info.setup_asset().size == 123
    assert info.checksums_asset() is not None


def test_fetch_latest_release_requests_env_configured_endpoint(
    opener: _FakeOpener, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API 根与仓库名走环境变量（E2E 假源逃生门）。"""
    monkeypatch.setenv("MIKASA_UPDATE_API_BASE", "http://127.0.0.1:9999/")
    monkeypatch.setenv("MIKASA_UPDATE_REPO", "someone/fork")
    fetch_latest_release()
    assert opener.calls == ["http://127.0.0.1:9999/repos/someone/fork/releases/latest"]


def test_fetch_latest_release_truncates_long_notes(opener: _FakeOpener) -> None:
    opener.payload = _payload(body="长" * (NOTES_CAP + 500))
    assert len(fetch_latest_release().notes) == NOTES_CAP


def test_fetch_latest_release_skips_malformed_assets(opener: _FakeOpener) -> None:
    """资产列表里混进坏形状不该整批失败，只跳过那一条。"""
    opener.payload = _payload(
        assets=[
            None,
            {"name": "只有名字"},
            {"name": 42, "browser_download_url": _SETUP_URL},
            {"name": "Mikasa-Setup-0.1.2-win64.exe", "browser_download_url": _SETUP_URL, "size": 7},
        ]
    )
    info = fetch_latest_release()
    assert [a.name for a in info.assets] == ["Mikasa-Setup-0.1.2-win64.exe"]
    assert info.setup_asset().size == 7
    assert info.checksums_asset() is None


def test_fetch_latest_release_ignores_setup_assets_of_other_naming(opener: _FakeOpener) -> None:
    """只认 Mikasa-Setup-*-win64.exe：别的 exe 混进发布也不许被选中。"""
    opener.payload = _payload(
        assets=[{"name": "Mikasa.exe", "browser_download_url": _SETUP_URL, "size": 1}]
    )
    assert fetch_latest_release().setup_asset() is None


# ---------------- 失败归类（一律 ProviderError → 502） ----------------


def test_fetch_latest_release_maps_http_error(opener: _FakeOpener) -> None:
    opener.error = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
    with pytest.raises(ProviderError, match="HTTP 404"):
        fetch_latest_release()


def test_fetch_latest_release_maps_network_error(opener: _FakeOpener) -> None:
    opener.error = urllib.error.URLError("name resolution failed")
    with pytest.raises(ProviderError, match="网络不可达或超时"):
        fetch_latest_release()


def test_fetch_latest_release_maps_bad_json(opener: _FakeOpener) -> None:
    opener.payload = b"<html>not json</html>"
    with pytest.raises(ProviderError, match="无法解析"):
        fetch_latest_release()


def test_fetch_latest_release_rejects_missing_tag(opener: _FakeOpener) -> None:
    opener.payload = json.dumps({"assets": []}).encode()
    with pytest.raises(ProviderError, match="无法解析"):
        fetch_latest_release()


def test_fetch_latest_release_rejects_prerelease_tag(opener: _FakeOpener) -> None:
    opener.payload = _payload(tag_name="v0.2.0-rc1")
    with pytest.raises(ProviderError, match="版本号无法识别"):
        fetch_latest_release()


def test_release_page_url_falls_back_to_tag_rule() -> None:
    assert release_page_url("v0.1.2") == "https://github.com/Liameab/mikasa/releases/tag/v0.1.2"


def test_asset_parses_the_server_side_digest() -> None:
    """资产里带 GitHub 的服务端 sha256（`sha256:…`）时要解析出来（ADR-0024 补丁）。

    它是"更新失败、进度永远 0%"那条报障的修法核心：有它就不必去 github.com
    下载校验和文件（那条链路在国内常被掐）。
    """
    from mikasa.update.release import _asset_from_json

    asset = _asset_from_json(
        {
            "name": "Mikasa-Setup-0.1.11-win64.exe",
            "browser_download_url": "https://github.com/x/y/releases/download/v1/a.exe",
            "size": 123,
            "digest": "sha256:" + "c" * 64,
        }
    )
    assert asset is not None and asset.digest == "sha256:" + "c" * 64

    # 缺席 / 空串 / 非字符串都当作"没有"（老 API 与假源会遇到）
    for raw in (
        {"name": "a", "browser_download_url": "https://github.com/x/y", "size": 1},
        None,
        7,
    ):
        plain = _asset_from_json(
            {
                **(raw if isinstance(raw, dict) else {}),
                "name": "a",
                "browser_download_url": "https://github.com/x/y",
                "size": 1,
            }
        )
        assert plain is not None and plain.digest is None
