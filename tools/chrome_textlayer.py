#!/usr/bin/env python
"""无头 Chrome 验收：连续滚动模式的"按需文字层"（2026-09-11）。

背景：页面视图是位图，字选不中；文字层把 PDF 真实文字透明铺在图上，选中即
真文本。但连续滚动一次铺满 = 每页一两百个 span × 几百页，DOM 扛不住——原实现
因此在连续模式下**干脆不铺**，代价是"连续滚动时选不中、复制不了"
（2026-09-11 用户实测反馈）。改成按需铺/滚远撤之后，本脚本守住三件事：

  1. **能选中**：用真实鼠标拖拽（CDP Input.dispatchMouseEvent）划过文字层，
     再用 Selection API 读回选中文本——**不用 Selection API 直接造选区**，
     因为它能绕过 `user-select: none`，那样测出来的是"假通过"；
  2. **没铺满**：文字层数量必须远小于总页数（虚拟化真的生效）；
  3. **滚远回收 + 滚到就铺**：滚到中段后，首页文字层被撤、中段页有文字层。

夹具用 PyMuPDF 现场生成 24 页中文 PDF（素材只有 3 页，撑不出虚拟化）。

用法：
  python tools/chrome_textlayer.py [--out-shot tools/shots/textlayer-e2e.png]
  [--pages 24] [--server-port N] [--cdp-port N] [--chrome <chrome.exe 路径>]
退出码：0 = 验收通过；1 = 任一断言失败 / console 有错。

注意：本工具会启动**独立**的离线服务与临时数据目录，不碰仓库 data/。
"""

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.chrome_corpus import free_port, wait_server, wait_until  # noqa: E402
from tools.chrome_probe import CDP, log, wait_json_list  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
MIKASA_EXE = REPO_ROOT / ".venv" / "Scripts" / "mikasa.exe"
INLINE = "#reader-inline"  # 知识库页的右栏内嵌阅读区（不弹浮层）
SEL_PAGES = f"{INLINE} .rd-page-fit"
SEL_LAYERS = f"{INLINE} .rd-textlayer"
SEL_CANVAS = f"{INLINE} .rd-page-canvas"


def make_pdf(path: Path, pages: int) -> None:
    """生成 pages 页可提取中文的 PDF（china-s = PyMuPDF 内置简体字体）。

    页面**刻意做矮**（400x300pt ≈ 611x458px @110dpi）：跨页拖选要在一屏之内跨过
    页边界才好测——信纸尺寸下下一页的起点在视口外，拖到边缘会触发自动滚动，
    断言就跟着滚动时序抖起来了。
    """
    import pymupdf

    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=400, height=300)
        page.insert_text(
            (30, 60), f"第 {i + 1} 页：连续滚动文字层验收", fontname="china-s", fontsize=16
        )
        page.insert_text(
            (30, 100),
            f"这是第 {i + 1} 页的正文段落，用来验证拖动选择选到的是真文字。",
            fontname="china-s",
            fontsize=11,
        )
    doc.save(str(path))
    doc.close()


async def drag_select(cdp, x0: float, y0: float, x1: float, y1: float) -> str:
    """在 (x0,y0) 按下鼠标、移动到 (x1,y1)、松开，返回浏览器认到的选中文本。

    **别指望它验证"拖拽延伸选区"**：无头 Chrome 里这套模拟**不会延伸选区**——
    实测起点落在第 13 页文字上、终点落在第 14 页文字上，`anchor`/`focus` 却都
    停在第 13 页那一行（加不加 `buttons: 1` 都一样）。所以它实际验证的是
    "**文字层在这一点上可被选中**"：`user-select: none` 的层按下松开什么都选
    不到，能选出字就说明这层是可选的真文字。跨页那种"整页变蓝"改用
    `GUARD_JS`（可选性计算值）+ `RANGE_MECHANISM_JS`（DOM 顺序）来守。
    """
    # **先清空已有选区**：不清的话，如果这次拖拽根本没生效（比如被某个
    # preventDefault 吞掉），getSelection() 返回的是**上一次残留的选区**，
    # 断言就会假装通过——负向对照实测踩中过（2026-09-11）。
    await cdp.evaluate("window.getSelection().removeAllRanges(); true")
    await cdp.call(
        "Input.dispatchMouseEvent",
        {"type": "mousePressed", "x": x0, "y": y0, "button": "left", "buttons": 1, "clickCount": 1},
    )
    steps = 12  # 分步移动：浏览器要看到带按键的 mousemove 才把它当拖选
    for i in range(1, steps + 1):
        await cdp.call(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseMoved",
                "x": x0 + (x1 - x0) * i / steps,
                "y": y0 + (y1 - y0) * i / steps,
                "button": "left",
                "buttons": 1,  # 左键持续按住——漏了它就不是"拖"
            },
        )
        await asyncio.sleep(0.02)
    await cdp.call(
        "Input.dispatchMouseEvent",
        {
            "type": "mouseReleased",
            "x": x1,
            "y": y1,
            "button": "left",
            "buttons": 0,
            "clickCount": 1,
        },
    )
    return await cdp.evaluate("String(window.getSelection())")


