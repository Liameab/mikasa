"""下载新版安装包 → 校验完整性 → 启动安装器（ADR-0022 / ADR-0024）。

**下载地址只来自 release.py 从 GitHub API 挑出的资产记录**，客户端
从头到尾碰不到 URL；因此这里唯一的入口校验是主机白名单（GitHub 自己
的下载域名 + 回环逃生门，与 papers/download.py 同一取舍）。

**完整性**：安装包与同批发布的 SHA256SUMS.txt 一起下载，逐字节核对
sha256 才允许启动安装器。挡的是"下载损坏 / 中途被替换"；发布账号本身
被攻破则挡不住（校验和与包同源），属已接受的残差，已记入 limitations。

**断点续传**（ADR-0024）：先落到 `xxx.exe.part`，之后带 `Range` 接着下，
传输类失败按退避自动重试——实测这条链路 ~300KB/s 且随时可能被 reset，
"断一次就从 0 重来"等于每次断都白等几分钟。半成品只允许存在于 `.part`
（后缀形态，launch_installer 有显式闸门），且**只在传输类失败时保留**；
完整性类失败（校验和不符 / 体积超限）一律删掉，免得留下"毒前缀"让
下一次续传一直撞同一堵墙。

**启动 = 双击语义**（os.startfile）：安装向导自己会先 taskkill 掉正在
运行的 Mikasa，所以调用方（Web 服务进程）随后会被结束——前端在这之后
不该再期待任何响应。
"""

from __future__ import annotations

import http.client
import ipaddress
import os
import re
import socket
import threading
import time
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

_PART_SUFFIX = ".part"  # 半成品后缀（**只能是后缀**，见 _part_path）
_RETRY_ATTEMPTS = 3  # 一次下载最多尝试几次（含首次）
_RETRY_BACKOFF = (2.0, 5.0)  # 每次失败后的退避秒数（长度 = 尝试次数 - 1）
_STALL_SECONDS = 90.0  # 状态冻结多久算"停滞"（单块读超时 60s，故 90s 是真停了）
_WORKER_GRACE = 10.0  # 占槽后等工作线程登记存活的宽限期（见 UpdateManager.start）

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
    """重定向逐跳复验 + 上限（重定向是绕过入口校验的第二入口）。

    跳数计数器是**每个 opener 一份**（_open 每次新建）：重试不该吃掉配额。
    """

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


def _open(url: str, timeout: float, *, range_start: int | None = None):
    """网络缝（带安全重定向处理器）：单测打桩点。

    range_start 走 `headers=` 下发（普通头）：stdlib 的重定向只复制普通头，
    `add_unredirected_header` 那类**不会被继承**——续传跨 302 就断了。
    Accept-Encoding 显式要 identity：按字节续写的前提是响应没被压缩。
    """
    headers = {"User-Agent": _USER_AGENT, "Accept-Encoding": "identity"}
    if range_start:
        headers["Range"] = f"bytes={range_start}-"
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    req = urllib.request.Request(url, headers=headers)
    return opener.open(req, timeout=timeout)


class _Transient(Exception):
    """传输类失败：连接断了 / 读超时 / 响应截断——半成品可续，应重试。"""


class _PartInvalid(Exception):
    """磁盘上的半成品不是远端文件的有效前缀（远端换包 / 起点不符）→ 删了重下。"""


def _part_path(dest: Path) -> Path:
    """半成品路径 = `dest` 同名 + `.part` 后缀。

    **只能加后缀**：`Path.with_suffix(".part")` 会把 `.exe` 换掉，得到
    `Mikasa-Setup-0.1.2-win64.part` 这种"看着就像安装包"的名字——那等于
    把"半成品不可启动"这条不变量交给运气。配对的闸门在 launch_installer。
    """
    return dest.with_name(dest.name + _PART_SUFFIX)


_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)
_UNSATISFIED_RE = re.compile(r"bytes\s+\*/(\d+)", re.IGNORECASE)


def _parse_range(header: str | None) -> tuple[int, int] | None:
    """`bytes 100-499/1234` → (起点, 总长)；总长未知（`*`）记 0。认不出 → None。"""
    match = _RANGE_RE.fullmatch((header or "").strip())
    if match is None:
        return None
    total = 0 if match.group(3) == "*" else int(match.group(3))
    return int(match.group(1)), total


