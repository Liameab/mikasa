"""出网目标的主机闸门（SSRF 防线；2026-09-20 从 papers/download.py 提取共用）。

**为什么要有这一层**：论文下载器（ADR-0020）从第一天起就拒绝内网地址；
而设置面板的「测试连接 / 读取本机模型 / 视觉测试」后来也开始接受用户给的
`base_url`，却没有同样的闸门——于是一个**任何网页都能触发的端点**变成了
内网探活扫描器（`?base_url=http://192.168.1.1` 会如实区分"拒绝连接/超时"，
`http://169.254.169.254/v1` 也会被打）。策略只写一次、两处共用，
免得哪天一边收紧、另一边漏着。

**判据**：主机解析出的**每一个**地址都必须是公网（`ip.is_global`）；
`allow_loopback=True` 时额外放行回环——本机服务是合法用途（Ollama、
E2E 的假源）。内网/链路本地/保留段（10/8、172.16/12、192.168/16、
169.254/16、组播…）一律拒绝。

**已知边界**（与 papers 一致）：DNS 解析与实际连接之间存在 rebinding 窗口，
本层不防——要防得在连接层锁 IP，代价与收益不成比例（单机、无鉴权是
已记录的形态）。想指向局域网里的模型服务，请直接编辑配置文件
（面板这条路刻意只放行公网与本机）。
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

# 各协议的默认端口（解析用；与 papers/download.py 的取值一致）
_DEFAULT_PORTS = {"https": 443, "http": 80}


def reject_reason(
    host: str,
    port: int,
    *,
    allow_loopback: bool = False,
    strict_resolution: bool = True,
    resolve: Any = socket.getaddrinfo,
) -> str | None:
    """放行返回 None，拒绝返回中文原因（调用方各自抛自己的异常类型）。

    resolve 可注入：单测传假 addrinfo 列表，零真实 DNS。

    strict_resolution：解析失败算不算拒绝。
      - True（默认，论文下载器用）：解析不了就是下载失败，直接报原因；
      - False（设置面板的**探测端点**用）：解析不了交给下游探测如实报
        "连不上"——那些端点的契约是"恒 200 + ok:false"，把它们变成 422
        会让前端的连接测试 pill 显示成参数错误（2026-09-20 实测踩到）。
    """
    try:
        infos = resolve(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        if not strict_resolution:
            return None
        return f"目标无法解析（{host}）：{exc}"
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        ip = ipaddress.ip_address(str(sockaddr[0]))
        if ip.is_global:
            continue
        if allow_loopback and ip.is_loopback:
            continue
        # 内网/链路本地/保留段统一挡在门外（is_global 对它们都判 False）
        return "目标解析到了内网地址，已拒绝（安全策略）"
    return None


def default_port(scheme: str) -> int:
    """协议默认端口（未知协议给 0：调用方应先做协议白名单）。"""
    return _DEFAULT_PORTS.get(scheme, 0)


def port_of(parts: Any) -> int:
    """URL 的端口（缺省走协议默认值）；写错时抛带中文的 ValueError。

    `urlsplit().port` 在端口越界或非数字时抛 ValueError——用户在面板地址栏
    多敲一位数字（`http://localhost:114344/v1`）就够触发。裸取的两处 guard
    会把它变成 500，而它们的契约是"恒 200 + ok:false"（2026-09-24 修）。
    所以取端口一律走这里，由调用方翻成自己那层的错误类型。
    """
    try:
        return parts.port or default_port(parts.scheme)
    except ValueError as exc:
        raise ValueError(f"地址里的端口不合法（{exc}）") from exc


_MAX_REDIRECTS = 3


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向逐跳复验 + 跳数上限（重定向是 SSRF 的第二个入口）。

    入口校验只看得到用户给的那个 URL，302 之后去的地方没人看——所以每一跳都要
    再问一遍调用方自己的策略函数 `validate(url)`（不通过就抛，整条下载中止；
    返回值不参与判断，所以各家的 `_validate_*` 直接传进来即可）。
    跳数计数器是**每个 handler 一份**（每次 build_opener 新建）：重试不该吃掉配额。

    两个下载器（论文 PDF / 应用更新）原先各写了一份一字不差的实现，差异只在
    validate 里，故合并到此（2026-09-24）。**策略本身仍各在各处**——这里只管
    "每一跳都过一遍它"。
    """

    def __init__(
        self, validate: Callable[[str], Any], *, max_redirects: int = _MAX_REDIRECTS
    ) -> None:
        super().__init__()
        self._validate = validate
        self._max_redirects = max_redirects
        self._hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - 覆写 stdlib 签名
        self._hops += 1
        if self._hops > self._max_redirects:
            raise urllib.error.HTTPError(req.full_url, 502, "重定向次数超过上限", headers, fp)
        self._validate(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def safe_opener(validate: Callable[[str], Any], *, max_redirects: int = _MAX_REDIRECTS) -> Any:
    """带逐跳复验重定向的 opener（validate 由调用方带上自己的策略）。"""
    return urllib.request.build_opener(SafeRedirectHandler(validate, max_redirects=max_redirects))
