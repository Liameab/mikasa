"""FastAPI 应用工厂：create_app(settings) → 完整 app。

装配内容：
  - AppServices 构建并挂 app.state（deps.get_services 取用）；
  - 页面路由（FileResponse 直出 html）+ /static 静态挂载；
  - /api 路由：health / qa / documents / eval（按里程碑逐步挂载）；
  - 异常处理器：ZhiwenError 家族 → 400/502 统一错误 JSON。

serve_app_factory：uvicorn --reload 专用入口（reload 需要 import string，
传"模块:函数"而非实例，见 cli serve 命令注释）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import ClientDisconnect

from mikasa import __version__
from mikasa.config.settings import Settings
from mikasa.errors import ProviderError, ZhiwenError, strip_paths
from mikasa.utils.logging import get_logger
from mikasa.web.limits import BodySizeLimit, body_limit_bytes
from mikasa.web.routers import documents, eval, health, qa, sessions
from mikasa.web.routers.papers import router as papers_router
from mikasa.web.routers.settings import router as settings_router
from mikasa.web.routers.update import router as update_router
from mikasa.web.services import AppServices

logger = get_logger("web")

# 静态资源目录 = web 包内 static/（hatchling wheel 打包包含它，
# pip 安装后 FileResponse/StaticFiles 依然可用，不依赖仓库路径）
STATIC_DIR = Path(__file__).parent / "static"

# 页面路由表：路径 → 静态文件名（URL 友好：/documents、/eval）
_PAGES = {
    "/": "index.html",
    "/documents": "documents.html",
    "/papers": "papers.html",
    "/eval": "eval.html",
}


def _error_body(status: int, type_name: str, message: str) -> JSONResponse:
    """统一错误响应壳：{"error": {"type", "message"}}，全部中文。"""
    return JSONResponse(
        status_code=status, content={"error": {"type": type_name, "message": message}}
    )


def _drop_surrogates(value: Any) -> Any:
    """递归洗掉字符串里的孤立代理项（校验错误的 `input` 原值可能是它们）。

    `json.dumps` 本身不报错（它按 UTF-16 转义），但 Starlette 随后
    `.encode("utf-8")` 会抛 UnicodeEncodeError —— 于是"客户端送了个坏字符串"
    变成了"服务器内部错误"。替换成 U+FFFD：调用方仍能看到自己送了什么，
    只是坏字符被标了出来。
    """
    if isinstance(value, str):
        return value.encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, dict):
        return {key: _drop_surrogates(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_drop_surrogates(item) for item in value]
    return value


def create_app(settings: Settings) -> FastAPI:
    """应用工厂。settings 显式注入（测试传隔离 data_dir 的 offline settings）。"""
    settings.ensure_dirs()
    app = FastAPI(
        title="Mikasa",
        description="带引用溯源与自动化评测的个人学习知识库问答系统（RAG + LLM）",
        version=__version__,
    )

    services = AppServices(settings)
    app.state.settings = settings
    app.state.services = services

    # ---- 请求体上限：必须在**读取之前**生效（见 limits 模块的说明） ----
    app.add_middleware(BodySizeLimit, max_bytes=body_limit_bytes(settings.web.upload_max_mb))

    # ---- 页面与静态资源（演示时浏览器直开 / 即首页） ----
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    for route, page in _PAGES.items():
        app.get(route, include_in_schema=False)(lambda page=page: FileResponse(STATIC_DIR / page))

    # ---- 业务路由 ----
    app.include_router(health.router)
    app.include_router(qa.router)
    app.include_router(documents.router)
    app.include_router(eval.router)
    app.include_router(sessions.router)
    app.include_router(settings_router)
    app.include_router(papers_router)
    app.include_router(update_router)

    # ---- 异常 → 统一错误 JSON（路由内不散落 try/except） ----
    @app.exception_handler(ClientDisconnect)
    async def client_disconnect_handler(request: Request, exc: ClientDisconnect) -> JSONResponse:
        """读到一半断流：多数是被上面的请求体上限掐断的，回 413。

        真正的客户端主动断开（关标签页）也走这里——那时响应没人收，
        返回什么都无所谓；但把 413 语义写在前面，超限场景才能给用户一句人话。
        """
        del exc
        logger.warning("请求体在读入过程中被中断：%s %s", request.method, request.url.path)
        return _error_body(
            413,
            "too_large",
            f"上传内容超过大小上限（{settings.web.upload_max_mb} MB），已中断。",
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """422 校验错误，但**响应体里要洗掉孤立代理项**。

        FastAPI 默认处理器把出错字段的**原值**（pydantic 的 `input`）回显进
        响应体；原值本身合法（它是 Python str），可再编码成 UTF-8 时就会炸
        ——`'\\ud800'.encode('utf-8')` 抛 UnicodeEncodeError，于是 422 变成
        500「服务器内部错误」（2026-09-16 对抗性实测：标题从坏 UTF-16 来源
        粘贴就能触发）。这里把字符串里的代理项替换成 U+FFFD 再渲染，形状与
        默认处理器完全一致（`{"detail": [...]}`），前端 errorMessage 零改动。
        """
        logger.warning(
            "请求校验失败：%s %s（%s）", request.method, request.url.path, exc.errors()[:1]
        )
        return JSONResponse(
            status_code=422,
            content={"detail": jsonable_encoder(_drop_surrogates(exc.errors()))},
        )

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity_error_handler(
        request: Request, exc: sqlite3.IntegrityError
    ) -> JSONResponse:
        """外键/唯一约束冲突 → 409（并发下"检查→写入"竞态的语义化）。

        路由里的"目标存在吗"用 SELECT 校验，写入是之后的 UPDATE/INSERT，两者不在
        同一事务里。并发下目标可能在两者之间被删掉（移动文档到刚被删的文件夹、
        会话移入刚被删的目录），随后撞 FOREIGN KEY 约束——原本直通到兜底 500
        （2026-09-11 审查实测）。对调用方来说这就是"你要的目标没了"，409 才对。
        """
        logger.warning(
            "完整性约束冲突（并发检查-写入竞态）：%s %s（%s）",
            request.method,
            request.url.path,
            exc,
        )
        return _error_body(409, "conflict", "目标已被其他操作改动（可能已删除），请刷新后重试。")

    @app.exception_handler(ZhiwenError)
    async def mikasa_error_handler(request: Request, exc: ZhiwenError) -> JSONResponse:
        """业务错误：ProviderError（上游 API 失败）502，其余 400。
        消息本身已脱敏（errors 层负责 redact 密钥），可直接回显。
        """
        del request
        status = 502 if isinstance(exc, ProviderError) else 400
        # strip_paths：异常消息常带服务器绝对路径（OSError/pymupdf 尤其多），
        # `--host 0.0.0.0` 下未鉴权客户端据此就能摸清目录布局
        return JSONResponse(
            status_code=status,
            content={"error": {"type": type(exc).__name__, "message": strip_paths(str(exc))}},
        )

    @app.exception_handler(OverflowError)
    async def int_overflow_handler(request: Request, exc: OverflowError) -> JSONResponse:
        """超大整型路径参数 → 404（不是 500）。

        路径里的 id 是裸 int，FastAPI 不做范围校验：20~4300 位的数字会原样
        传进 SQLite 绑定 → OverflowError。语义上"这个 id 不存在"就是 404
        （2026-09-11 修复：此前返回 500 + 完整栈日志）。
        """
        logger.warning(
            "整型溢出（按不存在的资源处理）：%s %s（%s）",
            request.method,
            request.url.path,
            exc,
        )
        return JSONResponse(status_code=404, content={"detail": "资源不存在"})

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        """兜底 500：完整异常进日志，响应不回显细节（防泄漏与恐慌）。"""
        logger.exception("未处理的服务器异常：%s %s", request.method, request.url.path)
        del exc
        return _error_body(500, "internal", "服务器内部错误，详见服务端日志。")

    return app


def serve_app_factory() -> FastAPI:
    """uvicorn --reload / factory 模式入口：按默认 profile 装配应用。

    reload 子进程通过 import string 重新加载本函数，不能闭包捕获 settings，
    因此每次进入都重新 load——.env 装载与 CLI 主入口同源
    （load_dotenv_file）。profile 取默认 "api"，但 config/config.yaml
    存在时其声明的 profile 优先（与 load_settings 查找链一致）；
    显式 --profile 走 CLI serve 非 reload 分支（直接传 app 实例）。
    """
    from mikasa.config.settings import load_dotenv_file, load_settings

    load_dotenv_file()
    return create_app(load_settings("api"))
