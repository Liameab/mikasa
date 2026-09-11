"""Mikasa 打包入口（PyInstaller 分析这个文件生成 exe）。

**双击即用**是打包版的默认体验：不带任何参数直接启动本地服务，并在就绪后
打开浏览器——与源码版双击 Mikasa.bat 的行为一致，只是不再需要 .venv。

带参数的调用（`Mikasa.exe doctor` / `ask "..."` / `ingest <路径>`）原样透传给
CLI，方便命令行用户与自动化脚本。

注意：这里**不做** `sys.argv` 的字符串替换式"默认参数注入"以外的任何事——
业务逻辑一律留在 mikasa.cli，打包与非打包两条路径共用同一份实现。
"""

from __future__ import annotations

import sys

# 打包版默认档位：local（本机 Ollama + fastembed，免密钥）
# ——api 需要用户自备密钥、offline 是 mock 演示档，都不适合做默认。
_DEFAULT_SERVE = ["serve", "--profile", "local", "--port", "8787"]


def main() -> None:
    if len(sys.argv) <= 1:
        sys.argv = [sys.argv[0], *_DEFAULT_SERVE]
    from mikasa.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
