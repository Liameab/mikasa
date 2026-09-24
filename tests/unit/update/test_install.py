"""更新下载与安装（mikasa.update.install）单元测试。

零网络：网络缝是 `install._open`，换成"按 URL 查表"的假实现；DNS 校验
注入假 resolve；`os.startfile` 用注入的假 starter 顶掉（**绝不真的启动
任何可执行文件**）。资产 URL 全部用 `http://127.0.0.1:9/...`——回环是
白名单的逃生门，走真实的 getaddrinfo 也稳定判为回环，不需要 DNS。

续传（ADR-0024）用 `_FakeAsset` 演：它像真源一样认 Range、能中途掐断一次，
并记下每次请求的起点——"第二次请求是不是从断点接着下"就是靠它断言的。

慢网络（2026-09-20）用 `_PacedAsset` + `_FakeTimer` 演：真源的 `read(n)` 给的是
"此刻拿到的"而不是"缓冲区里有的"，所以它把余量按片喂、每喂一片把假钟往前拨——
"这条连接有多快"就是这么造出来的。假钟只替掉 install 模块里的 `time`，不污染
进程里的真时钟。
"""

from __future__ import annotations

import hashlib
import io
import os
import urllib.error
import urllib.request
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


def _part(dest: Path) -> Path:
    """半成品路径（与被测代码同一个拼法）。"""
    return install_mod._part_path(dest)


def _addr(ip: str):
    """假 getaddrinfo 返回值（只要第 [4] 段的第一项）。"""
    return [(2, 1, 6, "", (ip, 0))]


def _public_resolve(host, port, type=None):  # noqa: ANN001, A002 - 与 socket.getaddrinfo 同形
    return _addr("93.184.216.34")


def _loopback_resolve(host, port, type=None):  # noqa: ANN001, A002
    return _addr("127.0.0.1")


class _FakeResp:
    """urlopen 返回值替身：read + headers + status + 上下文管理器。"""

    def __init__(
        self,
        data: bytes,
        *,
        status: int = 200,
        content_length: int | None = None,
        content_range: str | None = None,
        encoding: str | None = None,
    ) -> None:
        self._buf = io.BytesIO(data)
        self.status = status
        self.headers: dict[str, str] = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        if content_range is not None:
            self.headers["Content-Range"] = content_range
        if encoding is not None:
            self.headers["Content-Encoding"] = encoding

    def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _FakeAsset:
    """一个"像真的"资产源：认 Range、可中途掐断一次、记录每次请求的起点。

    `ranges` 就是"续传真的发生了吗"的证据：`[0, 32]` = 先整份下、断在 32
    字节处、第二次从 32 接着下；`[0, 0]` = 中间从头重来过。
    """

    def __init__(self, data: bytes, *, cut_after: int | None = None) -> None:
        self.data = data
        self.cut_after = cut_after  # 仅第一次响应：写到这么多字节就硬断
        self.cuts_done = 0
        self.ranges: list[int] = []

    def __call__(self, range_start: int | None) -> _FakeResp:
        start = range_start or 0
        self.ranges.append(start)
        total = len(self.data)
        if start >= total:
            raise urllib.error.HTTPError(
                _SETUP_URL,
                416,
                "Range Not Satisfiable",
                {"Content-Range": f"bytes */{total}"},
                None,
            )
        body = self.data[start:]
        declared = len(body)
        cut = self.cut_after is not None and self.cuts_done == 0 and len(body) > self.cut_after
        if cut:
            self.cuts_done = 1
            body = body[: self.cut_after]  # 声明完整长度、只给一半 = 传输截断
        if start:
            return _FakeResp(
                body,
                status=206,
                content_length=declared,
                content_range=f"bytes {start}-{total - 1}/{total}",
            )
        return _FakeResp(body, status=200, content_length=declared)


class _FakeClock:
    """手工往前拨的单调钟（"读了多久"由假连接自己拨，见 `_StreamResp`）。"""

    def __init__(self) -> None:
        self.now = 0.0


class _FakeTimer:
    """替掉 install 模块里的 `time`：单调钟走假钟，sleep 只记账不真睡。"""

    def __init__(self, clock: _FakeClock) -> None:
        self._clock = clock
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self._clock.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


