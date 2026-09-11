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

import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path

DEFAULT_PORT = 8787
_PORT_TRIES = 20
_WINDOW_TITLE = "Mikasa"


def _port_free(port: int) -> bool:
    """端口能否在本机回环上绑定（True = 可用）。"""
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _mikasa_already_on(port: int) -> bool:
    """该端口上是不是**已经跑着一个 Mikasa**（而不是别的程序）。

    为什么必须区分：服务的并发安全建立在"单进程"前提上（JobManager 是内存态、
    ingest 锁是进程内锁、invalidate_index 只失效本进程快照）。若无脑顺延端口，
    双击两次就是**两个进程写同一个 SQLite 库**——同名并发上传会撞唯一约束、
    回滚还会把对方正在用的 uploads 副本删掉（2026-09-11 发包前复查发现，
    与 Mikasa.bat "探到 8787 就只开浏览器"的处理对齐）。
    """
    import json
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as resp:
            return json.loads(resp.read().decode("utf-8")).get("name") == "Mikasa"
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _pick_port(preferred: int = DEFAULT_PORT) -> tuple[int, bool]:
    """找端口，返回 (端口, 是否已有 Mikasa 在跑)。

    已有 Mikasa → 直接复用它（只开窗口，不起第二个实例）；
    端口被**别的程序**占着 → 顺延找空位；全占则回退 preferred 让服务自己报错。
    """
    for port in range(preferred, preferred + _PORT_TRIES):
        if _mikasa_already_on(port):
            return port, True
        if _port_free(port):
            return port, False
    return preferred, False


_LOG_FILE: Path | None = None


def _log_hint() -> str:
    """弹窗里给用户**可复制**的日志路径（挂不上文件日志时退回环境变量写法）。"""
    return str(_LOG_FILE) if _LOG_FILE is not None else r"%LOCALAPPDATA%\Mikasa\logs\mikasa.log"


def _attach_uvicorn_file_log(log_file: Path) -> None:
    """把同一个文件 handler 也挂到 uvicorn 自己的 logger 上。

    为什么必须单独挂：`setup_logging` 只管 `mikasa` 命名空间，而**启动阶段的致命
    错误全在 uvicorn 那边**——端口被占时它打的是 `ERROR: [Errno 10048] error while
    attempting to bind...`，走 `uvicorn.error` 自带的 StreamHandler(stderr)，而
    console=False 的构建里 stderr 是空的：消息直接消失。后果是弹窗写着"详细日志见
    …\\logs\\"，用户打开却是一个**空文件**（2026-09-11 审查实测：把端口占住启动，
    日志 0 字节）。这正是本模块要消灭的那类"看不见的失败"。

    只挂 uvicorn 的两个 logger、不挂 root：INFO 级的三方噪声不进用户日志。
    """
    import logging

    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setLevel(logging.WARNING)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    )
    for name in ("uvicorn", "uvicorn.error"):
        logger_ = logging.getLogger(name)
        for old in list(logger_.handlers):  # 幂等：重复调用不叠加 handler
            logger_.removeHandler(old)
            old.close()
        logger_.addHandler(handler)
        logger_.propagate = False  # 别再往 stderr 写：那边是空的


