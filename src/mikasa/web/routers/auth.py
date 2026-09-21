"""登录/登出端点（ADR-0033）。

- `GET  /login`           登录页（自包含 HTML，无外部资源）
- `POST /api/auth/login`  表单口令 → 202 会话 Cookie（失败 401 并重渲染登录页）
- `POST /api/auth/logout` 清 Cookie → 回登录页

三个都走**表单**（不是 JSON）：登录页不用 JS 就能工作（局域网里手机浏览器
乱七八糟的版本都能用），而且 `SameSite=Lax` 的 Cookie 能天然挡住跨站表单
提交（详见 AuthGate 的说明）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from mikasa.config.settings import Settings
from mikasa.web import auth
from mikasa.web.deps import get_settings

router = APIRouter()


def _safe_next(raw: str | None) -> str:
    """只接受站内路径（挡 `//evil.com` 与绝对 URL 的开放重定向）。"""
    target = (raw or "").strip()
    if not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


@router.get("/login")
def login_page(request: Request, next: str = "/") -> Response:
    """登录页（已登录的人直接送回目标页，免得看到"已经登录了还要登"）。"""
    settings = get_settings(request)
    token = request.cookies.get(auth.COOKIE_NAME, "")
    if token and auth.token_ok(settings.data_dir, token):
        return RedirectResponse(_safe_next(next), status_code=303)
    return HTMLResponse(auth.login_page_html(next_url=_safe_next(next)))


@router.post("/api/auth/login")
def do_login(
    request: Request,
    password: str = Form(default=""),
    next: str = Form(default="/"),
    settings: Settings = Depends(get_settings),
) -> Response:
    """口令换会话。失败也是 HTML（401 + 登录页 + 一句错误）——表单直投，不该回 JSON。"""
    target = _safe_next(next)
    if not auth.verify_password(settings.data_dir, password):
        # 不区分"没设口令"与"口令不对"：前者在非回环绑定下根本起不来（serve 拦），
        # 后者是唯一现实情况；给同一句话避免暴露配置状态
        return HTMLResponse(
            auth.login_page_html(error="口令不对，再试一次。", next_url=target),
            status_code=401,
        )
    token = auth.make_token(settings.data_dir)
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        auth.COOKIE_NAME,
        token,
        max_age=auth.DEFAULT_TTL_SECONDS,
        httponly=True,  # JS 读不到（XSS 也偷不走会话）
        samesite="lax",  # 跨站 POST 不带 Cookie（CSRF 的第一道闸）
        path="/",
        # 不设 secure：局域网走的是明文 http，设了 Cookie 反而发不出去。
        # 真要上公网，请放在 HTTPS 反代后面（见 ADR-0033 的边界说明）。
    )
    return response


@router.post("/api/auth/logout")
def do_logout() -> Response:
    """登出：清 Cookie 回登录页（会话本身无状态，清了就没了）。"""
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return response