class _StreamResp:
    """一条连接的替身：余量按 `step` 一片片喂，每喂一片把假钟拨 `tick` 秒。

    `read(n)` 只给"此刻拿到的"（真源就是这样，不会一次给完），所以"很慢但活着
    的连接"= 每次给一点点、但每次都真的给了。`limit` = 这条连接最多给多少字节
    （给完就 return b"" 结束连接，用来演"每次都只下得动一小截"）。
    """

    def __init__(
        self,
        body: bytes,
        *,
        step: int,
        limit: int | None = None,
        status: int,
        headers: dict[str, str],
        clock: _FakeClock | None,
        tick: float,
    ) -> None:
        self._rest = body
        self._step = step
        self._limit = limit
        self._sent = 0
        self.status = status
        self.headers = headers
        self._clock = clock
        self._tick = tick

    def read(self, size: int = -1) -> bytes:
        if self._limit is not None and self._sent >= self._limit:
            return b""  # 这条连接到此为止（= 传输又断了）
        if self._clock is not None and self._tick:
            self._clock.now += self._tick
        piece, self._rest = self._rest[: self._step], self._rest[self._step :]
        self._sent += len(piece)
        return piece

    def __enter__(self) -> _StreamResp:
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _PacedAsset:
    """按"每片多大 / 每片花多久"演的假源：认 Range（续传从起点给余量）。

    `ranges` 与 `_FakeAsset` 同义：每次请求的起点，用来断言"换连接是接着下"。
    """

    def __init__(
        self,
        data: bytes,
        *,
        step: int,
        limit: int | None = None,
        tick: float = 0.0,
        clock: _FakeClock | None = None,
    ) -> None:
        self.data = data
        self.step = step
        self.limit = limit
        self.tick = tick
        self.clock = clock
        self.ranges: list[int] = []

    def __call__(self, range_start: int | None) -> _StreamResp:
        start = range_start or 0
        self.ranges.append(start)
        total = len(self.data)
        if start >= total:
            raise urllib.error.HTTPError(
                _SETUP_URL,
                416,
                "Range Not Satisfiable",
                {"Content-Range": f"bytes */{total}"},
                None,
            )
        body = self.data[start:]
        headers = {"Content-Length": str(len(body))}
        if start:
            headers["Content-Range"] = f"bytes {start}-{total - 1}/{total}"
            status = 206
        else:
            status = 200
        return _StreamResp(
            body,
            step=self.step,
            limit=self.limit,
            status=status,
            headers=headers,
            clock=self.clock,
            tick=self.tick,
        )


class _RouteOpener:
    """按 URL 查表的假网络缝；表里没有 → 404（HTTPError 走真实异常路径）。

    表的值可以是 bytes（整份下发）或 callable(range_start)（用来演
    206/416/截断这些分支）。`calls` 记 (url, 起点)，起点 None = 没带 Range。
    """

    def __init__(self, routes: dict[str, object], lengths: dict[str, int] | None = None) -> None:
        self.routes = routes
        self.lengths = lengths or {}
        self.calls: list[tuple[str, int | None]] = []

    def __call__(self, url: str, timeout: float, *, range_start: int | None = None):
        self.calls.append((url, range_start))
        if url not in self.routes:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        route = self.routes[url]
        if callable(route):
            return route(range_start)
        return _FakeResp(route, content_length=self.lengths.get(url))  # type: ignore[arg-type]

    @property
    def ranges(self) -> list[int | None]:
        """每次请求的 Range 起点（None = 没带）。"""
        return [start for _, start in self.calls]


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


# ---------------- Range 跨重定向 ----------------
#
# urllib 的重定向只复制**普通头**（content-length/content-type 之外的全部），
# 所以 Range 必须经 `headers=` 下发；这条不变量断了，续传跨 302 就静默失效。


def _redirect_handler():
    """逐跳复验的骨架在 utils/net（两个下载器共用），策略仍由本模块提供。"""
    from mikasa.utils.net import SafeRedirectHandler

    return SafeRedirectHandler(
        lambda u: install_mod._validate_asset_url(u, resolve=_public_resolve)
    )


def test_redirect_keeps_range_on_the_next_hop() -> None:
    handler = _redirect_handler()
    req = urllib.request.Request(
        "https://github.com/Liameab/mikasa/releases/download/v9.9.9/x.exe",
        headers={"User-Agent": "x", "Range": "bytes=1048576-"},
    )
    out = handler.redirect_request(
        req, io.BytesIO(), 302, "Found", {}, "https://objects.githubusercontent.com/y"
    )
    assert out is not None
    assert out.get_header("Range") == "bytes=1048576-"
    assert out.full_url == "https://objects.githubusercontent.com/y"


def test_open_sends_range_and_identity_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    """网络缝本身：起点 >0 才带 Range；始终要求 identity（免得按字节续写压缩流）。"""
    seen: dict[str, str] = {}

    class _Opener:
        def open(self, req, timeout=None):  # noqa: ANN001
            seen.update(req.headers)
            return _FakeResp(b"", content_length=0)

    monkeypatch.setattr(install_mod.urllib.request, "build_opener", lambda *a, **k: _Opener())
    with install_mod._open(_SETUP_URL, 5.0, range_start=32):
        pass
    assert seen["Range"] == "bytes=32-"
    assert seen["Accept-encoding"] == "identity"
    seen.clear()
    with install_mod._open(_SETUP_URL, 5.0):
        pass
    assert "Range" not in seen  # 没有半成品就不带 Range（不给服务端徒增分支）


# ---------------- 下载 ----------------


