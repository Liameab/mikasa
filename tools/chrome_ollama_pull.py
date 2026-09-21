#!/usr/bin/env python
"""无头 Chrome「本机模型拉取」E2E 验收（ADR-0032）。

自起一台隔离服务器 + 一台**假 Ollama**（本机回环），在真实设置面板上走完整条
链路：选「Ollama 本机」→ 点「拉取模型」→ **进度条实时前进**（轮询真的在跑）
→ 收尾显示"已就绪" → 面板上能看到进度文案的字节数。

为什么必须是假 Ollama：真拉一次是 5GB 级下载，CI/验收不可能等它，也不该
在别人机器上下东西。被测的是**前端轮询 + 后端单槽状态机 + NDJSON 解析**这条
链路；假服务按真实形状回帧（`{"status":"downloading","completed":N,"total":M}`
逐行），并把自己的请求记下来当取证——断言"模型名真的发到了 /api/pull"。

用法：
  python tools/chrome_ollama_pull.py [--out-shot tools/shots/ollama-pull-e2e.png]
退出码：0 = 验收通过；1 = 任一断言失败 / console 有错。
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.chrome_corpus import free_port, wait_server, wait_until  # noqa: E402
from tools.chrome_probe import CDP, log, wait_json_list  # noqa: E402

REPO_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIKASA_EXE = REPO_ROOT / ".venv" / "Scripts" / "mikasa.exe"

# 本机模型名（拉取请求里要带它；假 Ollama 不校验，但断言要看）
MODEL = "qwen3:8b"
TOTAL = 5 * 1024**3  # 5GB：与真模型同量级，进度文案才有意义

# **启动方式（重要）**：用 `--profile local` + `MIKASA_DATA_DIR` 隔离，
# **不能用 `--config`**——那会让面板进入只读态（`config_path` 非空 →
# 后端把字段全锁上、前端把芯片 disable），于是"点预设"这一步永远是空点，
# 整个 E2E 会以"行不出现"的样子失败（ADR-0018 的 E2E 备忘记过这条，本次又踩）。
# 假 Ollama 的地址走 local 档认的 `OLLAMA_BASE_URL` 环境变量注入。


class _FakeOllama(BaseHTTPRequestHandler):
    """假 Ollama：/api/tags 空列表；/api/pull 按真实 NDJSON 逐行推进度。"""

    pulls: list[dict] = []
    step_delay = 0.25  # 每帧间隔：让浏览器真的能看到进度往前走

    def log_message(self, *args) -> None:  # noqa: N802 - 静音访问日志
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.endswith("/api/tags") or self.path.endswith("/tags"):
            self._json({"models": []})  # 本机还没有模型
            return
        if self.path.endswith("/api/version"):
            self._json({"version": "0.0.0-fake"})
            return
        self._json({"error": "未知路径"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {}
        if self.path.endswith("/api/pull"):
            type(self).pulls.append(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            frames = [
                {"status": "pulling manifest"},
                {"status": "downloading", "completed": TOTAL // 5, "total": TOTAL},
                {"status": "downloading", "completed": TOTAL // 2, "total": TOTAL},
                {"status": "downloading", "completed": TOTAL, "total": TOTAL},
                {"status": "verifying sha256 digest"},
                {"status": "success"},
            ]
            for frame in frames:
                self.wfile.write((json.dumps(frame) + "\n").encode("utf-8"))
                self.wfile.flush()
                time.sleep(type(self).step_delay)
            return
        self._json({"error": "未知路径"}, status=404)


def _start_fake_ollama(port: int) -> ThreadingHTTPServer:
    _FakeOllama.pulls = []
    server = ThreadingHTTPServer(("127.0.0.1", port), _FakeOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def _run(args) -> int:
    args.server_port = args.server_port or free_port()
    args.cdp_port = args.cdp_port or free_port()
    args.fake_port = args.fake_port or free_port()

    _start_fake_ollama(args.fake_port)
    log(f"假 Ollama：http://127.0.0.1:{args.fake_port}")

    tmp = Path(tempfile.mkdtemp(prefix="ollama-pull-e2e-"))
    server = None
    chrome = None
    try:
        # 隔离数据目录走 MIKASA_DATA_DIR（**不是 --config**，理由见文件头）
        env = dict(
            os.environ,
            MIKASA_DATA_DIR=str(tmp / "data"),
            OLLAMA_BASE_URL=f"http://127.0.0.1:{args.fake_port}/v1",
        )
        with open(tmp / "serve.log", "w", encoding="utf-8") as logf:
            server = subprocess.Popen(
                [
                    str(MIKASA_EXE),
                    "serve",
                    "--profile",
                    "local",
                    "--port",
                    str(args.server_port),
                ],
                stdout=logf,
                stderr=subprocess.STDOUT,
                cwd=str(REPO_ROOT),
                env=env,
            )
        try:
            await wait_server(args.server_port)
        except RuntimeError:
            log("服务器启动失败，serve.log 尾：")
            log((tmp / "serve.log").read_text(encoding="utf-8")[-3000:])
            return 1

        url = f"http://127.0.0.1:{args.server_port}/"
        profile = tempfile.mkdtemp(prefix="ollama-pull-chrome-")
        chrome = subprocess.Popen(
            [
                args.chrome,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                f"--remote-debugging-port={args.cdp_port}",
                f"--user-data-dir={profile}",
                "--window-size=1400,900",
                url,
            ]
        )
        target = wait_json_list(args.cdp_port)
        bad: list[str] = []
        async with CDP(target["webSocketDebuggerUrl"]) as cdp:
            await cdp.call("Page.enable")
            await cdp.call("Log.enable")
            await cdp.call("Runtime.enable")
            await cdp.call("Page.reload")
            await asyncio.sleep(0.4)
            await wait_until(cdp, "document.readyState === 'complete'", "问答页加载")

            # 空库会弹引导面板，先关掉（否则点不到齿轮）
            await asyncio.sleep(0.5)
            await cdp.evaluate(
                "(() => { const b = document.querySelector('#onboard .btn.primary');"
                " if (b) b.click(); return true; })()"
            )
            await asyncio.sleep(0.3)

            # 打开设置面板 → 选「Ollama 本机」预设 → 拉取行出现
            await cdp.evaluate(
                "(() => { document.querySelector('#btn-settings').click(); return true; })()"
            )
            await wait_until(
                cdp,
                "!document.querySelector('#settings-panel').classList.contains('hidden')",
                "设置面板打开",
            )
            # 等面板的异步回填落地再点（回填晚到会把选择覆盖回服务端值；
            # 产品侧另有"过期响应守卫"兜底，这里按真实节奏走）
            await wait_until(
                cdp,
                "!!document.querySelector('#s-provider .s-chip.on')",
                "面板回填完成（有芯片被点亮）",
            )
            await cdp.evaluate(
                "(() => {"
                " document.querySelector('#s-provider .s-chip[data-preset=ollama]').click();"
                " return true; })()"
            )
            await wait_until(
                cdp,
                "!document.querySelector('#s-pull-row').classList.contains('hidden')",
                "「本机模型」行出现（选了 Ollama 预设才显示）",
            )

            # 点「拉取模型」：先看到 running 文案，再看到进度真在走
            await cdp.evaluate(
                "(() => { document.querySelector('#s-pull-btn').click(); return true; })()"
            )
            await wait_until(
                cdp,
                "(() => { const t = document.querySelector('#s-pull-state').textContent || '';"
                " return t.includes('正在拉取'); })()",
                "进入拉取中（轮询拿到了 running）",
                timeout=20.0,
            )
            await wait_until(
                cdp,
                "(() => { const w = document.querySelector('#s-pull-bar').style.width || '';"
                " return parseFloat(w) > 0; })()",
                "进度条开始前进",
                timeout=20.0,
            )

            # 收尾：done 文案 + 进度条归零
            await wait_until(
                cdp,
                "(() => { const t = document.querySelector('#s-pull-state').textContent || '';"
                " return t.includes('已就绪'); })()",
                "拉取完成提示",
                timeout=30.0,
            )
            final = await cdp.evaluate(
                "(() => { const t = document.querySelector('#s-pull-state').textContent;"
                " const c = document.querySelector('#s-pull-row') ? 1 : 0;"
                " return JSON.stringify({text: t, hasRow: c, cancelHidden:"
                " document.querySelector('#s-pull-cancel').classList.contains('hidden')}); })()"
            )
            import json as _json

            info = _json.loads(final)
            if MODEL not in info["text"]:
                bad.append(f"完成文案里没有模型名：{info['text']}")
            if not info["cancelHidden"]:
                bad.append("拉取结束后「取消」按钮没有隐藏")

            # 取证：假的 Ollama 真收到了那次 /api/pull，模型名对得上
            if not _FakeOllama.pulls:
                bad.append("假 Ollama 没收到 /api/pull 请求")
            elif _FakeOllama.pulls[0].get("name") != MODEL:
                bad.append(f"上游收到的模型名不对：{_FakeOllama.pulls[0]}")

            # 后端状态机也应当停在 done（前端文案不是唯一证据）
            status = await cdp.evaluate(
                "(async () => { const r = await fetch('/api/settings/ollama/pull');"
                " const d = await r.json(); return JSON.stringify(d); })()"
            )
            snapshot = _json.loads(status)
            if snapshot.get("status") != "done":
                bad.append(f"后端任务状态不是 done：{snapshot}")
            if snapshot.get("total") != TOTAL:
                bad.append(f"总字节没传对：{snapshot.get('total')}")

            if args.out_shot:
                import base64

                shot = await cdp.call("Page.captureScreenshot", {"format": "png"})
                Path(args.out_shot).parent.mkdir(parents=True, exist_ok=True)
                Path(args.out_shot).write_bytes(base64.b64decode(shot["data"]))
                log(f"截图：{args.out_shot}")

            if cdp.errors:
                bad.append("console 有错误：\n    " + "\n    ".join(cdp.errors[:5]))

        if bad:
            log("E2E 未过：\n  - " + "\n  - ".join(bad))
            return 1
        log("「本机模型拉取」E2E 通过（预设 → 拉取 → 进度前进 → 已就绪）")
        return 0
    finally:
        for proc in (chrome, server):
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description="「拉取本机模型」E2E（无头 Chrome）")
    parser.add_argument("--server-port", type=int, default=0)
    parser.add_argument("--cdp-port", type=int, default=0)
    parser.add_argument("--fake-port", type=int, default=0)
    parser.add_argument("--out-shot", default="")
    parser.add_argument(
        "--chrome",
        default=os.environ.get(
            "CHROME_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        ),
        help="Chrome 可执行文件（缺失时自动试 %LOCALAPPDATA% 下的安装）",
    )
    args = parser.parse_args()
    if not Path(args.chrome).exists():
        alt = Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe"
        if alt.exists():
            args.chrome = str(alt)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
