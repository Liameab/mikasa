"""打包入口（packaging/entry.py）的行为测试。

为什么单独测这个文件：它**只在打包后才跑**，源码形态下 430 个测试全绿也拦不住
这里的缺陷——而它管的两件事都是"用户看不见的灾难"：

  1. 文件日志：窗口形态（console=False）没有控制台，文件是唯一的排障入口。
     2026-09-11 实测缺陷：这条路径不经过 cli.main()，弹窗让用户去看的日志
     从未被写过。
  2. 端口选择：双击第二次必须**复用**已有实例，不能起第二个进程写同一个
     SQLite 库（并发上传撞唯一约束 + 回滚删掉对方在用的 uploads 副本）。

entry.py 不是包内模块（PyInstaller 直接分析脚本），故按路径加载。
"""

from __future__ import annotations

import importlib.util
import logging
import socket
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from mikasa.utils.logging import setup_logging

REPO_ROOT = Path(__file__).resolve().parents[3]
ENTRY_PATH = REPO_ROOT / "packaging" / "entry.py"


@pytest.fixture()
def entry() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mikasa_packaging_entry", ENTRY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _restore_logging():
    """用例结束后还原日志配置（文件 handler 不留给其他用例）。"""
    yield
    setup_logging(force=True)


# ---------------- 文件日志（窗口形态的排障入口） ----------------


def test_windowed_entry_attaches_file_handler(entry: ModuleType, tmp_path: Path, monkeypatch):
    """`_setup_file_logging` 必须真把 FileHandler 挂上，且落进数据目录。"""
    monkeypatch.setenv("MIKASA_DATA_DIR", str(tmp_path))

    log_file = entry._setup_file_logging()

    assert log_file == tmp_path / "logs" / "mikasa.log"
    handlers = logging.getLogger("mikasa").handlers
    assert any(isinstance(h, logging.FileHandler) for h in handlers), (
        "窗口形态没有控制台，没挂 FileHandler 等于用户排障无据可依"
    )

    logging.getLogger("mikasa").warning("窗口形态的排障记录")
    assert "窗口形态的排障记录" in log_file.read_text(encoding="utf-8")


def test_log_hint_uses_real_path_after_setup(entry: ModuleType, tmp_path: Path, monkeypatch):
    """弹窗里给的必须是**真实可复制**的路径，不是字面量 %LOCALAPPDATA%。"""
    monkeypatch.setenv("MIKASA_DATA_DIR", str(tmp_path))
    entry._LOG_FILE = entry._setup_file_logging()

    assert str(tmp_path) in entry._log_hint()
    assert "%LOCALAPPDATA%" not in entry._log_hint()


def test_setup_file_logging_survives_failure(entry: ModuleType, monkeypatch):
    """挂不上日志也不能让双击变"点了没反应"——失败返回 None，不抛。"""

    def boom() -> Path:
        raise OSError("磁盘只读")

    monkeypatch.setattr("mikasa.utils.logging.default_log_file", boom)

    assert entry._setup_file_logging() is None
    assert entry._log_hint()  # 退回环境变量写法，仍给得出提示


# ---------------- 窗口参数（用户直接感知的默认值） ----------------


def test_window_enables_text_selection(entry: ModuleType, monkeypatch):
    """窗口必须允许选中/复制文本。

    pywebview 的 `text_select` **默认 False**，会注入 `body { user-select: none }`
    ——整个窗口的文字都选不中。对"论文问答"这是硬伤（答案、引用原文、评测报告
    都要能拷走），2026-09-11 用户实测反馈后修。这条测试把默认值钉死。
    """
    captured: dict = {}
    fake = SimpleNamespace(
        create_window=lambda *args, **kwargs: captured.update(kwargs),
        start=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "webview", fake)
    # 兜底：万一跑到降级路径，也不要在测试里真去拉起 Edge/Chrome
    monkeypatch.setattr(entry, "_fallback_browser_window", lambda url: True)

    entry._open_window("http://127.0.0.1:8787/")

    assert captured.get("text_select") is True


# ---------------- 端口选择（防双实例写坏库） ----------------


def test_port_free_detects_bound_port(entry: ModuleType):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        busy = sock.getsockname()[1]

        assert entry._port_free(busy) is False


def test_pick_port_reuses_running_mikasa(entry: ModuleType, monkeypatch):
    """端口上已有 Mikasa → 复用（already=True），绝不顺延起第二个进程。

    场景：8787 被别的程序占着，8788 上跑着一个 Mikasa——必须返回 8788 复用，
    而不是继续顺延到 8789 另起一个（那才是写坏库的路径）。
    """
    monkeypatch.setattr(entry, "_mikasa_already_on", lambda port: port == 8788)
    monkeypatch.setattr(entry, "_port_free", lambda port: port != 8787)

    assert entry._pick_port(8787) == (8788, True)


def test_pick_port_skips_port_taken_by_other_app(entry: ModuleType, monkeypatch):
    """被**别的程序**占着 → 顺延（already=False），才允许起自己的服务。"""
    monkeypatch.setattr(entry, "_mikasa_already_on", lambda port: False)
    monkeypatch.setattr(entry, "_port_free", lambda port: port != 8787)

    assert entry._pick_port(8787) == (8788, False)


def test_uvicorn_startup_errors_reach_the_log_file(entry: ModuleType, tmp_path: Path, monkeypatch):
    """端口被占这类**启动期致命错误**走的是 uvicorn 自己的 logger，必须也落盘。

    只给 mikasa 命名空间挂文件 handler 时，uvicorn 的
    `ERROR: [Errno 10048] error while attempting to bind...` 会走它自带的
    stderr StreamHandler，而 console=False 的构建里 stderr 是空的——消息直接
    消失，弹窗却让用户去看日志，日志 0 字节（2026-09-11 审查实测）。
    """
    monkeypatch.setenv("MIKASA_DATA_DIR", str(tmp_path))
    log_file = entry._setup_file_logging()
    assert log_file is not None

    logging.getLogger("uvicorn.error").error("ERROR: [Errno 10048] 端口被占用")

    assert "10048" in log_file.read_text(encoding="utf-8")


def test_uvicorn_file_handler_is_not_duplicated(entry: ModuleType, tmp_path: Path, monkeypatch):
    """重复调用不能把 handler 叠加起来（否则同一条错误写 N 遍）。"""
    monkeypatch.setenv("MIKASA_DATA_DIR", str(tmp_path))
    entry._setup_file_logging()
    entry._setup_file_logging()

    handlers = logging.getLogger("uvicorn.error").handlers
    assert len(handlers) == 1
