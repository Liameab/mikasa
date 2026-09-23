#!/usr/bin/env python
"""无头 Chrome「边看边问」E2E 验收（A 档 c，2026-09-22）。

自起一台隔离服务器（offline profile + 临时 data_dir，真实 Web 进程）+ 两篇
语料，在真实页面上走一遍用户流程：

  1. 知识库页开 A 篇 → 右栏阅读区底部**出现提问栏**，范围默认「本篇」；
  2. 在正文里选中一段文字 → 提问栏出现「已选中 N 字」胶囊；点 ✕ 能去掉；
  3. 就本篇提问 → 等待计时可见 → 流式答案 + 引用角标 + **相关片段列表**
     （被引用的那张带 [n]）；
  4. 点相关片段 → 就地跳到那段原文并高亮（**不重载文档**——标题不变）；
  5. 切「全库」问另一篇的主题 → 片段列表出现「另一篇」的卡片，点它 →
     阅读器切到那一篇并定位；
  6. 关掉阅读区再打开 → 输入框、结果区、选中胶囊都是干净的；
  7. 问答页（浮层形态）点 [n] 角标开面板 → 提问栏同样在位；
  8. 全程 console 零错误（不豁免任何 URL），末帧截图。

就绪判定用新端点 `/api/documents/999999/ask/stream` 的 404 detail 文案：旧构建
没有这条路由（FastAPI 默认 `{"detail":"Not Found"}`），绝不能把它误认成自己的
服务（chrome_reader.py 用 /api/chunks 的 detail，同一套做法）。

用法：
  python tools/chrome_reader_ask.py [--out-shot tools/shots/reader-ask.png]
  [--server-port 8788] [--cdp-port 9334] [--chrome <chrome.exe 路径>]
退出码：0 = 验收通过；1 = 任一断言失败 / console 有错。
"""

import argparse
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.chrome_corpus import (  # noqa: E402  复用隔离服务器与页面操作辅助
    free_port,
    wait_until,
)
from tools.chrome_probe import CDP, log, wait_json_list  # noqa: E402

REPO_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIKASA_EXE = REPO_ROOT / ".venv" / "Scripts" / "mikasa.exe"

# A 篇：正则化（锚句用于"选中上下文"与"点片段跳转"的断言）
FIXTURE_A = """# L2 正则化笔记

## 为什么防过拟合

L2 正则化在损失中加入权重的平方和惩罚项，鼓励小而分散的权重。

## 权重衰减

权重衰减是 L2 正则化在梯度下降里的等价实现。
"""

# B 篇：注意力（与 A 无词面交集，"另一篇"的跳转断言靠它）
FIXTURE_B = """# 注意力机制笔记

## 缩放点积注意力

缩放点积注意力除以根号 dk，防止点积随维度增大而方差过大。
"""

QUESTION_A = "L2 正则化为什么能防止过拟合？"
QUESTION_B = "缩放点积注意力为什么要除以根号 dk？"


async def wait_new_server(port: int, timeout: float = 30.0) -> None:
    """等隔离服务器就绪；用新端点的 404 文案区分新旧构建。"""
    deadline = time.time() + timeout
    body = json.dumps({"question": "就绪探测", "scope": "doc"}).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/documents/999999/ask/stream",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(request, timeout=1.5) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:  # 404 也带 body，要读 detail
            if "文档不存在" in exc.read().decode("utf-8"):
                return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.3)
    raise RuntimeError("服务器未就绪或不是含「边看边问」端点的构建（见 tmpdir/serve.log）")


async def upload(cdp, path: Path, expect_rows: int) -> None:
    """真实 file input 链路喂文件，等树里出现第 expect_rows 行。"""
    await cdp.call("DOM.enable")
    doc = await cdp.call("DOM.getDocument", {"depth": 1})
    node = await cdp.call(
        "DOM.querySelector", {"nodeId": doc["root"]["nodeId"], "selector": "#file-input"}
    )
    await cdp.call("DOM.setFileInputFiles", {"nodeId": node["nodeId"], "files": [str(path)]})
    await wait_until(
        cdp,
        f"document.querySelectorAll('#kb-tree .doc-item').length === {expect_rows}",
        f"第 {expect_rows} 篇入库完成",
    )


