#!/usr/bin/env python
"""无头 Chrome「应用内更新」E2E 验收（ADR-0022 / ADR-0024）。

自起一台隔离服务器（offline profile + MIKASA_DATA_DIR 指向临时目录），
GitHub API 与下载主机全部用**本地假源**替换（`MIKASA_UPDATE_API_BASE`
是调用时读的环境变量；下载走回环 http——正是 install.py 给 E2E 留的
逃生门）。真实 Web 进程 + 真实浏览器走一遍用户流程：

  1. 打开问答页 → 静默检查 → 弹出「发现新版本」；版本号/当前版本/发布
     说明（Markdown 渲染过）都在；
  2. 「打开发布页」真的打开的是 release 页地址（打桩 window.open 收 URL）；
  3. 「下载并安装」→ 进度条走完 → 服务端把文件下到 updates/ 且 **sha256
     与假 SHA256SUMS.txt 一致** → 弹窗转入"正在安装"文案；
  3a. 下载中再 POST 一次下载端点：**幂等**（202 + adopted，不起第二个 worker）；
  3b. 假源在 1MiB 处**掐断一次**：客户端必须带 Range 从断点续下；
  3c. 切到「找论文」→ 顶栏胶囊显示进度；切回问答页 → 弹窗自动接上进度；
  3d. 收起弹窗（「后台继续」）→ 下载转后台；胶囊接管；**下完也不自动装**
      （安装器第一件事是杀掉 Mikasa，用户不在场不能替他按）；
  3e. 点胶囊重开弹窗 → 「立即安装」→ 才调 install（`MIKASA_UPDATE_SKIP_LAUNCH=1`
      让服务端不真的双击那个假 exe）；
  4. 「跳过此版本」→ toast + 关窗；刷新页面确实不再弹（且**没有再打**
     GitHub API——计数假源命中次数）；设置面板点「检查更新」手动再来一次
     仍会弹（跳过标记只在静默检查里生效）；
  5. 设置面板「关于」段：当前版本号回填、启动时检查的复选框默认开、取消
     勾选后刷新**完全不检查**（假源 0 命中）；
  6. 全程收集 console 错误与未捕获异常，有错退出码 1。

用法：
  python tools/chrome_update.py [--out-shot tools/shots/update-e2e.png]
  [--server-port <空闲自动>] [--cdp-port <空闲自动>] [--chrome <chrome.exe 路径>]
退出码：0 = 验收通过；1 = 任一断言失败 / console 有错。
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.chrome_probe import CDP, log, wait_json_list  # noqa: E402

REPO_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIKASA_EXE = REPO_ROOT / ".venv" / "Scripts" / "mikasa.exe"

REPO_SLUG = "Liameab/mikasa"
FAKE_VERSION = "9.9.9"
SETUP_NAME = f"Mikasa-Setup-{FAKE_VERSION}-win64.exe"
NOTES_TITLE = "更新内容"
# 假安装包：只要字节确定、长度已知即可（sha256 由假源现算）。
# **绝不会被真的执行**——服务端带着 MIKASA_UPDATE_SKIP_LAUNCH=1 跑。
INSTALLER_BYTES = (b"MZ" + bytes(range(256))) * 40000  # ≈10MB
# 分块 + 节流下发：真实下载要几分钟，假源秒发会让"进度条"这一段
# 永远来不及渲染（第一轮实测就卡在这里）——慢下来才验收得到。整段
# 下载 ≈17 秒，中间的切页/收起弹窗那几步才有时间窗。
_THROTTLE_CHUNK = 256 * 1024
_THROTTLE_SLEEP = 0.2

# 续传验收：假源在第一次响应写到这么多字节时**硬断连接**（声明的是完整
# Content-Length，所以客户端只能看到"响应截断"）。取 _THROTTLE_CHUNK 的
# 整数倍，客户端落盘的半成品长度就是确定的 1MiB。
_CUT_AFTER = 4 * _THROTTLE_CHUNK


def js_quote(s: str) -> str:
    """JS 字符串字面量（跨 evaluate 传值）。"""
    return json.dumps(s, ensure_ascii=False)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# 本地假 GitHub（API + 资产下载）
# ---------------------------------------------------------------------------


class _FakeGitHub(BaseHTTPRequestHandler):
    port = 0
    installer = INSTALLER_BYTES
    latest_hits = 0  # /releases/latest 命中次数（"没再打网络"的判据）
    ranges: list[str] = []  # 安装包请求带的 Range 头（"" = 整份下）
    cut_fired = False  # 掐断只做一次：第二次请求必须完整下发，续传才收得了尾

    def log_message(self, *args):  # noqa: ANN002 - 静音（access log 没用）
        del args

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        if self.path == f"/repos/{REPO_SLUG}/releases/latest":
            _FakeGitHub.latest_hits += 1
            base = f"http://127.0.0.1:{_FakeGitHub.port}/download"
            payload = {
                "tag_name": f"v{FAKE_VERSION}",
                "html_url": f"https://github.com/{REPO_SLUG}/releases/tag/v{FAKE_VERSION}",
                "body": (
                    f"## {NOTES_TITLE}\n\n"
                    "- 新增应用内更新：打开就提示、一键下载安装\n"
                    "- 修了若干毛病\n\n"
                    # 代码块是刻意的：发布说明里几乎一定有命令，而"代码围栏有没有
                    # 渲染成代码框、复制钮能不能用"只有这条链路能验（2026-09-21
                    # 用户报障的两条，断言就挂在这段上）。
                    "升级到本机模型：\n\n"
                    "```bash\nollama pull qwen3:8b\n```\n"
                ),
                "published_at": "2026-09-19T00:00:00Z",
                "assets": [
                    {
                        "name": SETUP_NAME,
                        "browser_download_url": f"{base}/{SETUP_NAME}",
                        "size": len(INSTALLER_BYTES),
                    },
                    {
                        "name": "SHA256SUMS.txt",
                        "browser_download_url": f"{base}/SHA256SUMS.txt",
                        "size": 66,
                    },
                ],
            }
            self._send(json.dumps(payload).encode("utf-8"), "application/json")
        elif self.path == "/download/SHA256SUMS.txt":
            digest = hashlib.sha256(INSTALLER_BYTES).hexdigest()
            self._send(f"{digest}  {SETUP_NAME}\n".encode(), "text/plain")
        elif self.path == f"/download/{SETUP_NAME}":
            self._send_asset()
        else:
            self.send_error(404)

    def _send_asset(self) -> None:
        """安装包：认 Range（206）、越界回 416、并且**掐断一次**给续传演现场。"""
        total = len(INSTALLER_BYTES)
        raw = (self.headers.get("Range") or "").strip()
        _FakeGitHub.ranges.append(raw)
        start = 0
        if raw:
            match = re.fullmatch(r"bytes=(\d+)-", raw)
            if match is None:
                self.send_error(400, "bad range")
                return
            start = int(match.group(1))
            if start >= total:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        body = INSTALLER_BYTES[start:]
        self.send_response(206 if start else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        if start:
            self.send_header("Content-Range", f"bytes {start}-{total - 1}/{total}")
        self.end_headers()
        cut = not _FakeGitHub.cut_fired and len(body) > _CUT_AFTER
        for i in range(0, len(body), _THROTTLE_CHUNK):
            self.wfile.write(body[i : i + _THROTTLE_CHUNK])
            self.wfile.flush()
            if cut and i + _THROTTLE_CHUNK >= _CUT_AFTER:
                # 声明完整长度、写一半就断（真实链路上就是这么被 reset 的）
                _FakeGitHub.cut_fired = True
                log(f"假源：按计划在 {_CUT_AFTER} 字节处掐断连接")
                self.connection.close()
                return
            time.sleep(_THROTTLE_SLEEP)


class FakeGitHub:
    """假源生命周期管理（后台线程 + 动态端口）。"""

    def __init__(self) -> None:
        self.port = 0
        self._httpd: ThreadingHTTPServer | None = None

    def start(self) -> None:
        self.port = free_port()
        _FakeGitHub.port = self.port
        _FakeGitHub.latest_hits = 0
        _FakeGitHub.ranges = []
        _FakeGitHub.cut_fired = False
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), _FakeGitHub)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def ranges(self) -> list[str]:
        """安装包请求的 Range 头序列（续传的证据）。"""
        return list(_FakeGitHub.ranges)

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()

    def env(self) -> dict:
        return {
            "MIKASA_UPDATE_API_BASE": f"http://127.0.0.1:{self.port}",
            "MIKASA_UPDATE_REPO": REPO_SLUG,
            # 关键：服务端只管下载与校验，**不真的启动**那个假安装包
            "MIKASA_UPDATE_SKIP_LAUNCH": "1",
        }

    def latest_hits(self) -> int:
        return _FakeGitHub.latest_hits


# ---------------------------------------------------------------------------
# 服务器与浏览器（与 chrome_papers.py 同款）
# ---------------------------------------------------------------------------


def api_get(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def api_post(port: int, path: str) -> tuple[int, dict]:
    """POST 一个端点，返回 (状态码, 响应体)。4xx/5xx 也照样返回，不抛。"""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8") or "{}"
        return exc.code, json.loads(raw)


def wait_status(port: int, want: str, what: str, timeout: float = 120.0) -> dict:
    """等服务端的任务状态到某一步（下载是后台任务，与页面无关）。"""
    deadline = time.time() + timeout
    body: dict = {}
    while time.time() < deadline:
        try:
            body = api_get(port, "/api/update/download/status")
            if body.get("status") == want:
                return body
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            pass
        time.sleep(0.3)
    raise RuntimeError(f"超时等不到状态 {want}（{what}），最后看到：{body}")


def start_server(port: int, env: dict, logfile) -> subprocess.Popen:
    cmd = (
        [str(MIKASA_EXE), "serve"]
        if MIKASA_EXE.is_file()
        else [sys.executable, "-m", "mikasa", "serve"]
    )
    return subprocess.Popen(
        [*cmd, "--profile", "offline", "--port", str(port)],
        stdout=logfile,
        stderr=subprocess.STDOUT,
        cwd=str(REPO_ROOT),
        env=env,
    )


def stop_server(server: subprocess.Popen) -> None:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()


def wait_server(port: int, timeout: float = 30.0) -> None:
    """等 uvicorn 起来（就绪判定用本次构建才有的更新端点）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            body = api_get(port, "/api/update/download/status")
            if body.get("status") == "idle":
                return
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            pass
        time.sleep(0.3)
    raise RuntimeError("服务器未就绪（日志见 tmpdir 下 serve.log）")


