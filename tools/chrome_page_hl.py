#!/usr/bin/env python
"""页面视图「高亮对齐」验收（PDF 渲染图 + 引用覆盖层的像素级校验）。

为什么存在：2026-09-11 用户实测发现"琥珀框与文字错位"。根因是覆盖层
`.rd-page-layer` 贴在**容器**上（inset:12px），而页面图有 max-width 且居中
——容器比图宽时归一化坐标（相对图片算）与覆盖层（相对容器算）就对不上。
实测 1827px 容器里 910px 的图，横向偏 446px、纵向按可视区算成 649px 而非
图片的 1287px。修复后必须有回归验收：肉眼"看起来差不多"挡不住这类偏移。

本工具自起隔离服务器（offline profile + 临时 data_dir），用 PyMuPDF 现场
生成一份两页英文 PDF（版权干净、内容已知），走真实链路：
  ① 知识库页上传 → 入库 → 点文档行 → 页面图渲染成功（内嵌阅读器可视性）；
  ② 切问答页，动态 import 复用页面上那个 reader 模块实例（同 URL 的 ESM 是
     单例）调 openChunk，量 DOM 矩形，与后端 /locate 的归一化坐标逐框比对。

断言：① 覆盖层与页面图矩形完全一致；② 每个高亮框的归一化位置与后端返回
一致（容差 1%）。全部通过退出码 0，否则 1。

用法：python tools/chrome_page_hl.py [--chrome <chrome.exe>] [--out-shot x.png]
"""

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chrome_probe import CDP, wait_json_list  # noqa: E402 复用 CDP 封装

REPO_ROOT = Path(__file__).resolve().parents[1]
MIKASA_EXE = REPO_ROOT / ".venv" / "Scripts" / "mikasa.exe"

# 探针 PDF 的正文：分两段写在不同页，词元序列唯一，定位必然命中
PAGE1_LINES = [
    "Mikasa Page Highlight Probe",
    "Alpha beta gamma delta epsilon zeta.",
    "Second line for the located chunk target.",
]
PAGE2_LINES = [
    "Page two begins here.",
    "Omega sigma tau upsilon phi chi psi.",
]