async def open_doc_row(cdp, title: str) -> None:
    """按**标题**点开文档 → 右栏阅读区打开且正文就绪。

    不按下标点：树是 `created_at DESC`（repo.list_documents），后上传的那篇在
    第 0 行——本脚本 2026-09-23 首次真跑就是这么栽的（点开的是"注意力"却按
    "正则化"断言，全篇在验另一篇）。点完再核对阅读器标题，点错就当场报错。
    """
    literal = json.dumps(title, ensure_ascii=False)
    await cdp.evaluate(
        "(() => { const row = [...document.querySelectorAll('#kb-tree .doc-item')]"
        f"  .find(n => n.textContent.includes({literal}));"
        f" if (!row) throw new Error('树里没有这篇：' + {literal});"
        " row.click(); return true; })()"
    )
    await wait_until(
        cdp,
        "(() => { const c = document.querySelector('#reading-card');"
        " return c && !c.classList.contains('hidden')"
        " && document.querySelectorAll('#reader-inline .rd-chunk').length > 0; })()",
        f"阅读区打开（{title}）",
    )
    shown = await cdp.evaluate("document.querySelector('#reader-inline .rd-title').textContent")
    if title not in shown:
        raise RuntimeError(f"打开的文档不对：期望「{title}」，实际「{shown}」")


async def ask(cdp, question: str, timeout: float = 90.0) -> None:
    """在提问栏里发一个问题，等流式收尾（发送钮恢复 = 收尾信号）。"""
    await cdp.evaluate(
        "(() => { const n = document.querySelector('#rd-ask-input');"
        f" n.value = {json.dumps(question, ensure_ascii=False)};"
        " n.dispatchEvent(new Event('input', {bubbles: true})); return true; })()"
    )
    await cdp.evaluate("document.querySelector('#rd-ask-send').click()")
    await wait_until(
        cdp,
        "document.querySelector('#rd-ask-send').textContent === '问'",
        f"流式收尾（{question[:12]}…）",
        timeout=timeout,
    )


