"""Web 进程级共享对象容器：一次构建，随 app.state 存活整个进程。

- AskService 常驻：IndexManager 缓存索引快照，跨请求复用（Web 问答页
  不必每次提问都重建 BM25/加载向量矩阵）；
- IngestService 常驻：embedding provider 一次构造的生命周期内复用；
- EvalJobManager 常驻：单槽评测任务状态机（见类 docstring）；
- 三者都是同步阻塞实现——FastAPI 同步 def 端点自动进线程池执行，
  互不阻塞（SQLite 走 WAL + 连接即开即关，线程安全）。

服务进程约定为单进程单 worker（见 cli serve 文档）：内存态的
JobManager 是轮询端点的唯一事实源，多 worker 会各自为政。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from mikasa.config.settings import Settings
from mikasa.ingest.service import IngestService
from mikasa.pipeline.ask import AskService
from mikasa.update import UpdateChecker, UpdateManager

if TYPE_CHECKING:
    from mikasa.eval.runner import ItemRecord


@dataclass
class EvalJob:
    """一场后台评测的进度状态（线程内由 Lock 保护，读取走 snapshot）。"""

    status: str  # running / done / error
    total: int
    done: int = 0
    current: str = "准备中"  # 当前进度描述：阶段标签或题号
    started_at: str = ""
    run_id: int | None = None  # done 后回填落库的 run_id
    error: str = ""

    def to_snapshot(self) -> dict:
        """轮询响应的不可变拷贝（调用方拿到的永远是某一时刻的一致快照）。"""
        return {
            "status": self.status,
            "total": self.total,
            "done": self.done,
            "current": self.current,
            "started_at": self.started_at,
            "run_id": self.run_id,
            "error": self.error,
        }


class EvalJobManager:
    """单槽评测任务管理器：同一时刻只允许一场后台评测在跑。

    状态流转：running（POST /api/eval/runs 占槽）→ done（落库成功，
    带 run_id）| error（任务异常，带消息）。POST 校验与任务推进分属
    请求线程与后台任务线程——所有读写都持 self._lock。

    "单槽"语义：done/error 后允许再 start 覆盖旧槽（新评测盖旧结果，
    历史记录仍完整留在 eval_runs 表里，互不冲突）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: EvalJob | None = None

    def start(self, total: int) -> bool:
        """占槽启动。已有 running 任务 → False（POST 端点回 409）。"""
        with self._lock:
            if self._job is not None and self._job.status == "running":
                return False
            self._job = EvalJob(
                status="running",
                total=total,
                started_at=datetime.now().isoformat(timespec="seconds"),
            )
            return True

    def on_item(self, record: ItemRecord) -> None:
        """后台任务每题回调：推进 done 计数并记录题号（含失败题）。"""
        with self._lock:
            if self._job is not None and self._job.status == "running":
                self._job.done += 1
                self._job.current = record.id

    def finish(self, run_id: int) -> None:
        """任务成功收尾：回填 run_id，供前端跳转报告。"""
        with self._lock:
            if self._job is not None:
                self._job.status = "done"
                self._job.run_id = run_id
                self._job.current = "完成"

    def fail(self, message: str) -> None:
        """任务异常收尾：状态可见、消息可回显（错误壳语义与 400 一致）。"""
        with self._lock:
            if self._job is not None:
                self._job.status = "error"
                self._job.error = message
                self._job.current = "失败"

    def snapshot(self) -> dict | None:
        """当前任务一致快照；无任务（含从未启动/槽被释放）→ None。"""
        with self._lock:
            return self._job.to_snapshot() if self._job is not None else None


class AppServices:
    """应用服务容器：路由经 deps.get_services 取用，测试可整体替换。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.ask = AskService(settings)
        self.ingest = IngestService(settings)
        self.eval_jobs = EvalJobManager()
        # 更新链路：检查器带 TTL 缓存、下载是单槽后台任务（见 update 包）
        self.updates = UpdateChecker()
        self.update_jobs = UpdateManager()

    def rebuild_ask(self) -> None:
        """测试替换 ask 服务用（保持 create_app 测试可注入性）。"""
        self.ask = AskService(self.settings)
