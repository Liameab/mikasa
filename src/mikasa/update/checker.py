"""带 TTL 缓存的版本检查器（每进程一个，随 AppServices 存活）。

为什么缓存：一次启动会打两次检查（前端自动 + 设置面板手动），而
GitHub 匿名 API 是每 IP 每小时 60 次——本地应用不该几分钟内把配额
用掉一半。TTL 内直接回上次结果；「立即检查」走 force=True 绕开。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from mikasa.update.release import ReleaseInfo, fetch_latest_release

DEFAULT_TTL = 600.0  # 秒；10 分钟内的重复检查不再打网络


class UpdateChecker:
    """版本检查 + TTL 缓存。fetch 可注入（单测零网络）。"""

    def __init__(
        self,
        ttl: float = DEFAULT_TTL,
        fetch: Callable[[], ReleaseInfo] = fetch_latest_release,
    ) -> None:
        self._ttl = ttl
        self._fetch = fetch
        self._lock = threading.Lock()
        self._cached: tuple[float, ReleaseInfo] | None = None

    def cached(self, *, force: bool = False) -> ReleaseInfo:
        """取最新 release（TTL 内复用缓存）。网络异常原样抛 ProviderError。"""
        with self._lock:
            entry = self._cached
            if not force and entry is not None and time.monotonic() - entry[0] < self._ttl:
                return entry[1]
        # 网络调用在锁外：慢请求不该把并发的状态查询一起堵住。
        # 代价是并发首次检查可能打两次网络——可接受（幂等的只读请求）。
        info = self._fetch()
        with self._lock:
            self._cached = (time.monotonic(), info)
        return info
