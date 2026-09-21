#!/usr/bin/env python
"""无头 Chrome 阅读面板 E2E 验收（2026-09-10，阅读视图 + 引用跳转）。

自起一台隔离服务器（offline profile + 临时 data_dir，真实 Web 进程），
在真实页面上走一遍用户流程：
  1. 上传一篇 md（真实 file input change 链路，与 chrome_corpus 同法）；
  2. **知识库页（内嵌形态）**点文档行 → 右栏切到阅读区、标题正确、
     首块含锚句（正文来自 /content 的拼 chunk）、底部状态栏有元数据；
  3. 切"原文件"标签 → 显示 md 原文且**含 `#` 标题行**——这条同时证明
     "文本视图丢标题行"是拼 chunk 的既有事实而非渲染 bug（原文件是兜底出口）；
  4. **问答页（浮层形态）**提问 → 等流式收尾 → 点 `[n]` 角标 → 浮层
     `#reader-panel` 打开并定位到该块（`.rd-chunk.lit`），且消息内的
     `.cite.lit` 旧行为不回归；
  5. Esc 关闭浮层；
  6. 全程收集 console 错误，零错误才算过；末帧截图。

就绪判定用 `/api/chunks/999999` 的 **detail 文案**而不是状态码：旧构建没这个
路由（返回 `{"detail":"Not Found"}`），绝不能把它误认成自己的服务。

用法：
  python tools/chrome_reader.py [--out-shot tools/shots/reader-e2e.png]
  [--server-port 8787] [--cdp-port 9333] [--chrome <chrome.exe 路径>]
退出码：0 = 验收通过；1 = 任一断言失败 / console 有错。
"""

import argparse
import asyncio
import base64
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
    submit_input,
    wait_until,
)
from tools.chrome_probe import CDP, log, wait_json_list  # noqa: E402

REPO_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIKASA_EXE = REPO_ROOT / ".venv" / "Scripts" / "mikasa.exe"

# 真实上传素材：首标题决定文档名；锚句用于断言"定位到的是真那一块"
ANCHOR = "L2 正则化在损失中加入权重的平方和惩罚项"
FIXTURE = (
    "# L2 正则化笔记\n\n"
    f"{ANCHOR}，鼓励小而分散的权重。\n\n"
    "## 为什么防过拟合\n\n"
    "权重越小，模型对输入扰动的敏感度越低，假设空间越平滑。\n"
)
QUESTION = "L2 正则化为什么能防止过拟合？"


