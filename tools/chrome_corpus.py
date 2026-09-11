#!/usr/bin/env python
"""无头 Chrome 语料库页 E2E 验收（v3 文件夹树，中文注释纪律）。

自起一台隔离服务器（offline profile + 临时 data_dir，真实 Web 进程），
在真实 /documents 页面上走一遍用户流程：
  1. 上传真实 md 素材（DOM.setFileInputFiles 走文件选择的 change 链路）
     → 树根级出现文档行、计数 pill 变 1 篇；
  2. UI 新建文件夹（输入行 Enter 提交）→ 树顶出现文件夹行；
  3. ⋯ 菜单行内改名 → 名字原位更新（不刷新）；
  4. 查找框实时过滤：文件夹/文档按词显隐、无匹配占位、清空恢复；
  5. 选中文档 → 详情卡回填（位置=根级）；
  6. 合成 DragEvent 拖文档行入文件夹 → PATCH 生效：行带 in-folder
     归入夹内、详情卡"位置"自动跟进；
  7. Page.reload 真实刷新 → 树回折叠但数据持久：展开后文档仍在夹内；
  8. ⋯ 菜单删除 → **自定义确认框**（非浏览器原生 confirm）：标题/后果说明/
     红色确认钮/默认焦点齐备，Esc 取消且不真删；
  9. 全程收集 console 错误（含网络失败），零错误才算过；末帧截图。

用法：
  python tools/chrome_corpus.py [--out-shot tools/shots/corpus-e2e.png]
  [--server-port 8787] [--cdp-port 9333] [--chrome <chrome.exe 路径>]
退出码：0 = 验收通过；1 = 任一断言失败 / console 有错。
"""

import argparse
import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.chrome_probe import CDP, log, wait_json_list  # noqa: E402

REPO_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIKASA_EXE = REPO_ROOT / ".venv" / "Scripts" / "mikasa.exe"

# 真实上传素材：首标题决定文档名（与产品语义一致）
FIXTURE = "# L2 正则化笔记\n\nL2 正则化在损失中加入权重的平方和惩罚项，鼓励小而分散的权重。\n"


def js_quote(s: str) -> str:
    """JS 字符串字面量（跨 evaluate 传值）。"""
    return json.dumps(s, ensure_ascii=False)


def free_port() -> int:
    """向系统要一个空闲端口（避免撞上残留的旧服务/旧 Chrome）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def evaluate(cdp, expr: str):
    return await cdp.evaluate(expr)


async def wait_until(cdp, expr: str, what: str, timeout: float = 20.0) -> None:
    """轮询页面表达式直到真值；超时抛错（失败信息带轮询目标）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await cdp.evaluate(expr):
            return
        await asyncio.sleep(0.2)
    raise RuntimeError(f"超时等不到：{what}")


