"""跨站写请求闸门（CSRF 面；2026-09-20 全量审查发现）。

**要挡的是什么**：Mikasa 是无鉴权的本机服务（`--host 0.0.0.0` 时同网段也够得着），
而浏览器里的**任何网页**都能对它发**简单请求**——不需要预检、不读响应也能产生
副作用。实测可达的清单（无体或表单体，全在 CORS 简单请求之列）：

  - `POST /api/update/install`：启动安装器（它第一件事是 taskkill 本应用）；
  - `POST /api/update/download`：盲下 80MB+ 的包；
  - `POST /api/eval/runs` / `/api/eval/synthesize`：盲烧 LLM 额度；
  - `POST /api/documents`（multipart 属简单请求）：往语料灌文档。

**判据只看 Origin**：非安全方法（POST/PUT/PATCH/DELETE）且带了 Origin 头时，
Origin 的主机必须与请求的 Host 相同；不同 → 403。
  - 浏览器同源请求（应用自身、pywebview 窗口、局域网访问）：两者的主机一致 ✓ 放行；
  - 恶意网页：Origin 是 `https://evil.com`，Host 是 `127.0.0.1:8787` → 403 ✓；
  - `Origin: null`（沙箱 iframe / file:// 页）：当作跨站拒掉；
  - 不带 Origin 的客户端（curl、脚本、CI、E2E 工具）：放行——那不是浏览器，
    是用户自己的工具，本来就有本机权限。

**边界（如实说）**：这挡的是"浏览器帮忙发的请求"，不是"能连到端口的人"——
没有鉴权的前提下，本机/同网段的程序想调什么仍调得到（那是已记录的形态：
单机单用户）。DNS rebinding 也不在这一层的射程内（Host 会被伪造成 localhost）。
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

# 有副作用的请求方法（GET/HEAD/OPTIONS 不动状态，不需要这道闸）
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def same_site(origin: str, host: str) -> bool:
    """Origin 与请求 Host 是否同一主机（主机名比较，端口不参与）。

    端口不比：同一台机器上不同端口互发请求在本机服务里没有安全含义，
    而 Host 与 Origin 的端口写法（省略默认端口等）容易不一致。
    """
    origin_host = (urlsplit(origin).hostname or "").strip("[]").lower()
    req_host = host.split(":")[0].strip("[]").lower()
    if not origin_host or not req_host:
        return False
    return origin_host == req_host


class CrossSiteWriteGuard:
    """ASGI 中间件：跨站写请求 → 403（浏览器之外不看 Origin，放行）。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope.get("method", "GET").upper() in _WRITE_METHODS:
            headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in scope.get("headers") or []
            }
            origin = headers.get("origin", "")
            if origin and (origin == "null" or not same_site(origin, headers.get("host", ""))):
                await self._reject(send)
                return
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send: Any) -> None:
        # 中文字面量走 \uXXXX 转义：这个模块要保持 ASCII（与 limits.py 同一写法）
        payload = {
            "error": {
                "type": "cross_site",
                "message": ("跨站请求已被拒绝（请从 Mikasa 自己的页面操作）"),
            }
        }
        body = json.dumps(payload).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
