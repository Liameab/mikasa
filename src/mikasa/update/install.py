"""下载新版安装包 → 校验完整性 → 启动安装器（ADR-0022）。

**下载地址只来自 release.py 从 GitHub API 挑出的资产记录**，客户端
从头到尾碰不到 URL；因此这里唯一的入口校验是主机白名单（GitHub 自己
的下载域名 + 回环逃生门，与 papers/download.py 同一取舍）。

**完整性**：安装包与同批发布的 SHA256SUMS.txt 一起下载，逐字节核对
sha256 才允许启动安装器。挡的是"下载损坏 / 中途被替换"；发布账号本身
被攻破则挡不住（校验和与包同源），属已接受的残差，已记入 limitations。

**启动 = 双击语义**（os.startfile）：安装向导自己会先 taskkill 掉正在
运行的 Mikasa，所以调用方（Web 服务进程）随后会被结束——前端在这之后
不该再期待任何响应。
"""

from __future__ import annotations

import ipaddress
import os
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mikasa.errors import ProviderError, ZhiwenError, strip_paths
from mikasa.update.errors import UpdateError
from mikasa.update.release import SETUP_ASSET_RE, Asset, ReleaseInfo
from mikasa.utils.hashing import sha256_file
from mikasa.utils.logging import get_logger

logger = get_logger("update")

_CHUNK = 256 * 1024
_TIMEOUT = 60.0  # 连接与单块读超时（大文件按块读，每块各自计时）
_MAX_ASSET_BYTES = 600 * 1024 * 1024  # 安装包上限（实测 ~90MB，留足余量）
_MAX_SUMS_BYTES = 1024 * 1024  # 校验和文件上限（实测几百字节）
_MAX_REDIRECTS = 3
_USER_AGENT = "Mikasa/0.1 (update-download)"

