"""论文 PDF 下载的防御链（SSRF 校验 + 类型/大小/重定向上限）。

下载目标只来自**来源 API 自己的记录**（导入端点不接用户任意 URL），
但 oa_url 可能指向任意出版商主机——纵深防御仍然必要：

1. **协议与地址**：https / http 都只放行解析到**公网地址**的主机，
   外加 http 的回环例外（E2E 假源与本地服务的逃生门——回环打不到内网，
   不削弱 SSRF 保证）；其余协议、URL 内嵌凭据、私网/链路本地/保留地址
   一律拒。
   *（2026-09-16 放宽：原策略只放行公网 https，实测中文开放获取论文约
   六成全文链接是明文 http（国内期刊/仓储普遍没上 https），等于"搜得到、
   导不进来"。链接来自上游记录而非用户输入、下的是公开论文、不带凭据，
   所以放行公网 http；代价是明文传输理论上可被中途替换，属已接受的残差。）*
2. **重定向逐跳复验**：跳转是 SSRF 的第二入口，每一跳都重新走校验，
   上限 3 跳。
3. **内容验证**：Content-Type 必须是 application/pdf + 首块 `%PDF-`
   嗅探（拦住"伪装成 PDF 的 HTML 错误页"）。
4. **体积上限**：50MB 硬上限，超限删半成品并报错。

已接受的残差（ADR-0019/0020）：① 校验用 `getaddrinfo` 与 urlopen 自身的
解析是两次独立 DNS——存在理论上的 DNS-rebinding 窗口，闭合它需要自建
连接层，对个人本地应用收益不成比例；② 公网 http 的明文完整性风险（见 1）。
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from mikasa.papers.errors import PaperError
from mikasa.papers.http import USER_AGENT
from mikasa.utils.net import reject_reason

_PDF_MAX_BYTES = 50 * 1024 * 1024
_DOWNLOAD_TIMEOUT = 90.0
_MAX_REDIRECTS = 3
_CHUNK = 64 * 1024
# 与检索源共用一份 UA（带项目主页：出版商的机器人策略也更认可识别的客户端）
_USER_AGENT = USER_AGENT

# 各来源共用的默认端口（urlsplit 不带端口时补上，供 getaddrinfo 用）
_DEFAULT_PORTS = {"https": 443, "http": 80}


def _check_host(host: str, port: int, *, resolve=socket.getaddrinfo) -> None:
    """校验主机解析结果：须解析到公网地址（SSRF 防线）。

    策略本体已提到 `utils/net.py`（设置面板同样要用），这里只做异常翻译。
    resolve 参数可注入：单测传假 addrinfo 列表，零真实 DNS。
    """
    reason = reject_reason(host, port, allow_loopback=False, resolve=resolve)
    if reason is not None:
        raise PaperError(f"论文下载{reason}")


def _check_http_host(host: str, port: int, *, resolve=socket.getaddrinfo) -> None:
    """http 专用：公网或回环皆可（回环 = E2E 假源/本地服务的逃生门）。

    与非回环的内网地址仍然一律拒绝——SSRF 防线是"打不到内网"，与协议无关。
    """
    reason = reject_reason(host, port, allow_loopback=True, resolve=resolve)
    if reason is not None:
        raise PaperError(f"论文下载{reason}")


def _validate_url(url: str, *, resolve=socket.getaddrinfo) -> urllib.parse.SplitResult:
    """下载前校验：协议白名单 + 无内嵌凭据 + 主机解析安全检查。"""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("https", "http"):
        raise PaperError("论文下载仅支持 http/https 链接")
    if parts.username or parts.password:
        raise PaperError("论文下载链接不允许内嵌凭据")
    if not parts.hostname:
        raise PaperError("论文下载链接缺少主机名")
    port = parts.port or _DEFAULT_PORTS[parts.scheme]
    if parts.scheme == "https":
        _check_host(parts.hostname, port, resolve=resolve)
    else:  # http：公网放行（中文论文的全文链接多是明文 http，见模块头）+ 回环例外
        _check_http_host(parts.hostname, port, resolve=resolve)
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