def make_probe_pdf(path: Path) -> None:
    """现场生成两页 PDF（默认字体只支持 ASCII，故用英文）。"""
    import pymupdf  # 官方新命名（与 loaders.py 一致，勿用旧名 fitz）

    doc = pymupdf.open()
    for lines in (PAGE1_LINES, PAGE2_LINES):
        page = doc.new_page()  # 默认 A4
        y = 120
        for line in lines:
            page.insert_text((80, y), line, fontsize=13)
            y += 34
    doc.save(str(path))
    doc.close()


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def wait_server(port: int, timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            http_json(f"http://127.0.0.1:{port}/api/health")
            return
        except Exception:
            await asyncio.sleep(0.4)
    raise RuntimeError("隔离服务器未就绪")


async def wait_until(cdp: CDP, expr: str, what: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await cdp.evaluate(expr):
            return
        await asyncio.sleep(0.3)
    raise RuntimeError(f"等待超时：{what}")


MEASURE = """(() => {
  const img = document.querySelector('.rd-page-img');
  if (!img) return {err: '页面图不存在'};
  const ir = img.getBoundingClientRect();
  // 视图还没切过来时 rect 全 0：此时归一化相除会得 NaN/Infinity，
  // 经 CDP returnByValue 变成 null，务必挡在门外
  if (!ir.width) return {err: '页面图不可见（视图未切换或图未渲染）'};
  const layer = document.querySelector('.rd-page-layer');
  const lr = layer ? layer.getBoundingClientRect() : null;
  return {
    img: [ir.x, ir.y, ir.width, ir.height],
    layer: lr ? [lr.x, lr.y, lr.width, lr.height] : null,
    loaded: img.complete && img.naturalWidth > 0,
    hls: [...document.querySelectorAll('.rd-hl')].map(h => {
      const r = h.getBoundingClientRect();
      return [(r.x - ir.x) / ir.width, (r.y - ir.y) / ir.height,
              r.width / ir.width, r.height / ir.height];
    }),
  };
})()"""


async def run(args) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="page-hl-"))
    server = chrome = None
    try:
        pdf = tmp / "page-hl-probe.pdf"
        make_probe_pdf(pdf)

        # ---- 隔离服务器（offline profile + 注入 data_dir，与 chrome_corpus 同法） ----
        cfg_text = (REPO_ROOT / "config" / "profiles" / "offline.yaml").read_text(encoding="utf-8")
        cfg = tmp / "serve.yaml"
        cfg.write_text(
            cfg_text.replace(
                "profile: offline", f"profile: offline\n\ndata_dir: {(tmp / 'data').as_posix()}"
            ),
            encoding="utf-8",
        )
        port = free_port()
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
                    str(port),
                ],
                stdout=logf,
                stderr=subprocess.STDOUT,
                cwd=str(REPO_ROOT),
            )
        await wait_server(port)
        base = f"http://127.0.0.1:{port}"

        # ---- 上传探针 PDF（走真实 file input 链路） ----
        cdp_port = free_port()
        profile = tempfile.mkdtemp(prefix="page-hl-chrome-")
        chrome = subprocess.Popen(
            [
                args.chrome,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                f"--remote-debugging-port={cdp_port}",
                f"--user-data-dir={profile}",
                "--window-size=2200,1000",
                f"{base}/documents",
            ]
        )
        target = wait_json_list(cdp_port)  # type: ignore[arg-type]
        async with CDP(target["webSocketDebuggerUrl"]) as cdp:
            await cdp.call("Page.enable")
            await cdp.call("Log.enable")
            await cdp.call("Runtime.enable")
            await wait_until(
                cdp,
                "document.readyState === 'complete' && !!document.querySelector('#file-input')",
                "知识库页就绪",
            )
            await cdp.call("DOM.enable")
            root = await cdp.call("DOM.getDocument", {"depth": 1})
            node = await cdp.call(
                "DOM.querySelector",
                {"nodeId": root["root"]["nodeId"], "selector": "#file-input"},
            )
            await cdp.call("DOM.setFileInputFiles", {"nodeId": node["nodeId"], "files": [str(pdf)]})
            await wait_until(
                cdp,
                "[...document.querySelectorAll('.doc-item')]"
                ".some(e => e.textContent.includes('page-hl-probe'))",
                "探针 PDF 入库",
            )

            # ---- 第一步：知识库页点文档行，页面视图应能正常渲染 ----
            clicked = await cdp.evaluate(
                """(() => {
                  const row = [...document.querySelectorAll('.doc-item')]
                    .find(e => e.textContent.includes('page-hl-probe'));
                  if (!row) return false;
                  row.click(); return true;
                })()"""
            )
            if not clicked:
                print("✗ 找不到探针文档行", file=sys.stderr)
                return 1
            try:
                await wait_until(
                    cdp,
                    "(() => { const i = document.querySelector('.rd-page-img');"
                    " return !!i && i.complete && i.naturalWidth > 0; })()",
                    "知识库页页面图渲染",
                    timeout=15.0,
                )
            except RuntimeError as err:
                print(f"✗ {err}", file=sys.stderr)
                return 1

            # ---- 后端真值：文档 id → 第一块 → locate 归一化矩形 ----
            docs = http_json(f"{base}/api/documents")["documents"]
            doc_id = next(d["id"] for d in docs if "page-hl-probe" in d["title"])
            content = http_json(f"{base}/api/documents/{doc_id}/content")
            chunk_id = content["chunks"][0]["chunk_id"]
            loc = http_json(f"{base}/api/documents/{doc_id}/locate/{chunk_id}")
            if not loc.get("rects"):
                print(f"✗ locate 未命中（chunk {chunk_id}）", file=sys.stderr)
                return 1

            # ---- 第二步：问答页浮层（用户点引用角标的真实路径）验高亮对齐 ----
            # 内嵌阅读器不走"引用跳转"，故换到问答页再复用 reader 模块实例调
            # openChunk（同 URL 的 ESM 是单例，拿到的是页面上那一个 reader）。
            await cdp.call("Page.navigate", {"url": f"{base}/"})
            await asyncio.sleep(0.5)
            await wait_until(cdp, "document.readyState === 'complete'", "问答页就绪")
            await asyncio.sleep(2.0)  # qa.js 启动 + initReader（模块顶层调用）
            await cdp.evaluate(
                f"import('/static/js/reader.js')"
                f".then(m => m.openChunk({chunk_id}, {{mode: 'page'}}))"
            )
            m = {}
            for _ in range(40):
                m = await cdp.evaluate(MEASURE)
                if m.get("loaded") and m.get("hls"):
                    break
                await asyncio.sleep(0.4)

            bad: list[str] = []
            if not m.get("img"):
                bad.append(f"页面图未渲染：{m}")
            else:
                # ① 覆盖层与图片矩形一致（本次修复的核心不变量）
                if m.get("layer"):
                    d = [round(a - b, 1) for a, b in zip(m["layer"], m["img"], strict=True)]
                    if any(abs(x) > 1.0 for x in d):
                        bad.append(f"覆盖层与页面图未对齐，差值(x,y,w,h)={d}")
                else:
                    bad.append("覆盖层不存在")
                # ② 每个高亮框与后端归一化坐标一致（后端是角坐标，DOM 是宽高）
                expect = [[r[0], r[1], r[2] - r[0], r[3] - r[1]] for r in loc["rects"]]
                got = m.get("hls") or []
                if len(got) != len(expect):
                    bad.append(f"高亮框数量不符：DOM {len(got)} vs 后端 {len(expect)}")
                # 数量不符已记入 problems，这里按短的那边比（strict=False 免得再抛）
                for i, (g, e) in enumerate(zip(got, expect, strict=False)):
                    diff = [round(a - b, 4) for a, b in zip(g, e, strict=True)]
                    if any(abs(x) > 0.01 for x in diff):
                        bad.append(f"高亮框{i}偏移超容差 1%：{diff}")

            # ---- 第三步：翻页控件（分页按钮 + 单页↔连续切换）----
            paging = await cdp.evaluate("""(async () => {
              const wait = (ms) => new Promise((r) => setTimeout(r, ms));
              const fits = () => document.querySelectorAll('.rd-page-fit').length;
              const label = () =>
                (document.querySelector('.rd-page-label')?.textContent || '').trim();
              const mode = () => (document.querySelector('#rd-mode')?.textContent || '').trim();
              const snap = () => ({ fits: fits(), label: label(), mode: mode() });
              const single = snap();
              document.querySelector('#rd-next')?.click();
              await wait(800);
              const afterNext = snap();
              document.querySelector('#rd-mode')?.click();   // → 连续
              await wait(1500);
              const scroll = snap();
              document.querySelector('#rd-mode')?.click();   // → 单页
              await wait(800);
              const back = snap();
              return { single, afterNext, scroll, back };
            })()""")
            if paging["single"]["fits"] != 1:
                bad.append(f"单页模式应只有 1 个页面节点：{paging['single']}")
            if "2" not in paging["afterNext"]["label"]:
                bad.append(f"点下一页后页码未更新：{paging['afterNext']}")
            if paging["scroll"]["fits"] != 2:  # 探针 PDF 共 2 页
                bad.append(f"连续模式应渲染全部 2 页：{paging['scroll']}")
            if "单页" not in paging["scroll"]["mode"]:
                bad.append(f"连续模式下按钮文字应提示切回单页：{paging['scroll']['mode']}")
            if paging["back"]["fits"] != 1:
                bad.append(f"切回单页后应只剩 1 个节点：{paging['back']}")

            # ---- 第四步：文字层（页面图上可选中复制）----
            tl = await cdp.evaluate("""(async () => {
              const wait = (ms) => new Promise((r) => setTimeout(r, ms));
              const layer = () => document.querySelector('.rd-textlayer');
              const spans = () => document.querySelectorAll('.rd-textlayer span').length;
              const tabHidden = () =>
                document.querySelector('#rd-tab-text')?.classList.contains('hidden');
              await wait(1500);   // 等文字层异步取回
              const single = { has: !!layer(), spans: spans(), tabHidden: tabHidden() };
              const sample = [...document.querySelectorAll('.rd-textlayer span')]
                .map((s) => s.textContent).join(' ').slice(0, 60);
              // 几何：每段文字都应落在图片内（归一化 0~1），否则说明坐标换算错了
              const fit = document.querySelector('.rd-page-fit')?.getBoundingClientRect();
              const geoms = fit ? [...document.querySelectorAll('.rd-textlayer span')].map((s) => {
                const r = s.getBoundingClientRect();
                return { w: r.width / fit.width, h: r.height / fit.height };
              }) : [];
              document.querySelector('#rd-mode')?.click();   // → 连续
              await wait(1500);
              const scroll = { has: !!layer(), spans: spans() };
              document.querySelector('#rd-mode')?.click();   // → 单页
              await wait(1200);
              return { single, sample, geoms, scroll };
            })()""")
            if not tl["single"]["has"] or tl["single"]["spans"] < 2:
                bad.append(f"单页模式应有文字层：{tl['single']}")
            # 注意断言不写死页码：翻页测试跑完停在第 2 页，此处内容以当前页为准
            if len(tl["sample"].strip()) < 5 or not any(c.isalpha() for c in tl["sample"]):
                bad.append(f"文字层内容不是真文本：{tl['sample'][:60]!r}")
            for g in tl["geoms"]:
                if not (0 < g["w"] <= 1.001 and 0 < g["h"] <= 1.001):
                    bad.append(f"文字层坐标越出图片范围：{g}")
                    break
            if tl["scroll"]["has"]:
                bad.append("连续模式不该铺文字层（几百页会撑爆 DOM）")
            if not tl["single"]["tabHidden"]:
                bad.append("PDF 的「文本」标签应被隐藏（页面视图已可选中复制）")

            # ---- 第五步：原文件视图（内嵌浏览器 PDF 阅读器——支持高亮/画笔）----
            original = await cdp.evaluate("""(async () => {
              const wait = (ms) => new Promise((r) => setTimeout(r, ms));
              const tabFile = document.querySelector('#rd-tab-file');
              const tabPage = document.querySelector('#rd-tab-page');
              const visible = (el) => !!el && !el.classList.contains('hidden');
              const before = { pageTab: visible(tabPage), fileTab: visible(tabFile) };
              tabFile?.click();
              await wait(2500);   // iframe 加载 PDF
              const frame = document.querySelector('.rd-frame');
              const after = {
                hasFrame: !!frame,
                srcHasPage: (frame?.getAttribute('src') || '').includes('#page='),
              };
              tabPage?.click();
              await wait(1200);
              const back = { pageActive: tabPage?.classList.contains('active') };
              return { before, after, back };
            })()""")
            if not original["before"]["pageTab"] or not original["before"]["fileTab"]:
                bad.append(f"PDF 应同时有「页面」与「原文件」标签：{original['before']}")
            if not original["after"]["hasFrame"]:
                bad.append("「原文件」视图应内嵌 iframe（浏览器 PDF 阅读器，可标注）")
            if not original["after"]["srcHasPage"]:
                bad.append("原文件 iframe 的 src 应带 #page= 定位锚点")
            if not original["back"]["pageActive"]:
                bad.append("切回「页面」标签后应恢复激活态")

            # ---- 第六步：缩放（连续可调）+ 放大后拖动平移 ----
            zoomtest = await cdp.evaluate("""(async () => {
              const wait = (ms) => new Promise((r) => setTimeout(r, ms));
              const canvas = document.querySelector('.rd-page-canvas');
              const range = document.querySelector('#rd-zoom-range');
              const label = () =>
                (document.querySelector('.rd-zoom-label')?.textContent || '').trim();
              const before = { label: label(), pannable: !!canvas?.classList.contains('pannable') };
              // 单调性：95% < 100% < 105%。修复前 100% 走 fit-content、其余走
              // "容器比例"，两套模型在 100% 处跳变（用户实测：拖到 100% 反而
              // 缩小一半）。现在统一以"图片原始显示宽度"为基准。
              const fitW = () => {
                const f = document.querySelector('.rd-page-fit');
                return f ? Math.round(f.getBoundingClientRect().width) : 0;
              };
              const widths = {};
              for (const v of ['95', '100', '105']) {
                range.value = v;
                range.dispatchEvent(new Event('input', { bubbles: true }));
                await wait(350);
                widths[v] = fitW();
              }
              // 溢出与拖动用 300%：基准改成"图片原始宽度"后，200% 在宽容器里
              // 可能并不溢出（探针页面图仅 910px 宽），断言改用大倍数
              range.value = '300';
              range.dispatchEvent(new Event('input', { bubbles: true }));
              await wait(600);
              const img = document.querySelector('.rd-page-img');
              const layer = document.querySelector('.rd-page-layer');
              const imgW = img?.getBoundingClientRect().width || 0;
              const layerW = layer?.getBoundingClientRect().width || 0;
              const zoomed = {
                label: label(),
                pannable: !!canvas?.classList.contains('pannable'),
                overflows: (canvas?.scrollWidth || 0) > (canvas?.clientWidth || 0),
                // 高亮层必须与图片同宽（放大后错位是真实 bug）
                layerAligned: Math.abs(imgW - layerW) <= 1.0,
              };
              // 合成拖动：按下 → 左移 → 抬起，看横向滚动是否跟随
              const from = canvas.scrollLeft;
              canvas.dispatchEvent(new MouseEvent('mousedown',
                { clientX: 300, clientY: 300, button: 0, bubbles: true }));
              await wait(60);
              window.dispatchEvent(new MouseEvent('mousemove',
                { clientX: 200, clientY: 300, bubbles: true }));
              await wait(60);
              window.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
              const panned = canvas.scrollLeft - from;
              // 复位到 100%，免得影响后续截图
              if (range) {
                range.value = '100';
                range.dispatchEvent(new Event('input', { bubbles: true }));
              }
              return { before, zoomed, panned, widths };
            })()""")
            if zoomtest["before"]["label"] != "100%":
                bad.append(f"默认缩放应为 100%：{zoomtest['before']}")
            if zoomtest["zoomed"]["label"] != "300%":
                bad.append(f"滑条改 300% 后标签未跟随：{zoomtest['zoomed']}")
            w = zoomtest.get("widths", {})
            if not (w.get("95", 0) < w.get("100", 0) < w.get("105", 0)):
                bad.append(f"缩放必须单调（95% < 100% < 105%）：{w}")
            if not zoomtest["zoomed"]["pannable"] or not zoomtest["zoomed"]["overflows"]:
                bad.append(f"放大后应可拖动且内容溢出：{zoomtest['zoomed']}")
            if not zoomtest["zoomed"]["layerAligned"]:
                bad.append(f"放大后高亮层必须与图片同宽（错位 bug）：{zoomtest['zoomed']}")
            if zoomtest["panned"] <= 0:
                bad.append(f"拖动未产生横向平移：{zoomtest['panned']}")

            # ---- 第七步：连续模式下缩放不跳页（真实的"跳好多页"bug）----
            zoomanchor = await cdp.evaluate("""(async () => {
              const wait = (ms) => new Promise((r) => setTimeout(r, ms));
              const canvas = document.querySelector('.rd-page-canvas');
              const mode = document.querySelector('#rd-mode');
              const range = document.querySelector('#rd-zoom-range');
              // 切到连续模式（探针 PDF 共 2 页）
              if ((mode?.textContent || '').includes('连续')) mode.click();
              await wait(1600);
              // 把第 2 页滚到视野顶部
              document.querySelector('.rd-page-fit[data-page="2"]')
                ?.scrollIntoView({ block: 'start' });
              await wait(700);
              // 第 2 页顶边相对画布顶边的偏移——缩放前后应基本不变
              const topOf2 = () => {
                const node = document.querySelector('.rd-page-fit[data-page="2"]');
                if (!node) return null;
                return Math.round(
                  node.getBoundingClientRect().top - canvas.getBoundingClientRect().top
                );
              };
              const before = topOf2();
              for (const v of ['200', '50', '100']) {
                range.value = v;
                range.dispatchEvent(new Event('input', { bubbles: true }));
                await wait(700);
              }
              const after = topOf2();
              if ((mode?.textContent || '').includes('单页')) mode.click();
              await wait(700);
              return { before, after };
            })()""")
            if zoomanchor["before"] is None or zoomanchor["after"] is None:
                bad.append(f"连续模式缩放用例无法定位第 2 页：{zoomanchor}")
            elif abs(zoomanchor["after"] - zoomanchor["before"]) > 40:
                bad.append(
                    "连续模式下缩放把阅读位置带跑了（跳页 bug）："
                    f"第 2 页顶边 {zoomanchor['before']} → {zoomanchor['after']}"
                )

            if args.out_shot:
                res = await cdp.call("Page.captureScreenshot", {"format": "png"})
                Path(args.out_shot).write_bytes(base64.b64decode(res["data"]))
                print(f"截图已写: {args.out_shot}", file=sys.stderr)

            print(
                json.dumps(
                    {
                        "page": loc.get("page"),
                        "rects": len(loc["rects"]),
                        "domBoxes": len(m.get("hls") or []),
                        "layerAligned": bool(m.get("layer"))
                        and all(
                            abs(a - b) <= 1.0 for a, b in zip(m["layer"], m["img"], strict=True)
                        )
                        if m.get("layer")
                        else False,
                        "consoleErrors": cdp.errors,
                        "problems": bad,
                    },
                    ensure_ascii=False,
                )
            )
            return 1 if (bad or cdp.errors) else 0
    finally:
        if chrome:
            chrome.terminate()
        if server:
            server.terminate()


def main() -> None:
    parser = argparse.ArgumentParser(description="页面视图高亮对齐验收（自起隔离服务器）")
    parser.add_argument(
        "--chrome", default=r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    )
    parser.add_argument("--out-shot", default=None, help="截图输出路径（PNG）")
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
