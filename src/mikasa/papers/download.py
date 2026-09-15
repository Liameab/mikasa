"""论文 PDF 下载的防御链（SSRF 校验 + 类型/大小/重定向上限）。

下载目标只来自**来源 API 自己的记录**（导入端点不接用户任意 URL），
但 oa_url 可能指向任意出版商主机——纵深防御仍然必要：

1. **协议与地址**：https 只放行解析到公网地址的主机；http 只放行解析
   到回环地址的（E2E 假源的逃生门——回环打不到内网，不削弱 SSRF 保证）；
   其余协议、URL 内嵌凭据、私网/链路本地/保留地址一律拒。
2. **重定向逐跳复验**：跳转是 SSRF 的第二入口，每一跳都重新走校验，
   上限 3 跳。
3. **内容验证**：Content-Type 必须是 application/pdf + 首块 `%PDF-`
   嗅探（拦住"伪装成 PDF 的 HTML 错误页"）。
4. **体积上限**：50MB 硬上限，超限删半成品并报错。

已接受的残差（ADR-0019）：校验用 `getaddrinfo` 与 urlopen 自身的解析是
两次独立 DNS——存在理论上的 DNS-rebinding 窗口，闭合它需要自建连接层，
对个人本地应用收益不成比例。
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from mikasa.papers.errors import PaperError

_PDF_MAX_BYTES = 50 * 1024 * 1024
_DOWNLOAD_TIMEOUT = 90.0
_MAX_REDIRECTS = 3
_CHUNK = 64 * 1024
_USER_AGENT = "Mikasa/0.1 (local paper search)"

# 各来源共用的默认端口（urlsplit 不带端口时补上，供 getaddrinfo 用）
_DEFAULT_PORTS = {"https": 443, "http": 80}


def _check_host(host: str, port: int, *, resolve=socket.getaddrinfo) -> None:
    """校验主机解析结果：https 须全公网；http 须全回环；其余一律拒。

    resolve 参数可注入：单测传假 addrinfo 列表，零真实 DNS。
    """
    try:
        infos = resolve(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise PaperError(f"论文下载目标无法解析（{host}）：{exc}") from exc
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        ip = ipaddress.ip_address(str(sockaddr[0]))
        # 内网/链路本地/保留段统一挡在门外（10/8、172.16/12、192.168/16、
        # 169.254/16、组播等 is_global 都判 False，见 Python 文档）
        if not ip.is_global:
            raise PaperError("论文下载目标解析到了内网地址，已拒绝（安全策略）")


def _validate_url(url: str, *, resolve=socket.getaddrinfo) -> urllib.parse.SplitResult:
    """下载前校验：协议白名单 + 无内嵌凭据 + 主机解析安全检查。"""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("https", "http"):
        raise PaperError("论文下载仅支持 http/https 链接")
    if parts.username or parts.password:
        raise PaperError("论文下载链接不允许内嵌凭据")
    if not parts.hostname:
        raise PaperError("论文下载链接缺少主机名")
    if parts.scheme == "https":
        _check_host(parts.hostname, parts.port or _DEFAULT_PORTS["https"], resolve=resolve)
    else:  # http：仅回环（本地假源）；连公网明文、更连不到内网
        try:
            infos = resolve(parts.hostname, parts.port or _DEFAULT_PORTS["http"])
        except OSError as exc:
            raise PaperError(f"论文下载目标无法解析（{parts.hostname}）：{exc}") from exc
        for info in infos:
            sockaddr = info[4]
            if sockaddr and not ipaddress.ip_address(str(sockaddr[0])).is_loopback:
                raise PaperError("论文下载目标不是本机服务（http 链接仅允许本地回环），已拒绝")
    return parts


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向逐跳复验 + 上限 3 跳（重定向是 SSRF 的第二入口）。

    resolve 可注入：单测传假 addrinfo，重定向校验零真实 DNS。
    """

    def __init__(
        self,
        *,
        max_redirects: int = _MAX_REDIRECTS,
        resolve=socket.getaddrinfo,  # noqa: ANN001 - 默认参数保持可注入
    ) -> None:
        super().__init__()
        self._max_redirects = max_redirects
        self._resolve = resolve
        self._hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - 覆写 stdlib 签名
        self._hops += 1
        if self._hops > self._max_redirects:
            raise urllib.error.HTTPError(req.full_url, 502, "重定向次数超过上限", headers, fp)
        _validate_url(newurl, resolve=self._resolve)  # 校验不过抛 PaperError，整条下载中止
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_url(url: str, timeout: float):
    """网络缝（带上安全重定向处理器）：单测打桩点。"""
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    return opener.open(req, timeout=timeout)


def download_pdf(url: str, dest: Path) -> int:
    """把 PDF 下载到 dest（防御链见模块头），返回写入字节数。

    失败抛 PaperError（中文文案）；超限时删除半成品。调用方负责
    dest 所在目录的存在性（web-tmp 子目录由路由层创建）。
    """
    _validate_url(url)
    try:
        resp = _open_url(url, timeout=_DOWNLOAD_TIMEOUT)
    except urllib.error.HTTPError as exc:
        raise PaperError(f"论文下载失败（HTTP {exc.code}）") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise PaperError("论文下载失败（网络不可达或超时）") from exc
    with resp:
        content_type = resp.headers.get("Content-Type", "") or ""
        if not content_type.lower().startswith("application/pdf"):
            raise PaperError(f"目标不是 PDF（Content-Type: {content_type or '未知'}）")
        try:
            first = resp.read(4096)
        except OSError as exc:
            raise PaperError(f"论文下载中断（{exc}）") from exc
        if not first.startswith(b"%PDF-"):
            raise PaperError("目标不是有效 PDF（文件头嗅探失败，可能只是网页）")
        total = 0
        with dest.open("wb") as out:
            while True:
                chunk = first if total == 0 else resp.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > _PDF_MAX_BYTES:
                    out.close()
                    dest.unlink(missing_ok=True)  # 不留半成品
                    raise PaperError("PDF 超过 50 MB 上限，已中止下载")
                out.write(chunk)
        return total