def span_box_js(selector: str) -> str:
    """取**视口内**第一个 span 的矩形（拖拽用的是屏幕坐标，滚出视口的点拖不到）。

    不能只取 `querySelector` 的第一个：连续滚动下文字层有几页，排在最前的那页
    可能已经在视口上方（中段拖选正是这么失败的——返回空选区）。
    """
    return f"""(() => {{
      for (const el of document.querySelectorAll({json.dumps(selector)})) {{
        const r = el.getBoundingClientRect();
        if (r.width > 4 && r.height > 4 && r.top >= 0 && r.bottom <= window.innerHeight) {{
          return JSON.stringify({{x: r.left, y: r.top, w: r.width, h: r.height}});
        }}
      }}
      return '';
    }})()"""


RANGE_MECHANISM_JS = f"""(() => {{
  const fits = [...document.querySelectorAll('{SEL_PAGES}')].sort(
    (a, b) => Number(a.dataset.page) - Number(b.dataset.page)
  );
  for (let i = 0; i + 1 < fits.length; i++) {{
    const a = fits[i], b = fits[i + 1];
    const sa = a.querySelector('.rd-textlayer span');
    const sb = b.querySelector('.rd-textlayer span');
    if (!sa || !sb || !sa.firstChild || !sb.firstChild) continue;
    const r = document.createRange();
    r.setStart(sa.firstChild, 0);
    r.setEnd(sb.firstChild, sb.firstChild.length);
    const img = b.querySelector('.rd-page-img');
    return JSON.stringify({{
      a: a.dataset.page, b: b.dataset.page,
      imgInRange: img ? r.intersectsNode(img) : null,
    }});
  }}
  return '{{}}';
}})()"""

GUARD_JS = f"""(() => {{
  const us = (s) => {{
    const el = document.querySelector(s);
    return el ? getComputedStyle(el).userSelect : '(缺失)';
  }};
  return JSON.stringify({{
    fit: us('{SEL_PAGES}'),
    img: us('{INLINE} .rd-page-img'),
    layer: us('{INLINE} .rd-page-layer'),
    text: us('{INLINE} .rd-textlayer span'),
  }});
}})()"""


