"""访问口令：把服务开给局域网/公网时的那道门（ADR-0033）。

默认只绑 `127.0.0.1`——只有本机能连，所以**不设口令也是安全的**（你自己那台
机器上的桌面端与本地浏览器）。一旦绑定非回环地址（用户要"手机 / 别的电脑用
浏览器打开"），同一网络里**任何人**都能读你的资料、删你的文档，还能花你的
API 额度（密钥就存在服务端配置里）。所以规则只有一条：

    **非回环绑定 = 必须设口令，没设就拒绝启动**（fail closed，serve 里拦）。

三段组成，全在标准库内、不引依赖：

1. `set_password()`：PBKDF2-HMAC-SHA256（20 万轮 + 16 字节随机盐）写进
   `<数据目录>/auth.json`，同批生成 32 字节**会话密钥**；
2. `make_token()` / `token_ok()`：会话 = **HMAC 签名的过期时间戳**
   ——无服务端状态、无数据库表；改了口令就换密钥，**所有旧会话立刻失效**
   （这是"改密码"该有的语义，不用额外做失效列表）；
3. `AuthGate`：ASGI 中间件。**回环来源免口令**（本机/桌面端零摩擦：能连到
   127.0.0.1 的人本来就坐在你机器上）；其余来源没带有效会话就——页面请求
   302 去 `/login`、接口请求 401 JSON。**门只在"绑了非回环地址"时生效**，
   所以本机自用与全部既有测试的行为一个字不变。

不是账号体系（多用户/每人一份数据是另一个里程碑，见 ADR-0033）：这里只解决
"别人能不能看"这一件事，用一把口令把它关掉。
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

_AUTH_FILE = "auth.json"
_ITERATIONS = 200_000
_SALT_BYTES = 16
_SECRET_BYTES = 32

COOKIE_NAME = "mikasa_session"
DEFAULT_TTL_SECONDS = 30 * 24 * 3600  # 30 天：局域网里用手机，天天登录会很烦

# 免口令路径：登录页本身与登录接口（其余一律要会话）
EXEMPT_PATHS = frozenset({"/login", "/api/auth/login"})


def auth_file(data_dir: Path) -> Path:
    """口令文件路径（数据目录下，与库/密钥同处）。"""
    return data_dir / _AUTH_FILE


def is_configured(data_dir: Path) -> bool:
    """设过口令没有（文件在不在；内容坏了算没设）。"""
    try:
        return bool(_read(data_dir))
    except OSError:
        return False


def is_loopback_host(host: str) -> bool:
    """监听地址是不是"只有本机能连"。

    `127.0.0.1` / `::1` / `localhost` 算；`0.0.0.0` / `::` / 具体网卡地址
    一律不算（那是"网络里所有人都能连"）。认不出来的字符串**按不安全处理**
    （宁可多要一次口令，也别以为安全）。
    """
    text = (host or "").strip().lower()
    if text in ("", "localhost"):
        return True  # 空 = 没显式指定，等价默认
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _read(data_dir: Path) -> dict[str, Any] | None:
    path = auth_file(data_dir)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    secret = raw.get("secret")
    if not isinstance(secret, str) or not secret:
        return None
    return raw


def _derive(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def set_password(data_dir: Path, password: str) -> None:
    """写入（或覆盖）口令。**每次调用都换新盐与新会话密钥** → 旧会话全部失效。"""
    if not password:
        raise ValueError("口令不能为空")
    salt = secrets.token_bytes(_SALT_BYTES)
    payload = {
        "version": 1,
        "iterations": _ITERATIONS,
        "salt": salt.hex(),
        "hash": _derive(password, salt, _ITERATIONS).hex(),
        "secret": secrets.token_bytes(_SECRET_BYTES).hex(),
    }
    data_dir.mkdir(parents=True, exist_ok=True)
    path = auth_file(data_dir)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)  # 同目录原子替换（与配置覆盖层同一纪律）


def clear_password(data_dir: Path) -> bool:
    """删掉口令文件（返回是否有东西可删）。删完非回环绑定会被 serve 拒绝。"""
    path = auth_file(data_dir)
    if not path.is_file():
        return False
    path.unlink()
    return True


def verify_password(data_dir: Path, password: str) -> bool:
    """校验口令（**常数时间比较**；文件坏了/没设 → False，即"谁都进不去"）。"""
    data = _read(data_dir)
    if data is None or not password:
        return False
    try:
        iterations = int(data.get("iterations") or _ITERATIONS)
        salt = bytes.fromhex(str(data["salt"]))
        expected = bytes.fromhex(str(data["hash"]))
    except (KeyError, TypeError, ValueError):
        return False
    return hmac.compare_digest(_derive(password, salt, iterations), expected)


def _secret_bytes(data_dir: Path) -> bytes | None:
    data = _read(data_dir)
    if data is None:
        return None
    try:
        return bytes.fromhex(str(data["secret"]))
    except (KeyError, TypeError, ValueError):
        return None


def make_token(data_dir: Path, *, ttl: int = DEFAULT_TTL_SECONDS, now: float | None = None) -> str:
    """签发会话：`<过期时间戳>.<HMAC>`（无状态，服务端不存任何东西）。"""
    secret = _secret_bytes(data_dir)
    if secret is None:
        raise ValueError("还没有设置访问口令")
    expires = int((now if now is not None else time.time()) + ttl)
    signature = hmac.new(secret, str(expires).encode("ascii"), hashlib.sha256).hexdigest()
    return f"{expires}.{signature}"


def token_ok(data_dir: Path, token: str, *, now: float | None = None) -> bool:
    """会话有效吗：签名对得上 **且** 没过期。"""
    secret = _secret_bytes(data_dir)
    if secret is None or not token or "." not in token:
        return False
    expires_text, _, signature = token.partition(".")
    try:
        expires = int(expires_text)
    except ValueError:
        return False
    expected = hmac.new(secret, expires_text.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return False
    return expires > int(now if now is not None else time.time())


def _client_is_loopback(client: Any) -> bool:
    """ASGI scope 里的 client 是不是本机。

    形如 `("127.0.0.1", 51234)`；测试客户端给的可能是 `"testclient"` 这种
    非 IP 字符串——**认不出来就算"不是本机"**（宁可多要一次口令）。
    """
    if not client:
        return False
    host = client[0] if isinstance(client, (tuple, list)) and client else client
    try:
        return ipaddress.ip_address(str(host)).is_loopback
    except ValueError:
        return False


def _cookies(scope: dict[str, Any]) -> dict[str, str]:
    jar: dict[str, str] = {}
    for key, value in scope.get("headers") or []:
        if key.decode("latin-1").lower() != "cookie":
            continue
        for part in value.decode("latin-1").split(";"):
            name, _, val = part.strip().partition("=")
            if name:
                jar[name] = val
    return jar


class AuthGate:
    """ASGI 中间件：非回环来源必须带有效会话，否则页面去登录、接口回 401。

    只在**绑定了非回环地址**时启用（`enabled=False` 时整段直通，本机自用
    与全部既有测试的行为不变）。没设口令时**也拒绝**（fail closed）——
    正常路径上 serve 已经在启动前拦掉了，这里是第二层。
    """

    def __init__(self, app: Any, *, data_dir: Path, enabled: bool) -> None:
        self.app = app
        self._data_dir = data_dir
        self._enabled = enabled

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not self._enabled:
            await self.app(scope, receive, send)
            return
        if _client_is_loopback(scope.get("client")):
            await self.app(scope, receive, send)  # 本机免口令
            return
        path = scope.get("path", "/")
        if path in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return
        token = _cookies(scope).get(COOKIE_NAME, "")
        if token and token_ok(self._data_dir, token):
            await self.app(scope, receive, send)
            return
        await self._reject(scope, path, send)

    @staticmethod
    async def _reject(scope: dict[str, Any], path: str, send: Any) -> None:
        """接口 → 401 JSON（前端可提示）；页面 → 302 去登录页。"""
        if path.startswith("/api/"):
            body = json.dumps(
                {
                    "error": {
                        "type": "unauthorized",
                        "message": "需要访问口令：请在登录页输入口令后再试",
                    }
                },
                ensure_ascii=False,
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        target = "/login"
        if path and path != "/":
            target = f"/login?next={quote(path, safe='')}"
        await send(
            {
                "type": "http.response.start",
                "status": 302,
                "headers": [
                    (b"location", target.encode("ascii", "replace")),
                    (b"content-length", b"0"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b""})


def login_page_html(*, error: str = "", next_url: str = "/") -> str:
    """登录页（自包含 HTML，无外部资源——断网/局域网里也能打开）。

    样式只借配色，不引 style.css：这一页必须在**任何静态资源都取不到**的
    情况下也能正常显示（静态资源本身也在这道门后面）。
    """
    alert = f'<p class="err">{error}</p>' if error else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mikasa · 需要口令</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ margin: 0; min-height: 100vh; display: flex; align-items: center;
         justify-content: center; background: #faf9f5; color: #2b2a27;
         font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; }}
  form {{ background: #fff; border: 1px solid #e6e1d6; border-radius: 12px;
         padding: 28px 30px; width: min(360px, 88vw);
         box-shadow: 0 18px 48px rgba(20,20,19,.12); }}
  h1 {{ font-size: 18px; margin: 0 0 6px; }}
  p.hint {{ color: #7a756c; font-size: 13px; margin: 0 0 16px; line-height: 1.6; }}
  input {{ width: 100%; box-sizing: border-box; padding: 10px 12px; font-size: 15px;
          border: 1px solid #d9d3c6; border-radius: 8px; background: #fbfaf6; }}
  button {{ width: 100%; margin-top: 14px; padding: 10px; font-size: 15px;
           border: 0; border-radius: 8px; background: #cc785c; color: #fff;
           cursor: pointer; }}
  button:hover {{ background: #b96a50; }}
  p.err {{ color: #c0392b; font-size: 13px; margin: 0 0 12px; }}
</style>
</head>
<body>
  <form method="post" action="/api/auth/login">
    <h1>Mikasa 需要访问口令</h1>
    <p class="hint">这台 Mikasa 开给了局域网访问，所以要口令。
      口令在运行 Mikasa 的那台电脑上设置（<code>mikasa auth set-password</code>）。</p>
    {alert}
    <input type="hidden" name="next" value="{next_url}">
    <input type="password" name="password" placeholder="访问口令" autofocus
           autocomplete="current-password">
    <button type="submit">进入</button>
  </form>
</body>
</html>
"""
