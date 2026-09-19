#!/usr/bin/env python
"""无头 Chrome 聊天设置验收（中文注释纪律）。

在真实问答页上走一遍设置交互：
  1. 打开一条历史会话（凑出"我"署名气泡）；
  2. 齿轮开面板 → 昵称改"张三"（断言历史气泡署名即时换名）；
  3. 字号拖到 17（断言 .msg-scroll 计算字号 17px）；
  4. 点预设背景色（断言背景色变量生效）；
  5. DOM.setFileInputFiles 真实走一遍"选择图片"链路（压缩 → dataURL
     进 localStorage、背景图变量生效、预览出现）；
  6. Page.reload 后断言全部持久化（刷新不丢）；
  全程收集 console 错误，有错退出码 1。

用法：
  python tools/chrome_settings.py <url> [--out-shot x.png]
  [--port 9333] [--chrome <chrome.exe 路径>]
"""

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.chrome_probe import CDP, wait_json_list  # noqa: E402

NICK = "张三"


def make_fixture(path):
    """生成一张纯色小图作为上传素材（PIL，已入 venv）。"""
    from PIL import Image

    Image.new("RGB", (400, 260), (24, 90, 170)).save(path, "PNG")


async def run(args):
    profile = tempfile.mkdtemp(prefix="probe-settings-")
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
            for _ in range(60):
                state = await cdp.evaluate(
                    "document.readyState + '|' + "
                    "String(document.querySelectorAll('#session-list .session-item').length)"
                )
                if state.startswith("complete|"):
                    break
                await asyncio.sleep(0.25)
            await asyncio.sleep(1.2)

            # 1. 打开第一条历史会话（生成"我 · 时间"署名气泡）
            await cdp.evaluate(
                "document.querySelector('#session-list .session-item')?.click(); true"
            )
            await asyncio.sleep(1.2)
            n_user = await cdp.evaluate("document.querySelectorAll('.msg.user .who').length")
            if n_user == 0:
                raise RuntimeError("没打开到带用户消息的会话（列表为空？）")
            who0 = await cdp.evaluate(
                "document.querySelector('.msg.user .who').textContent.slice(0, 10)"
            )

            # 2. 齿轮开面板 + 改昵称（change 提交）
            await cdp.evaluate("document.querySelector('#btn-settings').click(); true")
            await asyncio.sleep(0.4)
            panel_vis = await cdp.evaluate(
                "!document.querySelector('#settings-panel').classList.contains('hidden')"
            )
            # 2b. 标题栏置顶（2026-09-20 用户反馈"往下翻就点不到退出了"）：
            #     滚到底之后，标题与 ✕ 必须仍贴在面板顶部且可点
            sticky = await cdp.evaluate("""(() => {
              const panel = document.querySelector('#settings-panel');
              const head = panel.querySelector('.s-head');
              const btn = document.querySelector('#s-close').getBoundingClientRect();
              const panelTop = panel.getBoundingClientRect().top;
              const before = head.getBoundingClientRect().top;
              panel.scrollTop = panel.scrollHeight;          // 滚到底
              const after = head.getBoundingClientRect().top;
              const out = {
                scrolled: panel.scrollTop > 0,
                before, after, panelTop,
                headStuck: Math.abs(after - panelTop) <= 2,
                closeClickable: btn.bottom > panelTop && btn.top < panelTop + 60,
              };
              panel.scrollTop = 0;                            // 还原，别影响后面的步骤
              return out;
            })()""")
            await cdp.evaluate(
                f"const i = document.querySelector('#s-nick'); i.value = {json.dumps(NICK)}; "
                "i.dispatchEvent(new Event('change')); true"
            )
            await asyncio.sleep(0.3)
            who1 = await cdp.evaluate(
                "document.querySelector('.msg.user .who').textContent.slice(0, 10)"
            )

            # 3. 字号 17（input 即时生效）
            await cdp.evaluate(
                "const r = document.querySelector('#s-font'); r.value = '17'; "
                "r.dispatchEvent(new Event('input')); true"
            )
            await asyncio.sleep(0.3)
            font_px = await cdp.evaluate(
                "getComputedStyle(document.querySelector('#messages')).fontSize"
            )

            # 4. 预设背景色：点第二个色板（第一个是"默认"），期望值**从页面上读**
            #    ——写死色名/色值的版本在换色板时会假失败（2026-09-19 换 Claude
            #    暖米色板时正是它先报了警：工具没错，是它的假设过期了）。
            picked = await cdp.evaluate("""(() => {
              const b = [...document.querySelectorAll('.s-sw')][1];
              const color = b.dataset.color || '';   // 原始 hex，与 CSS 变量逐字可比
              b.click();
              return { label: b.title, color };
            })()""")
            await asyncio.sleep(0.3)
            bg_var = await cdp.evaluate(
                "getComputedStyle(document.documentElement).getPropertyValue('--chat-bg').trim()"
            )

            # 5. 真实"选择图片"（CDP 直接给 file input 喂文件，触发同一条 change 链路）
            fixture = os.path.join(tempfile.gettempdir(), "settings-bg.png")
            make_fixture(fixture)
            await cdp.call("DOM.enable")
            doc = await cdp.call("DOM.getDocument", {"depth": 1})
            qres = await cdp.call(
                "DOM.querySelector",
                {"nodeId": doc["root"]["nodeId"], "selector": "#s-bgimg"},
            )
            await cdp.call("DOM.setFileInputFiles", {"nodeId": qres["nodeId"], "files": [fixture]})
            # 压缩是异步的：轮询到背景图变量带上 data:image
            for _ in range(40):
                img_var = await cdp.evaluate(
                    "getComputedStyle(document.documentElement).getPropertyValue('--chat-img').trim()"
                )
                if img_var.startswith("url("):
                    break
                await asyncio.sleep(0.25)
            if not img_var.startswith("url("):
                raise RuntimeError("背景图未应用（压缩链路失败？）")
            preview_ok = await cdp.evaluate(
                "!document.querySelector('#s-bgimg-preview').classList.contains('hidden')"
            )

            # 6. 刷新验持久化
            await cdp.call("Page.reload", {"ignoreCache": True})
            await asyncio.sleep(2.5)
            after = await cdp.evaluate("""(() => {
              const cs = getComputedStyle(document.documentElement);
              return {
                nick: localStorage.getItem('mikasa.ui.nick'),
                font: cs.getPropertyValue('--chat-font').trim(),
                bg: cs.getPropertyValue('--chat-bg').trim(),
                img: cs.getPropertyValue('--chat-img').startsWith('url('),
                inputNick: document.querySelector('#s-nick').value,
              };
            })()""")
            await asyncio.sleep(1.0)

            shot = None
            if args.out_shot:
                res = await cdp.call("Page.captureScreenshot", {"format": "png"})
                shot = res["data"]

            await asyncio.sleep(1.5)
            report = {
                "userWhoBubbles": n_user,
                "whoBefore": who0,
                "panelVisible": panel_vis,
                "stickyHead": sticky,
                "whoAfterNick": who1,
                "msgFontPx": font_px,
                "bgColorVar": bg_var,
                "bgImageApplied": img_var.startswith("url("),
                "bgImageKind": img_var[:32],
                "previewShown": preview_ok,
                "afterReload": after,
                "consoleErrors": cdp.errors,
            }
            print(json.dumps(report, ensure_ascii=False))
            if shot:
                with open(args.out_shot, "wb") as f:
                    f.write(base64.b64decode(shot))
                print(f"截图已写: {args.out_shot}", file=sys.stderr)

            bad = []
            if not sticky["scrolled"] or not sticky["headStuck"] or not sticky["closeClickable"]:
                bad.append(
                    f"设置面板滚到底后标题栏没置顶（或 ✕ 点不到）："
                    f"before={sticky['before']:.0f} after={sticky['after']:.0f} "
                    f"panelTop={sticky['panelTop']:.0f} scrolled={sticky['scrolled']}"
                )
            if who1.split(" · ")[0] != NICK:
                bad.append(f"历史气泡署名没换成 {NICK}")
            if font_px != "17px":
                bad.append(f"字号未生效: {font_px}")
            if bg_var != picked["color"]:
                bad.append(
                    f"背景色变量不符：点的是「{picked['label']}」{picked['color']}，实得 {bg_var}"
                )
            if not img_var.startswith("url(") or not preview_ok:
                bad.append("背景图链路失败")
            persist_ok = (
                after["nick"] == NICK
                and after["font"] == "17px"
                and after["bg"] == picked["color"]
                and after["img"]
                and after["inputNick"] == NICK
            )
            if not persist_ok:
                bad.append("刷新后持久化丢失")
            if cdp.errors:
                bad.append("console 有错误")
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
    parser = argparse.ArgumentParser(description="无头 Chrome 聊天设置验收")
    parser.add_argument("url", help="目标页面地址，如 http://127.0.0.1:8787/")
    parser.add_argument("--out-shot", default=None, help="截图输出路径（PNG）")
    parser.add_argument("--port", type=int, default=9333)
    parser.add_argument(
        "--chrome",
        default=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    )
    args = parser.parse_args()
    import os

    if not os.path.exists(args.chrome):
        local = os.environ.get("LOCALAPPDATA", "")
        alt = os.path.join(local, r"Google\Chrome\Application\chrome.exe")
        if os.path.exists(alt):
            args.chrome = alt
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
