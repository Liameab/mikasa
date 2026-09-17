#!/usr/bin/env python
"""无头 Chrome 笔记编辑器 E2E 验收（M6 ①，中文注释纪律）。

自起一台隔离服务器（offline profile + 临时 data_dir，真实 Web 进程），
在真实 /documents 页面上走一遍"写笔记"的完整用户流程：
  1. 点「＋ 新建笔记」→ 居中模态打开（标题框/正文框/预览/保存钮齐备）；
  2. 填标题与正文（含 `#` 标题行、代码围栏、管道表）→ **右侧预览实时渲染**
     （.code-block / .md-table / 小标题都在，不是纯文本）；
  3. 保存 → 模态关闭、树根级出现文档行、meta 显示「笔记」、计数变 1 篇；
  4. 点该行 → 右栏阅读区打开；⋯ 菜单出现「编辑笔记」；
  5. 打开编辑器 → 正文框内容与写入的**逐字符相同**（证明走 uploads 原文
     而不是 /content 拼块）；
  6. 追加新关键词保存 → 树里仍**只有一行**（编辑 = 原地替换，不是新增）；
  7. 有未保存修改时按 Esc → 弹出二次确认，且**底下的阅读区没被一起关掉**
     （模态的捕获态拦截生效）；再按 Esc 只关确认框、编辑器还在；
  8. 直接调 /api/ask 问新关键词 → 答案/引用命中该笔记（入库即被检索）；
  9. 全程收集 console 错误（含网络失败），零错误才算过；末帧截图。

用法：
  python tools/chrome_notes.py [--out-shot tools/shots/notes-e2e.png]
  [--server-port 8787] [--cdp-port 9333] [--chrome <chrome.exe 路径>]
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
import urllib.error
import urllib.request
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

# 正文刻意包含 markdown 结构：预览断言的靶子，也是"回填要逐字符"的靶子
TITLE = "快排复习手册"
BODY = """# 快速排序

分治：选基准、划分、递归。

```python
def qs(a):
    return a if len(a) < 2 else qs([x for x in a[1:] if x <= a[0]])
```