async def wait_until(cdp, expr: str, what: str, timeout: float = 40.0) -> None:
    """轮询页面表达式直到真值；超时抛错（失败信息带轮询目标）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await cdp.evaluate(expr):
            return
        await asyncio.sleep(0.2)
    raise RuntimeError(f"超时等不到：{what}")


async def reload_page(cdp, expect: str, what: str, timeout: float = 30.0) -> None:
    """刷新页面并等目标出现（每次刷新 = 一次"重新打开应用"）。"""
    await cdp.call("Page.reload")
    await asyncio.sleep(0.4)
    await wait_until(cdp, expect, what, timeout)
    await asyncio.sleep(0.3)  # 让弹窗动画/后续设置面板操作稳定


DIALOG = "!!document.querySelector('#update-dialog')"


async def screenshot(cdp, path: str) -> None:
    """截当前页到 path（目录自动创建）。"""
    import base64

    shot = await cdp.call("Page.captureScreenshot", {"format": "png"})
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(base64.b64decode(shot["data"]))
    log(f"截图：{path}")


async def run(args):
    tmp = Path(tempfile.mkdtemp(prefix="update-e2e-"))
    log(f"临时目录：{tmp}")
    data_dir = tmp / "data"  # 隔离的生命线：下载文件也落在这里
    fake = FakeGitHub()
    fake.start()
    env = dict(os.environ)
    env["MIKASA_DATA_DIR"] = str(data_dir)
    env.update(fake.env())
    server = None
    chrome = None
    bad: list[str] = []
    try:
        if args.server_port is None:
            args.server_port = free_port()
        if args.cdp_port is None:
            args.cdp_port = free_port()
        port = args.server_port
        logfile = open(tmp / "serve.log", "w", encoding="utf-8")  # noqa: SIM115
        server = start_server(port, env, logfile)
        try:
            wait_server(port)
        except RuntimeError:
            log("服务器启动失败，serve.log 尾：")
            log((tmp / "serve.log").read_text(encoding="utf-8")[-3000:])
            raise

        profile = tempfile.mkdtemp(prefix="update-chrome-")
        chrome = subprocess.Popen(
            [
                args.chrome,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                f"--remote-debugging-port={args.cdp_port}",
                f"--user-data-dir={profile}",
                "--window-size=1600,900",
                f"http://127.0.0.1:{port}/",
            ]
        )
        target = wait_json_list(args.cdp_port)
        async with CDP(target["webSocketDebuggerUrl"]) as cdp:
            await cdp.call("Page.enable")
            await cdp.call("Log.enable")
            for _ in range(60):
                state = await cdp.evaluate("document.readyState")
                if state == "complete":
                    break
                await asyncio.sleep(0.25)
            await asyncio.sleep(0.8)

            # ---- 0. 空库会弹首启引导：先关掉，再刷新（引导是"每库一次"，
            #         之后每次加载才是干净的"老用户"场景）----
            await cdp.evaluate(
                "(() => { const b = [...document.querySelectorAll('#onboard .btn')]"
                ".find(x => x.textContent.includes('开始使用'));"
                " if (b) b.click(); return true; })()"
            )
            await asyncio.sleep(0.3)

            # ---- 1. 静默检查 → 弹窗 ----
            await reload_page(cdp, DIALOG, "启动检查后发现新版本弹窗")
            first = await cdp.evaluate("""(() => {
              const d = document.querySelector('#update-dialog');
              return {
                head: d.querySelector('.upd-head').textContent,
                sub: d.querySelector('.upd-sub').textContent,
                notes: d.querySelector('.upd-notes').textContent,
                buttons: [...d.querySelectorAll('.upd-actions .btn')].map(b => b.textContent),
                focused: document.activeElement?.textContent || '',
                hits: null,
              };
            })()""")
            log(f"弹窗：{first['head']} / {first['sub']}")
            if args.out_shot:
                await screenshot(cdp, args.out_shot)

            # ---- 1b. 发布说明的**排版与复制**（2026-09-21 用户报障两条）----
            # 必须**趁弹窗开着**捕获与点击：断言区在流程末尾，那时弹窗已关、
            # 页面也刷新过（第一版就把查询写在那儿，结果 notesCount=0——现场
            # 证据就是这么来的）。
            # ① 代码围栏要变成真正的代码框（有底色），不能退化成一行纯文本：
            #    渲染器产出的 .code-block 样式挂在 `.bubble` 作用域下，而说明区
            #    是 .upd-notes——不带上那个类就没有样式（曾经如此）。
            # ② 「复制」要真的能用：渲染器只画按钮，点击行为得由容器挂委托。
            code_state = await cdp.evaluate("""(() => {
              const notesEl = document.querySelector('.upd-notes');
              const box = document.querySelector('.upd-notes .code-block');
              if (!box) {
                return {
                  found: false,
                  notesCount: document.querySelectorAll('.upd-notes').length,
                  html: notesEl ? notesEl.innerHTML.slice(0, 200) : '(没有 .upd-notes)',
                };
              }
              const pre = box.querySelector('pre');
              const btn = box.querySelector('.btn-copy');
              // 打桩剪贴板：无头 Chrome 里 writeText 会被权限拒掉、降级的
              // execCommand 也可能返回 false（按钮就不变"已复制"）。打桩后
              // 既能验"点了有反应"，还能顺带验**复制到的正是那条命令**——
              // 比只看按钮文案更强。
              window.__copied = null;
              try {
                Object.defineProperty(navigator, 'clipboard', {
                  configurable: true,
                  value: {writeText: async (t) => { window.__copied = String(t); }},
                });
              } catch {}
              btn?.click();
              return {
                found: true,
                // 底色在外层 .code-block 上（pre 自己透明）——第一版查错了元素
                bg: getComputedStyle(box).backgroundColor,
                hasCopy: !!btn,
                text: (pre.textContent || '').trim(),
              };
            })()""")
            await asyncio.sleep(0.3)
            code_state["copyLabel"] = await cdp.evaluate(
                "(document.querySelector('.upd-notes .btn-copy')?.textContent || '')"
            )
            code_state["copied"] = await cdp.evaluate("window.__copied")

            # ---- 2. 「打开发布页」打开的是 release 页（打桩 window.open）----
            opened = await cdp.evaluate("""(() => {
              window.__opened = [];
              window.open = (u) => { window.__opened.push(String(u)); return null; };
              const d = document.querySelector('#update-dialog');
              const btn = [...d.querySelectorAll('.upd-actions .btn')]
                .find(b => b.textContent.includes('打开发布页'));
              btn.click();
              return window.__opened;
            })()""")

            # ---- 3. 下载并安装（服务端不真的启动那个假 exe）----
            await cdp.evaluate(
                "(() => { const d = document.querySelector('#update-dialog');"
                " [...d.querySelectorAll('.upd-actions .btn')]"
                ".find(b => b.textContent.includes('下载并安装')).click(); return true; })()"
            )
            await wait_until(
                cdp,
                "document.querySelector('#update-dialog .upd-progress-text')"
                ".textContent.includes('正在下载')",
                "下载进度出现",
            )
            # 百分比要等第一个字节回来才有的写（服务端先取校验和，再开下载）
            await wait_until(
                cdp,
                "document.querySelector('#update-dialog .upd-progress-text')"
                ".textContent.includes('%')",
                "进度条带上百分比",
            )
            progress_text = await cdp.evaluate(
                "document.querySelector('#update-dialog .upd-progress-text').textContent"
            )
            if args.out_shot:
                await screenshot(cdp, str(Path(args.out_shot).with_name("update-e2e-progress.png")))

            # ---- 3a. 已经在下了再点一次：幂等（老行为回 409，前端把它当失败
            #          "启动下载失败：更新下载已经开始了"，用户就再也点不动）----
            again_code, again_body = api_post(port, "/api/update/download")
            first_ranges = fake.ranges()  # 到这一步只该有一次整份请求

            # ---- 3b. 切到别的功能：进度胶囊跨页可见（下载不受影响）----
            await cdp.call("Page.navigate", {"url": f"http://127.0.0.1:{port}/papers"})
            await wait_until(cdp, "document.readyState === 'complete'", "找论文页加载完成")
            await wait_until(cdp, "!!document.querySelector('#update-pill')", "顶栏出现更新胶囊")
            pill_other = await cdp.evaluate("document.querySelector('#update-pill').textContent")
            if args.out_shot:
                await screenshot(cdp, str(Path(args.out_shot).with_name("update-e2e-pill.png")))

            # ---- 3c. 回到问答页：弹窗自动接上进度（不再"回来就断了"）----
            await cdp.call("Page.navigate", {"url": f"http://127.0.0.1:{port}/"})
            await wait_until(cdp, DIALOG, "回到问答页后弹窗自动接上")
            await wait_until(
                cdp,
                "document.querySelector('#update-dialog .upd-progress-text')"
                ".textContent.includes('正在下载')",
                "弹窗继续显示下载进度",
            )
            resumed_text = await cdp.evaluate(
                "document.querySelector('#update-dialog .upd-progress-text').textContent"
            )
            # 装一个 fetch 探针：之后"有没有偷偷调安装端点"就靠它断言
            await cdp.evaluate("""(() => {
              window.__posts = [];
              const orig = window.fetch;
              window.fetch = (url, opts) => {
                if (opts && String(opts.method || '').toUpperCase() === 'POST') {
                  window.__posts.push(String(url));
                }
                return orig(url, opts);
              };
              return true;
            })()""")

            # ---- 3d. 收起弹窗：下载转后台，胶囊接管 ----
            await cdp.evaluate(
                "(() => { const d = document.querySelector('#update-dialog');"
                " [...d.querySelectorAll('.upd-actions .btn')]"
                ".find(b => b.textContent.includes('后台继续')).click(); return true; })()"
            )
            await wait_until(cdp, "!document.querySelector('#update-dialog')", "弹窗已收起")
            await wait_until(cdp, "!!document.querySelector('#update-pill')", "胶囊接管进度")

            # ---- 3e. 等它下完：**弹窗不在场就不自动装**（安装器第一件事是
            #          杀掉 Mikasa，用户不在场时不能替他按）----
            done_status = wait_status(port, "done", "后台下载完成")
            await wait_until(
                cdp,
                "document.querySelector('#update-pill').textContent.includes('已就绪')",
                "胶囊转为「已就绪」",
            )
            posts_before_install = await cdp.evaluate("window.__posts.slice()")
            pill_done = await cdp.evaluate("document.querySelector('#update-pill').textContent")
            alive = api_get(port, "/api/health")  # 进程还活着（没人被悄悄杀掉）

            # ---- 3f. 从胶囊重开 → 弹窗给「立即安装」，点了才装 ----
            await cdp.evaluate("document.querySelector('#update-pill').click(); true")
            await wait_until(cdp, DIALOG, "从胶囊重新打开弹窗")
            await cdp.evaluate(
                "(() => { const d = document.querySelector('#update-dialog');"
                " [...d.querySelectorAll('.upd-actions .btn')]"
                ".find(b => b.textContent.includes('立即安装')).click(); return true; })()"
            )
            await wait_until(
                cdp,
                "document.querySelector('#update-dialog .upd-progress-text')"
                ".textContent.includes('安装程序已启动')",
                "点「立即安装」后启动安装器",
                timeout=60.0,
            )
            posts_after_install = await cdp.evaluate("window.__posts.slice()")
            installed = await cdp.evaluate("""(() => {
              const d = document.querySelector('#update-dialog');
              return {
                head: d.querySelector('.upd-head').textContent,
                text: d.querySelector('.upd-progress-text').textContent,
                error: d.querySelector('.upd-error').textContent,
                disabled: [...d.querySelectorAll('.upd-actions .btn')].every(b => b.disabled),
              };
            })()""")

            # ---- 3g. 服务端侧证据：续传真的发生了、落盘完整、sha256 对得上 ----
            status = done_status
            downloaded = data_dir / "updates" / SETUP_NAME
            updates_files = sorted(p.name for p in (data_dir / "updates").iterdir())
            ranges = fake.ranges()
            digest_ok = (
                downloaded.is_file()
                and hashlib.sha256(downloaded.read_bytes()).hexdigest()
                == hashlib.sha256(INSTALLER_BYTES).hexdigest()
            )

            # ---- 4. 跳过此版本 → 刷新不再弹；手动检查仍会弹 ----
            # 先刷新拿一个**全新**的弹窗：上一段停在"安装已启动"，那个
            # 弹窗的按钮按设计全部禁用了（点什么都不该有反应）。
            await reload_page(cdp, DIALOG, "刷新后弹窗重新出现")
            await cdp.evaluate(
                "(() => { const d = document.querySelector('#update-dialog');"
                " [...d.querySelectorAll('.upd-actions .btn')]"
                ".find(b => b.textContent.includes('跳过此版本')).click(); return true; })()"
            )
            await asyncio.sleep(0.5)
            skip_state = await cdp.evaluate("""(() => ({
              dialog: !!document.querySelector('#update-dialog'),
              toast: [...document.querySelectorAll('#toast .toast-msg')].at(-1)?.textContent || '',
              stored: localStorage.getItem('mikasa.ui.skipVersion') || '',
            }))()""")

            hits_before = fake.latest_hits()
            await reload_page(cdp, "document.readyState === 'complete'", "刷新完成")
            await asyncio.sleep(1.5)  # 给"如果会打网络"留出时间窗
            after_skip = await cdp.evaluate("!!document.querySelector('#update-dialog')")
            hits_after_skip = fake.latest_hits() - hits_before

            # 手动检查：设置面板 →「检查更新」
            manual = await cdp.evaluate("""(async () => {
              document.querySelector('#btn-settings').click();
              const version = document.querySelector('#s-version').textContent;
              const checked = document.querySelector('#s-check-updates').checked;
              document.querySelector('#s-check-update').click();
              return { version, checked };
            })()""")
            await wait_until(cdp, DIALOG, "手动检查后弹窗再次出现")

            # ---- 5. 关掉弹窗，关掉"启动时检查"→ 刷新完全不检查 ----
            await cdp.evaluate(
                "(() => { const d = document.querySelector('#update-dialog');"
                " [...d.querySelectorAll('.upd-actions .btn')]"
                ".find(b => b.textContent.includes('以后再说')).click(); return true; })()"
            )
            await cdp.evaluate(
                "(() => { const box = document.querySelector('#s-check-updates');"
                " box.checked = false; box.dispatchEvent(new Event('change', {bubbles: true}));"
                " return true; })()"
            )
            hits_before = fake.latest_hits()
            await reload_page(cdp, "document.readyState === 'complete'", "刷新完成（已关检查）")
            await asyncio.sleep(1.5)
            off_state = await cdp.evaluate("""(() => ({
              dialog: !!document.querySelector('#update-dialog'),
              stored: localStorage.getItem('mikasa.ui.checkUpdates'),
              checked: document.querySelector('#s-check-updates').checked,
            }))()""")
            hits_after_off = fake.latest_hits() - hits_before

            # ---- 断言 ----
            if f"发现新版本 v{FAKE_VERSION}" not in first["head"]:
                bad.append(f"弹窗标题不对：{first['head']}")
            if "当前 v" not in first["sub"]:
                bad.append(f"弹窗没有显示当前版本：{first['sub']}")
            if NOTES_TITLE not in first["notes"] or "一键下载安装" not in first["notes"]:
                bad.append(f"发布说明没有渲染出来：{first['notes'][:60]}")

            # 1b. 发布说明的排版与复制（数据在弹窗打开时捕获，见上）
            if not code_state.get("found"):
                bad.append(
                    "发布说明里的代码块没渲染成代码框（渲染器或作用域类掉了）："
                    f"notesCount={code_state.get('notesCount')} "
                    f"html={code_state.get('html')!r}"
                )
            else:
                if code_state["bg"] in ("rgba(0, 0, 0, 0)", "transparent"):
                    bad.append(f"代码框没有底色（样式没作用到更新说明上）：{code_state['bg']}")
                if code_state["text"] != "ollama pull qwen3:8b":
                    bad.append(f"代码框内容不对：{code_state['text']!r}")
                if not code_state["hasCopy"]:
                    bad.append("代码框里没有「复制」按钮")
                else:
                    if "已复制" not in code_state["copyLabel"]:
                        bad.append(f"点了「复制」没有反应（按钮文案：{code_state['copyLabel']!r}）")
                    if code_state.get("copied") != "ollama pull qwen3:8b":
                        bad.append(f"复制到的内容不对：{code_state.get('copied')!r}")

            for need in ("下载并安装", "以后再说", "跳过此版本", "打开发布页"):
                if need not in first["buttons"]:
                    bad.append(f"弹窗缺少按钮「{need}」：{first['buttons']}")
            if "下载并安装" not in first["focused"]:
                bad.append(f"焦点没有落在主行动钮上：{first['focused']}")
            if not opened or not opened[0].endswith(f"/releases/tag/v{FAKE_VERSION}"):
                bad.append(f"「打开发布页」打开的不是 release 页：{opened}")
            if status.get("status") != "done" or status.get("version") != FAKE_VERSION:
                bad.append(f"下载任务状态不对：{status}")
            if "%" not in progress_text or "MB" not in progress_text:
                bad.append(f"进度文案没有百分比/体积：{progress_text}")
            # ---- 订阅续传/接续（ADR-0024）----
            if again_code != 202 or not again_body.get("adopted"):
                bad.append(
                    f"下载中再点一次应当幂等采纳（202 + adopted），实得：{again_code} {again_body}"
                )
            if len(first_ranges) != 1 or first_ranges[0]:
                bad.append(f"幂等那次不该多打一次安装包请求：{first_ranges}")
            if "%" not in pill_other:
                bad.append(f"切到别的功能后胶囊没有显示进度：{pill_other!r}")
            if "正在下载" not in resumed_text:
                bad.append(f"回到问答页没有接上下载进度：{resumed_text!r}")
            if any("/api/update/install" in u for u in posts_before_install):
                bad.append(f"弹窗已收起却仍自动装了（安装器会杀掉应用）：{posts_before_install}")
            if "已就绪" not in pill_done:
                bad.append(f"下载完成后胶囊文案不对：{pill_done!r}")
            if not alive.get("version"):
                bad.append("下载完成后进程应当还活着")
            if not any("/api/update/install" in u for u in posts_after_install):
                bad.append(f"点「立即安装」没有调用安装端点：{posts_after_install}")
            if not ranges or ranges[0]:
                bad.append(f"第一次安装包请求不该带 Range：{ranges}")
            if not any(r.startswith("bytes=") for r in ranges):
                bad.append(f"掐断之后没有续传（只看到这些 Range）：{ranges}")
            if len(ranges) < 2:
                bad.append(f"续传请求数不对：{ranges}")
            if updates_files != [SETUP_NAME]:
                bad.append(f"更新目录里应当只有成品（半成品必须改名或删除）：{updates_files}")
            if not digest_ok:
                bad.append(f"落盘文件缺失或 sha256 不符：{downloaded}")
            if installed["error"]:
                bad.append(f"安装阶段出错：{installed['error']}")
            if "正在安装" not in installed["head"]:
                bad.append(f"安装阶段标题不对：{installed['head']}")
            if "安装程序已启动" not in installed["text"]:
                bad.append(f"安装阶段文案不对：{installed['text']}")
            if not installed["disabled"]:
                bad.append("安装阶段按钮应全部禁用")
            if skip_state["dialog"]:
                bad.append("点「跳过此版本」后弹窗没有关闭")
            if skip_state["stored"] != FAKE_VERSION:
                bad.append(f"跳过标记没有落盘：{skip_state['stored']}")
            if "不再提示" not in skip_state["toast"]:
                bad.append(f"跳过 toast 文案不对：{skip_state['toast']}")
            if after_skip:
                bad.append("已跳过该版本，刷新后仍弹窗")
            if hits_after_skip != 0:
                bad.append(f"跳过标记没省下检查请求（假源又被打 {hits_after_skip} 次）")
            current = api_get(port, "/api/health")["version"]
            if manual["version"] != f"v{current}":
                bad.append(f"设置面板版本号没回填：{manual['version']}（应为 v{current}）")
            if manual["checked"] is not True:
                bad.append("「启动时检查更新」默认应为勾选")
            if off_state["stored"] != "0" or off_state["checked"]:
                bad.append(f"取消勾选没有落盘：{off_state}")
            if off_state["dialog"]:
                bad.append("关掉启动检查后刷新仍弹窗")
            if hits_after_off != 0:
                bad.append(f"关掉启动检查后仍打了检查请求（{hits_after_off} 次）")
            if cdp.errors:
                bad.append("console 有错误")
            if bad:
                log("验收未过：" + "；".join(bad))
                for item in cdp.errors:
                    log(f"  console: {item}")
                return 1
            log("更新弹窗 E2E 全部通过")
            return 0
    finally:
        if server is not None:
            stop_server(server)
        fake.stop()
        if chrome is not None:
            chrome.terminate()
            try:
                chrome.wait(timeout=5)
            except subprocess.TimeoutExpired:
                chrome.kill()


def main():
    parser = argparse.ArgumentParser(description="无头 Chrome 应用内更新验收")
    parser.add_argument("--out-shot", default=None, help="截图输出路径（PNG）")
    parser.add_argument("--server-port", type=int, default=None, help="服务端口（缺省动态分配）")
    parser.add_argument("--cdp-port", type=int, default=None, help="CDP 端口（缺省动态分配）")
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