def test_download_asset_writes_part_and_reports_progress(
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
    assert _part(dest).read_bytes() == _SETUP_BYTES  # 成品由调用方校验后改名，这里只有半成品
    assert not dest.exists()
    assert seen[0] == (0, len(_SETUP_BYTES))  # 首帧先给总量（前端能画进度条）
    assert seen[-1] == (len(_SETUP_BYTES), len(_SETUP_BYTES))
    assert [d for d, _ in seen] == sorted(d for d, _ in seen)


def test_download_asset_prefers_content_length_over_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """元信息里的 size 可能不准，响应里的 Content-Length 才是真的。"""
    opener = _RouteOpener({_SETUP_URL: _SETUP_BYTES}, lengths={_SETUP_URL: len(_SETUP_BYTES)})
    monkeypatch.setattr(install_mod, "_open", opener)
    seen: list[tuple[int, int]] = []
    download_asset(
        Asset(_SETUP_NAME, _SETUP_URL, 99999),
        tmp_path / _SETUP_NAME,
        lambda d, t: seen.append((d, t)),
    )
    assert seen[0][1] == len(_SETUP_BYTES)


def test_download_asset_resumes_from_existing_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """半成品在 → 带起点续下（206 追加），进度从断点起报、不倒退。"""
    monkeypatch.setattr(install_mod, "_RETRY_BACKOFF", ())
    monkeypatch.setattr(install_mod, "_CHUNK", 4)
    source = _FakeAsset(_SETUP_BYTES)
    monkeypatch.setattr(install_mod, "_open", _RouteOpener({_SETUP_URL: source}))
    dest = tmp_path / _SETUP_NAME
    _part(dest).write_bytes(_SETUP_BYTES[:10])
    seen: list[tuple[int, int]] = []

    written = download_asset(
        Asset(_SETUP_NAME, _SETUP_URL, len(_SETUP_BYTES)), dest, lambda d, t: seen.append((d, t))
    )

    assert written == len(_SETUP_BYTES)
    assert source.ranges == [10]  # 一次请求，从 10 字节处接着下
    assert _part(dest).read_bytes() == _SETUP_BYTES
    assert seen[0] == (10, len(_SETUP_BYTES))
    assert seen[-1] == (len(_SETUP_BYTES), len(_SETUP_BYTES))


def test_download_asset_retries_after_a_cut_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """传输被掐断：退避重试并**从断点接着下**，半成品留着（不再白下整份）。"""
    monkeypatch.setattr(install_mod, "_RETRY_BACKOFF", (0.0,))
    monkeypatch.setattr(install_mod.time, "sleep", lambda _s: None)  # 退避别真等
    source = _FakeAsset(_SETUP_BYTES, cut_after=12)  # 先断在 12 字节
    monkeypatch.setattr(install_mod, "_open", _RouteOpener({_SETUP_URL: source}))
    dest = tmp_path / _SETUP_NAME
    retries: list[tuple[int, str]] = []

    written = download_asset(
        Asset(_SETUP_NAME, _SETUP_URL, len(_SETUP_BYTES)),
        dest,
        lambda d, t: None,
        on_retry=lambda attempt, why: retries.append((attempt, why)),
    )

    assert written == len(_SETUP_BYTES)
    assert source.ranges == [0, 12]  # 第二次真的从断点续
    assert retries and retries[0][0] == 2 and "不完整" in retries[0][1]
    assert _part(dest).read_bytes() == _SETUP_BYTES


def test_download_asset_keeps_going_past_the_old_attempt_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """每次只下得动一小截、每次都断：**照样下到完**（不再试 3 次就判失败）。

    这条是 2026-09-20 用户报"没有代理就没法更新"的直接回归：原来固定 3 次尝试，
    这条链路一半的连接连不上，3 次用光就认输；现在只要一轮让 `.part` 变大了
    就不算失败。场景故意设成 7 次重试——**既越过老的 3 次上限，也越过"连续没下动
    就认输"的 5 次阈值**，所以"下动了就不算失败"这条口径一破，本用例必红。
    """
    monkeypatch.setattr(install_mod.time, "sleep", lambda _s: None)
    data = b"x" * 800
    source = _PacedAsset(data, step=100, limit=100)  # 这条连接只下得动 100 字节
    monkeypatch.setattr(install_mod, "_open", _RouteOpener({_SETUP_URL: source}))
    dest = tmp_path / _SETUP_NAME
    retries: list[int] = []

    written = download_asset(
        Asset(_SETUP_NAME, _SETUP_URL, len(data)),
        dest,
        lambda d, t: None,
        on_retry=lambda attempt, why: retries.append(attempt),
    )

    assert written == len(data)
    assert source.ranges == [0, 100, 200, 300, 400, 500, 600, 700]  # 每次从断点接着下
    assert retries == [2, 3, 4, 5, 6, 7, 8]  # 7 次重试，全部发生在"下动了"之后
    assert _part(dest).read_bytes() == data


def test_download_asset_gives_up_only_after_consecutive_barren_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """连续几次**一点没下动**才认输：报错写清次数，半成品留着（下次接着下）。"""
    monkeypatch.setattr(install_mod.time, "sleep", lambda _s: None)
    opener = _RouteOpener({_SETUP_URL: b""}, lengths={_SETUP_URL: 100})
    monkeypatch.setattr(install_mod, "_open", opener)
    dest = tmp_path / _SETUP_NAME

    with pytest.raises(ProviderError, match="连续 5 次没能下动"):
        download_asset(Asset(_SETUP_NAME, _SETUP_URL, 0), dest, lambda d, t: None)

    assert len(opener.calls) == install_mod._MAX_BARREN_ATTEMPTS
    assert _part(dest).exists() and not dest.exists()


def test_download_asset_keeps_the_part_when_it_hits_the_hard_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """撞上硬上限也要**留着半成品**（下次点重试从它接着下，不是从 0 重来）。"""
    monkeypatch.setattr(install_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(install_mod, "_MAX_ATTEMPTS", 3)
    data = b"x" * 500
    source = _PacedAsset(data, step=100, limit=100)
    monkeypatch.setattr(install_mod, "_open", _RouteOpener({_SETUP_URL: source}))
    dest = tmp_path / _SETUP_NAME

    with pytest.raises(ProviderError, match="半成品留着"):
        download_asset(Asset(_SETUP_NAME, _SETUP_URL, len(data)), dest, lambda d, t: None)

    assert _part(dest).read_bytes() == data[:300]  # 3 次尝试各下动 100 字节
    assert not dest.exists()


def test_download_asset_switches_away_from_a_crawling_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """慢但活着的连接：换一条接着下；换够上限还是慢，就认了慢慢下完。

    造的是"整条链路都很慢"（每条连接都 1KB/5 秒 ≈ 200B/s，远低于地板）：
    前 `_SLOW_ABORT_LIMIT` 次各换一条（每次都接着下、字节不丢），之后不再掐
    ——握手要几十秒，来回换反倒更慢。所以连接数正好是"上限 + 1"。
    """
    clock = _FakeClock()
    timer = _FakeTimer(clock)
    monkeypatch.setattr(install_mod, "time", timer)
    monkeypatch.setattr(install_mod, "_CHUNK", 1024)
    data = b"x" * (64 * 1024)
    source = _PacedAsset(data, step=1024, tick=5.0, clock=clock)
    monkeypatch.setattr(install_mod, "_open", _RouteOpener({_SETUP_URL: source}))
    dest = tmp_path / _SETUP_NAME

    written = download_asset(Asset(_SETUP_NAME, _SETUP_URL, len(data)), dest, lambda d, t: None)

    assert written == len(data)
    assert len(source.ranges) == install_mod._SLOW_ABORT_LIMIT + 1
    assert source.ranges == sorted(source.ranges) and source.ranges[0] == 0
    assert _part(dest).read_bytes() == data


def test_download_asset_restarts_when_the_server_ignores_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """服务端不支持续传（回 200）：截断重写，结果照样正确。"""
    monkeypatch.setattr(install_mod, "_RETRY_BACKOFF", ())
    opener = _RouteOpener({_SETUP_URL: _SETUP_BYTES})
    monkeypatch.setattr(install_mod, "_open", opener)
    dest = tmp_path / _SETUP_NAME
    _part(dest).write_bytes(b"garbage-from-an-older-attempt")

    written = download_asset(
        Asset(_SETUP_NAME, _SETUP_URL, len(_SETUP_BYTES)), dest, lambda d, t: None
    )

    assert written == len(_SETUP_BYTES)
    assert opener.ranges == [len(b"garbage-from-an-older-attempt"), None]
    assert _part(dest).read_bytes() == _SETUP_BYTES


def test_download_asset_accepts_416_when_part_is_already_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """半成品已经跟远端一样长（416 + `bytes */N`）：当作下完了，交给校验和判真伪。"""
    monkeypatch.setattr(install_mod, "_RETRY_BACKOFF", ())
    source = _FakeAsset(_SETUP_BYTES)
    monkeypatch.setattr(install_mod, "_open", _RouteOpener({_SETUP_URL: source}))
    dest = tmp_path / _SETUP_NAME
    _part(dest).write_bytes(_SETUP_BYTES)

    written = download_asset(
        Asset(_SETUP_NAME, _SETUP_URL, len(_SETUP_BYTES)), dest, lambda d, t: None
    )

    assert written == len(_SETUP_BYTES)
    assert source.ranges == [len(_SETUP_BYTES)]  # 只发了一次请求（且没下任何字节）


def test_download_asset_restarts_when_remote_size_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Content-Range 的总长与发布元信息不符 = 远端换过包 → 删半成品整份重下。"""
    monkeypatch.setattr(install_mod, "_RETRY_BACKOFF", ())
    source = _FakeAsset(_SETUP_BYTES * 2)
    monkeypatch.setattr(install_mod, "_open", _RouteOpener({_SETUP_URL: source}))
    dest = tmp_path / _SETUP_NAME
    _part(dest).write_bytes(b"x" * 5)  # 长度对不上新包，但起点本身合法

    download_asset(Asset(_SETUP_NAME, _SETUP_URL, len(_SETUP_BYTES)), dest, lambda d, t: None)

    assert source.ranges == [5, 0]  # 第一次起点校验不过，第二次整份重下
    assert _part(dest).read_bytes() == _SETUP_BYTES * 2


def test_download_asset_enforces_size_cap_without_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """体积上限：按**总量**判（含已有半成品），命中就删半成品、不重试。"""
    monkeypatch.setattr(install_mod, "_MAX_ASSET_BYTES", 8)
    monkeypatch.setattr(install_mod, "_CHUNK", 4)
    opener = _RouteOpener({_SETUP_URL: b"0123456789abcdef"})
    monkeypatch.setattr(install_mod, "_open", opener)
    dest = tmp_path / _SETUP_NAME

    with pytest.raises(UpdateError, match="体积上限"):
        download_asset(Asset(_SETUP_NAME, _SETUP_URL, 0), dest, lambda d, t: None)

    assert len(opener.calls) == 1  # 策略类失败不重试
    assert not _part(dest).exists() and not dest.exists()


def test_download_asset_size_cap_counts_the_existing_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """续传场景下的上限：半成品 6 字节 + 远端声明 20 字节 > 上限 8 → 立刻中止。"""
    monkeypatch.setattr(install_mod, "_MAX_ASSET_BYTES", 8)
    opener = _RouteOpener({_SETUP_URL: _FakeAsset(b"0123456789abcdefghij")})
    monkeypatch.setattr(install_mod, "_open", opener)
    dest = tmp_path / _SETUP_NAME
    _part(dest).write_bytes(b"012345")

    with pytest.raises(UpdateError, match="体积上限"):
        download_asset(Asset(_SETUP_NAME, _SETUP_URL, 0), dest, lambda d, t: None)
    assert not _part(dest).exists()


def test_download_asset_rejects_compressed_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """压缩流按字节续写就是静默损坏：宁可当场失败。"""
    monkeypatch.setattr(install_mod, "_RETRY_BACKOFF", ())
    opener = _RouteOpener({_SETUP_URL: lambda _start: _FakeResp(b"", encoding="gzip")})
    monkeypatch.setattr(install_mod, "_open", opener)
    with pytest.raises(UpdateError, match="被压缩"):
        download_asset(Asset(_SETUP_NAME, _SETUP_URL, 0), tmp_path / _SETUP_NAME, lambda d, t: None)
    assert len(opener.calls) == 1


def test_download_asset_maps_http_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opener = _RouteOpener({})  # 404
    monkeypatch.setattr(install_mod, "_open", opener)
    with pytest.raises(ProviderError, match="HTTP 404"):
        download_asset(Asset(_SETUP_NAME, _SETUP_URL, 0), tmp_path / _SETUP_NAME, lambda d, t: None)
    assert len(opener.calls) == 1  # 4xx 不重试


def test_fetch_text_rejects_oversized_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install_mod, "_open", _RouteOpener({_SUMS_URL: b"x" * 32}))
    with pytest.raises(UpdateError, match="超出预期大小"):
        fetch_text(_SUMS_URL, max_bytes=16)


def test_fetch_text_retries_a_flaky_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """校验和只有几百字节，却在整场下载**之前**：一次握手失败不该让更新胎死腹中。"""
    calls = {"n": 0}

    def _flaky(url: str, timeout: float, *, range_start: int | None = None):  # noqa: ANN202
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError("connection reset")
        return _FakeResp(b"abc")

    monkeypatch.setattr(install_mod, "_open", _flaky)
    monkeypatch.setattr(install_mod.time, "sleep", lambda _s: None)

    assert fetch_text(_SUMS_URL, max_bytes=64) == "abc"
    assert calls["n"] == 2


def test_fetch_text_gives_up_after_repeated_network_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """网络一直不通：重试到上限后如实报"网络不可达或超时"（HTTP 类错误不重试）。"""
    calls = {"n": 0}

    def _dead(url: str, timeout: float, *, range_start: int | None = None):  # noqa: ANN202
        calls["n"] += 1
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(install_mod, "_open", _dead)
    monkeypatch.setattr(install_mod.time, "sleep", lambda _s: None)

    with pytest.raises(ProviderError, match="网络不可达或超时"):
        fetch_text(_SUMS_URL, max_bytes=64)
    assert calls["n"] == install_mod._TEXT_ATTEMPTS


def test_fetch_text_does_not_retry_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """404 之类再试也是同样的结果：一次就报错，不浪费时间。"""
    calls = {"n": 0}

    def _not_found(url: str, timeout: float, *, range_start: int | None = None):  # noqa: ANN202
        calls["n"] += 1
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(install_mod, "_open", _not_found)
    with pytest.raises(ProviderError, match="HTTP 404"):
        fetch_text(_SUMS_URL, max_bytes=64)
    assert calls["n"] == 1


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


def _started(manager: UpdateManager) -> install_mod.JobTicket:
    ticket = manager.start("0.1.2", _SETUP_NAME)
    assert ticket is not None
    return ticket


def test_expected_digest_prefers_the_api_digest_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有 API digest 时**不联网**取校验和。

    2026-09-21 用户报障（更新失败、进度永远 0%）的根因就是"先下 SHA256SUMS.txt"：
    那个文件在 github.com，国内握手会被掐（连吃三次 WinError 10054）。而
    GitHub API 的 asset.digest 是同一份文件的服务端哈希、走 api.github.com。
    """
    opener = _RouteOpener({})
    monkeypatch.setattr(install_mod, "_open", opener)
    setup = Asset(_SETUP_NAME, _SETUP_URL, 1, digest="sha256:" + "a" * 64)

    assert install_mod._expected_digest(None, setup) == "a" * 64
    assert opener.calls == []  # 一次都没连


def test_expected_digest_falls_back_to_sums_when_no_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有 digest（老 API / 假源）→ 回退下载校验和文件，判据一条不减。"""
    sums = _sums_for(_SETUP_BYTES)
    monkeypatch.setattr(install_mod, "_open", _RouteOpener({_SUMS_URL: sums}))
    setup = Asset(_SETUP_NAME, _SETUP_URL, len(_SETUP_BYTES))

    expected = install_mod._expected_digest(Asset("SHA256SUMS.txt", _SUMS_URL, len(sums)), setup)
    assert expected == hashlib.sha256(_SETUP_BYTES).hexdigest()


def test_expected_digest_refuses_when_neither_source_exists() -> None:
    """ "没有校验和文件"的老规矩不变：两个来源都没有 → 不自动更新。"""
    setup = Asset(_SETUP_NAME, _SETUP_URL, 1)
    with pytest.raises(UpdateError, match="既没有校验和文件"):
        install_mod._expected_digest(None, setup)


def test_run_download_works_with_digest_and_no_sums_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**发布里没有 SHA256SUMS.txt 也能更新**——只要安装包自带服务端 digest。"""
    source = _FakeAsset(_SETUP_BYTES)
    monkeypatch.setattr(install_mod, "_open", _RouteOpener({_SETUP_URL: source}))
    manager = UpdateManager()
    setup = Asset(
        _SETUP_NAME,
        _SETUP_URL,
        len(_SETUP_BYTES),
        digest="sha256:" + hashlib.sha256(_SETUP_BYTES).hexdigest(),
    )

    run_download(manager, _started(manager), _release(setup), tmp_path / "updates")

    assert manager.snapshot()["status"] == "done"
    path = manager.result_path()
    assert path is not None and path.read_bytes() == _SETUP_BYTES


def test_run_download_rejects_when_digest_disagrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """digest 与下载到的字节不符 → 与"校验和不符"同一条路：失败且删半成品。"""
    source = _FakeAsset(_SETUP_BYTES)
    monkeypatch.setattr(install_mod, "_open", _RouteOpener({_SETUP_URL: source}))
    manager = UpdateManager()
    setup = Asset(_SETUP_NAME, _SETUP_URL, len(_SETUP_BYTES), digest="sha256:" + "b" * 64)

    run_download(manager, _started(manager), _release(setup), tmp_path / "updates")

    assert manager.snapshot()["status"] == "error"
    assert "校验" in manager.snapshot()["error"]
    assert not _part(tmp_path / "updates" / _SETUP_NAME).exists()


def test_run_download_completes_and_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sums = _sums_for(_SETUP_BYTES)
    source = _FakeAsset(_SETUP_BYTES)
    monkeypatch.setattr(install_mod, "_open", _RouteOpener({_SETUP_URL: source, _SUMS_URL: sums}))
    manager = UpdateManager()
    ticket = _started(manager)

    run_download(manager, ticket, _ready_release(sums), tmp_path / "updates")

    snapshot = manager.snapshot()
    assert snapshot["status"] == "done"
    assert snapshot["downloaded"] == len(_SETUP_BYTES)
    path = manager.result_path()
    assert path is not None and path.read_bytes() == _SETUP_BYTES
    assert path.parent == tmp_path / "updates"
    assert not _part(path).exists()  # 半成品改名成成品，不留残余


def test_run_download_recovers_from_a_poisoned_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """半成品内容是坏的（长度却对得上）：续传拼出来必然校验不过 → 整份重下。

    这就是"毒前缀"闭环：不删半成品的话，用户每点一次都撞同一堵墙。
    """
    sums = _sums_for(_SETUP_BYTES)
    source = _FakeAsset(_SETUP_BYTES)
    monkeypatch.setattr(install_mod, "_open", _RouteOpener({_SETUP_URL: source, _SUMS_URL: sums}))
    updates = tmp_path / "updates"
    updates.mkdir()
    dest = updates / _SETUP_NAME
    _part(dest).write_bytes(b"Z" * 20)  # 前 20 字节是上次留下的垃圾
    manager = UpdateManager()

    run_download(manager, _started(manager), _ready_release(sums), updates)

    assert manager.snapshot()["status"] == "done"
    assert source.ranges == [20, 0]  # 先续传 → 校验不过 → 从头重下
    assert dest.read_bytes() == _SETUP_BYTES


def test_run_download_rejects_checksum_mismatch_and_removes_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """校验和始终不符（远端被换包）：失败，且**半成品被删掉**（不留毒瘤）。"""
    sums = _sums_for(b"another-payload")  # 与下载到的字节不符
    source = _FakeAsset(_SETUP_BYTES)
    monkeypatch.setattr(install_mod, "_open", _RouteOpener({_SETUP_URL: source, _SUMS_URL: sums}))
    updates = tmp_path / "updates"
    manager = UpdateManager()

    run_download(manager, _started(manager), _ready_release(sums), updates)

    snapshot = manager.snapshot()
    assert snapshot["status"] == "error"
    assert "校验和不符" in snapshot["error"]
    assert not (updates / _SETUP_NAME).exists()
    assert not _part(updates / _SETUP_NAME).exists()
    assert manager.result_path() is None
    assert source.ranges.count(0) == 2  # 整份下了两次（第二次是删掉半成品后的重来）


def test_run_download_reports_missing_checksums_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有 SHA256SUMS.txt 就不自动更新（宁可让用户手动下），且**一个字节都不下**。"""
    opener = _RouteOpener({})
    monkeypatch.setattr(install_mod, "_open", opener)
    manager = UpdateManager()

    run_download(
        manager,
        _started(manager),
        _release(Asset(_SETUP_NAME, _SETUP_URL, 1)),
        tmp_path / "updates",
    )

    assert "没有校验和文件" in manager.snapshot()["error"]
    assert opener.calls == []  # 校验和都拿不到，不该先花几分钟下安装包


def test_run_download_reports_missing_setup_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(install_mod, "_open", _RouteOpener({}))
    manager = UpdateManager()
    ticket = manager.start("0.1.2", "")
    assert ticket is not None
    run_download(manager, ticket, _release(), tmp_path / "updates")
    assert "没有安装包" in manager.snapshot()["error"]


def test_run_download_cleans_previous_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """残留清理：别的版本（含它下到一半的 .part）都扫掉，本次的两个留着。"""
    sums = _sums_for(_SETUP_BYTES)
    monkeypatch.setattr(
        install_mod, "_open", _RouteOpener({_SETUP_URL: _FakeAsset(_SETUP_BYTES), _SUMS_URL: sums})
    )
    updates = tmp_path / "updates"
    updates.mkdir(parents=True)
    stale = updates / "Mikasa-Setup-0.1.1-win64.exe"
    stale.write_bytes(b"old")
    stale_part = updates / "Mikasa-Setup-0.1.1-win64.exe.part"
    stale_part.write_bytes(b"half")
    mine = _part(updates / _SETUP_NAME)
    mine.write_bytes(_SETUP_BYTES[:4])  # 本次的半成品：留着，续传要用
    manager = UpdateManager()

    run_download(manager, _started(manager), _ready_release(sums), updates)

    assert manager.snapshot()["status"] == "done"
    assert not stale.exists() and not stale_part.exists()
    assert (updates / _SETUP_NAME).read_bytes() == _SETUP_BYTES


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


def test_launch_installer_rejects_part_file(tmp_path: Path) -> None:
    """半成品永远不可启动。

    名字闸门（SETUP_ASSET_RE）只在 `.part` 是后缀时才挡得住——把标记挪到
    `-win64.exe` 前面就能匹配上——所以另有一条显式闸门，这条用例钉住它。
    """
    updates = tmp_path / "updates"
    updates.mkdir()
    part = _part(updates / _SETUP_NAME)
    part.write_bytes(b"MZ")
    with pytest.raises(UpdateError, match="半成品"):
        launch_installer(part, updates, start=lambda _: None)
    sneaky = updates / "Mikasa-Setup-0.1.2.part-win64.exe"
    sneaky.write_bytes(b"MZ")
    assert install_mod.SETUP_ASSET_RE.fullmatch(sneaky.name)  # 名字闸门确实挡不住


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


def _tick_clock():
    """可控时钟：让"过了宽限期"这件事在测试里立刻发生。"""
    state = {"now": 1000.0}

    class _Clock:
        def __call__(self) -> float:
            return state["now"]

        def advance(self, seconds: float) -> None:
            state["now"] += seconds

    return _Clock()


def test_manager_is_single_slot() -> None:
    manager = UpdateManager()
    assert manager.snapshot() is None
    ticket = manager.start("0.1.2", _SETUP_NAME)
    assert ticket is not None and ticket.adopted is False

    manager.progress(ticket.token, 10, 100)
    manager.verifying(ticket.token)
    assert manager.snapshot()["status"] == "verifying"
    assert manager.result_path() is None  # 校验中还不能安装

    manager.fail(ticket.token, "网络断了")
    assert manager.snapshot()["status"] == "error"
    # error 后允许重占槽（重下一次）
    assert manager.start("0.1.2", _SETUP_NAME) is not None


def test_manager_adopts_a_live_job_instead_of_conflicting() -> None:
    """有活任务在跑 → 返回 None（调用方只跟随状态），不再是"抢不到就 409"。"""
    manager = UpdateManager()
    ticket = manager.start("0.1.2", _SETUP_NAME)
    assert ticket is not None
    manager.worker_started(ticket.token)
    assert manager.start("0.1.2", _SETUP_NAME) is None


def test_manager_grants_grace_before_taking_over() -> None:
    """线程还没登记的窗口期（BackgroundTasks 在响应之后才跑）不许被接管。"""
    clock = _tick_clock()
    manager = UpdateManager(grace=10.0, clock=clock)
    ticket = manager.start("0.1.2", _SETUP_NAME)
    assert ticket is not None
    clock.advance(9.0)
    assert manager.start("0.1.2", _SETUP_NAME) is None


def test_manager_takes_over_after_the_worker_died() -> None:
    """线程没了且过了宽限期 → 接管（这是"再也下不动"的自愈路径）。"""
    clock = _tick_clock()
    manager = UpdateManager(grace=10.0, clock=clock)
    ticket = manager.start("0.1.2", _SETUP_NAME)
    assert ticket is not None
    manager.progress(ticket.token, 5, 100)
    clock.advance(11.0)

    taken = manager.start("0.1.2", _SETUP_NAME)

    assert taken is not None and taken.adopted is True
    snapshot = manager.snapshot()
    assert snapshot["status"] == "running"
    assert snapshot["downloaded"] == 0  # 新槽，进度重来（文件层面会续传）
    assert snapshot["attempt"] == 1


def test_manager_ignores_a_stale_token() -> None:
    """被接管的旧线程再写什么都无效——否则僵尸 finish() 会抢走新任务的成果。"""
    clock = _tick_clock()
    manager = UpdateManager(grace=0.0, clock=clock)
    old = manager.start("0.1.2", _SETUP_NAME)
    assert old is not None
    clock.advance(1.0)
    new = manager.start("0.1.2", _SETUP_NAME)
    assert new is not None and new.token != old.token

    manager.progress(old.token, 99, 100)
    manager.verifying(old.token)
    manager.finish(old.token, Path("old"))
    manager.fail(old.token, "旧线程的抱怨")

    snapshot = manager.snapshot()
    assert snapshot["status"] == "running" and snapshot["downloaded"] == 0
    assert manager.result_path() is None
    assert manager.is_current(old.token) is False and manager.is_current(new.token) is True


def test_manager_does_not_take_over_a_finished_download(tmp_path: Path) -> None:
    """done 且成品还在 → 不重占槽（否则白下 90MB）；文件没了才允许重下。"""
    manager = UpdateManager()
    ticket = manager.start("0.1.2", _SETUP_NAME)
    assert ticket is not None
    exe = tmp_path / _SETUP_NAME
    exe.write_bytes(b"MZ")
    manager.finish(ticket.token, exe)

    assert manager.start("0.1.2", _SETUP_NAME) is None

    exe.unlink()
    assert manager.start("0.1.2", _SETUP_NAME) is not None


def test_manager_reports_attempt_and_stall() -> None:
    clock = _tick_clock()
    manager = UpdateManager(stall=90.0, clock=clock)
    ticket = manager.start("0.1.2", _SETUP_NAME)
    assert ticket is not None
    manager.worker_started(ticket.token)
    manager.progress(ticket.token, 1, 100)
    assert manager.snapshot()["stalled"] is False
    assert manager.snapshot()["alive"] is True

    manager.retrying(ticket.token, 2, "断了")
    clock.advance(91.0)

    snapshot = manager.snapshot()
    assert snapshot["attempt"] == 2
    assert snapshot["stalled"] is True  # 如实说"看起来停了"
    manager.worker_finished(ticket.token)
    assert manager.snapshot()["alive"] is False


def test_manager_progress_ignored_after_terminal_state(tmp_path: Path) -> None:
    """状态机收尾后迟到的进度回调不该把状态改回去。"""
    manager = UpdateManager()
    ticket = manager.start("0.1.2", _SETUP_NAME)
    assert ticket is not None
    manager.finish(ticket.token, tmp_path / "x")
    manager.progress(ticket.token, 1, 2)
    assert manager.snapshot()["status"] == "done"
