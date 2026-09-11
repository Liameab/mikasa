"""Mikasa 打包入口（PyInstaller 分析这个文件生成 exe）。

**双击 = 一个独立窗口的桌面应用**（不是浏览器标签页）。做法是"套壳"：
后端仍是我们自己的 FastAPI 服务（跑在后台线程），窗口用系统自带的
WebView2（Windows 11 内置）渲染同一套界面——微信桌面版、VS Code 走的
是同一条路。好处是前端零改动、双形态共用一份 UI。

带参数的调用（`Mikasa.exe doctor` / `ask "..."` / `ingest <路径>`）原样
透传给 CLI，方便命令行用户与自动化脚本。

打包特有的三个坑都在这里处理（源码版不需要）：
  1. **端口冲突**：双击时上一个服务往往还开着（用户不会想到要先关它），
     绑不上端口就静默退出 = "点了没反应"。这里顺延找空位。
  2. **无控制台**：exe 以 console=False 构建（像正经软件，不弹黑窗口），
     报错时用户什么都看不到——用系统弹窗兜底，日志同时落盘。
  3. **WebView2 缺失**：极少数精简版系统没有；此时降级用 Edge/Chrome 的
     --app 模式开一个无地址栏窗口，最坏情况才退回默认浏览器。
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

DEFAULT_PORT = 8787
_PORT_TRIES = 20
_WINDOW_TITLE = "Mikasa — 文档问答工作台"


def _port_free(port: int) -> bool:
    """端口能否在本机回环上绑定（True = 可用）。"""
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _pick_port(preferred: int = DEFAULT_PORT) -> int:
    """从 preferred 起顺延找第一个可用端口；全占则返回 preferred（让服务自己报错）。"""
    for port in range(preferred, preferred + _PORT_TRIES):
        if _port_free(port):
            return port
    return preferred


def _alert(message: str) -> None:
    """无控制台的构建里，错误只能靠系统弹窗让用户看见。"""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "Mikasa 启动失败", 0x10)
    except Exception:  # noqa: BLE001 - 弹窗失败也不能再抛（否则又静默了）
        pass


def _serve_forever(port: int) -> None:
    """在后台线程里跑 FastAPI 服务（窗口关闭时进程退出，无需优雅停机）。

    用 `uvicorn.Server` 而不是 `uvicorn.run`：后者会装信号处理器，非主线程
    安装会抛 ValueError。
    """
    import uvicorn

    from mikasa.config.settings import load_dotenv_file, load_settings
    from mikasa.web.app import create_app

    load_dotenv_file()
    settings = load_settings("local")
    config = uvicorn.Config(
        create_app(settings),
        host="127.0.0.1",
        port=port,
        log_level="warning",  # 窗口形态没有终端，日志走文件（setup_logging 已落盘）
    )
    uvicorn.Server(config).run()


def _wait_ready(port: int, timeout: float = 25.0) -> bool:
    """等服务端口就绪（窗口加载 URL 前必须确认，否则用户看到"无法连接"）。"""
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2):
                return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    return False


def _fallback_browser_window(url: str) -> bool:
    """降级：用 Edge/Chrome 的 --app 模式开一个无地址栏窗口（仍像独立应用）。

    仅在 WebView2 不可用时走到这里——比默认浏览器标签页体面得多。
    """
    candidates = [
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe",
    ]
    for exe in candidates:
        if exe.is_file():
            subprocess.Popen([str(exe), f"--app={url}", f"--window-size=1280,860"])
            return True
    return False


def _open_window(url: str) -> None:
    """打开应用窗口：优先 WebView2 独立窗口，其次 Edge/Chrome 应用窗口，最后默认浏览器。"""
    try:
        import webview

        webview.create_window(_WINDOW_TITLE, url, width=1280, height=860, min_size=(900, 600))
        webview.start()  # 阻塞到窗口关闭；daemon 服务线程随之退出
        return
    except Exception:  # noqa: BLE001 - 缺 WebView2 等情况：降级，不让用户看到崩溃
        pass
    if not _fallback_browser_window(url):
        webbrowser.open(url)
        # 没有窗口可挂，进程不能立刻退出（否则服务没了）——等待用户 Ctrl+C 或手动结束
        while True:
            time.sleep(3600)


def _run_cli() -> None:
    """命令行用法：透传给 CLI。"""
    from mikasa.cli import main as cli_main

    try:
        cli_main()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - 无控制台时也要留痕
        _alert(f"{type(exc).__name__}: {exc}")
        raise


def main() -> None:
    if sys.argv[1:]:
        _run_cli()
        return

    port = _pick_port()
    threading.Thread(target=_serve_forever, args=(port,), daemon=True).start()
    if not _wait_ready(port):
        _alert(
            "服务启动超时。\n\n"
            "常见原因：本机 Ollama 未启动（本地模型档需要它）。\n"
            "详细日志见：%LOCALAPPDATA%\\Mikasa\\logs\\"
        )
        raise SystemExit(1)
    try:
        _open_window(f"http://127.0.0.1:{port}/")
    except BaseException as exc:  # noqa: BLE001 - 双击场景没有终端可看
        _alert(
            f"启动失败：{type(exc).__name__}: {exc}\n\n"
            "详细日志见：%LOCALAPPDATA%\\Mikasa\\logs\\"
        )
        raise


if __name__ == "__main__":
    main()