async def run(args) -> int:
    bad: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="textlayer-e2e-"))
    server = chrome = None
    try:
        if args.server_port is None:
            args.server_port = free_port()
        if args.cdp_port is None:
            args.cdp_port = free_port()
        pdf = tmp / "连续滚动验收.pdf"
        make_pdf(pdf, args.pages)

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
        await wait_server(args.server_port)

        url = f"http://127.0.0.1:{args.server_port}/documents"
        chrome = subprocess.Popen(
            [
                args.chrome,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                f"--remote-debugging-port={args.cdp_port}",
                f"--user-data-dir={tempfile.mkdtemp(prefix='textlayer-chrome-')}",
                "--window-size=1400,900",
                url,
            ]
        )
        target = wait_json_list(args.cdp_port)
        async with CDP(target["webSocketDebuggerUrl"]) as cdp:
            await cdp.call("Page.enable")
            await cdp.call("Log.enable")
            await cdp.call("DOM.enable")
            await wait_until(
                cdp,
                "document.readyState === 'complete' && !!document.querySelector('#kb-tree')",
                "知识库页就绪",
            )
            # 空库打开会弹首启引导面板，它**盖住整页**：不先关掉，后面的鼠标拖拽
            # 全会落在面板上（实测选中的是面板文案），测出来的不是阅读器。
            # 它有竞态：面板等 /api/health 回来才挂上，可能晚于"页面就绪"，
            # 所以先**等它出现**再关，不能只 if 一下（那样会漏掉、后面全程被盖）。
            try:
                await wait_until(
                    cdp, "!!document.querySelector('.onboard-mask')", "首启引导出现", timeout=10.0
                )
            except RuntimeError:
                bad.append("空库没有弹出首启引导面板")
            else:
                await cdp.evaluate(
                    "document.querySelector('.onboard-card .btn.primary').click(); true"
                )
                await wait_until(cdp, "!document.querySelector('.onboard-mask')", "首启引导已关闭")

            # ---- 上传夹具 PDF ----
            doc_root = await cdp.call("DOM.getDocument", {"depth": 1})
            q = await cdp.call(
                "DOM.querySelector",
                {"nodeId": doc_root["root"]["nodeId"], "selector": "#file-input"},
            )
            await cdp.call("DOM.setFileInputFiles", {"nodeId": q["nodeId"], "files": [str(pdf)]})
            await wait_until(
                cdp,
                "document.querySelectorAll('#kb-tree .doc-item').length === 1",
                "PDF 入库完成",
            )

            # ---- 打开阅读区 → 页面视图（单页模式先铺一层，作为对照） ----
            await cdp.evaluate("document.querySelector('#kb-tree .doc-item').click()")
            await wait_until(cdp, f"!!document.querySelector('{INLINE}')", "阅读区打开")
            await cdp.evaluate("document.querySelector('#rd-tab-page').click()")
            await wait_until(
                cdp, f"document.querySelectorAll('{SEL_PAGES}').length === 1", "单页模式就绪"
            )
            await wait_until(
                cdp, f"document.querySelectorAll('{SEL_LAYERS}').length === 1", "单页文字层已铺"
            )

            single_sel = await drag_select_layer(cdp, f"{SEL_LAYERS} span")
            if "页" not in single_sel:
                bad.append(f"单页模式拖选没有选到文字：{single_sel!r}")

            # ---- 切连续滚动：页节点全建，文字层按需铺 ----
            await cdp.evaluate("document.querySelector('#rd-mode').click()")
            await wait_until(
                cdp,
                f"document.querySelectorAll('{SEL_PAGES}').length === {args.pages}",
                "连续模式：页节点全建",
            )
            await wait_until(
                cdp, f"document.querySelectorAll('{SEL_LAYERS}').length > 0", "文字层出现"
            )

            m = json.loads(
                await cdp.evaluate(
                    f"""(() => {{
                      const span = document.querySelector('{SEL_LAYERS} span');
                      return JSON.stringify({{
                        pages: document.querySelectorAll('{SEL_PAGES}').length,
                        layers: document.querySelectorAll('{SEL_LAYERS}').length,
                        spans: document.querySelectorAll('{SEL_LAYERS} span').length,
                        userSelect: span ? getComputedStyle(span).userSelect : '',
                      }});
                    }})()"""
                )
            )
            log(f"连续模式指标：{m}")
            if m["spans"] <= 0:
                bad.append("文字层铺了但没有 span")
            # 虚拟化：24 页里常驻的应该只有视口附近几页（放宽到 1/2 页数即可判定）
            if m["layers"] >= args.pages / 2:
                bad.append(f"文字层几乎铺满（{m['layers']}/{m['pages']}），虚拟化没生效")
            if m["userSelect"] == "none":
                bad.append("文字层的 user-select 是 none —— 用户拖不出选区")

            # 视口内的某一页：拖选必须选到真文字
            cont_sel = await drag_select_layer(cdp, f"{SEL_LAYERS} span")
            if "页" not in cont_sel:
                bad.append(f"连续模式拖选没有选到文字：{cont_sel!r}")

            # ---- 滚到中段：滚到的页要铺、滚远的页要撤 ----
            first_had = await cdp.evaluate(
                f"!!document.querySelector('{SEL_PAGES}[data-page=\"1\"] .rd-textlayer')"
            )
            await cdp.evaluate(
                f"""(() => {{
                  const c = document.querySelector('{SEL_CANVAS}');
                  c.scrollTop = c.scrollHeight * 0.5;
                  return true;
                }})()"""
            )
            mid = args.pages // 2
            await wait_until(
                cdp,
                f"!!document.querySelector('{SEL_PAGES}[data-page=\"{mid}\"] .rd-textlayer')",
                "滚到中段后该页文字层已铺",
                timeout=15.0,
            )
            await wait_until(
                cdp,
                f"!document.querySelector('{SEL_PAGES}[data-page=\"1\"] .rd-textlayer')",
                "首页文字层已回收",
                timeout=15.0,
            )
            if not first_had:
                bad.append("滚到中段前，首页本该有文字层（夹具或视口不对）")

            # 中段再拖选一次：滚过去的页也要能选
            mid_sel = await drag_select_layer(cdp, f"{SEL_LAYERS} span")
            if "页" not in mid_sel:
                bad.append(f"滚到中段后拖选没有选到文字：{mid_sel!r}")

            # ---- 放大后仍要能选字 ----
            # 用户实测：放大之后一个字都选不中。根因是"拖动平移"的 mousedown
            # **无条件 preventDefault**，把左键拖拽整个吞掉——而平移与拖选天然
            # 互斥，修法是让起点落在文字上时交给选择。这里用真实拖拽守住。
            await cdp.evaluate(
                "document.getElementById('rd-zoom-in').click();"
                "document.getElementById('rd-zoom-in').click(); true"
            )
            await asyncio.sleep(0.8)
            if not await cdp.evaluate(
                f"document.querySelector('{SEL_CANVAS}').classList.contains('pannable')"
            ):
                bad.append("点了放大但画布没进可平移状态（缩放没生效？）")
            zoom_sel = await drag_select_layer(cdp, f"{SEL_LAYERS} span")
            if "页" not in zoom_sel:
                bad.append(f"放大后拖选没有选到文字：{zoom_sel!r}")
            # 复原缩放，免得影响后面的断言
            await cdp.evaluate("document.getElementById('rd-zoom-reset').click(); true")
            await asyncio.sleep(0.5)

            # ---- 跨页拖选"整页变蓝"的防护 ----
            # 机制（Range API，与 user-select 无关）：从 A 页文字跨到 B 页文字的选区
            # **确实会把 B 页的 <img> 框进去**——位图一旦可被用户选中，浏览器就会给
            # 它铺满整块选区底色，这正是用户看到的"下一页整页变蓝"。
            # 判据因此取"位图/覆盖层是否 user-select: none"（修复的机制本身）：
            # **CDP 模拟的鼠标拖拽在无头 Chrome 里不会延伸选区**——起终点都精确落在
            # 目标页上，anchor/focus 却停在起点那一行（2026-09-11 实测），靠它验不了
            # 这件事。Range API 也不能当判据：它不受 user-select 约束。
            mech = json.loads(await cdp.evaluate(RANGE_MECHANISM_JS))
            log(f"跨页选区机制：{mech}")
            if not mech.get("imgInRange"):
                bad.append("前提不成立：跨页选区本应包含下一页的位图（DOM 顺序变了？）")
            guard = json.loads(await cdp.evaluate(GUARD_JS))
            log(f"可选性：{guard}")
            if guard["img"] != "none" or guard["layer"] != "none" or guard["fit"] != "none":
                bad.append(f"位图/高亮层可被选中（{guard}）—— 跨页拖选会把整页刷成选区底色")
            if guard["text"] != "text":
                bad.append(f"文字层不可选（text={guard['text']}）—— 又回到选不中")

            if args.out_shot:
                shot = await cdp.call("Page.captureScreenshot", {"format": "png"})
                Path(args.out_shot).parent.mkdir(parents=True, exist_ok=True)
                Path(args.out_shot).write_bytes(base64.b64decode(shot["data"]))
                log(f"截图已写: {args.out_shot}")

            # ---- console 错误 ----
            if cdp.errors:
                bad.append(f"console 有 {len(cdp.errors)} 条错误：{cdp.errors[:3]}")

        if bad:
            log("验收失败：")
            for b in bad:
                log(f"  ✘ {b}")
            return 1
        log("连续滚动文字层验收通过 ✔")
        return 0
    finally:
        for proc in (chrome, server):
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    proc.kill()


