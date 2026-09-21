#!/usr/bin/env python
"""无头 Chrome「生成图片」E2E 验收（文生图，ADR-0031）。

自起一台隔离服务器 + 一台**假出图服务**（本机回环，免密钥——正好走
`api_key_env` 留空那条路），在真实问答页上走完整条链路：

    点「生成图片」→ 填提示词 → 生成 → 图出现在对话里（真解码，不是裂图）
    → 后端确实收到了 image_size/model（取证）→ 会话里落了一条助手消息
    → 刷新页面，图还在（历史回放）

为什么自带假服务（同 chrome_note_ocr / chrome_eval）：真出图要密钥、按张
计费、十几秒一次且结果不可复现。被测的是**前端与 Web 链路的真实行为**；
假服务同时是取证点——它把收到的请求原样记下来，断言"提示词、模型名、
尺寸真的发出去了"。少了这条，一个"没带 image_size"的回归会静默通过
（而 SiliconFlow 那边会直接 400）。

用法：
  python tools/chrome_image.py [--out-shot tools/shots/image-e2e.png]
退出码：0 = 验收通过；1 = 任一断言失败 / console 有错。
"""

import argparse
import asyncio
import base64
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.chrome_corpus import (  # noqa: E402
    free_port,
    js_quote,
    wait_server,
    wait_until,
)
from tools.chrome_probe import CDP, log, wait_json_list  # noqa: E402

REPO_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIKASA_EXE = REPO_ROOT / ".venv" / "Scripts" / "mikasa.exe"

PROMPT = "一只坐在窗台上的白猫，简笔画风格"

_CONFIG_TEMPLATE = """\
# 「生成图片」E2E 专用配置：offline 的等价物 + 一个指向假出图服务的 image 段。
profile: offline
data_dir: {data_dir}

chunking:
  size: 400
  overlap: 50
  title_prefix: true

retrieval:
  bm25_top_k: 20
  dense_top_k: 20
  dense_enabled: false
  fusion_top_k: 10
  fusion_k: 60

reranker:
  backend: none

llm:
  backend: mock
  model: mock-zh-1
  temperature: 0.0
  max_tokens: 1024

image:
  # api_key_env 留空 = 免密钥本机服务（provider 注入占位钥匙），
  # 正好把那条分支也走一遍
  backend: api
  base_url: http://127.0.0.1:{fake_port}/v1
  model: fake-image-1
  size: 512x512
  timeout_seconds: 30.0

embedding:
  backend: none

judge:
  enabled: false
"""