# GitHub 官方的下载域名（browser_download_url 在 github.com，随后 302 到
# 后两者之一）。白名单是精确集合 + 后缀判断，见 _validate_asset_url。
_ALLOWED_HOSTS = frozenset(
    {
        "github.com",
        "api.github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)
_ALLOWED_HOST_SUFFIX = ".githubusercontent.com"
_DEFAULT_PORTS = {"https": 443, "http": 80}


def _validate_asset_url(url: str, *, resolve=socket.getaddrinfo) -> None:
    """下载前的入口校验：协议 + 主机白名单（回环例外见下）。

    resolve 可注入：单测传假 addrinfo，零真实 DNS（与 papers 同款）。
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UpdateError("更新下载仅支持 http/https 链接")
    host = parts.hostname or ""
    if not host:
        raise UpdateError("更新下载链接缺少主机名")
    if parts.scheme == "https" and (host in _ALLOWED_HOSTS or host.endswith(_ALLOWED_HOST_SUFFIX)):
        return
    # 其余一律要求 **http + 回环**：E2E 假源（本地假 GitHub + 假资产）的
    # 逃生门。回环打不到内网，SSRF 保证不因它松动——这一条同时挡住
    # "API 响应被换成任意公网主机"的情况（那是最容易被利用的方向）。
    if parts.scheme == "http":
        port = parts.port or _DEFAULT_PORTS["http"]
        try:
            infos = resolve(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise UpdateError(f"更新下载目标无法解析（{host}）：{exc}") from exc
        for info in infos:
            sockaddr = info[4]
            if sockaddr and ipaddress.ip_address(str(sockaddr[0])).is_loopback:
                return
    raise UpdateError("更新下载目标不是 GitHub 官方域名，已拒绝（安全策略）")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向逐跳复验 + 上限（重定向是绕过入口校验的第二入口）。"""

    def __init__(self, *, max_redirects: int = _MAX_REDIRECTS, resolve=socket.getaddrinfo) -> None:  # noqa: ANN001
        super().__init__()
        self._max_redirects = max_redirects
        self._resolve = resolve
        self._hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - 覆写 stdlib 签名
        self._hops += 1
        if self._hops > self._max_redirects:
            raise urllib.error.HTTPError(req.full_url, 502, "重定向次数超过上限", headers, fp)
        _validate_asset_url(newurl, resolve=self._resolve)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, timeout: float):
    """网络缝（带安全重定向处理器）：单测打桩点。"""
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    return opener.open(req, timeout=timeout)


def download_asset(
    asset: Asset,
    dest: Path,
    on_progress: Callable[[int, int], None],
    *,
    timeout: float = _TIMEOUT,
) -> int:
    """把资产下载到 dest，返回字节数。进度经 on_progress(done, total) 上报。

    total 优先取响应的 Content-Length（跳转后才是最终大小），拿不到时
    退回 release 元信息里的 size；两者都没有则为 0（前端显示不确定态）。
    失败抛 ZhiwenError，**不留半成品**。
    """
    _validate_asset_url(asset.url)
    try:
        resp = _open(asset.url, timeout)
    except urllib.error.HTTPError as exc:
        raise ProviderError(f"更新下载失败（HTTP {exc.code}）") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError("更新下载失败（网络不可达或超时）") from exc

    written = 0
    with resp:
        try:
            declared = int(resp.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            declared = 0
        total = declared or asset.size
        on_progress(0, total)
        with dest.open("wb") as out:
            try:
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > _MAX_ASSET_BYTES:
                        out.close()
                        dest.unlink(missing_ok=True)  # 不留半成品
                        raise UpdateError("安装包超过体积上限，已中止下载")
                    out.write(chunk)
                    on_progress(written, total)
            except (TimeoutError, OSError) as exc:
                out.close()
                dest.unlink(missing_ok=True)
                raise ProviderError(f"更新下载中断（{exc}）") from exc

    if total and written != total:
        dest.unlink(missing_ok=True)
        raise ProviderError(f"更新下载不完整（{written}/{total} 字节），请重试")
    return written


def fetch_text(url: str, *, max_bytes: int, timeout: float = _TIMEOUT) -> str:
    """下载一个小文本文件（SHA256SUMS.txt 用）；超限即中止。"""
    _validate_asset_url(url)
    try:
        resp = _open(url, timeout)
    except urllib.error.HTTPError as exc:
        raise ProviderError(f"校验和文件下载失败（HTTP {exc.code}）") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError("校验和文件下载失败（网络不可达或超时）") from exc
    with resp:
        raw = resp.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise UpdateError("校验和文件超出预期大小，已中止（安全策略）")
    return raw.decode("utf-8", errors="replace")


def expected_sha256(sums_text: str, asset_name: str) -> str | None:
    """从 SHA256SUMS 文本里取某文件的哈希（`<hash>  <name>` 每行一条）。"""
    for line in sums_text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        digest, name = parts
        if name.strip().lstrip("*") == asset_name:  # sha256sum 的二进制模式前缀
            return digest.strip().lower()
    return None


def launch_installer(
    path: Path, updates_dir: Path, *, start: Callable[[str], None] | None = None
) -> None:
    """启动安装向导（= 资源管理器里双击）。仅 Windows。

    三道闸：文件在、文件在更新目录里、名字是 Mikasa 安装包——防止任何
    路径把任意可执行文件塞进来再说"双击它"。start 可注入（单测在
    POSIX 上也能验闸门，不需要真的双击任何东西）。
    """
    resolved = path.resolve()
    if not resolved.is_file():
        raise UpdateError("安装包不在磁盘上（可能已被清理），请重新下载")
    if resolved.parent != updates_dir.resolve():
        raise UpdateError("安装包路径不在更新目录里，已拒绝启动（安全策略）")
    if not SETUP_ASSET_RE.fullmatch(resolved.name):
        raise UpdateError("要启动的文件不是 Mikasa 安装包，已拒绝（安全策略）")
    # 双击语义只有 Windows 有（os.startfile）；拿不到就是平台不支持
    starter = start if start is not None else getattr(os, "startfile", None)
    if starter is None:
        raise UpdateError("自动安装仅支持 Windows（发布包是 Windows 安装向导）")
    # E2E 逃生门：验收要走到"启动安装器"这一步，但假安装包只是几个字节，
    # 绝不能真的双击它。置 1 只让这一步**少做**一件事（不引入任何新的
    # 可执行路径），是安全方向上的开关。
    if os.environ.get("MIKASA_UPDATE_SKIP_LAUNCH") == "1":
        logger.info("MIKASA_UPDATE_SKIP_LAUNCH=1：跳过实际启动安装器（E2E）")
        return
    starter(str(resolved))


@dataclass
class UpdateJob:
    """一次后台下载的进度状态（线程内由 Lock 保护，读取走 snapshot）。"""

    status: str  # running（下载中）/ verifying（校验中）/ done / error
    version: str
    asset_name: str
    total: int = 0
    downloaded: int = 0
    error: str = ""
    file_path: str = ""

    def to_snapshot(self) -> dict:
        """轮询响应的不可变拷贝（调用方拿到的永远是某一时刻的一致快照）。"""
        return {
            "status": self.status,
            "version": self.version,
            "asset_name": self.asset_name,
            "total": self.total,
            "downloaded": self.downloaded,
            "error": self.error,
            "file_path": self.file_path,
        }


class UpdateManager:
    """单槽更新下载管理器：同一时刻只允许一个下载在跑（语义同 EvalJobManager）。

    状态流转：running → verifying → done | error。done/error 后允许再
    start 覆盖旧槽（重下一次）；所有读写都持 self._lock。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: UpdateJob | None = None

    def start(self, version: str, asset_name: str) -> bool:
        """占槽启动。已有 running/verifying 任务 → False（端点回 409）。"""
        with self._lock:
            if self._job is not None and self._job.status in ("running", "verifying"):
                return False
            self._job = UpdateJob(status="running", version=version, asset_name=asset_name)
            return True

    def progress(self, downloaded: int, total: int) -> None:
        with self._lock:
            if self._job is not None and self._job.status == "running":
                self._job.downloaded = downloaded
                if total:
                    self._job.total = total

    def verifying(self) -> None:
        """下载完成、开始核对校验和：状态可见（大文件算哈希要几秒）。"""
        with self._lock:
            if self._job is not None and self._job.status == "running":
                self._job.status = "verifying"

    def finish(self, path: Path) -> None:
        with self._lock:
            if self._job is not None:
                self._job.status = "done"
                self._job.file_path = str(path)

    def fail(self, message: str) -> None:
        with self._lock:
            if self._job is not None:
                self._job.status = "error"
                self._job.error = message

    def snapshot(self) -> dict | None:
        with self._lock:
            return self._job.to_snapshot() if self._job is not None else None

    def result_path(self) -> Path | None:
        """done 状态下的安装包路径（install 端点用）；其余状态 → None。"""
        with self._lock:
            if self._job is None or self._job.status != "done" or not self._job.file_path:
                return None
            return Path(self._job.file_path)


def run_download(manager: UpdateManager, release: ReleaseInfo, updates_dir: Path) -> None:
    """后台任务体：下载 → 校验 → 落定。异常全部落在任务状态里（不往上抛）。

    预检（有没有安装包/校验和资产）放在后台任务里而不是 POST 预检：
    它们只依赖已取到的 release 元信息，但把判据集中在一处，避免"预检
    放行、任务里才失败"两套逻辑漂移。
    """
    try:
        asset = release.setup_asset()
        if asset is None:
            raise UpdateError(
                "这个版本里没有安装包（Mikasa-Setup-*-win64.exe），请到发布页手动下载"
            )
        sums_asset = release.checksums_asset()
        if sums_asset is None:
            raise UpdateError("这个版本里没有校验和文件（SHA256SUMS.txt），为安全起见不自动更新")

        updates_dir.mkdir(parents=True, exist_ok=True)
        # 清掉上次的残留（含上次下到一半的文件）：退出前只留本次这一个
        for old in updates_dir.iterdir():
            if old.is_file() and old.name != asset.name:
                old.unlink(missing_ok=True)
        dest = updates_dir / asset.name

        download_asset(asset, dest, manager.progress)
        manager.verifying()

        sums_text = fetch_text(sums_asset.url, max_bytes=_MAX_SUMS_BYTES)
        expected = expected_sha256(sums_text, asset.name)
        if expected is None:
            raise UpdateError(f"校验和文件里没有 {asset.name} 的记录，已中止（安全策略）")
        actual = sha256_file(dest)
        if actual.lower() != expected:
            dest.unlink(missing_ok=True)
            raise UpdateError("安装包校验和不符，已删除（下载损坏或被篡改，请到发布页手动下载）")

        manager.finish(dest)
        logger.info("更新包已就绪：%s（%.0f MB）", asset.name, dest.stat().st_size / 1e6)
    except ZhiwenError as exc:
        logger.warning("更新下载失败：%s", exc)
        manager.fail(strip_paths(str(exc)))
    except Exception as exc:  # noqa: BLE001 - 后台任务兜底：任何意外都要落在状态里
        logger.exception("更新下载任务未预期失败")
        manager.fail(f"更新下载失败：{strip_paths(str(exc))}")