async def wait_new_server(port: int, timeout: float = 30.0) -> None:
    """等隔离服务器就绪；用新路由的 detail 文案区分新旧构建。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/chunks/999999", timeout=1.5
            ) as resp:
                if "原文片段不存在" in resp.read().decode("utf-8"):
                    return
        except urllib.error.HTTPError as exc:  # 404 也带 body，要读 detail
            if "原文片段不存在" in exc.read().decode("utf-8"):
                return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.3)
    raise RuntimeError("服务器未就绪或不是含阅读端点的构建（见 tmpdir/serve.log）")


async def run(args):
    tmp = Path(tempfile.mkdtemp(prefix="reader-e2e-"))
    server = None
    chrome = None
    bad: list[str] = []
    try:
        if args.server_port is None:
            args.server_port = free_port()
        if args.cdp_port is None:
            args.cdp_port = free_port()

        # ---- 隔离服务器（offline profile + 注入 data_dir，与 chrome_corpus 同法） ----
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
        profile = tempfile.mkdtemp(prefix="reader-chrome-")
        chrome = subprocess.Popen(
            [
                args.chrome,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                f"--remote-debugging-port={args.cdp_port}",
                f"--user-data-dir={profile}",
                "--window-size=1400,900",
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

            # ---- 1) 上传素材（真实 file input 链路） ----
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
                "文档行出现（入库完成）",
            )

            # ---- 2) 点文档行 → 右栏内嵌阅读区打开（不弹浮层，见 reader.js） ----
            await cdp.evaluate("document.querySelector('#kb-tree .doc-item').click()")
            await wait_until(
                cdp,
                "(() => { const c = document.querySelector('#reading-card');"
                " return c && !c.classList.contains('hidden')"
                " && document.querySelectorAll('#reader-inline .rd-chunk').length > 0; })()",
                "右栏阅读区打开且正文渲染完成",
            )
            title = await cdp.evaluate(
                "document.querySelector('#reader-inline .rd-title').textContent"
            )
            if "L2 正则化笔记" not in title:
                bad.append(f"阅读区标题不对：{title!r}")
            chunks = await cdp.evaluate(
                "document.querySelectorAll('#reader-inline .rd-chunk').length"
            )
            if chunks < 1:
                bad.append(f"阅读区没有正文块：{chunks}")
            first = await cdp.evaluate(
                "document.querySelector('#reader-inline .rd-chunk').textContent"
            )
            if ANCHOR not in first:
                bad.append(f"首块不含锚句：{first[:60]!r}")
            heads = await cdp.evaluate(
                "document.querySelectorAll('#reader-inline .rd-head-mark').length"
            )
            if heads < 1:
                bad.append("md 文档应渲染出小标题（heading_path）")
            foot = await cdp.evaluate(
                "document.querySelector('#reader-inline .rd-foot')?.textContent || ''"
            )
            if "块" not in foot or "字符" not in foot:
                bad.append(f"内嵌状态栏缺少元数据：{foot!r}")

            # ---- 2.5) PDF：合并"页面"视图（原样渲染 + 页码导航 + 字号钮） ----
            import pymupdf as fitz_lib

            pdf_fixture = tmp / "页面样本.pdf"
            pdf_doc = fitz_lib.open()
            page = pdf_doc.new_page()
            page.insert_text((72, 96), "Page one: helical anchor capacity degrades with cycles.")
            pdf_doc.save(pdf_fixture)
            pdf_doc.close()
            q = await cdp.call(
                "DOM.querySelector",
                {"nodeId": doc["root"]["nodeId"], "selector": "#file-input"},
            )
            await cdp.call(
                "DOM.setFileInputFiles", {"nodeId": q["nodeId"], "files": [str(pdf_fixture)]}
            )
            await wait_until(
                cdp,
                "document.querySelectorAll('#kb-tree .doc-item').length === 2",
                "PDF 文档行出现",
            )
            await cdp.evaluate(
                "(() => { const rows=[...document.querySelectorAll('#kb-tree .doc-item')];"
                " const row = rows.find(r => r.textContent.includes('页面样本'));"
                " if (row) row.click(); })()"
            )
            await wait_until(
                cdp,
                "(() => { const i = document.querySelector('#reader-inline .rd-page-img');"
                " return !!i && i.naturalWidth > 0; })()",
                "PDF 打开默认落在页面视图且页面图加载完成",
            )
            pdf_state = await cdp.evaluate("""(() => {
              const label = document.querySelector('#reader-inline .rd-page-label');
              const open = document.querySelector('#reader-inline .rd-page-open');
              return {
                tabs: ['rd-tab-text', 'rd-tab-page', 'rd-tab-file'].map((id) => {
                  const b = document.getElementById(id);
                  if (!b) return id + ':missing';
                  const vis = b.classList.contains('hidden') ? 'hidden' : 'shown';
                  return b.textContent + ':' + vis;
                }),
                label: label ? label.textContent : '',
                openHref: open ? open.getAttribute('href') : '',
                zoomControls: ['rd-zoom-out', 'rd-zoom-in', 'rd-zoom-reset'].every(
                  (id) => !!document.getElementById(id)),
                labels: [...document.querySelectorAll('#reader-inline .rd-font-btn')]
                  .map((b) => b.textContent),
              };
            })()""")
            # PDF 有三个标签：文本（隐藏）/ 页面（当前）/ 原文件。
            # 旧断言读的是 `#rd-tab-file` 并期望"页面"——那是"原文件"标签还没恢复
            # 时的 UI，留到今天只会永远红（2026-09-11 排查踩到）。
            if pdf_state["tabs"] != ["文本:hidden", "页面:shown", "原文件:shown"]:
                bad.append(f"PDF 标签可见性不对：{pdf_state['tabs']!r}")
            if "/" not in pdf_state["label"]:
                bad.append(f"页面标签缺少页数：{pdf_state['label']!r}")
            # 页面视图的控件已从"字号钮"改为"缩放"（2026-09-11：页面图是位图，
            # 字号对它无效），这里的断言跟着改——留旧选择器会让工具永远红。
            if not pdf_state["zoomControls"]:
                bad.append("页面视图缺少缩放控件（缩小/放大/原始大小）")
            if pdf_state["labels"]:
                bad.append(f"缩放控件不该再有字号钮：{pdf_state['labels']!r}")

            # ---- 可选：把面板的 DOM 结构抄下来（--dump-dom）----
            # 为什么需要：写 E2E 断言的人（或另一个 agent）未必跑得起来浏览器
            # （沙箱里 CDP 会被拦），而"猜选择器"是这类测试最常见的返工来源。
            # 这里输出的是**真实渲染后**的树：tag#id.class + 短文本，够写断言。
            if args.dump_dom is not None:
                tree = await cdp.evaluate(
                    # r-string：里面的正则与 \n 要原样交给 JS（普通字符串会把
                    # \n 提前变成真换行，JS 侧直接语法错误——这里踩过一次）
                    r"""(() => {
                      // 两种形态都要认：知识库页是内嵌（#reader-inline），
                      // 问答页是浮层（#reader-panel）——只写一个会抄到空
                      const root = document.querySelector('#reader-panel')
                        || document.querySelector('#reader-inline')
                        || document.querySelector('#reading-card');
                      if (!root) return '(这一页没有阅读器容器)';
                      const lines = [];
                      const SHOW_TEXT = new Set(['BUTTON','SPAN','LABEL','H3','H4','DIV','P']);
                      const walk = (node, depth) => {
                        if (depth > 6 || lines.length > 220) return;
                        for (const child of node.children) {
                          const id = child.id ? '#' + child.id : '';
                          const cls = child.className && typeof child.className === 'string'
                            ? '.' + child.className.trim().split(/\s+/).join('.') : '';
                          let text = '';
                          if (SHOW_TEXT.has(child.tagName)) {
                            text = (child.childElementCount === 0 ? child.textContent : '')
                              .trim().slice(0, 28);
                          }
                          lines.push('  '.repeat(depth) + child.tagName.toLowerCase() + id + cls
                            + (text ? '  «' + text + '»' : '')
                            + (child.classList.contains('hidden') ? '  [hidden]' : ''));
                          walk(child, depth + 1);
                        }
                      };
                      walk(root, 0);
                      return lines.join('\n');
                    })()"""
                )
                if args.dump_dom:
                    Path(args.dump_dom).write_text(tree, encoding="utf-8")
                    log(f"DOM 结构已写入：{args.dump_dom}")
                else:
                    print(tree)

            # ---- 2.6) 「识别本页」（A 档 d，2026-09-21）----
            # 隔离服务器跑的是 offline 档（没有任何视觉模型）→ 识图端点回 400 与
            # 那句可照做的指引。所以这条断言验的是**按钮接上了、点了会如实说话**：
            # 真识图效果由 tools/smoke_ollama_vision.py（真机 + 真模型）覆盖，
            # 这里不引入外部服务（E2E 的纪律：不发真请求到第三方）。
            ocr_label = await cdp.evaluate(
                "document.querySelector('#rd-ocr')?.textContent?.trim() || ''"
            )
            if ocr_label != "识别本页":
                bad.append(f"页面视图缺少「识别本页」按钮：{ocr_label!r}")
            await cdp.evaluate("document.querySelector('#rd-ocr')?.click()")
            await wait_until(
                cdp,
                "(() => { const t = document.querySelector('#toast')?.textContent || '';"
                " return t.includes('视觉模型') || t.includes('识别失败'); })()",
                "点「识别本页」后的提示",
            )
            ocr_hint = await cdp.evaluate("document.querySelector('#toast')?.textContent || ''")
            if "视觉模型" not in ocr_hint:
                # 未接入视觉模型时必须给出"怎么接"的原话，而不是一句"识别失败"了事
                bad.append(f"未接入视觉模型时应给出可照做的提示，实得：{ocr_hint!r}")

            # ---- 3) 切回 md 文档，再切"原文件"标签：md 原文含 # 标题行 ----
            # 注意：**不能**用 `#rd-tab-file` 文案判断"有没有切回 md"——PDF 的第二
            # 个标签文案同样是「原文件」，切失败也照样通过，于是错误被推迟到几步之后
            # 才以一个莫名其妙的超时爆出来（2026-09-11 排查踩到）。改为断言阅读器
            # 标题变成 md 的标题。
            clicked = await cdp.evaluate(
                "(() => { const rows=[...document.querySelectorAll('#kb-tree .doc-item')];"
                " const row = rows.find(r => r.textContent.includes('L2 正则化笔记'));"
                " if (!row) return false; row.click(); return true; })()"
            )
            if not clicked:
                bad.append("语料树里找不到 md 文档行，无法切回（后续断言全不可信）")
            await wait_until(
                cdp,
                "document.querySelector('#reader-inline .rd-title')?.textContent"
                " === 'L2 正则化笔记'",
                "阅读器标题切回 md 文档",
            )
            await cdp.evaluate("document.querySelector('#rd-tab-file').click()")
            try:
                await wait_until(
                    cdp,
                    "!!document.querySelector('#reader-inline .rd-raw')",
                    "原文件视图渲染完成",
                    timeout=10.0,
                )
            except RuntimeError:
                log(
                    "原文件视图现场："
                    + str(
                        await cdp.evaluate(
                            "(() => {"
                            " const o = document.querySelector('#reader-inline .rd-orig');"
                            " const ti = document.querySelector('#reader-inline .rd-title');"
                            " return JSON.stringify({ title: ti && ti.textContent,"
                            " built: o && o.dataset.built,"
                            " inner: ((o && o.innerHTML) || '').slice(0, 120) });"
                            " })()"
                        )
                    )
                )
                raise
            raw = await cdp.evaluate("document.querySelector('#reader-inline .rd-raw').textContent")
            if "# L2 正则化笔记" not in raw:
                bad.append("原文件视图应显示含标题行的 md 原文")
            if ANCHOR not in raw:
                bad.append("原文件视图缺失正文锚句")

            # ---- 4) 问答页点引用角标 → 面板定位 ----
            await cdp.call("Page.navigate", {"url": f"{base}/"})
            await wait_until(
                cdp,
                "document.readyState === 'complete' && !!document.querySelector('#question')",
                "问答页就绪",
            )
            await asyncio.sleep(0.8)
            await submit_input(cdp, "#question", QUESTION)
            await wait_until(
                cdp,
                "document.querySelectorAll('.msg.assistant .bubble .cite:not(.bad)').length > 0"
                " && !document.querySelector('.msg.assistant.streaming')",
                "回答完成且带引用角标",
            )
            await cdp.evaluate(
                "document.querySelector('.msg.assistant .bubble .cite:not(.bad)').click()"
            )
            await wait_until(
                cdp,
                "(() => { const p = document.querySelector('#reader-panel');"
                " return p && !p.classList.contains('hidden')"
                " && !!document.querySelector('#reader-panel .rd-chunk.lit'); })()",
                "面板定位到引用块（.rd-chunk.lit）",
            )
            lit = await cdp.evaluate(
                "document.querySelector('#reader-panel .rd-chunk.lit').textContent"
            )
            if ANCHOR not in lit:
                bad.append(f"定位到的高亮块不含锚句：{lit[:60]!r}")
            # 旧行为不回归：消息内引用卡同时高亮（chip 获得 .lit）
            if not await cdp.evaluate(
                "!!document.querySelector('.msg.assistant .bubble .cite.lit,"
                " .msg.assistant .cite.lit')"
            ):
                bad.append("消息内引用 chip 的 .lit 高亮丢了（旧行为回归）")

            # ---- 截图：面板开着、引用块高亮（验收证据就是这一帧） ----
            if args.out_shot:
                data = await cdp.call("Page.captureScreenshot", {"format": "png"})
                Path(args.out_shot).parent.mkdir(parents=True, exist_ok=True)
                Path(args.out_shot).write_bytes(base64.b64decode(data["data"]))
                log(f"截图已写: {args.out_shot}")

            # ---- 5) Esc 关闭 ----
            await cdp.call(
                "Input.dispatchKeyEvent",
                {"type": "keyDown", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
            )
            await wait_until(
                cdp,
                "document.querySelector('#reader-panel').classList.contains('hidden')",
                "Esc 关闭面板",
            )

            # console 体检：**预期内的 4xx 要精确豁免，别的一律不放过**。
            # 2.6 段点「识别本页」时，隔离服务器是 offline 档（没有视觉模型）→
            # /api/notes/ocr 如实回 400 → 浏览器在 console 记一条 "Failed to load
            # resource … 400"。那条 400 正是那一段在断言的行为（"点了会如实说话"），
            # 不豁免的话 E2E 会因为"验的东西本身"而红（2026-09-21 实测到）。
            # 豁免只按 URL + 状态码匹配，其他任何 console 错误照旧判失败。
            allowed = [e for e in cdp.errors if "/api/notes/ocr" in e and "400" in e]
            errors = [e for e in cdp.errors if e not in allowed]
            if errors:
                bad.append(f"console 有 {len(errors)} 条错误：{errors[:3]}")
    finally:
        if chrome is not None:
            chrome.kill()
        if server is not None:
            server.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    if bad:
        for b in bad:
            log(f"✗ {b}")
        return 1
    log("✓ chrome_reader：阅读面板全链路验收通过（console 零错误）")
    return 0


def main():
    parser = argparse.ArgumentParser(description="无头 Chrome 阅读面板 E2E 验收")
    parser.add_argument("--out-shot", default=None, help="截图输出路径（PNG）")
    parser.add_argument(
        "--dump-dom",
        nargs="?",
        const="",
        default=None,
        help="把阅读器面板的 DOM 结构打到 stdout（或指定文件）——写断言前先看结构用",
    )
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
