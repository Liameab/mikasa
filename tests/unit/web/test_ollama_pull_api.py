"""本机模型拉取端点（/api/settings/ollama/pull，ADR-0032）单元测试。

替身走 `pull_model`（真拉取是 5GB 级下载，测试永远不许真跑）。覆盖：
启动与进度回读、失败如实落到 error、模型名校验、**同一时刻只允许一个任务**、
以及取消的三态语义（管理器层单测——TestClient 会同步跑完后台任务，
端点层测不到"正在跑"的中间态）。
"""

from __future__ import annotations

import threading

import pytest

from mikasa.errors import ProviderError
from mikasa.web.services import PullJobManager


class _FakePull:
    """拉取替身：按脚本报进度，可挂起（模拟"还在下"）。"""

    def __init__(self, *, error: Exception | None = None, hang: bool = False) -> None:
        self.error = error
        self.hang = hang
        self.release = threading.Event()
        self.seen: list[tuple[str, int, int]] = []

    def __call__(self, base_url, model, *, on_progress=None, is_cancelled=None):  # noqa: ANN001
        if callable(on_progress):
            on_progress("pulling manifest", 0, 0)
            on_progress("downloading", 512, 1024)
        if self.error is not None:
            raise self.error
        if self.hang:
            self.release.wait(timeout=5)
            if callable(is_cancelled) and is_cancelled():
                return
        if callable(on_progress):
            on_progress("success", 1024, 1024)


@pytest.fixture()
def fake_pull(monkeypatch):
    def _install(**kwargs) -> _FakePull:
        fake = _FakePull(**kwargs)
        monkeypatch.setattr("mikasa.web.routers.settings.pull_model", fake)
        return fake

    return _install


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


def test_pull_runs_and_reports_progress(client, fake_pull):
    """202 启动 → GET 能读到 done 与字节数（前端进度条的数据源）。"""
    c, _ = client
    fake_pull()
    resp = c.post("/api/settings/ollama/pull", json={"model": "qwen3:8b"})
    assert resp.status_code == 202, resp.text
    assert resp.json()["model"] == "qwen3:8b"

    snapshot = c.get("/api/settings/ollama/pull").json()
    assert snapshot["status"] == "done"
    assert snapshot["completed"] == 1024 and snapshot["total"] == 1024
    assert snapshot["percent"] == 100.0


def test_pull_failure_is_reported_not_swallowed(client, fake_pull):
    """拉取失败 → 状态 error + 可读消息（不是静默回到 idle）。"""
    c, _ = client
    fake_pull(error=ProviderError("Ollama 里没有这个模型：nope:1b"))
    assert c.post("/api/settings/ollama/pull", json={"model": "nope:1b"}).status_code == 202
    snapshot = c.get("/api/settings/ollama/pull").json()
    assert snapshot["status"] == "error"
    assert "没有这个模型" in snapshot["error"]


def test_pull_rejects_bad_model_name(client, fake_pull):
    """模型名形状不对 → 422（不让明显不是名字的输入打到 Ollama）。"""
    c, _ = client
    fake_pull()
    for bad in ("", "  ", "-bad", "有中文", "a" * 200):
        assert c.post("/api/settings/ollama/pull", json={"model": bad}).status_code == 422, bad


def test_second_pull_while_running_is_409(client, monkeypatch):
    """单槽：一个任务在跑时再来一个 → 409（两个 5GB 并发只会互相抢带宽）。

    这一条要把后台任务本身换成空操作：TestClient 会在响应返回**之前**同步
    跑完 BackgroundTasks，真去拉的话任务早就结束了，测不到"正在跑"的中间态。
    """
    c, _ = client
    monkeypatch.setattr("mikasa.web.routers.settings._run_pull", lambda *a, **k: None)
    assert c.post("/api/settings/ollama/pull", json={"model": "qwen3:8b"}).status_code == 202

    assert c.get("/api/settings/ollama/pull").json()["status"] == "running"
    second = c.post("/api/settings/ollama/pull", json={"model": "qwen3:4b"})
    assert second.status_code == 409
    # HTTPException 走 FastAPI 默认形状（ZhiwenError 才是 {"error": {...}}）
    assert "已经有一个拉取任务在跑" in second.json()["detail"]


def test_idle_when_never_started(client):
    """从没拉过 → status: idle（前端据此显示"未开始"，而不是空对象）。"""
    c, _ = client
    assert c.get("/api/settings/ollama/pull").json() == {"status": "idle"}


def test_cancel_without_job_is_409(client):
    c, _ = client
    assert c.delete("/api/settings/ollama/pull").status_code == 409


# ---------------------------------------------------------------------------
# 管理器（取消语义在端点层测不到：TestClient 同步跑完后台任务）
# ---------------------------------------------------------------------------


def test_manager_cancel_flow():
    """取消请求 → is_cancelled 变真 → 线程收尾落 cancelled（不是 done）。"""
    manager = PullJobManager()
    assert manager.start("qwen3:8b")
    assert manager.is_cancelled() is False
    assert manager.cancel() is True
    assert manager.is_cancelled() is True
    manager.finish_cancelled()
    assert manager.snapshot()["status"] == "cancelled"


def test_manager_progress_ignores_writes_after_finish():
    """任务收尾后的回调不再改写状态（后台线程与轮询天然并发）。"""
    manager = PullJobManager()
    manager.start("qwen3:8b")
    manager.finish()
    manager.progress("downloading", 1, 2)
    snapshot = manager.snapshot()
    assert snapshot["status"] == "done" and snapshot["completed"] == 0


def test_manager_cancel_needs_running_job():
    manager = PullJobManager()
    assert manager.cancel() is False  # 没有任务
    manager.start("qwen3:8b")
    manager.fail("boom")
    assert manager.cancel() is False  # 已结束的任务不给取消


def test_manager_fail_keeps_message():
    manager = PullJobManager()
    manager.start("qwen3:8b")
    manager.fail("磁盘满了")
    assert manager.snapshot()["error"] == "磁盘满了"


def test_app_services_expose_pull_manager(offline_settings):
    """容器里必须真有一个槽（回归：装配漏掉会静默变 no-op）。"""
    from mikasa.web.services import AppServices

    assert isinstance(AppServices(offline_settings).pulls, PullJobManager)