def _setup_file_logging() -> Path | None:
    """给窗口形态挂上文件日志——它没有控制台，文件是唯一的排障入口。

    必须显式挂：窗口路径（`_serve_forever`）**不经过 cli.main()**，而包内首次
    get_logger() 触发的无参 setup_logging() 只挂控制台 handler——在
    console=False 的构建里等于扔进黑洞。2026-09-11 实测：弹窗让用户去看
    %LOCALAPPDATA%\\Mikasa\\logs\\，而那个文件在窗口形态下从未被写过。

    失败也不能让启动挂掉：宁可没有日志，也不能双击变"点了没反应"。
    """
    try:
        from mikasa.utils.logging import default_log_file, setup_logging

        log_file = default_log_file()
        setup_logging(log_file=log_file, force=True)  # force：导入期已无参初始化过
        _attach_uvicorn_file_log(log_file)  # 端口被占这类启动错误也要落盘
        return log_file
    except Exception:  # noqa: BLE001
        return None


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

    **必须用独立的 --user-data-dir 并等它结束**：
      - 独立 profile：否则浏览器会把活交给已在运行的实例、自己立刻退出（那样
        "等进程结束"就变成"窗口刚开就退出"，服务线程随之被杀，窗口显示无法访问）；
      - 等它结束：这样**关掉窗口 = 进程退出 = 我们也能退出**。原来的写法丢掉
        Popen 句柄、然后 `while True: sleep(3600)` 挂死——用户关掉窗口后
        Mikasa.exe 仍在后台占着端口和 SQLite，没有窗口、没有托盘、没有控制台，
        只能去任务管理器结束，下次双击还只会再开一个窗口指向这个隐形进程
        （2026-09-11 审查指出）。
    """
    candidates = [
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe",
    ]
    for exe in candidates:
        if exe.is_file():
            profile = tempfile.mkdtemp(prefix="mikasa-window-")
            proc = subprocess.Popen(
                [str(exe), f"--app={url}", "--window-size=1280,860", f"--user-data-dir={profile}"]
            )
            proc.wait()  # 阻塞到窗口关闭（与 webview.start() 的语义对齐）
            shutil.rmtree(profile, ignore_errors=True)
            return True
    return False


def _open_window(url: str) -> None:
    """打开应用窗口：优先 WebView2 独立窗口，其次 Edge/Chrome 应用窗口，最后默认浏览器。"""
    try:
        import webview

        webview.create_window(
            _WINDOW_TITLE,
            url,
            width=1280,
            height=860,
            min_size=(900, 600),
            # 必须显式开：pywebview 的 text_select **默认 False**，会往页面注入
            # `body { user-select: none; cursor: default }`（见其 js/customize.js）
            # ——整个窗口的文字都选不中、复制不了。对"论文问答"来说这是硬伤：
            # 答案、引用原文、报告数据全都要能拷走（2026-09-11 用户实测反馈）。
            text_select=True,
        )
        webview.start()  # 阻塞到窗口关闭；daemon 服务线程随之退出
        return
    except Exception:  # noqa: BLE001 - 缺 WebView2 等情况：降级，不让用户看到崩溃
        pass
    if not _fallback_browser_window(url):
        # 最后一道兜底：默认浏览器。这条路**拿不到窗口句柄**，无法知道用户何时
        # 关掉页面，只能挂住——进程一旦返回就会杀掉 daemon 服务线程，刚打开的
        # 页面立刻变"无法访问此网站"。用户需在任务管理器结束 Mikasa.exe。
        webbrowser.open(url)
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

    global _LOG_FILE
    _LOG_FILE = _setup_file_logging()  # 窗口形态的排障入口，越早挂越好
    port, already = _pick_port()
    if already:
        # 已经有一个 Mikasa 在跑：只开窗口指向它，绝不起第二个实例
        # （两个进程写同一个库 = 数据损坏，见 _mikasa_already_on 的说明）
        try:
            _open_window(f"http://127.0.0.1:{port}/")
        except BaseException as exc:  # noqa: BLE001
            _alert(f"无法打开窗口：{type(exc).__name__}: {exc}")
            raise
        return

    threading.Thread(target=_serve_forever, args=(port,), daemon=True).start()
    if not _wait_ready(port):
        _alert(
            "服务启动超时。\n\n"
            "常见原因：本机 Ollama 未启动（本地模型档需要它）。\n"
            f"详细日志见：{_log_hint()}"
        )
        raise SystemExit(1)
    try:
        _open_window(f"http://127.0.0.1:{port}/")
    except BaseException as exc:  # noqa: BLE001 - 双击场景没有终端可看
        _alert(f"启动失败：{type(exc).__name__}: {exc}\n\n详细日志见：{_log_hint()}")
        raise


if __name__ == "__main__":
    main()
