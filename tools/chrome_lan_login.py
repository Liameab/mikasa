#!/usr/bin/env python
"""无头 Chrome「局域网访问 + 口令」E2E 验收（ADR-0033）。

用户的需求原话：*"浏览器和软件都可以用"* + *"每个人的数据独立，不是任何人
可以看到我的资料"*。这个工具验的就是这两句的交界处：

  1. 服务绑在 `0.0.0.0`（开给网络），**本机（回环）访问照旧免口令**；
  2. **从局域网地址访问**（同一台机器走自己的网卡 IP——来源不再是回环）
     → 浏览器被送到登录页；
  3. 输错口令 → 401 + 一句"口令不对"，仍在登录页；
  4. 输对口令 → 进到应用（顶栏/知识库页在），且**后续接口都放行**；
  5. 深链接（直接开 /documents）登录后会被送回原来的地址；
  6. console 零错误。

为什么必须真浏览器跑：门是按**来源地址**判的（回环免、其余要会话），而
`TestClient` 只能冒充这个地址、验不了"浏览器拿着 Cookie 再回来"的那一段；
`SameSite=Lax` 的 Cookie、302 跳转、表单投递这些也只有真浏览器才作数。

用法：
  python tools/chrome_lan_login.py [--out-shot tools/shots/lan-login-e2e.png]
退出码：0 = 通过；1 = 任一断言失败 / console 有错。
"""

import argparse
import asyncio
import base64
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.chrome_corpus import free_port, wait_until  # noqa: E402
from tools.chrome_probe import CDP, log, wait_json_list  # noqa: E402

REPO_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIKASA_EXE = REPO_ROOT / ".venv" / "Scripts" / "mikasa.exe"

PASSWORD = "lan-e2e-secret"


