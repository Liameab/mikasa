#!/usr/bin/env python
"""无头 Chrome「拍照转笔记」E2E 验收（M6 ②，中文注释纪律）。

自起一台隔离服务器 + 一台**假 OpenAI 兼容视觉服务**（本机回环），在真实
/documents 页面上走完整条链路：新建笔记 → 识别图片 → 文本进编辑器（可改）
→ 保存 → 原图存下来 → 重开笔记原图还在。

为什么自带假视觉服务（与 chrome_eval.py 同一套路）：真识别要外部模型、要
密钥、十几秒一次且结果不可复现。被测的是**前端与 Web 链路的真实行为**，
模型输出只是让流程动起来的东西——假服务同时还当了取证点：它把收到的请求
原样记下来，断言"图片真的发出去了"（content 是数组、含 image_url +
data:image/jpeg;base64）。少了这条断言，一个"只发提示词"的回归会静默通过。

用法：
  python tools/chrome_note_ocr.py [--out-shot tools/shots/note-ocr-e2e.png]
退出码：0 = 验收通过；1 = 任一断言失败 / console 有错。
"""

import argparse
import asyncio
import base64
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import urllib.request
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

# 假视觉服务返回的"识别结果"：带标题、列表与公式，顺便验证预览渲染
OCR_TEXT = "## 板书：牛顿第二定律\n\n- F = m a\n- 适用：宏观低速\n"

_CONFIG_TEMPLATE = """\
# 「拍照转笔记」E2E 专用配置：offline 的等价物 + 一个指向假服务的视觉段。
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

vision:
  # backend=local 免密钥（注入占位钥匙）——正好用来指本机假服务
  backend: local
  base_url: http://127.0.0.1:{fake_port}/v1
  model: fake-vl-1
  max_tokens: 512
  timeout_seconds: 30.0

embedding:
  backend: none

judge:
  enabled: false
"""


def _png(size: int = 240) -> bytes:
    """现造一张 PNG 喂给文件输入（纯白底 + 一道黑边，够浏览器解码即可）。"""
    rows = []
    for y in range(size):
        edge = y < 4 or y >= size - 4
        rows.append(
            b"\x00" + b"".join(b"\x00\x00\x00" if edge else b"\xff\xff\xff" for _ in range(size))
        )
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