def _png(size: int = 128) -> bytes:
    """现造一张 PNG 当"生成结果"（棋盘格，肉眼可辨不是空白）。"""
    rows = []
    for y in range(size):
        row = b"\x00"
        for x in range(size):
            dark = ((x // 16) + (y // 16)) % 2
            row += b"\x30\x30\x30" if dark else b"\xd8\xd0\xc8"
        rows.append(row)
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class _FakeImageHandler(BaseHTTPRequestHandler):
    """假出图端点：记请求体（取证），回 SiliconFlow 那套 `images[].url`。"""

    seen: list[dict] = []
    port = 0

    def log_message(self, *args) -> None:  # 静音（访问日志会淹掉验收输出）
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的命名
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {"raw": raw.decode("utf-8", errors="replace")}
        type(self).seen.append(
            {"path": self.path, "body": body, "auth": self.headers.get("Authorization")}
        )
        if self.path.endswith("/images/generations"):
            self._json({"images": [{"url": f"http://127.0.0.1:{type(self).port}/gen.png"}]})
            return
        self._json({"error": "未知路径"}, status=404)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.endswith("/gen.png"):
            data = _png()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.endswith("/models"):
            self._json({"data": [{"id": "fake-image-1"}]})
            return
        self._json({"error": "未知路径"}, status=404)


def _start_fake_image(port: int) -> ThreadingHTTPServer:
    _FakeImageHandler.port = port
    _FakeImageHandler.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", port), _FakeImageHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def _run(args) -> int:
    args.server_port = args.server_port or free_port()
    args.cdp_port = args.cdp_port or free_port()
    args.fake_port = args.fake_port or free_port()

    _start_fake_image(args.fake_port)
    log(f"假出图服务：http://127.0.0.1:{args.fake_port}/v1/images/generations")

    tmp = Path(tempfile.mkdtemp(prefix="image-e2e-"))
    server = None
    chrome = None
    try:
        cfg = tmp / "serve.yaml"
        cfg.write_text(
            _CONFIG_TEMPLATE.format(data_dir=(tmp / "data").as_posix(), fake_port=args.fake_port),
            encoding="utf-8",
        )
        with open(tmp / "serve.log", "w", encoding="utf-8") as logf:
            server = subprocess.Popen(
                [
                    str(MIKASA_EXE),
                    "serve",
                    "--profile",
                    "offline",
                    "--config",
                    str(cfg),
                    "--port",
                    str(args.server_port),
                ],
                stdout=logf,
                stderr=subprocess.STDOUT,
                cwd=str(REPO_ROOT),
            )
        try:
            await wait_server(args.server_port)
        except RuntimeError:
            log("服务器启动失败，serve.log 尾：")
            log((tmp / "serve.log").read_text(encoding="utf-8")[-3000:])
            return 1

        url = f"http://127.0.0.1:{args.server_port}/"
        profile = tempfile.mkdtemp(prefix="image-chrome-")
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
            await wait_until(
                cdp,
                "!!document.querySelector('#gen-image')",
                "「生成图片」按钮出现",
            )
            # 空库首次打开会弹引导面板（盖住整页）——关掉它，后面的截图才是对话本身
            await asyncio.sleep(0.5)
            await cdp.evaluate(
                "(() => { const b = document.querySelector('#onboard .btn.primary');"
                " if (b) b.click(); return true; })()"
            )
            await asyncio.sleep(0.3)

            # 1) 点开对话框 → 填提示词 → 生成
            await wait_until(
                cdp,
                "(() => { document.querySelector('#gen-image').click(); return true; })()",
                "点开生成对话框",
            )
            await wait_until(cdp, "!!document.querySelector('.img-box')", "对话框出现")
            await wait_until(
                cdp,
                f"(() => {{ const t = document.querySelector('.img-prompt');"
                f" t.value = {js_quote(PROMPT)};"
                f" document.querySelector('.img-box .btn.primary').click(); return true; }})()",
                "填提示词并点生成",
            )

            # 2) 图出现在对话里（真解码：naturalWidth > 0；裂图时为 0）
            await wait_until(
                cdp,
                "(() => { const i = document.querySelector('.msg.assistant .md-figure img');"
                " return !!i && i.complete && i.naturalWidth > 0; })()",
                "生成的图片在对话里渲染完成",
                timeout=60.0,
            )
            shot = await cdp.call(
                "Runtime.evaluate",
                {
                    "expression": "(() => { const i = document.querySelector('.md-figure img');"
                    " return JSON.stringify({src: i.getAttribute('src'), w: i.naturalWidth,"
                    " alt: i.getAttribute('alt')}); })()",
                    "returnByValue": True,
                },
            )
            info = json.loads(shot["result"]["value"])
            if not info["src"].startswith("/api/images/generated/"):
                bad.append(f"图片地址不是同源生成图端点：{info['src']}")
            if info["w"] <= 0:
                bad.append("图片没有真正解码（naturalWidth=0）")
            if PROMPT not in (info["alt"] or ""):
                bad.append(f"alt 里没有提示词：{info['alt']}")

            # 3) 取证：后端真的把提示词/模型/尺寸发出去了
            if not _FakeImageHandler.seen:
                bad.append("假出图服务没有收到任何请求")
            else:
                sent = _FakeImageHandler.seen[0]["body"]
                if sent.get("prompt") != PROMPT:
                    bad.append(f"上游收到的提示词不对：{sent.get('prompt')}")
                if sent.get("model") != "fake-image-1":
                    bad.append(f"上游收到的模型名不对：{sent.get('model')}")
                if sent.get("image_size") != "512x512":
                    bad.append(f"上游收到的尺寸不对（SiliconFlow 必填）：{sent.get('image_size')}")
                if not (_FakeImageHandler.seen[0]["auth"] or "").startswith("Bearer "):
                    bad.append("请求没有带 Authorization 头（免密钥服务也要占位钥匙）")

            # 4) 落库：会话里有一条助手消息带着这张图
            sessions = await cdp.call(
                "Runtime.evaluate",
                {
                    "expression": "(async () => { const r = await fetch('/api/sessions');"
                    " const d = await r.json(); return JSON.stringify(d); })()",
                    "awaitPromise": True,
                    "returnByValue": True,
                },
            )
            data = json.loads(sessions["result"]["value"])
            if len(data.get("sessions", [])) != 1:
                bad.append(f"生成图片应当顺手建出一个会话，实际 {len(data.get('sessions', []))} 个")

            if args.out_shot:
                shot_data = await cdp.call("Page.captureScreenshot", {"format": "png"})
                Path(args.out_shot).parent.mkdir(parents=True, exist_ok=True)
                Path(args.out_shot).write_bytes(base64.b64decode(shot_data["data"]))
                log(f"截图：{args.out_shot}")

            # 5) 刷新后仍在（历史回放走同一条渲染路径）
            await cdp.call("Page.reload")
            await asyncio.sleep(0.6)
            await wait_until(cdp, "document.readyState === 'complete'", "重载完成")
            await wait_until(
                cdp,
                "!!document.querySelector('#session-list .session-item, #session-list .tree-item')",
                "会话树里出现刚才的会话",
            )
            await wait_until(
                cdp,
                "(() => { const it = document.querySelector('#session-list .session-item');"
                " if (!it) return false; it.click(); return true; })()",
                "打开会话",
            )
            await wait_until(
                cdp,
                "(() => { const i = document.querySelector('.msg.assistant .md-figure img');"
                " return !!i && i.complete && i.naturalWidth > 0; })()",
                "刷新后图片仍在（历史回放）",
                timeout=30.0,
            )

            # 探针把 console.error 与未捕获异常都收进 cdp.errors（见 chrome_probe）
            if cdp.errors:
                bad.append("console 有错误：\n    " + "\n    ".join(cdp.errors[:5]))

        if bad:
            log("E2E 未过：\n  - " + "\n  - ".join(bad))
            return 1
        log("「生成图片」E2E 通过（生成 → 落盘 → 落库 → 刷新仍在）")
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
    parser = argparse.ArgumentParser(description="「生成图片」E2E（无头 Chrome）")
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
    # 与 chrome_probe 同款兜底：个人机器上 Chrome 常装在用户目录而不是 Program Files
    if not Path(args.chrome).exists():
        alt = (
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Google"
            / "Chrome"
            / "Application"
            / "chrome.exe"
        )
        if alt.exists():
            args.chrome = str(alt)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