def _lan_ip() -> str:
    """本机在局域网里的地址（不真发包：connect 一个外网地址只为问路由表）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    finally:
        sock.close()


async def _run(args) -> int:
    args.server_port = args.server_port or free_port()
    args.cdp_port = args.cdp_port or free_port()
    lan_ip = args.lan_ip or _lan_ip()
    log(f"局域网地址：{lan_ip}（服务绑 0.0.0.0，来源地址决定要不要口令）")

    tmp = Path(tempfile.mkdtemp(prefix="lan-login-e2e-"))
    server = None
    chrome = None
    try:
        # 口令写在这次运行的数据目录里（服务端读的就是它）
        from mikasa.web import auth

        data_dir = tmp / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        auth.set_password(data_dir, PASSWORD)

        env = dict(os.environ, MIKASA_DATA_DIR=str(data_dir))
        with open(tmp / "serve.log", "w", encoding="utf-8") as logf:
            server = subprocess.Popen(
                [
                    str(MIKASA_EXE),
                    "serve",
                    "--profile",
                    "offline",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    str(args.server_port),
                ],
                stdout=logf,
                stderr=subprocess.STDOUT,
                cwd=str(REPO_ROOT),
                env=env,
            )

        bad: list[str] = []
        # ---- 服务就绪：用**回环**探（回环免口令，所以这里应当直接 200） ----
        deadline = time.time() + 30
        loopback_ok = False
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{args.server_port}/api/health", timeout=2
                ) as resp:
                    loopback_ok = resp.status == 200
                    break
            except (urllib.error.URLError, OSError):
                try:
                    # 401 也算"服务起来了"（口令门生效，但回环不该被拦）
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{args.server_port}/api/health", timeout=2
                    )
                except urllib.error.HTTPError as exc:
                    if exc.code == 401:
                        bad.append("回环访问被要求口令——本机自用不该有摩擦（ADR-0033）")
                        break
                except (urllib.error.URLError, OSError):
                    pass
            time.sleep(0.4)
        if not loopback_ok and not bad:
            log("服务器 30 秒内没起来，serve.log 尾：")
            log((tmp / "serve.log").read_text(encoding="utf-8")[-2000:])
            return 1

        # ---- 局域网来源（不带 Cookie）应当被拦 ----
        try:
            urllib.request.urlopen(f"http://{lan_ip}:{args.server_port}/api/health", timeout=3)
            bad.append("局域网来源直接拿到了 /api/health（门没生效）")
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                bad.append(f"局域网来源应得 401，实得 {exc.code}")
        except (urllib.error.URLError, OSError) as exc:
            # 连不上本身不算失败（防火墙可能挡本机走网卡），但浏览器那几步会当场暴露
            log(f"注意：从 {lan_ip} 直连服务失败（{exc}）——浏览器步骤会再验一次")

        # ---- 真浏览器：从局域网地址打开 ----
        profile = tempfile.mkdtemp(prefix="lan-login-chrome-")
        chrome = subprocess.Popen(
            [
                args.chrome,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                f"--remote-debugging-port={args.cdp_port}",
                f"--user-data-dir={profile}",
                "--window-size=1400,900",
                f"http://{lan_ip}:{args.server_port}/documents",  # 深链接，顺便验 next 回跳
            ]
        )
        target = wait_json_list(args.cdp_port)
        async with CDP(target["webSocketDebuggerUrl"]) as cdp:
            await cdp.call("Page.enable")
            await cdp.call("Log.enable")
            await cdp.call("Runtime.enable")
            await cdp.call("Page.reload")
            await asyncio.sleep(0.5)
            await wait_until(cdp, "document.readyState === 'complete'", "页面加载")

            # 1) 落在登录页
            await wait_until(
                cdp,
                "!!document.querySelector('form[action=\"/api/auth/login\"]')",
                "局域网访问被送到登录页",
                timeout=20.0,
            )
            next_value = await cdp.evaluate("document.querySelector('input[name=next]').value")
            if next_value != "/documents":
                bad.append(f"深链接没带进 next：{next_value!r}")

            # 登录页本身值得留一张图（它是这次新增的界面）
            if args.out_shot:
                shot = await cdp.call("Page.captureScreenshot", {"format": "png"})
                base = Path(args.out_shot)
                login_shot = base.with_name(base.stem + "-login" + base.suffix)
                login_shot.parent.mkdir(parents=True, exist_ok=True)
                login_shot.write_bytes(base64.b64decode(shot["data"]))
                log(f"登录页截图：{login_shot}")

            async def submit(password: str) -> None:
                await cdp.evaluate(
                    "(() => {"
                    " const input = document.querySelector('input[name=password]');"
                    f" input.value = {password!r};"
                    " document.querySelector('form').submit(); return true; })()"
                )
                await asyncio.sleep(0.8)

            # 2) 错口令 → 仍在登录页 + 有提示
            await submit("wrong-password")
            await wait_until(
                cdp, "!!document.querySelector('input[name=password]')", "错口令后仍在登录页"
            )
            err = await cdp.evaluate("document.querySelector('p.err')?.textContent || ''")
            if "口令不对" not in err:
                bad.append(f"错口令没有给出提示：{err!r}")

            # 3) 对口径 → 回到 /documents（next 生效）
            await submit(PASSWORD)
            await wait_until(
                cdp,
                "!!document.querySelector('#kb-tree') && location.pathname === '/documents'",
                "登录后回到原来的深链接",
                timeout=20.0,
            )

            # 4) 会话真的生效：从局域网地址带 Cookie 调接口
            health = await cdp.evaluate(
                "(async () => { const r = await fetch('/api/health');"
                " return JSON.stringify({status: r.status, name: (await r.json()).name}); })()"
            )
            if '"status":200' not in health.replace(" ", ""):
                bad.append(f"登录后接口仍不放行：{health}")

            # 5) 直开根页也应当直接进应用（不再看到登录页）
            await cdp.call("Page.navigate", {"url": f"http://{lan_ip}:{args.server_port}/"})
            await asyncio.sleep(0.8)
            await wait_until(
                cdp,
                "(() => location.pathname === '/'"
                " && !document.querySelector('input[name=password]'))()",
                "已登录时直开根页不再要口令",
                timeout=20.0,
            )

            if args.out_shot:
                shot = await cdp.call("Page.captureScreenshot", {"format": "png"})
                Path(args.out_shot).parent.mkdir(parents=True, exist_ok=True)
                Path(args.out_shot).write_bytes(base64.b64decode(shot["data"]))
                log(f"截图：{args.out_shot}")

            # 401 那几次会在 console 记 "Failed to load resource"，是我们**故意**
            # 制造的（错口令那次），精确豁免
            allowed = [e for e in cdp.errors if "401" in e and "auth/login" in e]
            unexpected = [e for e in cdp.errors if e not in allowed]
            if unexpected:
                bad.append("console 有错误：\n    " + "\n    ".join(unexpected[:5]))

        if bad:
            log("E2E 未过：\n  - " + "\n  - ".join(bad))
            return 1
        log("「局域网访问 + 口令」E2E 通过（回环免口令 → 局域网要登录 → 登录后可用）")
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
    parser = argparse.ArgumentParser(description="「局域网访问 + 口令」E2E（无头 Chrome）")
    parser.add_argument("--server-port", type=int, default=0)
    parser.add_argument("--cdp-port", type=int, default=0)
    parser.add_argument("--lan-ip", default="", help="指定局域网地址（默认自动探测）")
    parser.add_argument("--out-shot", default="")
    parser.add_argument(
        "--chrome",
        default=os.environ.get(
            "CHROME_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        ),
    )
    args = parser.parse_args()
    if not Path(args.chrome).exists():
        alt = Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe"
        if alt.exists():
            args.chrome = str(alt)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