async def drag_select_layer(cdp, span_selector: str) -> str:
    """在某个 span 上做一次真实拖拽并返回选中文本（元素不可见就返回空串）。"""
    box = await cdp.evaluate(span_box_js(span_selector))
    if not box:
        return ""
    b = json.loads(box)
    if b["w"] < 4 or b["h"] < 4:
        return ""
    y = b["y"] + b["h"] / 2
    # 从 span 左端拖到右端（略进 1px，避免落在边界外）
    return await drag_select(cdp, b["x"] + 1, y, b["x"] + b["w"] - 1, y)


def main() -> int:
    parser = argparse.ArgumentParser(description="连续滚动文字层 E2E 验收")
    parser.add_argument("--pages", type=int, default=24, help="夹具 PDF 页数")
    parser.add_argument("--out-shot", default=None)
    parser.add_argument("--server-port", type=int, default=None)
    parser.add_argument("--cdp-port", type=int, default=None)
    parser.add_argument(
        "--chrome", default=r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    )
    args = parser.parse_args()
    # 用户安装目录的兜底猜测（$LOCALAPPDATA 常见于个人机）——与 chrome_probe/chrome_corpus 同款
    if not os.path.exists(args.chrome):
        alt = os.path.join(
            os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"
        )
        if os.path.exists(alt):
            args.chrome = alt
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
