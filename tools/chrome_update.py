#!/usr/bin/env python
"""无头 Chrome「应用内更新」E2E 验收（ADR-0022）。

自起一台隔离服务器（offline profile + MIKASA_DATA_DIR 指向临时目录），
GitHub API 与下载主机全部用**本地假源**替换（`MIKASA_UPDATE_API_BASE`
是调用时读的环境变量；下载走回环 http——正是 install.py 给 E2E 留的
逃生门）。真实 Web 进程 + 真实浏览器走一遍用户流程：

  1. 打开问答页 → 静默检查 → 弹出「发现新版本」；版本号/当前版本/发布
     说明（Markdown 渲染过）都在；
  2. 「打开发布页」真的打开的是 release 页地址（打桩 window.open 收 URL）；
  3. 「下载并安装」→ 进度条走完 → 服务端把文件下到 updates/ 且 **sha256
     与假 SHA256SUMS.txt 一致** → 自动调 install（`MIKASA_UPDATE_SKIP_LAUNCH=1`
     让服务端不真的双击那个假 exe）→ 弹窗转入"正在安装"文案；
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
INSTALLER_BYTES = (b"MZ" + bytes(range(256))) * 12000  # ≈3MB
# 分块 + 节流下发：真实下载要几分钟，假源秒发会让"进度条"这一段
# 永远来不及渲染（第一轮实测就卡在这里）——慢下来才验收得到。
_THROTTLE_CHUNK = 256 * 1024
_THROTTLE_SLEEP = 0.15


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
                    "- 修了若干毛病"
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
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(INSTALLER_BYTES)))
            self.end_headers()
            for i in range(0, len(INSTALLER_BYTES), _THROTTLE_CHUNK):
                self.wfile.write(INSTALLER_BYTES[i : i + _THROTTLE_CHUNK])
                self.wfile.flush()
                time.sleep(_THROTTLE_SLEEP)
        else:
            self.send_error(404)


class FakeGitHub:
    """假源生命周期管理（后台线程 + 动态端口）。"""

    def __init__(self) -> None:
        self.port = 0
        self._httpd: ThreadingHTTPServer | None = None

    def start(self) -> None:
        self.port = free_port()
        _FakeGitHub.port = self.port
        _FakeGitHub.latest_hits = 0
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), _FakeGitHub)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

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
            progress_text = await cdp.evaluate(
                "document.querySelector('#update-dialog .upd-progress-text').textContent"
            )
            if args.out_shot:
                await screenshot(cdp, str(Path(args.out_shot).with_name("update-e2e-progress.png")))
            await wait_until(
                cdp,
                "document.querySelector('#update-dialog .upd-progress-text')"
                ".textContent.includes('安装程序已启动')",
                "下载完成并自动启动安装器",
                timeout=60.0,
            )
            installed = await cdp.evaluate("""(() => {
              const d = document.querySelector('#update-dialog');
              return {
                head: d.querySelector('.upd-head').textContent,
                text: d.querySelector('.upd-progress-text').textContent,
                error: d.querySelector('.upd-error').textContent,
                disabled: [...d.querySelectorAll('.upd-actions .btn')].every(b => b.disabled),
              };
            })()""")

            # ---- 3b. 服务端侧证据：文件真的落了盘、sha256 与假校验和一致 ----
            status = api_get(port, "/api/update/download/status")
            downloaded = data_dir / "updates" / SETUP_NAME
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
