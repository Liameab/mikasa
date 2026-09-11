"""Web 界面（M3）：把问答/文档/评测三件能力 Web 化，供演示。

- 后端 FastAPI + 手写 SSE（不引 sse-starlette，事件协议见 ask.StreamEvent）；
- 前端零构建工具链的原生 HTML + ES Modules（用户无 node 环境）；
- 进程内单实例服务（内存态索引快照 / 评测单槽任务管理器不能多 worker，
  见 docs/architecture.md 与 serve 命令注释）。

对外入口：create_app(settings)（工厂显式注入 settings，测试可传隔离
data_dir 的 offline settings）；uvicorn reload 走 serve_app_factory()。
import mikasa.web 保持轻量——fastapi 只在此模块链上被引入。
"""

from __future__ import annotations

from mikasa.web.app import create_app, serve_app_factory

__all__ = ["create_app", "serve_app_factory"]
