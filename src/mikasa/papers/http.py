"""论文源的公共 HTTP 纪律：统一 User-Agent + 网络级重试。

**为什么要有这个模块**（2026-09-19 实测）：三个源各写一份 UA、各自把超时
定在 15–20 秒，而用户机器到 arXiv 的实测延迟是 **2–22 秒**（同一条 URL
连测五次：0.8s / 2.2s / 8.4s / 15s 超时 / 22.5s）——超时即判失败、整个
来源从结果里消失，用户看到的是"搜出来就没几条"。同时 arXiv 对不带主页的
匿名 UA 会间歇性 406 或直接挂住不答；同一地址用 curl 的 UA 稳定 200。

**重试只针对网络级失败**（超时、连接重置）：HTTP 状态错误不重试——那是
上游在明确说话，立刻重打只会把限流打得更死（2026-09-16 的教训，见
arxiv.py 的节流注释：当天三个源里两个被自己打成了 429）。
"""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import TypeVar

from mikasa.papers.errors import PaperError

# 带项目主页的 UA：arxiv 的 API 条款要求可识别的客户端，实测也确实是
# "带主页的 UA 更快更稳"（0.8s vs 15s 超时）。三源 + PDF 下载共用一份。
USER_AGENT = "Mikasa/0.1 (+https://github.com/Liameab/mikasa)"

RETRY_DELAY = 1.5  # 秒；网络抖动通常几百毫秒级，1.5 秒够等它缓过来
_RETRIES = 1  # 只重试一次：第二次还失败说明不是抖动

# 值得重试的 HTTP 状态：只有 **406**。它不是上游在"正常说话"，而是链路中间的
# 设备对非浏览器客户端间歇性注入的拒绝（2026-09-19 实测：同一条 URL，curl 与
# urllib 交替得到 200 / 406 / 直接超时，与 UA、Accept、参数顺序都无关）。
# 429 与 5xx 仍然不重试——那些是上游明确的"别打了"（见 arxiv.py 节流注释）。
_RETRYABLE_HTTP = frozenset({406})

T = TypeVar("T")


def with_retry(
    attempt: Callable[[], T], *, retries: int = _RETRIES, delay: float = RETRY_DELAY
) -> T:
    """执行 attempt()；网络级失败（或 406）重试一次，其余异常直接上抛。

    注意 `urllib.error.HTTPError` 是 `URLError` 的子类——必须**先**接住它，
    否则 429/5xx 会被当成网络抖动重试，把限流打得更死。
    """
    last: Exception | None = None
    for n in range(retries + 1):
        try:
            return attempt()
        except urllib.error.HTTPError as exc:
            if exc.code not in _RETRYABLE_HTTP:
                raise
            last = exc
            if n < retries:
                time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
            if n < retries:
                time.sleep(delay)
    assert last is not None  # 循环只有两条出路：返回或抛错
    raise last


class ThrottledClient:
    """一个来源的 HTTP 纪律：**发起前**节流 + 网络级重试 + 中文错误翻译。

    arXiv / CORE / DOAJ 原先各写了一份一字不差的 `_http_get` + `_read`，
    差异只有标签、最小间隔和 429 文案（2026-09-24 合并）。每个来源一个实例，
    节流状态随之从模块级全局变成实例属性。

    **节流记在发起前，成功失败都算一次请求**（2026-09-16 修正）：原实现
    "成功后补睡"，理由是"失败重试不该再付等待成本"——实测站不住：连打几次
    后 arXiv 回 429，而它恰恰把**失败请求也算进配额**，于是"越失败越猛打"
    把额度越打越死（当天三个源里两个被自己打成 429）。发前节流才是正确的
    礼貌客户端行为。
    """

    def __init__(self, label: str, min_interval: float, *, busy_hint: str = "") -> None:
        self._label = label
        self._min_interval = min_interval
        self._busy_hint = busy_hint or f"{label} 请求过于频繁（HTTP 429），请等半分钟再试"
        self._lock = threading.Lock()
        self._last_ok = 0.0

    def reset(self) -> None:
        """清空节流状态（单测用：不想让上一条用例的等待渗进来）。"""
        with self._lock:
            self._last_ok = 0.0

    def get(self, url: str, timeout: float) -> bytes:
        """唯一网络缝：GET 并读全部字节；发起前按该来源的限速等够间隔。"""
        with self._lock:
            wait = self._last_ok + self._min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_ok = time.monotonic()
        try:
            # 重试在节流**之内**：重试那一次距上次发起已隔一个超时周期
            # （≥15 秒），远大于各源的节流间隔，不必再等
            return with_retry(lambda: self._read(url, timeout))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise PaperError(self._busy_hint) from exc
            raise PaperError(f"{self._label} 服务返回错误（HTTP {exc.code}），请稍后重试") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise PaperError(f"无法连接 {self._label}（网络不可达或超时），请稍后重试") from exc

    @staticmethod
    def _read(url: str, timeout: float) -> bytes:
        """裸 HTTP 调用（重试包裹的那一层）。"""
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