| 场景 | 复杂度 |
| --- | --- |
| 平均 | O(n log n) |
"""
ADDED = "\n补充关键词：三路划分把相等元素聚成一段，避免重复元素退化。\n"


async def fill(cdp, selector: str, value: str) -> None:
    """设值并派发 input（编辑器的预览/字数统计都监听 input）。

    一律包 IIFE：evaluate 共享页面全局作用域，顶层 const 二次声明会 SyntaxError。
    """
    await cdp.evaluate(
        f"(() => {{ const n = document.querySelector({js_quote(selector)}); "
        f"if (!n) return false; n.value = {js_quote(value)}; "
        f"n.dispatchEvent(new Event('input', {{bubbles: true}})); return true; }})()"
    )


async def click(cdp, selector: str, what: str) -> None:
    """点击元素（找不到即失败——比静默返回更有诊断价值）。"""
    ok = await cdp.evaluate(
        f"(() => {{ const n = document.querySelector({js_quote(selector)}); "
        f"if (!n) return false; n.click(); return true; }})()"
    )
    if not ok:
        raise RuntimeError(f"点不到：{what}（{selector}）")


async def click_text(cdp, selector: str, label: str) -> None:
    """按文案点一组候选元素里的某一个（按钮文案是用户可见契约）。"""
    ok = await cdp.evaluate(
        f"(() => {{ const n = [...document.querySelectorAll({js_quote(selector)})]"
        f".find(x => x.textContent.includes({js_quote(label)})); "
        "if (!n) return false; n.click(); return true; })()"
    )
    if not ok:
        raise RuntimeError(f"点不到文案为「{label}」的元素（{selector}）")


async def press_escape(cdp) -> None:
    await cdp.call(
        "Input.dispatchKeyEvent",
        {"type": "keyDown", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
    )


def ask(port: int, question: str) -> dict:
    """直接打 /api/ask（offline MockLLM：与语料有 ≥4 字连续重叠即引用作答）。"""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/ask",
        data=json.dumps({"question": question}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def run(args):
    tmp = Path(tempfile.mkdtemp(prefix="notes-e2e-"))
    server = None
    chrome = None
    try:
        args.server_port = args.server_port or free_port()
        args.cdp_port = args.cdp_port or free_port()

        cfg_text = (REPO_ROOT / "config" / "profiles" / "offline.yaml").read_text(encoding="utf-8")
        data_dir = (tmp / "data").as_posix()
        cfg = tmp / "serve.yaml"
        cfg.write_text(
            cfg_text.replace("profile: offline", f"profile: offline\n\ndata_dir: {data_dir}"),
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
        profile = tempfile.mkdtemp(prefix="notes-chrome-")
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
            await wait_until(
                cdp,
                "document.readyState === 'complete' && !!document.querySelector('#kb-tree')",
                "知识库页就绪",
            )
            await asyncio.sleep(1.0)  # 首载 refreshCorpusTree 网络往返

            # 0) 关掉首启引导：空库会弹全屏遮罩（z-index 200）。
            # 程序化 .click() 能穿透遮罩，但真实用户点不到——不关掉的话
            # 后面每一步都是"用户其实点不着"的假通过。
            if await cdp.evaluate("!!document.querySelector('.onboard-mask')"):
                await click_text(cdp, ".onboard-card button", "开始使用")
                await wait_until(cdp, "!document.querySelector('.onboard-mask')", "首启引导关闭")

            # 1) 新建笔记：模态打开且四要素齐备
            await click(cdp, "#new-note", "新建笔记按钮")
            await wait_until(cdp, "!!document.querySelector('.note-box')", "笔记编辑器出现")
            shape = await cdp.evaluate(
                "(() => ({title: !!document.querySelector('.note-title'),"
                " input: !!document.querySelector('.note-input'),"
                " preview: !!document.querySelector('.note-preview'),"
                " folder: !!document.querySelector('.note-folder')}))()"
            )
            if not all(shape.values()):
                bad.append(f"编辑器结构不全：{shape}")

            # 2) 填内容 → 预览实时渲染（不是纯文本回显）
            await fill(cdp, ".note-title", TITLE)
            await fill(cdp, ".note-input", BODY)
            await wait_until(
                cdp, "!!document.querySelector('.note-preview .code-block')", "预览代码块"
            )
            preview = await cdp.evaluate(
                "(() => ({code: document.querySelectorAll('.note-preview .code-block').length,"
                " table: document.querySelectorAll('.note-preview .md-table').length,"
                " head: document.querySelectorAll('.note-preview h3,.note-preview h4').length}))()"
            )
            if not (preview["code"] == 1 and preview["table"] == 1 and preview["head"] >= 1):
                bad.append(f"预览渲染不合预期：{preview}")
            if args.out_shot:
                # 编辑器开着的样子单独留一张（末帧是保存后的树/阅读区，看不到编辑器）
                shot = Path(args.out_shot)
                res = await cdp.call("Page.captureScreenshot", {"format": "png"})
                sibling = shot.with_name(f"{shot.stem}-editor{shot.suffix}")
                sibling.write_bytes(base64.b64decode(res["data"]))
                log(f"编辑器截图已写: {sibling}")

            # 3) 保存 → 模态关闭、树出现笔记行、计数 1 篇
            await click(cdp, ".note-foot .btn.primary", "保存按钮")
            await wait_until(cdp, "!document.querySelector('.note-backdrop')", "编辑器关闭")
            await wait_until(
                cdp,
                "document.querySelectorAll('#kb-tree .doc-item').length === 1",
                "树里出现笔记行",
            )
            row = await cdp.evaluate(
                "(() => { const r = document.querySelector('#kb-tree .doc-item');"
                " return {title: r.querySelector('.s-title').textContent,"
                " meta: r.querySelector('.s-meta').textContent,"
                " active: r.classList.contains('active')}; })()"
            )
            if row["title"] != TITLE:
                bad.append(f"树里标题不对：{row['title']!r}")
            if "笔记" not in row["meta"]:
                bad.append(f"meta 没标识为笔记：{row['meta']!r}")
            if not row["active"]:
                bad.append("保存后没有选中新行（右侧阅读区会因此塌掉）")
            pill = await cdp.evaluate("document.querySelector('#doc-count').textContent")
            if "1 篇" not in pill:
                bad.append(f"计数不是 1 篇：{pill!r}")

            # 4) 点行开阅读区 → ⋯ 菜单里出现「编辑笔记」
            await cdp.evaluate("document.querySelector('#kb-tree .doc-item').click()")
            await wait_until(
                cdp,
                "!document.querySelector('#reading-card').classList.contains('hidden')",
                "阅读区打开",
            )
            await cdp.evaluate("document.querySelector('#kb-tree .doc-item .t-more').click()")
            await wait_until(cdp, "!!document.querySelector('#ctx-menu .ctx-item')", "⋯ 菜单展开")
            menu = await cdp.evaluate(
                "[...document.querySelectorAll('#ctx-menu .ctx-item')].map(n => n.textContent)"
            )
            if "编辑笔记" not in menu:
                bad.append(f"笔记行菜单缺「编辑笔记」：{menu}")
            await cdp.evaluate(
                "[...document.querySelectorAll('#ctx-menu .ctx-item')]"
                ".find(n => n.textContent === '编辑笔记').click()"
            )

            # 5) 回填：正文框与写入内容逐字符相同
            await wait_until(cdp, "!!document.querySelector('.note-box')", "编辑器再次打开")
            await wait_until(
                cdp,
                "document.querySelector('.note-input')?.value.length > 0",
                "正文回填完成",
            )
            loaded = await cdp.evaluate(
                "(() => ({title: document.querySelector('.note-title').value,"
                " body: document.querySelector('.note-input').value}))()"
            )
            if loaded["title"] != TITLE:
                bad.append(f"回填标题不符：{loaded['title']!r}")
            if loaded["body"] != BODY:
                bad.append(
                    "回填正文与写入不一致（走 /content 拼块就会丢 # 标题行与围栏）："
                    f"{loaded['body']!r}"
                )

            # 6) 未保存修改 + Esc：确认框出现，且底下的阅读区不被一起关掉
            await fill(cdp, ".note-input", BODY + ADDED)
            await press_escape(cdp)
            await wait_until(cdp, "!!document.querySelector('.confirm-backdrop')", "二次确认出现")
            still_open = await cdp.evaluate(
                "(() => ({editor: !!document.querySelector('.note-box'),"
                " confirmTitle: document.querySelector('.cf-title')?.textContent || '',"
                " reading: !!document.querySelector('#reading-card:not(.hidden)')}))()"
            )
            if "放弃" not in still_open["confirmTitle"]:
                bad.append(f"确认框标题不对：{still_open['confirmTitle']!r}")
            if not still_open["editor"]:
                bad.append("确认框出现时编辑器被关掉了（应等用户选择）")
            if not still_open["reading"]:
                bad.append("一次 Esc 把底下的阅读区也关了（模态拦截失效）")
            # 再按一次 Esc：只收掉确认框，编辑器还在
            await press_escape(cdp)
            await wait_until(cdp, "!document.querySelector('.confirm-backdrop')", "确认框关闭")
            if not await cdp.evaluate("!!document.querySelector('.note-box')"):
                bad.append("确认框的 Esc 连带关掉了编辑器")

            # 7) 保存改动 → 树里仍只有一行（编辑 = 原地替换）
            await click(cdp, ".note-foot .btn.primary", "保存按钮")
            await wait_until(cdp, "!document.querySelector('.note-backdrop')", "编辑器关闭")
            await asyncio.sleep(0.6)
            rows = await cdp.evaluate(
                "(() => { const rs = [...document.querySelectorAll('#kb-tree .doc-item')];"
                " return {n: rs.length, title: rs[0]?.querySelector('.s-title')?.textContent}; })()"
            )
            if rows["n"] != 1 or rows["title"] != TITLE:
                bad.append(f"编辑后树形态不对（应仍是同一条）：{rows}")
            # 保存前阅读区是开着的（第 4 步点过行）：保存后必须还在。
            # 同名替换换了行 id，若不按新 id 重选，刷新会判"选中行已被删除"
            # → 收起阅读区 —— 用户正在读的那篇突然消失，像是被保存弄丢了。
            if await cdp.evaluate(
                "document.querySelector('#reading-card').classList.contains('hidden')"
            ):
                bad.append("保存后阅读区被收起了（应保持打开并显示新内容）")
            row_after = await cdp.evaluate(
                "(() => { const r = document.querySelector('#kb-tree .doc-item');"
                " return r ? r.classList.contains('active') : false; })()"
            )
            if not row_after:
                bad.append("保存后新行没有被重新选中")

            # 8) 入库即被检索：直接问后端（offline MockLLM 按字面重叠命中）
            answer = ask(args.server_port, "三路划分把相等元素聚成一段是为了什么")["answer"]
            if answer["refused"] or not answer["citations"]:
                bad.append(f"改完的正文没能被检索到：{answer}")
            elif answer["citations"][0]["document_title"] != TITLE:
                bad.append(f"引用指向了别的文档：{answer['citations'][0]}")

            # 9) 反例：上传的普通文档**不能**出现「编辑笔记」（此前只测了正例，
            #    菜单项门控若写反——比如对所有行都加——不会被发现）
            fixture = tmp / "上传件.md"
            fixture.write_text(
                "# 上传件\n\n这是一篇上传的文档，正文不属于编辑器管。\n", encoding="utf-8"
            )
            await cdp.call("DOM.enable")
            root = await cdp.call("DOM.getDocument", {"depth": 1})
            node = await cdp.call(
                "DOM.querySelector",
                {"nodeId": root["root"]["nodeId"], "selector": "#file-input"},
            )
            await cdp.call(
                "DOM.setFileInputFiles", {"nodeId": node["nodeId"], "files": [str(fixture)]}
            )
            await wait_until(
                cdp,
                "document.querySelectorAll('#kb-tree .doc-item').length === 2",
                "上传件出现在树里",
            )
            # 文档列表按新建倒序：刚上传的排第一
            uploaded_menu = await cdp.evaluate(
                """(() => {
                  document.querySelector('#kb-tree .doc-item .t-more').click();
                  return [...document.querySelectorAll('#ctx-menu .ctx-item')]
                    .map((n) => n.textContent);
                })()"""
            )
            if "编辑笔记" in uploaded_menu:
                bad.append(f"上传件不该有「编辑笔记」：{uploaded_menu}")
            await cdp.evaluate("document.body.click()")  # 关掉菜单

            # 9) 截图 + console 错误收尾
            if args.out_shot:
                res = await cdp.call("Page.captureScreenshot", {"format": "png"})
                Path(args.out_shot).write_bytes(base64.b64decode(res["data"]))
                log(f"截图已写: {args.out_shot}")
            await asyncio.sleep(1.5)  # 静默给 1.5s 消化异步报错
            if cdp.errors:
                bad.append("console 有错误：" + "；".join(cdp.errors[:3]))

        if bad:
            log("验收未过：\n  - " + "\n  - ".join(bad))
            log("服务日志尾：\n" + (tmp / "serve.log").read_text(encoding="utf-8")[-2000:])
            return 1
        log("笔记编辑器 E2E 验收通过")
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
    parser = argparse.ArgumentParser(description="无头 Chrome 笔记编辑器 E2E 验收")
    parser.add_argument("--out-shot", default=None, help="截图输出路径（PNG）")
    parser.add_argument("--server-port", type=int, default=None, help="隔离服务器端口")
    parser.add_argument("--cdp-port", type=int, default=None, help="Chrome 调试端口")
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