def _unsatisfied_total(header: str | None) -> int:
    """416 响应里的 `bytes */1234` → 1234；认不出 → 0。"""
    match = _UNSATISFIED_RE.fullmatch((header or "").strip())
    return int(match.group(1)) if match else 0


def _declared_length(resp) -> int:  # noqa: ANN001 - 鸭子类型的响应对象
    try:
        return int(resp.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        return 0


def _download_once(
    asset: Asset,
    part: Path,
    on_progress: Callable[[int, int], None],
    *,
    timeout: float,
) -> int:
    """一次下载尝试：尽量把 part 补齐（有半成品就续），返回补齐后的大小。

    三种异常各归其位：`_Transient`（可重试）/ `_PartInvalid`（半成品作废，
    从头来）/ 其余（UpdateError = 策略类，ProviderError = HTTP 错误，直接失败）。
    """
    base = part.stat().st_size if part.exists() else 0
    try:
        resp = _open(asset.url, timeout, range_start=base or None)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and base:
            # 要的位置越界 = 远端已经不比本地半成品长：交给校验和判真伪
            total = _unsatisfied_total(exc.headers.get("Content-Range") if exc.headers else None)
            if total and total == base:
                return base
            raise _PartInvalid(f"续传位置越界（416，远端 {total or '未知'} 字节）") from exc
        raise ProviderError(f"更新下载失败（HTTP {exc.code}）") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _Transient(f"网络不可达或超时：{exc}") from exc

    with resp:
        encoding = (resp.headers.get("Content-Encoding") or "").strip().lower()
        if encoding not in ("", "identity"):
            # 压缩响应按字节续写就是静默损坏，宁可失败
            raise UpdateError(f"更新下载响应被压缩（{encoding}），已中止（安全策略）")
        declared = _declared_length(resp)
        if getattr(resp, "status", 200) == 206:
            parsed = _parse_range(resp.headers.get("Content-Range"))
            if parsed is None or parsed[0] != base:
                raise _PartInvalid("续传起点与本地半成品不符")
            total = parsed[1] or base + declared
            if asset.size and total and total != asset.size:
                raise _PartInvalid(f"远端安装包大小已变（{total} ≠ {asset.size}）")
            append = True
        else:
            # 200 = 服务端忽略了 Range（或本来就没带）：整份重写
            if base:
                raise _PartInvalid("服务端不支持续传（回了完整响应）")
            total = declared or asset.size
            append = False
        if total and total > _MAX_ASSET_BYTES:
            raise UpdateError("安装包超过体积上限，已中止下载")

        on_progress(base, total)
        written = 0
        try:
            with part.open("ab" if append else "wb") as out:
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if base + written > _MAX_ASSET_BYTES:
                        raise UpdateError("安装包超过体积上限，已中止下载")
                    out.write(chunk)
                    on_progress(base + written, total)
        except (TimeoutError, OSError, http.client.IncompleteRead) as exc:
            # 半成品留在磁盘上：下一次尝试（或用户下次点重试）从它接着下
            raise _Transient(f"下载中断：{exc}") from exc

    # 完整性判据按**最终大小**（半成品 + 本次写入），不是"本次写了多少"——
    # 这一条不改，续传会被自己的完整性检查判死。
    if total and base + written != total:
        raise _Transient(f"下载不完整（{base + written}/{total} 字节）")
    return base + written


def download_asset(
    asset: Asset,
    dest: Path,
    on_progress: Callable[[int, int], None],
    *,
    timeout: float = _TIMEOUT,
    on_retry: Callable[[int, str], None] | None = None,
) -> int:
    """把资产完整下到 dest，返回字节数。进度经 on_progress(已下, 总量) 上报。

    总量优先取响应里的权威值（206 的 Content-Range / 200 的 Content-Length），
    拿不到才退回 release 元信息里的 size；都没有则为 0（前端显示不确定态）。

    半成品在 `dest + ".part"`；传输类失败退避重试，**重试与跨会话都从它接着
    下**。调用方仍必须核对 sha256：`.part` 可能是上一轮的半截，也可能远端
    已经换过包——本函数无从判断。
    """
    _validate_asset_url(asset.url)
    part = _part_path(dest)
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            return _download_once(asset, part, on_progress, timeout=timeout)
        except _PartInvalid as exc:
            logger.warning("更新续传基准作废（%s），从头重下", exc)
            part.unlink(missing_ok=True)
            if attempt >= _RETRY_ATTEMPTS:
                raise ProviderError(f"更新下载失败（{exc}），已重试 {attempt} 次") from exc
        except _Transient as exc:
            if attempt >= _RETRY_ATTEMPTS:
                raise ProviderError(f"更新下载中断（{exc}），已重试 {attempt - 1} 次") from exc
            backoff = _RETRY_BACKOFF[min(attempt - 1, len(_RETRY_BACKOFF) - 1)]
            logger.warning("更新下载中断（%s），%.0f 秒后接着下", exc, backoff)
            if on_retry is not None:
                on_retry(attempt + 1, str(exc))
            time.sleep(backoff)
        except UpdateError:
            part.unlink(missing_ok=True)  # 策略类失败（超限/被压缩）不留半成品
            raise
    raise AssertionError("重试循环不可能走到这里")  # pragma: no cover - 兜底防御


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

    四道闸：文件在、文件在更新目录里、不是半成品、名字是 Mikasa 安装包——
    防止任何路径把任意可执行文件塞进来再说"双击它"。start 可注入（单测在
    POSIX 上也能验闸门，不需要真的双击任何东西）。
    """
    resolved = path.resolve()
    if not resolved.is_file():
        raise UpdateError("安装包不在磁盘上（可能已被清理），请重新下载")
    if resolved.parent != updates_dir.resolve():
        raise UpdateError("安装包路径不在更新目录里，已拒绝启动（安全策略）")
    if resolved.name.endswith(_PART_SUFFIX):
        # 双保险：SETUP_ASSET_RE 只在标记是后缀时才挡得住（把 .part 挪到
        # -win64.exe 前面就能匹配上），所以把这条不变量写在真正依赖它的地方
        raise UpdateError("这是没下完的半成品，已拒绝启动（安全策略）")
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


@dataclass(frozen=True)
class JobTicket:
    """一次占槽的凭据。

    token 决定"谁有权改状态"：被接管的旧工作线程拿着过期 token，它的每次
    写入都会被丢弃——否则僵尸线程会把新任务的状态改回去，甚至往同一个
    `.part` 里续写。adopted=True 表示"接上/接管了已有任务"（前端只跟随状态，
    别再起新的）。
    """

    token: str
    adopted: bool


@dataclass
class UpdateJob:
    """一次后台下载的进度状态（线程内由 Lock 保护，读取走 snapshot）。"""

    token: str
    status: str  # running（下载中）/ verifying（校验中）/ done / error
    version: str
    asset_name: str
    total: int = 0
    downloaded: int = 0
    attempt: int = 1  # 第几次尝试（重试时递增，前端如实显示）
    error: str = ""
    file_path: str = ""
    started_at: float = 0.0  # 单调时钟：只用于算宽限期与停滞
    updated_at: float = 0.0
    worker: bool = False  # 工作线程是否已登记存活（见 UpdateManager.attach）

    def to_snapshot(self, now: float, stall: float) -> dict:
        """轮询响应的不可变拷贝（调用方拿到的永远是某一时刻的一致快照）。"""
        active = self.status in ("running", "verifying")
        return {
            "status": self.status,
            "version": self.version,
            "asset_name": self.asset_name,
            "total": self.total,
            "downloaded": self.downloaded,
            "attempt": self.attempt,
            "error": self.error,
            "file_path": self.file_path,
            # 派生字段：alive=False 且过了宽限期 = 线程没了，新任务可以接管；
            # stalled 只是"看起来停了"（前端据此说一句实话），不等于可以接管
            "alive": self.worker,
            "stalled": active and (now - self.updated_at) > stall,
        }


class UpdateManager:
    """单槽更新下载管理器：同一时刻只允许一个下载在跑（语义同 EvalJobManager）。

    状态流转：running → verifying → done | error。与评测槽的差别在于这个槽
    必须**自愈**：下载是分钟级任务，工作线程一旦悄悄消失（异常没落在状态里、
    被杀），槽会永远停在 running，之后每次 POST 都撞 409——用户看到的就是
    "再也下不动了"。所以这里额外记两件事：工作线程存活（worker_started /
    worker_finished，由任务外壳保证配对）与状态最后变化时刻；线程没了且过了
    宽限期，新任务可以直接接管（决策表见 start）。
    """

    def __init__(
        self,
        *,
        grace: float = _WORKER_GRACE,
        stall: float = _STALL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.Lock()
        self._job: UpdateJob | None = None
        self._grace = grace
        self._stall = stall
        self._clock = clock
        self._seq = 0

    def start(self, version: str, asset_name: str) -> JobTicket | None:
        """占槽 / 接上 / 接管。返回 None = 已有活任务，调用方只跟随状态。

        决策表（busy = 工作线程活着，或还没过启动宽限期）：
          槽空 / error                             → 新占槽
          running·verifying + 同版本同资产 + busy    → None（接上，不重下）
          running·verifying + 其余情况               → 接管（线程已死 / 换了版本）
          done + 文件还在 + 同版本                   → None（别白下 90MB，该去安装）
          done + 文件没了 / 换了版本                 → 新占槽
        """
        with self._lock:
            job = self._job
            if job is not None and job.status in ("running", "verifying"):
                same = job.version == version and job.asset_name == asset_name
                if same and self._busy(job):
                    return None
                return self._occupy(version, asset_name, adopted=True)
            if (
                job is not None
                and job.status == "done"
                and job.version == version
                and job.file_path
                and Path(job.file_path).is_file()
            ):
                return None
            return self._occupy(version, asset_name, adopted=False)

    def _occupy(self, version: str, asset_name: str, *, adopted: bool) -> JobTicket:
        """换一个新槽（调用方持锁）。"""
        now = self._clock()
        self._seq += 1
        self._job = UpdateJob(
            token=f"job{self._seq}",
            status="running",
            version=version,
            asset_name=asset_name,
            started_at=now,
            updated_at=now,
        )
        return JobTicket(token=self._job.token, adopted=adopted)

    def _busy(self, job: UpdateJob) -> bool:
        """任务是否占着槽：工作线程活着，或还没过启动宽限期（调用方持锁）。"""
        return job.worker or (self._clock() - job.started_at) < self._grace

    def worker_started(self, token: str) -> None:
        """工作线程登记存活（任务外壳入口）。"""
        with self._lock:
            job = self._job
            if job is not None and job.token == token:
                job.worker = True
                job.updated_at = self._clock()

    def worker_finished(self, token: str) -> None:
        """工作线程退出（任务外壳 finally）：此后槽可被接管。"""
        with self._lock:
            job = self._job
            if job is not None and job.token == token:
                job.worker = False
                job.updated_at = self._clock()

    def is_current(self, token: str) -> bool:
        """工作线程动手前自查：token 过期（已被接管）就该停手。"""
        with self._lock:
            return self._job is not None and self._job.token == token

    def progress(self, token: str, downloaded: int, total: int) -> None:
        with self._lock:
            job = self._job
            if job is not None and job.token == token and job.status == "running":
                job.downloaded = downloaded
                if total:
                    job.total = total
                job.updated_at = self._clock()

    def retrying(self, token: str, attempt: int, reason: str) -> None:
        """一次传输失败后的重试：次数可见（前端据此说"正在重试第 N 次"）。"""
        with self._lock:
            job = self._job
            if job is not None and job.token == token and job.status == "running":
                job.attempt = attempt
                job.updated_at = self._clock()
                logger.info("更新下载重试第 %d 次：%s", attempt, reason)

    def verifying(self, token: str) -> None:
        """下载完成、开始核对校验和：状态可见（大文件算哈希要几秒）。"""
        with self._lock:
            job = self._job
            if job is not None and job.token == token and job.status == "running":
                job.status = "verifying"
                job.updated_at = self._clock()

    def finish(self, token: str, path: Path) -> None:
        with self._lock:
            job = self._job
            if job is not None and job.token == token:
                job.status = "done"
                job.file_path = str(path)
                job.updated_at = self._clock()

    def fail(self, token: str, message: str) -> None:
        with self._lock:
            job = self._job
            if job is not None and job.token == token:
                job.status = "error"
                job.error = message
                job.updated_at = self._clock()

    def snapshot(self) -> dict | None:
        with self._lock:
            if self._job is None:
                return None
            return self._job.to_snapshot(self._clock(), self._stall)

    def result_path(self) -> Path | None:
        """done 状态下的安装包路径（install 端点用）；其余状态 → None。"""
        with self._lock:
            if self._job is None or self._job.status != "done" or not self._job.file_path:
                return None
            return Path(self._job.file_path)


def _clear_residue(updates_dir: Path, asset_name: str) -> None:
    """清掉本次之外的残留：只留本次的成品与半成品。

    版本号就在文件名里，所以"跨版本续传"这种危险事天然不可能发生；
    别的版本下到一半的 `.part` 也一并扫掉，不让它们白占几十兆磁盘。
    """
    keep = {asset_name, asset_name + _PART_SUFFIX}
    for old in updates_dir.iterdir():
        if old.is_file() and old.name not in keep:
            old.unlink(missing_ok=True)


def _expected_digest(sums_asset: Asset, setup: Asset) -> str:
    """取本次安装包的期望 sha256；校验和文件里没有记录 → 中止（老规矩）。"""
    sums_text = fetch_text(sums_asset.url, max_bytes=_MAX_SUMS_BYTES)
    expected = expected_sha256(sums_text, setup.name)
    if expected is None:
        raise UpdateError(f"校验和文件里没有 {setup.name} 的记录，已中止（安全策略）")
    return expected


def run_download(
    manager: UpdateManager, ticket: JobTicket, release: ReleaseInfo, updates_dir: Path
) -> None:
    """后台任务体：下载 → 校验 → 落定。异常全部落在任务状态里（不往上抛）。

    预检（有没有安装包/校验和资产）放在后台任务里而不是 POST 预检：
    它们只依赖已取到的 release 元信息，但把判据集中在一处，避免"预检
    放行、任务里才失败"两套逻辑漂移。
    """
    token = ticket.token
    try:
        asset = release.setup_asset()
        if asset is None:
            raise UpdateError(
                "这个版本里没有安装包（Mikasa-Setup-*-win64.exe），请到发布页手动下载"
            )
        sums_asset = release.checksums_asset()
        if sums_asset is None:
            raise UpdateError("这个版本里没有校验和文件（SHA256SUMS.txt），为安全起见不自动更新")

        setup = asset  # 上面已挡掉 None；另起个名字，闭包里也保住收窄
        updates_dir.mkdir(parents=True, exist_ok=True)
        _clear_residue(updates_dir, setup.name)
        dest = updates_dir / setup.name
        part = _part_path(dest)
        # 校验和先取：没有它就不该先花几分钟下 80MB
        expected = _expected_digest(sums_asset, setup)

        def fetch() -> None:
            download_asset(
                setup,
                dest,
                lambda done, total: manager.progress(token, done, total),
                on_retry=lambda attempt, why: manager.retrying(token, attempt, why),
            )

        fetch()
        if not manager.is_current(token):
            logger.info("更新任务已被接管，旧线程停手：%s", asset.name)
            return
        if sha256_file(part) != expected:
            # 校验和不符的两个来源：上一轮留下的"毒前缀"，或远端重传过包。
            # 删掉整份再来一次；再不符就认输（别让它一直撞同一堵墙）。
            logger.warning("安装包校验和不符，删除半成品整份重下：%s", asset.name)
            part.unlink(missing_ok=True)
            if not manager.is_current(token):
                return
            fetch()
        manager.verifying(token)
        if sha256_file(part) != expected:
            part.unlink(missing_ok=True)
            raise UpdateError("安装包校验和不符，已删除（下载损坏或被篡改，请到发布页手动下载）")
        os.replace(part, dest)  # 先校验后改名：坏文件永远拿不到最终名字
        manager.finish(token, dest)
        logger.info("更新包已就绪：%s（%.0f MB）", asset.name, dest.stat().st_size / 1e6)
    except ZhiwenError as exc:
        logger.warning("更新下载失败：%s", exc)
        manager.fail(token, strip_paths(str(exc)))
    except Exception as exc:  # noqa: BLE001 - 后台任务兜底：任何意外都要落在状态里
        logger.exception("更新下载任务未预期失败")
        manager.fail(token, f"更新下载失败：{strip_paths(str(exc))}")