async def run(args):
    tmp = Path(tempfile.mkdtemp(prefix="reader-ask-e2e-"))
    server = None
    chrome = None
    bad: list[str] = []
    try:
        args.server_port = args.server_port or free_port()
        args.cdp_port = args.cdp_port or free_port()

        # ---- 隔离服务器（offline profile + 注入 data_dir） ----
        cfg_text = (REPO_ROOT / "config" / "profiles" / "offline.yaml").read_text(encoding="utf-8")
        cfg = tmp / "serve.yaml"
        cfg.write_text(
            cfg_text.replace(
                "profile: offline", f"profile: offline\n\ndata_dir: {(tmp / 'data').as_posix()}"
            ),
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
        await wait_new_server(args.server_port)

        base = f"http://127.0.0.1:{args.server_port}"
        profile = tempfile.mkdtemp(prefix="reader-ask-chrome-")
        chrome = subprocess.Popen(
            [
                args.chrome,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                f"--remote-debugging-port={args.cdp_port}",
                f"--user-data-dir={profile}",
                "--window-size=1400,1000",
                f"{base}/documents",
            ]
        )
        target = wait_json_list(args.cdp_port)
        async with CDP(target["webSocketDebuggerUrl"]) as cdp:
            await cdp.call("Page.enable")
            await cdp.call("Log.enable")
            await wait_until(
                cdp,
                "document.readyState === 'complete' && !!document.querySelector('#kb-tree')",
                "知识库页就绪",
            )
            await asyncio.sleep(1.0)
            # 空库会弹首启引导（z-index 200）：不关掉后面全是"点不着"的假通过
            if await cdp.evaluate("!!document.querySelector('.onboard-mask')"):
                await cdp.evaluate(
                    "[...document.querySelectorAll('.onboard-card button')]"
                    ".find(b => b.textContent.includes('开始使用'))?.click()"
                )
                await wait_until(cdp, "!document.querySelector('.onboard-mask')", "首启引导关闭")

            # ---- 1) 两篇语料入库 ----
            path_a = tmp / "L2 正则化笔记.md"
            path_a.write_text(FIXTURE_A, encoding="utf-8")
            path_b = tmp / "注意力机制笔记.md"
            path_b.write_text(FIXTURE_B, encoding="utf-8")
            await upload(cdp, path_a, 1)
            await upload(cdp, path_b, 2)

            # ---- 2) 开 A 篇：提问栏在位、范围默认「本篇」 ----
            await open_doc_row(cdp, "L2 正则化笔记")
            if not await cdp.evaluate(
                "!document.querySelector('#rd-ask').classList.contains('hidden')"
            ):
                bad.append("阅读区打开后提问栏没有出现")
            scope_state = await cdp.evaluate(
                "(() => ({doc: document.querySelector('#rd-ask-doc').classList.contains('active'),"
                " all: document.querySelector('#rd-ask-all').classList.contains('active')}))()"
            )
            if not (scope_state["doc"] and not scope_state["all"]):
                bad.append(f"范围默认值不对（应为「本篇」）：{scope_state}")

            # ---- 3) 选中正文文字 → 出现上下文胶囊；✕ 能去掉 ----
            await cdp.evaluate(
                "(() => { const el = document.querySelector('#reader-inline .rd-chunk');"
                " const r = document.createRange(); r.selectNodeContents(el);"
                " const s = window.getSelection(); s.removeAllRanges(); s.addRange(r);"
                " el.closest('.rd-text').dispatchEvent(new MouseEvent('mouseup', {bubbles:true}));"
                " })()"
            )
            await wait_until(
                cdp,
                "!document.querySelector('#rd-ask-sel').classList.contains('hidden')",
                "选中胶囊出现",
            )
            sel_text = await cdp.evaluate(
                "document.querySelector('#rd-ask-sel .rd-ask-sel-text').textContent"
            )
            if "已选中" not in sel_text:
                bad.append(f"选中胶囊文案不对：{sel_text!r}")
            await cdp.evaluate("document.querySelector('.rd-ask-sel-x').click()")
            if not await cdp.evaluate(
                "document.querySelector('#rd-ask-sel').classList.contains('hidden')"
            ):
                bad.append("点 ✕ 之后选中胶囊没有收起")
            # 再选一次（后面提问要带上下文）
            await cdp.evaluate(
                "(() => { const el = document.querySelector('#reader-inline .rd-chunk');"
                " const r = document.createRange(); r.selectNodeContents(el);"
                " const s = window.getSelection(); s.removeAllRanges(); s.addRange(r);"
                " el.closest('.rd-text').dispatchEvent(new MouseEvent('mouseup', {bubbles:true}));"
                " })()"
            )

            # ---- 4) 就本篇提问：等待可见 → 答案 + 引用 + 相关片段 ----
            await cdp.evaluate(
                "(() => { const n = document.querySelector('#rd-ask-input');"
                f" n.value = {json.dumps(QUESTION_A, ensure_ascii=False)};"
                " n.dispatchEvent(new Event('input', {bubbles:true})); })()"
            )
            await cdp.evaluate("document.querySelector('#rd-ask-send').click()")
            who = await cdp.evaluate("document.querySelector('.rd-ask-who').textContent")
            if "正在" not in who:
                bad.append(f"等待提示没出现（用户会以为卡死）：{who!r}")
            await wait_until(
                cdp,
                "document.querySelector('#rd-ask-send').textContent === '问'",
                "本篇提问流式收尾",
                timeout=90.0,
            )
            got = await cdp.evaluate(
                "(() => ({cites: document.querySelectorAll('.rd-ask-body .cite').length,"
                " srcs: document.querySelectorAll('.rd-ask-srcs .rd-src').length,"
                " cited: document.querySelectorAll('.rd-ask-srcs .rd-src.cited').length,"
                " marks: [...document.querySelectorAll('.rd-ask-srcs .rd-src.cited .pill')]"
                "   .map(n => n.textContent)}))()"
            )
            if got["cites"] < 1 or not await cdp.evaluate(
                "document.querySelector('.rd-ask-body').textContent.trim().length > 0"
            ):
                bad.append(f"答案没渲染出来或没有引用角标：{got}")
            if got["srcs"] < 1:
                bad.append(f"相关片段列表是空的（本篇应当有命中）：{got}")
            if got["cited"] < 1 or "[1]" not in got["marks"]:
                # 带上一小段真实 DOM：失败时能一眼看出是"没标 cited"还是"卡片没渲染"
                srcs_html = await cdp.evaluate(
                    "document.querySelector('.rd-ask-srcs').innerHTML.slice(0, 300)"
                )
                bad.append(f"被引用的片段没有标出 [n]：{got} ｜ DOM: {srcs_html!r}")

            # ---- 5) 点相关片段 → 就地跳转高亮（不重载文档） ----
            title_before = await cdp.evaluate(
                "document.querySelector('#reader-inline .rd-title').textContent"
            )
            # 没有 .cited 卡片时不硬点（前面已经记了这条失败）：让 E2E 跑完剩余步骤，
            # 一次拿到完整的问题清单，而不是死在第一个 null 上
            if got["cited"] >= 1:
                await cdp.evaluate("document.querySelector('.rd-ask-srcs .rd-src.cited').click()")
                await wait_until(
                    cdp,
                    "!!document.querySelector('#reader-inline .rd-chunk.lit')",
                    "点片段后正文出现高亮",
                )
                lit_text = await cdp.evaluate(
                    "document.querySelector('#reader-inline .rd-chunk.lit').textContent"
                )
                title_after = await cdp.evaluate(
                    "document.querySelector('#reader-inline .rd-title').textContent"
                )
                if "正则化" not in lit_text:
                    bad.append(f"高亮的不是命中的那块：{lit_text[:40]!r}")
                if title_after != title_before:
                    bad.append(f"点本篇片段不该换文档：{title_before!r} → {title_after!r}")

            # ---- 6) 切「全库」问 B 篇主题 → 「另一篇」卡片 → 切过去 ----
            narrow = await cdp.evaluate("document.querySelectorAll('.rd-ask-srcs .rd-src').length")
            if narrow < 1:
                bad.append("切全库之前就没有片段，后面的对比无意义")
            await cdp.evaluate("document.querySelector('#rd-ask-all').click()")
            scope_now = await cdp.evaluate(
                "document.querySelector('#rd-ask-all').classList.contains('active')"
            )
            if not scope_now:
                bad.append("点「全库」之后胶囊没有选中")
            await ask(cdp, QUESTION_B)
            other = await cdp.evaluate(
                "(() => [...document.querySelectorAll('.rd-ask-srcs .rd-src')]"
                ".filter(n => n.textContent.includes('另一篇')).length)()"
            )
            if other < 1:
                bad.append("全库口径下没有出现「另一篇」的片段卡片")
            else:
                await cdp.evaluate(
                    "(() => [...document.querySelectorAll('.rd-ask-srcs .rd-src')]"
                    ".find(n => n.textContent.includes('另一篇')).click())()"
                )
                await wait_until(
                    cdp,
                    "document.querySelector('#reader-inline .rd-title').textContent"
                    ".includes('注意力')",
                    "点「另一篇」后阅读器切到那篇",
                )

            # ---- 7) 关掉再打开：输入/结果/选中都是干净的 ----
            await cdp.evaluate("document.querySelector('#rd-close').click()")
            await wait_until(
                cdp,
                "document.querySelector('#reading-card').classList.contains('hidden')",
                "阅读区收起",
            )
            await open_doc_row(cdp, "L2 正则化笔记")
            clean = await cdp.evaluate(
                "(() => ({input: document.querySelector('#rd-ask-input').value,"
                " outHidden: document.querySelector('#rd-ask-out').classList.contains('hidden'),"
                " selHidden: document.querySelector('#rd-ask-sel').classList.contains('hidden'),"
                " scope: document.querySelector('#rd-ask-doc').classList.contains('active')}))()"
            )
            if clean["input"] or not clean["outHidden"] or not clean["selHidden"]:
                bad.append(f"重开之后提问区没清干净：{clean}")

            # ---- 8) 问答页（浮层形态）：点 [n] 角标开面板，提问栏同样在位 ----
            await cdp.call("Page.navigate", {"url": f"{base}/"})
            await wait_until(
                cdp,
                "document.readyState === 'complete' && !!document.querySelector('#question')",
                "问答页就绪",
            )
            await asyncio.sleep(0.8)
            await cdp.evaluate(
                "(() => { const n = document.querySelector('#question');"
                f" n.value = {json.dumps(QUESTION_A, ensure_ascii=False)};"
                " n.dispatchEvent(new Event('input', {bubbles:true})); })()"
            )
            await cdp.evaluate("document.querySelector('#send').click()")
            await wait_until(
                cdp,
                "!!document.querySelector('.msg.assistant .cite')",
                "问答页答案与引用角标出现",
                timeout=60.0,
            )
            await cdp.evaluate("document.querySelector('.msg.assistant .cite').click()")
            await wait_until(
                cdp,
                "(() => { const p = document.querySelector('#reader-panel');"
                " return p && !p.classList.contains('hidden')"
                " && document.querySelectorAll('#reader-panel .rd-chunk').length > 0; })()",
                "浮层阅读面板打开",
            )
            if await cdp.evaluate(
                "document.querySelector('#reader-panel #rd-ask').classList.contains('hidden')"
            ):
                bad.append("浮层形态里提问栏没有出现")
            # 刚打开的面板提问区必须是干净的（同第 7 步的规矩）：带着上一次的
            # 答案/片段进来，用户会以为那是"这一篇"的结果
            fresh = await cdp.evaluate("""(() => {
              const p = document.querySelector('#reader-panel');
              const n = p.querySelector('#rd-ask-input');
              return {input: n.value, placeholder: n.placeholder,
                      outHidden: p.querySelector('#rd-ask-out').classList.contains('hidden'),
                      srcs: p.querySelectorAll('.rd-ask-srcs .rd-src').length,
                      outHtml: p.querySelector('#rd-ask-out').innerHTML.slice(0, 200)};
            })()""")
            if fresh["input"] or not fresh["outHidden"] or fresh["srcs"]:
                bad.append(f"浮层面板刚打开提问区就有残留：{fresh}")

            if args.out_shot:
                res = await cdp.call("Page.captureScreenshot", {"format": "png"})
                Path(args.out_shot).write_bytes(base64.b64decode(res["data"]))
                log(f"截图已写: {args.out_shot}")
            await asyncio.sleep(1.0)
            if cdp.errors:
                bad.append("console 有错误：" + "；".join(cdp.errors[:3]))

        if bad:
            log("验收未过：\n  - " + "\n  - ".join(bad))
            log("服务日志尾：\n" + (tmp / "serve.log").read_text(encoding="utf-8")[-2000:])
            return 1
        log("「边看边问」E2E 验收通过（提问 → 答案+片段 → 就地跳转 → 全库跨篇 → 重开清空）")
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
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="无头 Chrome「边看边问」E2E 验收")
    parser.add_argument("--out-shot", default=None, help="截图输出路径（PNG）")
    parser.add_argument("--server-port", type=int, default=None)
    parser.add_argument("--cdp-port", type=int, default=None)
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
