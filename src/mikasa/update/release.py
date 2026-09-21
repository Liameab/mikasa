"""查 GitHub Release：有没有比当前更新的版本。

数据来源 = GitHub REST `/releases/latest`（匿名可读，无需令牌，本机
实测国内可直连）。只读元信息与资产清单——**本模块挑出的下载地址是
全流程唯一的 URL 来源**，客户端不参与（见 install.py 的主机白名单）。

测试性：API 根与仓库名由环境变量在**调用时**读取
（`MIKASA_UPDATE_API_BASE` / `MIKASA_UPDATE_REPO`），E2E 用本地假
API 整体替换，与 papers 各来源同款逃生门。

失败一律抛 ProviderError（→ 502）：GitHub 挂了、网络断了、响应解不开
都属"上游不可用"。前端对检查失败是静默的，错误只进日志与状态接口。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from mikasa.errors import ProviderError

_DEFAULT_API_BASE = "https://api.github.com"
_DEFAULT_REPO = "Liameab/mikasa"
_TIMEOUT = 15.0
_USER_AGENT = "Mikasa/0.1 (update-check)"

# 发布说明展示上限：正文进 JSON 响应，超长截断（README 级别的长文没必要全传）
NOTES_CAP = 4000

# 资产名匹配：发布脚本（tools/make_release.py / build_installer.py）产出的
# 固定命名。**只认名字不认顺序**——万一将来多传了别的 exe，也不会装错。
SETUP_ASSET_RE = re.compile(r"Mikasa-Setup-.+-win64\.exe")
SUMS_ASSET_RE = re.compile(r"SHA256SUMS\.txt")


def api_base() -> str:
    """GitHub API 根地址（测试性：E2E 指向本地假 API）。"""
    return os.environ.get("MIKASA_UPDATE_API_BASE", _DEFAULT_API_BASE).rstrip("/")


def repo_slug() -> str:
    """仓库 slug（owner/name），允许环境变量覆盖（fork 或改名时用）。"""
    return os.environ.get("MIKASA_UPDATE_REPO", _DEFAULT_REPO)


def parse_version(text: str) -> tuple[int, ...] | None:
    """`v0.1.2` / `0.1` → 数字元组；认不出来 → None。

    宽松取数字段（GitHub 的 tag 前缀、位数都不由我们定），但只认
    `数字.数字…` 形态——`v1.2.0-beta` 这类预发布标签直接判 None，
    宁可不提示，也不把 beta 推给普通用户。
    """
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", text.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(latest: str, current: str) -> bool:
    """latest 是否比 current 新。任一侧解析不了 → False（保守不打扰）。

    元组比较天然是"按位补零"语义：(0, 2) < (0, 2, 1)，不需要对齐位数。
    """
    new, old = parse_version(latest), parse_version(current)
    if new is None or old is None:
        return False
    return new > old


@dataclass(frozen=True)
class Asset:
    """一个发布资产（安装包 / 校验和文件）。

    `digest` 是 GitHub API 给的服务端 sha256（形如 `sha256:abc…`），**可能缺席**
    （老 API / 非 GitHub 的假源）——所以有它时优先用、没有才回退下载 SHA256SUMS.txt。
    """

    name: str
    url: str
    size: int
    digest: str | None = None


@dataclass(frozen=True)
class ReleaseInfo:
    """一次 release 查询的结果（版本、说明、资产清单）。"""

    version: str  # 归一化后的纯数字版本："0.1.2"
    tag: str  # GitHub 上的原始 tag："v0.1.2"
    html_url: str  # 发布页（浏览器回退路径）
    notes: str  # 发布说明正文（Markdown，已截断）
    published_at: str
    assets: tuple[Asset, ...]

    def find_asset(self, pattern: re.Pattern[str]) -> Asset | None:
        for asset in self.assets:
            if pattern.fullmatch(asset.name):
                return asset
        return None

    def setup_asset(self) -> Asset | None:
        """安装向导（自动更新的下载目标）。"""
        return self.find_asset(SETUP_ASSET_RE)

    def checksums_asset(self) -> Asset | None:
        """SHA256SUMS.txt（完整性校验的来源）。"""
        return self.find_asset(SUMS_ASSET_RE)


def _open(url: str, timeout: float):
    """网络缝：单测打桩点（与 papers/download.py 同款做法）。"""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    return urllib.request.urlopen(req, timeout=timeout)


def _asset_from_json(raw: Any) -> Asset | None:
    """把 API 里的一个资产对象转成 Asset；形状不对就跳过（不整批失败）。"""
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    url = raw.get("browser_download_url")
    if not isinstance(name, str) or not isinstance(url, str):
        return None
    size = raw.get("size")
    digest = raw.get("digest")
    return Asset(
        name=name,
        url=url,
        size=size if isinstance(size, int) else 0,
        digest=digest if isinstance(digest, str) and digest else None,
    )


def fetch_latest_release(timeout: float = _TIMEOUT) -> ReleaseInfo:
    """拉最新 release。任何失败 → ProviderError（中文文案）。"""
    url = f"{api_base()}/repos/{repo_slug()}/releases/latest"
    try:
        with _open(url, timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise ProviderError(f"检查更新失败（GitHub 返回 HTTP {exc.code}）") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError("检查更新失败（网络不可达或超时）") from exc

    try:
        data = json.loads(raw)
        tag = data["tag_name"]
        html_url = data.get("html_url", "")
        notes = data.get("body") or ""
        published_at = data.get("published_at", "")
        assets_raw = data.get("assets", [])
    except (ValueError, KeyError, TypeError) as exc:
        raise ProviderError("检查更新失败（GitHub 返回内容无法解析）") from exc
    if not isinstance(tag, str) or not tag:
        raise ProviderError("检查更新失败（返回内容里没有版本号）")

    version = parse_version(tag)
    if version is None:
        raise ProviderError(f"检查更新失败（版本号无法识别：{tag}）")

    assets = tuple(a for a in (_asset_from_json(item) for item in assets_raw) if a is not None)
    return ReleaseInfo(
        version=".".join(str(part) for part in version),
        tag=tag,
        html_url=html_url if isinstance(html_url, str) else "",
        notes=notes[:NOTES_CAP] if isinstance(notes, str) else "",
        published_at=published_at if isinstance(published_at, str) else "",
        assets=assets,
    )


def release_page_url(tag: str) -> str:
    """发布页地址（回退路径用；API 没给 html_url 时按规则拼）。"""
    return f"https://github.com/{repo_slug()}/releases/tag/{urllib.parse.quote(tag)}"
