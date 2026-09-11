"""Mikasa 打包入口（PyInstaller 分析这个文件生成 exe）。

**双击即用**是打包版的默认体验，也是它和源码版的区别所在：
  - 不带参数：自己找可用端口 → 起本地服务 → 自动打开浏览器。用户看不到
    命令行，也不需要知道端口是什么。
  - 带参数（`Mikasa.exe doctor` / `ask "..."` / `ingest <路径>`）：原样透传
    给 CLI，方便命令行用户与自动化脚本（输出会写进日志文件，见下）。

两个打包特有的坑，都在这里处理（源码版不需要）：
  1. **端口冲突**：双击时上一个服务往往还开着（用户不会想到要先去关它），
     绑不上端口就静默退出等于"点了没反应"。这里顺延找空位。
  2. **无控制台**：exe 以 console=False 构建（像正经软件，不弹黑窗口），
     于是报错时用户什么都看不到——用系统弹窗兜底，日志同时落盘。
"""

from __future__ import annotations

import socket
import sys
import threading
import webbrowser

DEFAULT_PORT = 8787
_PORT_TRIES = 20


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


def main() -> None:
    args = sys.argv[1:]
    if args:
        # 命令行用法：透传给 CLI（Mikasa.exe doctor / ask / ingest ...）
        from mikasa.cli import main as cli_main

        try:
            cli_main()
        except SystemExit:
            raise
        except BaseException as exc:  # noqa: BLE001 - 无控制台时也要留痕
            _alert(f"{type(exc).__name__}: {exc}")
            raise
        return

    # 双击用法：起服务 + 开浏览器
    port = _pick_port()
    # 等一小会儿再开浏览器：服务冷启动要几秒，立刻打开会看到"无法连接"
    threading.Timer(4.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()
    sys.argv = [sys.argv[0], "serve", "--profile", "local", "--port", str(port)]
    from mikasa.cli import main as cli_main

    try:
        cli_main()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - 双击场景没有终端可看
        _alert(
            f"启动失败：{type(exc).__name__}: {exc}\n\n"
            "常见原因：\n"
            "  · 本机 Ollama 未启动（本地模型档需要它）\n"
            "  · 端口被占且 20 个候选端口都不可用\n\n"
            "详细日志见：%LOCALAPPDATA%\\Mikasa\\logs\\"
        )
        raise


if __name__ == "__main__":
    main()
