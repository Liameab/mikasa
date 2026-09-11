"""FastAPI 依赖：从 request.app.state 取 settings / services。

刻意不走全局单例：create_app(settings) 显式注入 → app.state，
每个测试进程的 TestClient 天然隔离，无需 monkeypatch。
"""

from __future__ import annotations

from fastapi import Request

from mikasa.config.settings import Settings
from mikasa.web.services import AppServices


def get_settings(request: Request) -> Settings:
    """当前请求对应的运行时配置。"""
    return request.app.state.settings


def get_services(request: Request) -> AppServices:
    """当前请求对应的共享服务容器。"""
    return request.app.state.services