class _FakeVisionHandler(BaseHTTPRequestHandler):
    """假多模态端点：记录请求体（取证用），按固定文案回一个 Markdown。"""

    requests: list[dict] = []  # 类级：收尾断言"图片真的发出去了"

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 的接口名
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        _FakeVisionHandler.requests.append(body)
        payload = {
            "id": "chatcmpl-fake-vl",
            "object": "chat.completion",
            "created": 0,
            "model": body.get("model", "fake-vl-1"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": OCR_TEXT},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 42, "completion_tokens": 17, "total_tokens": 59},
        }
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _start_fake_vision(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), _FakeVisionHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _image_content_of(request: dict) -> list[dict] | None:
    """从请求体里挖出 content 数组（最后一条 user 消息）。"""
    for message in reversed(request.get("messages") or []):
        if isinstance(message.get("content"), list):
            return message["content"]
    return None


async def click(cdp, selector: str, what: str) -> None:
    ok = await cdp.evaluate(
        f"(() => {{ const n = document.querySelector({js_quote(selector)}); "
        "if (!n) return false; n.click(); return true; })()"
    )
    if not ok:
        raise RuntimeError(f"点不到：{what}（{selector}）")


async def feed_file(cdp, selector: str, path: Path) -> None:
    """CDP 喂文件给 input[type=file]（会触发 change，与用户选择等效）。"""
    await cdp.call("DOM.enable")
    root = await cdp.call("DOM.getDocument", {"depth": 1})
    node = await cdp.call(
        "DOM.querySelector", {"nodeId": root["root"]["nodeId"], "selector": selector}
    )
    if not node["nodeId"]:
        raise RuntimeError(f"找不到文件输入：{selector}")
    await cdp.call("DOM.setFileInputFiles", {"nodeId": node["nodeId"], "files": [str(path)]})


def _api(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def run(args):
    tmp = Path(tempfile.mkdtemp(prefix="note-ocr-e2e-"))
    fake = None
    server = None
    chrome = None
    try:
        args.server_port = args.server_port or free_port()
        args.cdp_port = args.cdp_port or free_port()
        args.fake_port = args.fake_port or free_port()

        fake = _start_fake_vision(args.fake_port)
        log(f"假视觉服务：http://127.0.0.1:{args.fake_port}/v1/chat/completions")

        # 准备要喂的图片（真 PNG：前端会把它 canvas 压缩成 JPEG 再上传）
        image_path = tmp / "板书.png"
        image_path.write_bytes(_png())

        data_dir = (tmp / "data").as_posix()
        cfg = tmp / "serve.yaml"
        cfg.write_text(
            _CONFIG_TEMPLATE.format(data_dir=data_dir, fake_port=args.fake_port),
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
            raise

        url = f"http://127.0.0.1:{args.server_port}/documents"
        profile = tempfile.mkdtemp(prefix="note-ocr-chrome-")
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
        bad = []
        async with CDP(target["webSocketDebuggerUrl"]) as cdp:
            await cdp.call("Page.enable")
            await cdp.call("Log.enable")
            await cdp.call("Runtime.enable")
            await cdp.call("Page.reload")
            await asyncio.sleep(0.4)
            await wait_until(cdp, "document.readyState === 'complete'", "知识库页加载")
            await wait_until(
                cdp, "!!document.querySelector('#kb-tree')", "知识库树就绪", timeout=25.0
            )
            await asyncio.sleep(1.0)
            # 空库会弹首启引导（z-index 200）：不关掉的话后面都是"用户点不着"的假通过
            if await cdp.evaluate("!!document.querySelector('.onboard-mask')"):
                await cdp.evaluate(
                    "[...document.querySelectorAll('.onboard-card button')]"
                    ".find(b => b.textContent.includes('开始使用'))?.click()"
                )
                await wait_until(cdp, "!document.querySelector('.onboard-mask')", "首启引导关闭")

            # 1) 新建笔记 → 编辑器打开
            await click(cdp, "#new-note", "新建笔记按钮")
            await wait_until(cdp, "!!document.querySelector('.note-box')", "笔记编辑器出现")
            await cdp.evaluate(
                "(() => { const n = document.querySelector('.note-title');"
                " n.value = '板书：牛顿第二定律';"
                " n.dispatchEvent(new Event('input', {bubbles:true})); })()"
            )

            # 2) 识别图片：喂图 → 等识别文本进正文框
            await feed_file(cdp, ".note-image-input", image_path)
            await wait_until(
                cdp,
                "document.querySelector('.note-input').value.includes('牛顿第二定律')",
                "识别文本进入正文",
                timeout=60.0,
            )
            # 缩略图条：压缩后的原图立刻可见（识别成功与否都该在）
            thumbs = await cdp.evaluate(
                "document.querySelectorAll('.note-media .note-media-thumb').length"
            )
            if thumbs != 1:
                bad.append(f"识别后应有 1 张缩略图，实际 {thumbs}")
            # 预览按 Markdown 渲染（标题变成 h3/h4，而不是原样显示 ##）。
            # 预览是 120ms 防抖渲染的：不等它落定就读会读到上一帧（本工具实测踩过）
            await wait_until(
                cdp,
                "document.querySelectorAll('.note-preview h3, .note-preview h4').length >= 1",
                "预览渲染出小标题",
                timeout=10.0,
            )
            rendered = await cdp.evaluate(
                "(() => { const p = document.querySelector('.note-preview');"
                " return {head: p.querySelectorAll('h3,h4').length,"
                " li: p.querySelectorAll('li').length}; })()"
            )
            if rendered["head"] < 1 or rendered["li"] < 1:
                bad.append(f"预览没按 Markdown 渲染：{rendered}")
            # 识别结果**可改**（产品语义：先给用户改，再入库）
            await cdp.evaluate(
                "(() => { const n = document.querySelector('.note-input');"
                " n.value = n.value.replace('宏观低速', '宏观低速（惯性系）');"
                " n.dispatchEvent(new Event('input', {bubbles:true})); })()"
            )
            if args.out_shot:
                # 编辑器开着 + 缩略图条的样子单独留一张（末帧是关闭后的树，
                # 看不到本功能的主角——chrome_notes.py 同款做法）
                shot = Path(args.out_shot)
                res = await cdp.call("Page.captureScreenshot", {"format": "png"})
                sibling = shot.with_name(f"{shot.stem}-editor{shot.suffix}")
                sibling.write_bytes(base64.b64decode(res["data"]))
                log(f"编辑器截图已写: {sibling}")

            # 3) 保存 → 正文入库 + 原图随笔记保存（等待异步上传完成）
            await click(cdp, ".note-foot .btn.primary", "保存按钮")
            await wait_until(cdp, "!document.querySelector('.note-backdrop')", "编辑器关闭")
            await wait_until(
                cdp,
                "document.querySelectorAll('#kb-tree .doc-item').length === 1",
                "树里出现笔记行",
                timeout=30.0,
            )
            doc_id = await cdp.evaluate(
                "(() => { const r = document.querySelector('#kb-tree .doc-item');"
                " return r ? Number(r.dataset.id || r.dataset.docId || 0) : 0; })()"
            )
            # 直接从服务端核对：识别文本进了正文、原图落了盘
            docs = _api(args.server_port, "/api/documents")["documents"]
            if len(docs) != 1:
                bad.append(f"应只有 1 篇笔记，实际 {len(docs)}")
            else:
                doc_id = docs[0]["id"]
            media = _api(args.server_port, f"/api/notes/{doc_id}/media")["items"]
            if len(media) != 1:
                bad.append(f"原图应存了 1 张，实际 {len(media)}：{media}")
            body = _api(args.server_port, f"/api/notes/{doc_id}")
            if "（惯性系）" not in body["body"]:
                bad.append("用户改过的正文没有按预期保存（说明落的是识别原文）")

            # 4) 重开笔记：原图缩略图条还在（"事后对照"的落点）
            await cdp.evaluate("document.querySelector('#kb-tree .doc-item .t-more').click()")
            await wait_until(cdp, "!!document.querySelector('#ctx-menu .ctx-item')", "⋯ 菜单展开")
            await cdp.evaluate(
                "[...document.querySelectorAll('#ctx-menu .ctx-item')]"
                ".find(n => n.textContent === '编辑笔记').click()"
            )
            await wait_until(cdp, "!!document.querySelector('.note-box')", "编辑器再次打开")
            await wait_until(
                cdp,
                "document.querySelectorAll('.note-media .note-media-thumb').length === 1",
                "已存原图回到缩略图条",
                timeout=20.0,
            )
            shown = await cdp.evaluate(
                "document.querySelector('.note-media .note-media-thumb').getAttribute('src')"
            )
            if f"/api/notes/{doc_id}/media/" not in (shown or ""):
                bad.append(f"缩略图指向不对：{shown!r}")
            # 正文回填要逐字符（识别结果 + 用户改的那一处）
            loaded = await cdp.evaluate("document.querySelector('.note-input').value")
            if "（惯性系）" not in loaded or "F = m a" not in loaded:
                bad.append(f"回填正文不符：{loaded[:80]!r}")
            await cdp.evaluate(
                "[...document.querySelectorAll('.note-foot button')]"
                ".find(b => b.textContent === '取消').click()"
            )

            # 5) 取证：假服务收到的请求里，图片真的以 data URL 发出去了
            if not _FakeVisionHandler.requests:
                bad.append("假视觉服务一次都没被调用——识图根本没发请求")
            else:
                content = _image_content_of(_FakeVisionHandler.requests[-1])
                if not content:
                    bad.append(
                        f"content 不是数组（图片没发出去）：{_FakeVisionHandler.requests[-1]}"
                    )
                else:
                    kinds = [part.get("type") for part in content]
                    if "image_url" not in kinds:
                        bad.append(f"请求里没有 image_url：{kinds}")
                    else:
                        url = content[kinds.index("image_url")]["image_url"]["url"]
                        if not url.startswith("data:image/jpeg;base64,"):
                            bad.append(f"图片不是 data URL：{url[:60]!r}")

            if args.out_shot:
                res = await cdp.call("Page.captureScreenshot", {"format": "png"})
                Path(args.out_shot).write_bytes(base64.b64decode(res["data"]))
                log(f"截图已写: {args.out_shot}")
            await asyncio.sleep(1.5)
            if cdp.errors:
                bad.append("console 有错误：" + "；".join(cdp.errors[:3]))

        log(f"假视觉服务共被调用 {len(_FakeVisionHandler.requests)} 次")
        if bad:
            log("验收未过：\n  - " + "\n  - ".join(bad))
            log("服务日志尾：\n" + (tmp / "serve.log").read_text(encoding="utf-8")[-2000:])
            return 1
        log("拍照转笔记 E2E 验收通过（识别 → 改 → 保存 → 原图留存 → 重开可见）")
        return 0
    finally:
        if chrome is not None:
            chrome.terminate()
            try:
                chrome.wait(timeout=5)
            except subprocess.TimeoutExpired:
                chrome.kill()
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
        if fake is not None:
            fake.shutdown()
            fake.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="无头 Chrome「拍照转笔记」E2E 验收")
    parser.add_argument("--out-shot", default=None, help="截图输出路径（PNG）")
    parser.add_argument("--server-port", type=int, default=None)
    parser.add_argument("--cdp-port", type=int, default=None)
    parser.add_argument("--fake-port", type=int, default=None)
    parser.add_argument(
        "--chrome", default=r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    )
    args = parser.parse_args()
    if not os.path.exists(args.chrome):
        alt = os.path.join(
            os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"
        )
        if os.path.exists(alt):
            args.chrome = alt
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