async def wait_server(port: int, timeout: float = 30.0) -> None:
    """等 uvicorn 起来。

    就绪判定 = 200 且响应带 folders 键——若端口上盘踞着旧构建的 Mikasa
    （无 kb-folders 端点，返回 404），绝不能把它误认成自己。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/kb-folders", timeout=1.5
            ) as resp:
                if resp.status == 200 and "folders" in resp.read().decode("utf-8"):
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.3)
    raise RuntimeError("服务器未就绪（日志见 tmpdir 下 serve.log）")


async def set_input_value(cdp, selector: str, value: str) -> None:
    """设 input 值并触发 input 事件（search 的实时过滤监听 input）。

    一律包 IIFE：evaluate 共享页面全局作用域，顶层 const 二次声明会
    SyntaxError（踩过）。
    """
    await cdp.evaluate(
        f"(() => {{ const i = document.querySelector({js_quote(selector)}); "
        f"i.value = {js_quote(value)}; "
        f"i.dispatchEvent(new Event('input', {{bubbles: true}})); return true; }})()"
    )


async def submit_input(cdp, selector: str, value: str) -> None:
    """行内输入框：设值后发 Enter（editInput 的提交路径）。"""
    await cdp.evaluate(
        f"(() => {{ const i = document.querySelector({js_quote(selector)}); "
        f"i.value = {js_quote(value)}; "
        f"i.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Enter', bubbles: true}})); "
        f"return true; }})()"
    )


async def run(args):
    tmp = Path(tempfile.mkdtemp(prefix="corpus-e2e-"))
    server = None
    chrome = None
    try:
        # 端口缺省时动态分配：避免撞上用户环境里残留的旧服务/旧 Chrome
        if args.server_port is None:
            args.server_port = free_port()
        if args.cdp_port is None:
            args.cdp_port = free_port()
        # ---- 隔离服务器：offline profile 全文 + data_dir 注入（殊途同归） ----
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
            # 起不来必有日志：先吐出来再失败（tmpdir 随后会被清理）
            log("服务器启动失败，serve.log 尾：")
            log((tmp / "serve.log").read_text(encoding="utf-8")[-3000:])
            raise

        url = f"http://127.0.0.1:{args.server_port}/documents"
        profile = tempfile.mkdtemp(prefix="corpus-chrome-")
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
                "语料页就绪",
            )
            await asyncio.sleep(1.0)  # 首载 refreshCorpusTree 网络往返

            # 0) 空库形态：引导占位 + 计数 0 篇
            empty_hint = await cdp.evaluate(
                "document.querySelector('#kb-tree .empty')?.textContent || ''"
            )
            if "还没有文档" not in empty_hint:
                bad.append(f"空库引导缺失：{empty_hint!r}")
            pill0 = await cdp.evaluate("document.querySelector('#doc-count').textContent")
            if "0 篇" not in pill0:
                bad.append(f"初始计数不是 0 篇：{pill0!r}")

            # 1) 真实文件上传（file input change 链路）
            fixture = tmp / "L2 正则化笔记.md"
            fixture.write_text(FIXTURE, encoding="utf-8")
            await cdp.call("DOM.enable")
            doc = await cdp.call("DOM.getDocument", {"depth": 1})
            q = await cdp.call(
                "DOM.querySelector",
                {"nodeId": doc["root"]["nodeId"], "selector": "#file-input"},
            )
            await cdp.call(
                "DOM.setFileInputFiles", {"nodeId": q["nodeId"], "files": [str(fixture)]}
            )
            await wait_until(
                cdp,
                "document.querySelectorAll('#kb-tree .doc-item').length === 1",
                "文档行出现在树根级（入库完成）",
            )
            row = await cdp.evaluate("""(() => {
              const r = document.querySelector('#kb-tree .doc-item');
              return {
                id: Number(r.dataset.docId),
                title: r.querySelector('.s-title').textContent,
                meta: r.querySelector('.s-meta').textContent,
                inFolder: r.classList.contains('in-folder'),
              };
            })()""")
            if row["title"] != "L2 正则化笔记":
                bad.append(f"文档标题不符：{row['title']!r}")
            if row["inFolder"] or not row["meta"].startswith("md · "):
                bad.append(f"根级文档行形态不对：{row!r}")
            doc_id = row["id"]
            if (await cdp.evaluate("document.querySelector('#doc-count').textContent")) != "1 篇":
                bad.append("上传后计数未变 1 篇")

            # 2) UI 新建根级文件夹（Enter 提交）
            await cdp.evaluate("document.querySelector('#new-kb-folder').click(); true")
            await wait_until(
                cdp, "!!document.querySelector('#kb-tree input.tree-input')", "新建输入行出现"
            )
            await submit_input(cdp, "#kb-tree input.tree-input", "复习资料")
            await wait_until(
                cdp,
                "document.querySelectorAll('#kb-tree .t-folder').length === 1",
                "文件夹行出现",
            )
            fid = await cdp.evaluate(
                "Number(document.querySelector('#kb-tree .t-folder').dataset.folderId)"
            )
            name0 = await cdp.evaluate(
                "document.querySelector('#kb-tree .t-folder .t-name').textContent"
            )
            if name0 != "复习资料":
                bad.append(f"新建文件夹名不符：{name0!r}")

            # 3) ⋯ 菜单 → 行内改名（span → input → Enter）
            await cdp.evaluate("document.querySelector('#kb-tree .t-folder .t-more').click(); true")
            await wait_until(
                cdp, "document.querySelector('#ctx-menu')?.style.display !== 'none'", "菜单弹出"
            )
            items = await cdp.evaluate(
                "[...document.querySelectorAll('#ctx-menu .ctx-item')].map(e => e.textContent)"
            )
            if "重命名" not in items:
                bad.append(f"文件夹菜单项缺失：{items}")
            await cdp.evaluate(
                "[...document.querySelectorAll('#ctx-menu .ctx-item')]"
                ".find(e => e.textContent === '重命名').click(); true"
            )
            await wait_until(
                cdp,
                "!!document.querySelector('#kb-tree input.tree-input')",
                "改名输入行出现",
            )
            await submit_input(cdp, "#kb-tree input.tree-input", "参考资料")
            # 改名提交 → 重绘是异步的：t-name 在输入态/新行之间可能短暂缺失，
            # 轮询表达式必须空值守卫（直接解引用会让 evaluate 抛异常）
            await wait_until(
                cdp,
                "document.querySelector('#kb-tree .t-folder .t-name')?.textContent === "
                + js_quote("参考资料"),
                "文件夹改名生效",
            )

            # 4) 查找过滤：命中文档/命中文件夹/无匹配占位/清空恢复
            await set_input_value(cdp, "#kb-search", "正则化")
            await asyncio.sleep(0.3)
            s1 = await cdp.evaluate(
                "document.querySelectorAll('#kb-tree .doc-item').length + '|' + "
                "document.querySelectorAll('#kb-tree .t-folder').length"
            )
            if s1 != "1|0":
                bad.append(f"搜'正则化'应只显文档（1|0）：实际 {s1}")
            await set_input_value(cdp, "#kb-search", "参考")
            await asyncio.sleep(0.3)
            s2 = await cdp.evaluate(
                "document.querySelectorAll('#kb-tree .doc-item').length + '|' + "
                "document.querySelectorAll('#kb-tree .t-folder').length"
            )
            if s2 != "0|1":
                bad.append(f"搜'参考'应只显文件夹（0|1）：实际 {s2}")
            await set_input_value(cdp, "#kb-search", "查无此词")
            await asyncio.sleep(0.3)
            s3 = await cdp.evaluate("""(() => ({
              rows: document.querySelectorAll('#kb-tree .t-folder, #kb-tree .doc-item').length,
              hint: document.querySelector('#kb-tree .empty')?.textContent || '',
            }))()""")
            if s3["rows"] != 0 or "没有匹配" not in s3["hint"]:
                bad.append(f"无匹配占位缺失：{s3}")
            await set_input_value(cdp, "#kb-search", "")
            await asyncio.sleep(0.3)
            s4 = await cdp.evaluate(
                "document.querySelectorAll('#kb-tree .doc-item').length + '|' + "
                "document.querySelectorAll('#kb-tree .t-folder').length"
            )
            if s4 != "1|1":
                bad.append(f"清空查找应全恢复（1|1）：实际 {s4}")

            # 5) 选中文档 → 右栏切到阅读区（正文 + 底部状态栏），上传/总览收起
            await cdp.evaluate("document.querySelector('#kb-tree .doc-item').click(); true")
            await wait_until(
                cdp,
                "!document.querySelector('#reading-card')?.classList.contains('hidden')"
                " && document.querySelectorAll('#reader-inline .rd-chunk').length > 0",
                "右栏切到阅读区并渲染正文",
            )
            detail = await cdp.evaluate("""(() => ({
              title: document.querySelector('#reader-inline .rd-title')?.textContent || '',
              foot: document.querySelector('#reader-inline .rd-foot')?.textContent || '',
              chunks: document.querySelectorAll('#reader-inline .rd-chunk').length,
              introHidden: document.querySelector('#intro-card')?.classList.contains('hidden'),
            }))()""")
            if detail["title"] != "L2 正则化笔记":
                bad.append(f"阅读区标题不符：{detail['title']!r}")
            if "根级（未归档）" not in detail["foot"]:
                bad.append(f"状态栏位置应为根级：{detail['foot']!r}")
            if not detail["introHidden"]:
                bad.append("选中文档后上传卡应收起（右栏让位给正文）")

            # 6) 合成 DragEvent：文档行 → 文件夹行（真实 DnD 协议序列）。
            # 选择器经 js_quote 注入，避免 JS 花括号与任何格式化语法互踩
            src_sel = f'.doc-item[data-doc-id="{doc_id}"]'
            dst_sel = f'.t-folder[data-folder-id="{fid}"]'
            drag_js = "\n".join(
                [
                    "(() => {",
                    f"  const src = document.querySelector({js_quote(src_sel)});",
                    f"  const dst = document.querySelector({js_quote(dst_sel)});",
                    "  const dt = new DataTransfer();",
                    "  const ev = (type) =>",
                    "    new DragEvent(type, {bubbles: true, dataTransfer: dt});",
                    "  src.dispatchEvent(ev('dragstart'));",
                    "  dst.dispatchEvent(ev('dragover'));",
                    "  dst.dispatchEvent(ev('drop'));",
                    "  src.dispatchEvent(ev('dragend'));",
                    "  return true;",
                    "})()",
                ]
            )
            await cdp.evaluate(drag_js)
            # PATCH 在途：等行归入夹内（auto-expand 后带 in-folder 可见）
            in_folder_expr = (
                "document.querySelector(" + js_quote(src_sel) + ")"
                "?.classList.contains('in-folder') === true"
            )
            await wait_until(cdp, in_folder_expr, "拖拽后文档行归入文件夹")
            await asyncio.sleep(0.5)
            api_state = await cdp.evaluate(
                "fetch('/api/documents').then(r => r.json()).then(b => b.documents[0])"
            )
            if api_state.get("folder_id") != fid:
                bad.append(f"拖拽后 API folder_id 应为 {fid}：实际 {api_state.get('folder_id')}")
            # 状态栏"位置"自动跟进（notifyActive 经 onSelect → updateLocation）
            pos_after = await cdp.evaluate(
                "document.querySelector('#reader-inline .rd-foot')?.textContent || ''"
            )
            if "参考资料" not in pos_after:
                bad.append(f"状态栏位置应随拖拽变'参考资料'：{pos_after!r}")

            # 7) 真实刷新：树回折叠、数据持久（文档仍归属文件夹）
            await cdp.call("Page.reload", {"ignoreCache": True})
            await wait_until(
                cdp,
                "document.readyState === 'complete' && "
                "document.querySelectorAll('#kb-tree .t-folder').length === 1",
                "刷新后页面就绪",
            )
            await asyncio.sleep(0.8)
            # 折叠态：夹在、文档行不在（还没展开）
            coll = await cdp.evaluate("document.querySelectorAll('#kb-tree .doc-item').length")
            if coll != 0:
                bad.append("刷新后文件夹应折叠（0 文档行可见）")
            # 点开文件夹 → 文档还在夹内；点文档 → 状态栏位置仍'参考资料'
            await cdp.evaluate("document.querySelector('#kb-tree .t-folder').click(); true")
            await wait_until(
                cdp,
                "document.querySelectorAll('#kb-tree .doc-item.in-folder').length === 1",
                "展开后夹内文档行可见",
            )
            pill1 = await cdp.evaluate("document.querySelector('#doc-count').textContent")
            if pill1 != "1 篇":
                bad.append(f"刷新后计数丢失：{pill1!r}")
            await cdp.evaluate("document.querySelector('#kb-tree .doc-item').click(); true")
            await asyncio.sleep(0.6)
            pos_kept = await cdp.evaluate(
                "document.querySelector('#reader-inline .rd-foot')?.textContent || ''"
            )
            if "参考资料" not in pos_kept:
                bad.append(f"刷新后状态栏位置丢失：{pos_kept!r}")

            # 8) 删除确认框 = 自定义组件（2026-09-10 替换浏览器原生 confirm）
            await cdp.evaluate("document.querySelector('#kb-tree .doc-item .t-more').click(); true")
            await wait_until(cdp, "!!document.querySelector('#ctx-menu')", "行尾 ⋯ 菜单打开")
            await cdp.evaluate("""(() => {
              const del = [...document.querySelectorAll('#ctx-menu *')]
                .reverse().find(e => e.textContent.trim() === '删除' && !e.children.length);
              if (del) (del.closest('button') || del).click();
            })()""")
            await wait_until(
                cdp, "!!document.querySelector('.confirm-box')", "自定义删除确认框出现"
            )
            cf = await cdp.evaluate("""(() => ({
              title: document.querySelector('.cf-title')?.textContent || '',
              detail: document.querySelector('.cf-detail')?.textContent || '',
              ok: document.querySelector('.cf-actions .btn.danger')?.textContent || '',
              hasNativeBackdropOnly: !document.querySelector('.confirm-backdrop')
                ? 'no-backdrop' : 'ok',
              focused: document.activeElement?.textContent || '',
            }))()""")
            if "删除文档《" not in cf["title"]:
                bad.append(f"确认框标题不对：{cf['title']!r}")
            if "不可恢复" not in cf["detail"]:
                bad.append(f"确认框缺少后果说明：{cf['detail']!r}")
            if cf["ok"] != "确认删除" or cf["hasNativeBackdropOnly"] != "ok":
                bad.append(f"确认框按钮/遮罩形态不对：{cf}")
            if cf["focused"] != "确认删除":
                bad.append(f"确认框默认焦点不在确认钮：{cf['focused']!r}")
            # Esc 取消：框消失且**不真删**（文档行数不变）
            await cdp.call(
                "Input.dispatchKeyEvent",
                {"type": "keyDown", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
            )
            await wait_until(cdp, "!document.querySelector('.confirm-backdrop')", "Esc 取消确认框")
            if await cdp.evaluate("document.querySelectorAll('#kb-tree .doc-item').length") != 1:
                bad.append("取消删除后文档行数变了（不该真删）")

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
            tail = (tmp / "serve.log").read_text(encoding="utf-8")[-2000:]
            log("服务日志尾：\n" + tail)
            return 1
        log("语料页 E2E 验收通过")
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
    parser = argparse.ArgumentParser(description="无头 Chrome 语料库页 E2E 验收")
    parser.add_argument("--out-shot", default=None, help="截图输出路径（PNG）")
    parser.add_argument(
        "--server-port",
        type=int,
        default=None,
        help="隔离服务器端口（缺省动态分配空闲端口）",
    )
    parser.add_argument(
        "--cdp-port", type=int, default=None, help="Chrome 调试端口（缺省动态分配）"
    )
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
