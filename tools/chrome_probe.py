#!/usr/bin/env python
"""无头 Chrome 探针 —— 前端改版验收工具（中文注释纪律）。

为什么存在：`--dump-dom` 只能看"页面自己渲染成什么样"，无法做交互
（点开文件夹、切换标签页），console 错误也只能从 stderr 里捞。本工具走
Chrome DevTools Protocol（CDP）：启动一个可点击的无头 Chrome，可以：
  1. 等页面就绪后**执行任意页面脚本**（如展开全部文件夹）；
  2. 收集 console 错误（Log.entryAdded，独立于 stderr）；
  3. 输出一组断言用的关键指标（JSON）；
  4. 可选截图落盘（配合像素校验脚本目检样式）。

用法：
  python tools/chrome_probe.py <url> [--click-folders] [--out-shot 输出.png]
  [--chrome <chrome.exe 路径>] [--port 9333]

输出：末行打印 JSON（页面就绪、指标、console 错误列表）；有 console 错误
时退出码为 1（供脚本/CI 判失败）。

依赖：websockets（开发工具，已入 venv）。
"""

import argparse
import asyncio
import base64
import json
import subprocess
import sys
import tempfile
import time
import urllib.request

import websockets

DEFAULT_PORT = 9333


def log(msg):
    print(msg, file=sys.stderr)


