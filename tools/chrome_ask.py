#!/usr/bin/env python
"""无头 Chrome 问答驱动 —— 答案富渲染/交互的端到端验收工具（中文注释纪律）。

与 chrome_probe.py（看会话树）互补：本工具在真实页面上**发一条问题**，
等流式收尾后输出渲染断言指标，用于验收 renderAnswer 的块级输出
（表格 / 代码块 / 复制钮）在真页面上的呈现：

用法：
  python tools/chrome_ask.py <url> --question "要问的话" [--mode kb|free]
    [--out-shot 输出.png] [--require-table] [--require-code]
    [--timeout 秒] [--port 9333] [--chrome <chrome.exe 路径>]

流程：页面就绪 → 切模式、把问题写进输入框、点发送 → 轮询直到流式
占位消失（.msg.assistant.streaming 归零）→ 收集指标与 console 错误 →
--require-* 未满足或 console 有错时退出码 1。

依赖：websockets（开发工具，已入 venv）。
"""

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import time

# 脚本直跑时 sys.path[0] 是 tools/ 自身，"tools.*" 包式导入需要仓库根入路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.chrome_probe import CDP, wait_json_list  # 复用极简 CDP 封装与就绪等待


async def run(args):
    profile = tempfile.mkdtemp(prefix="probe-ask-")
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
            # 页面就绪（问答页要等 refreshSessions 网络往返）
            for _ in range(60):
                state = await cdp.evaluate(
                    "document.readyState + '|' + "
                    "String(document.querySelectorAll('.health-pill span').length)"
                )
                if state.startswith("complete|"):
                    break
                await asyncio.sleep(0.25)
            await asyncio.sleep(1.2)

            # 触发提问：切模式 → 写输入框 → 点发送（走页面同一条 sendQuestion 链路）
            q = json.dumps(args.question, ensure_ascii=False)
            await cdp.evaluate(
                f"""(async () => {{
                  document.querySelector('#mode-{args.mode}')?.click();
                  const box = document.querySelector('#question');
                  if (box) box.value = {q};
                  const btn = document.querySelector('#send');
                  if (btn) btn.click();
                  return true;
                }})()"""
            )

            # 轮询流式收尾（meta/delta/done；free 长答在本地小模型上可达分钟级）
            deadline = time.time() + args.timeout
            while time.time() < deadline:
                state = await cdp.evaluate(
                    "JSON.stringify({"
                    "streaming: document.querySelectorAll('.msg.assistant.streaming').length,"
                    "bubbles: document.querySelectorAll('.msg.assistant .bubble').length,"
                    "})"
                )
                state = json.loads(state)
                if state["bubbles"] >= 1 and state["streaming"] == 0:
                    break
                await asyncio.sleep(2.0)
            else:
                raise RuntimeError(f"超时 {args.timeout}s：回答未收尾（streaming 未归零）")

            await asyncio.sleep(1.0)  # done 帧后让渲染/委托稳定

            metrics = await cdp.evaluate("""(() => {
              const q = s => document.querySelectorAll(s);
              const last = [...q('.msg.assistant .bubble')].at(-1);
              return {
                codeBlocks: q('.code-block').length,
                copyButtons: q('.btn-copy').length,
                tables: q('.md-table-wrap table.md-table').length,
                citeChips: q('.cite').length,
                citeBad: q('.cite.bad').length,
                headings: q('.bubble h3, .bubble h4, .bubble h5, .bubble h6').length,
                lists: q('.bubble ul, .bubble ol').length,
                lastSnippet: (last ? last.textContent : '').slice(0, 60),
              };
            })()""")

            shot = None
            if args.out_shot:
                res = await cdp.call("Page.captureScreenshot", {"format": "png"})
                shot = res["data"]

            # 静默 1.5s 消化可能的异步报错后收 console
            await asyncio.sleep(1.5)
            print(
                json.dumps(
                    {"metrics": metrics, "consoleErrors": cdp.errors},
                    ensure_ascii=False,
                )
            )
            if shot:
                with open(args.out_shot, "wb") as f:
                    f.write(base64.b64decode(shot))
                print(f"截图已写: {args.out_shot}", file=sys.stderr)

            # 验收门：console 零错误 + 要求的产物出现
            bad = []
            if cdp.errors:
                bad.append("console 有错误")
            if args.require_table and not metrics["tables"]:
                bad.append("未渲染出表格")
            if args.require_code and not metrics["codeBlocks"]:
                bad.append("未渲染出代码块")
            if bad:
                print("验收未过：" + "；".join(bad), file=sys.stderr)
                return 1
            return 0
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except subprocess.TimeoutExpired:
            chrome.kill()


def main():
    parser = argparse.ArgumentParser(description="无头 Chrome 问答驱动（答案渲染验收）")
    parser.add_argument("url", help="目标页面地址，如 http://127.0.0.1:8787/")
    parser.add_argument("--question", required=True, help="要发送的问题")
    parser.add_argument("--mode", default="free", choices=["kb", "free"], help="问答模式")
    parser.add_argument("--require-table", action="store_true", help="断言答案含表格")
    parser.add_argument("--require-code", action="store_true", help="断言答案含代码块")
    parser.add_argument("--timeout", type=int, default=300, help="等待回答收尾的超时秒数")
    parser.add_argument("--out-shot", default=None, help="截图输出路径（PNG）")
    parser.add_argument("--port", type=int, default=9333)
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
