#!/usr/bin/env python
"""改名输入框点击回归验证 —— 无头 Chrome 交互探针。

为什么存在：2026-09-09 实测发现"行内改名输入框一点就关"——进入改名时
input 替换的是行内名字 span，仍位于可点击行内部；行点击委托只排除了
button（qa-tree.js / kb-tree.js 各两处），点击 input 想移动光标时 click
冒泡到行 → toggleFolder / selectDoc → 全树重绘 → 输入框被销毁，只能
再点一次"重命名"。修复 = 委托守卫改为排除 button, .tree-input。

本工具验证：进入行内改名 → 点击输入框内部 → 输入框必须仍存活（树
未被重绘），再派发 Esc 取消（零数据改动、不提交改名）。问答页（会话/
文件夹行）与知识库页（文档/文件夹行）通用。console 错误 → 退出码 1。

用法：
  python tools/chrome_rename_check.py http://127.0.0.1:8787/           # 问答页
  python tools/chrome_rename_check.py http://127.0.0.1:8787/documents  # 知识库页

依赖：websockets（与 chrome_probe.py 同款 CDP 封装，代码取自其上）。
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chrome_probe import CDP, wait_json_list  # noqa: E402 复用 chrome_probe 封装


async def wait_truthy(cdp, expr, timeout=8.0):
    """轮询直到页面表达式为真（菜单项/输入框等异步 DOM 出现的通用等法）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await cdp.evaluate(expr):
            return True
        await asyncio.sleep(0.15)
    return False


async def run(args):
    profile = tempfile.mkdtemp(prefix="rename-check-")
    chrome = subprocess.Popen(
        [
            args.chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            f"--remote-debugging-port={args.port}",
            f"--user-data-dir={profile}",
            "--window-size=1400,900",
            args.url,
        ]
    )
    step = {"name": "未执行", "ok": False, "detail": ""}

    def mark(name, ok, detail=""):
        step.update(name=name, ok=ok, detail=detail)

    try:
        target = wait_json_list(args.port)
        async with CDP(target["webSocketDebuggerUrl"]) as cdp:
            await cdp.call("Page.enable")
            await cdp.call("Log.enable")
            # 就绪：任一行出现（.t-folder 文件夹行 / .session-item 会话与文档行，
            # 两类行都带 ⋯ 菜单与行内改名）
            if not await wait_truthy(
                cdp, "document.querySelector('.t-folder .t-more, .session-item .t-more') !== null"
            ):
                mark("页面就绪", False, "未找到任何可操作行（服务/页面有问题？）")
            else:
                rows_before = await cdp.evaluate(
                    "document.querySelectorAll('.session-item, .t-folder').length"
                )
                # 打开首行 ⋯ 菜单 → 点「重命名」
                ok = await cdp.evaluate(
                    "(() => { const b = document.querySelector("
                    "'.t-folder .t-more, .session-item .t-more'); b.click(); return true; })()"
                )
                assert ok
                ok = await wait_truthy(
                    cdp,
                    "[...document.querySelectorAll('.ctx-item')].some("
                    "e => e.textContent.includes('重命名'))",
                )
                if not ok:
                    mark("打开改名菜单", False, "菜单里没有「重命名」项")
                else:
                    await cdp.evaluate(
                        "[...document.querySelectorAll('.ctx-item')].find("
                        "e => e.textContent.includes('重命名')).click(); true"
                    )
                    # 等输入框替换名字 span 就位
                    if not await wait_truthy(cdp, "document.querySelector('.tree-input') !== null"):
                        mark("进入行内改名", False, ".tree-input 未出现")
                    else:
                        # 点击输入框内部（模拟用户点文字想定位光标）：
                        # 修复前 click 冒泡到行 → 树重绘 → input 被销毁
                        await cdp.evaluate("document.querySelector('.tree-input').click(); true")
                        await asyncio.sleep(0.3)
                        alive = await cdp.evaluate(
                            "(() => { const i = document.querySelector('.tree-input');"
                            " return i !== null && i.isConnected; })()"
                        )
                        if not alive:
                            mark("点击输入框内部", False, "输入框被关闭（树被重绘，bug 仍存在）")
                        else:
                            # Esc 取消：应还原原 span 且行数不变（零数据残留）
                            await cdp.evaluate(
                                "(() => { const i = document.querySelector('.tree-input');"
                                " i.dispatchEvent(new KeyboardEvent('keydown',"
                                " { key: 'Escape', bubbles: true })); return true; })()"
                            )
                            await asyncio.sleep(0.3)
                            restored = await cdp.evaluate(
                                "document.querySelector('.tree-input') === null"
                            )
                            rows_after = await cdp.evaluate(
                                "document.querySelectorAll('.session-item, .t-folder').length"
                            )
                            if restored and rows_after == rows_before:
                                mark(
                                    "点击存活 + Esc 还原",
                                    True,
                                    f"行数 {rows_before} 前后一致，无残留",
                                )
                            else:
                                mark(
                                    "Esc 取消还原",
                                    False,
                                    f"restored={restored} 行数 {rows_before}→{rows_after}",
                                )

            await asyncio.sleep(1.0)  # 静默等异步报错冒完再收 console 错误
            print(json.dumps({"step": step, "consoleErrors": cdp.errors}, ensure_ascii=False))
            return 0 if (step["ok"] and not cdp.errors) else 1
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except subprocess.TimeoutExpired:
            chrome.kill()


def main():
    parser = argparse.ArgumentParser(description="改名输入框点击回归验证（前端交互验收）")
    parser.add_argument("url", help="目标页面，如 http://127.0.0.1:8787/ 或 .../documents")
    parser.add_argument("--port", type=int, default=9334)
    parser.add_argument(
        "--chrome",
        default=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    )
    args = parser.parse_args()
    if not os.path.exists(args.chrome):
        local = os.environ.get("LOCALAPPDATA", "")
        alt = os.path.join(local, r"Google\Chrome\Application\chrome.exe")
        if os.path.exists(alt):
            args.chrome = alt
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