def wait_json_list(port, timeout=15):
    """等 Chrome 的 CDP 列表端点就绪，返回页面目标（ws 地址）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as resp:
                targets = json.loads(resp.read().decode("utf-8"))
            for t in targets:
                if t.get("type") == "page":
                    return t
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError("Chrome CDP 未就绪")


class CDP:
    """极简 CDP 客户端：发方法等对应响应，事件只留错误日志。"""

    def __init__(self, ws_url):
        self.ws_url = ws_url
        self._id = 0
        self._waiters = {}
        self.errors = []  # console/网络错误（Log.entryAdded，level=error）

    async def __aenter__(self):
        self.ws = await websockets.connect(self.ws_url, max_size=16 * 1024 * 1024)
        asyncio.create_task(self._listen())
        return self

    async def __aexit__(self, *exc):
        await self.ws.close()

    async def _listen(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if "id" in msg and msg["id"] in self._waiters:
                self._waiters.pop(msg["id"]).set_result(msg)
            elif msg.get("method") == "Log.entryAdded":
                entry = msg["params"]["entry"]
                if entry.get("level") == "error":
                    self.errors.append(entry.get("text", "") + "  @ " + entry.get("url", ""))
            elif msg.get("method") == "Runtime.exceptionThrown":
                # 未捕获异常（模块求值期错误、事件回调里的 TypeError 等）走的是
                # 这个事件，**不是** Log.entryAdded——2026-09-11 教训：探针原先
                # 只收 console.error，于是"整条 ESM 模块链已死"的页面依然报
                # consoleErrors: []，把一次真实故障放过去了。要求 Runtime.enable。
                detail = msg["params"].get("exceptionDetails", {})
                desc = (detail.get("exception") or {}).get("description") or detail.get("text", "")
                first = desc.strip().split("\n")[0][:300]  # 只看首行，堆栈留给调试
                self.errors.append("未捕获异常: " + first)

    async def call(self, method, params=None):
        self._id += 1
        fut = asyncio.get_event_loop().create_future()
        self._waiters[self._id] = fut
        await self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        resp = await fut
        if "error" in resp:
            raise RuntimeError(f"CDP {method} 失败: {resp['error']}")
        return resp.get("result", {})

    async def evaluate(self, expr):
        """页面里执行表达式，返回 JS 值（出错时抛 Runtime.exceptionDetails）。"""
        res = await self.call(
            "Runtime.evaluate",
            {
                "expression": expr,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        if "exceptionDetails" in res:
            # text 常见是干巴巴的 "Uncaught"，真正消息在 exception.description
            detail = res["exceptionDetails"]
            desc = detail.get("exception", {}).get("description", "") or detail.get("text", "")
            raise RuntimeError(f"页面脚本异常: {desc.strip()[:400]}")
        return res.get("result", {}).get("value")


async def run(args):
    profile = tempfile.mkdtemp(prefix="probe-profile-")
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
    try:
        target = wait_json_list(args.port)
        async with CDP(target["webSocketDebuggerUrl"]) as cdp:
            await cdp.call("Page.enable")
            await cdp.call("Log.enable")
            await cdp.call("Runtime.enable")  # 未捕获异常（Runtime.exceptionThrown）必需
            # Chrome 是带 URL 启动的：等我们连上 CDP，页面往往已加载完，而模块
            # 求值期的异常只在加载那一刻抛出、事后不补发 —— 故监听就绪后重载
            # 一次，确保这类错误必被捕获（无头探针每次全新 profile，无缓存干扰）。
            await cdp.call("Page.reload")
            await asyncio.sleep(0.4)  # 让重载真正起步，免得下面轮询到旧页面的 complete
            # 等页面完全就绪（问答页要等 refreshSessions 的网络往返）
            for _ in range(60):
                state = await cdp.evaluate(
                    "document.readyState + '|' + String("
                    "document.querySelectorAll('#session-list .session-item').length)"
                )
                if state.startswith("complete|"):
                    break
                await asyncio.sleep(0.25)
            await asyncio.sleep(1.0)  # 等 refreshSessions 网络往返（首载/树渲染）

            if args.click_folders:
                # 展开全部文件夹（行点击 = 开合，qa-tree 的 toggleFolder 逻辑）
                await cdp.evaluate(
                    "[...document.querySelectorAll('.t-folder')].forEach(r => r.click()); true"
                )
                await asyncio.sleep(0.8)

            metrics = await cdp.evaluate("""(() => {
              const q = s => document.querySelectorAll(s).length;
              return {
                sessionItems: q('.session-item'),
                sessionRowsInFolder: q('.session-item.in-folder'),
                folderRows: q('.t-folder'),
                caretOpen: [...document.querySelectorAll('.t-caret')]
                  .filter(e => e.textContent.trim() === '▾').length,
                healthPill: (document.querySelector('.health-pill')?.textContent ||
                  '').trim().slice(0, 80),
                brand: (document.querySelector('.brand')?.textContent || '').trim(),
              };
            })()""")

            shot = None
            if args.out_shot:
                res = await cdp.call("Page.captureScreenshot", {"format": "png"})
                shot = res["data"]

            # 静默给页面 1.5s 消化可能的异步报错，再收 console 错误
            await asyncio.sleep(1.5)

            print(
                json.dumps(
                    {
                        "metrics": metrics,
                        "consoleErrors": cdp.errors,
                    },
                    ensure_ascii=False,
                )
            )
            if shot:
                with open(args.out_shot, "wb") as f:
                    f.write(base64.b64decode(shot))
                log(f"截图已写: {args.out_shot}")
            return 1 if cdp.errors else 0
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except subprocess.TimeoutExpired:
            chrome.kill()


def main():
    parser = argparse.ArgumentParser(description="无头 Chrome 探针（前端验收工具）")
    parser.add_argument("url", help="目标页面地址，如 http://127.0.0.1:8787/")
    parser.add_argument(
        "--click-folders", action="store_true", help="页面就绪后展开全部文件夹（树交互验收用）"
    )
    parser.add_argument("--out-shot", default=None, help="截图输出路径（PNG）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--chrome",
        default=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    )
    args = parser.parse_args()
    # 用户安装目录的兜底猜测（$LOCALAPPDATA 常见于个人机）
    import os

    if not os.path.exists(args.chrome):
        local = os.environ.get("LOCALAPPDATA", "")
        alt = os.path.join(local, r"Google\Chrome\Application\chrome.exe")
        if os.path.exists(alt):
            args.chrome = alt
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
