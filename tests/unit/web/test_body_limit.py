"""请求体上限：必须在读取过程中拦截，而不是读完再判。

守的是一个很具体的场景：Starlette 会把 multipart 的 file part 写进临时文件，
所以"先 `await request.form()` 再量大小"等于把磁盘交给客户端——`--host 0.0.0.0`
（无鉴权）下一个超大上传就能写满系统盘，413 是事后返回的。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from mikasa.web.limits import BodySizeLimit, body_limit_bytes


def _echo_app() -> FastAPI:
    """最小下游应用：读完 body 后回报收到了多少字节。"""
    app = FastAPI()

    @app.post("/upload")
    async def upload(request: Request) -> dict:
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
        return {"received": total}

    return app


def test_oversized_content_length_is_rejected_without_reading():
    """Content-Length 就超限 → 直接 413，一个字节都不读（正常客户端路径）。"""
    app = _echo_app()
    app.add_middleware(BodySizeLimit, max_bytes=100)

    resp = TestClient(app).post("/upload", content=b"x" * 5000)

    assert resp.status_code == 413
    assert "过" in resp.text or "large" in resp.text


def test_within_limit_passes_through():
    """限额之内必须正常放行（别把上限写成"一律拒绝"）。"""
    app = _echo_app()
    app.add_middleware(BodySizeLimit, max_bytes=1024)

    resp = TestClient(app).post("/upload", content=b"x" * 300)

    assert resp.status_code == 200
    assert resp.json() == {"received": 300}


def test_body_limit_bytes_leaves_form_overhead():
    """上限 = 配置值 + 表单开销余量：multipart 的边界与字段名也占字节。"""
    assert body_limit_bytes(1) > 1024 * 1024
    assert body_limit_bytes(500) > 500 * 1024 * 1024
