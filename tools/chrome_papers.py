#!/usr/bin/env python
"""无头 Chrome「在线找论文」E2E 验收（知识库页面板，M7 / ADR-0019）。

自起一台隔离服务器（offline profile + MIKASA_DATA_DIR 指向临时目录），
论文的两个来源用**本地假源**整体替换（环境变量后门：arxiv.py 与
openalex.py 的 base URL 都是调用时读环境变量），假源同时扮 arXiv（Atom
XML）、OpenAlex（works JSON）与论文 PDF 主机（回环 http —— 正是
papers/download.py 给 E2E 留的逃生门）。真实 Web 进程 + 真实浏览器走一遍
用户流程：

  1. 检索 → 结果行（标题/作者/年份/来源徽标）渲染，交错分页；
  2. 「加载更多」→ 第二页追加（offset 前进，不重置列表）；
  3. **逐源降级**：让 OpenAlex 假源对特定词回 500 → 结果照出、顶部出现
     "部分来源暂时不可用"提示（200 而不是整单失败）；
  4. 点「导入」→ 下载 PDF → 入库 → 树里出现该文档（宿主机侧核对
     /api/documents 与上传目录落盘）；再点一次走 sha256 跳过（200 文案）；
  5. 无开放获取的那条：导入按钮禁用（提前告知，不让用户点了才吃 409）；
  6. OpenAlex 密钥：展开 → 粘贴 → 保存 → .env 落盘 + has_api_key 变真；
     清空保存 → 清除；
  7. 布局：点文档行进阅读视图时论文面板收起，关闭阅读后回来；
  8. 全程收集 console 错误与未捕获异常，有错退出码 1。

用法：
  python tools/chrome_papers.py [--out-shot tools/shots/papers-e2e.png]
  [--server-port <空闲自动>] [--cdp-port <空闲自动>] [--chrome <chrome.exe 路径>]
退出码：0 = 验收通过；1 = 任一断言失败 / console 有错。
"""

import argparse
import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.chrome_probe import CDP, log, wait_json_list  # noqa: E402

REPO_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIKASA_EXE = REPO_ROOT / ".venv" / "Scripts" / "mikasa.exe"

FAKE_KEY = "sk-papers-e2e-0001"  # 只写进临时数据目录，不可能是真密钥
BOOM_WORD = "boom"  # 含此词的检索让 OpenAlex 假源回 500（降级路径开关）
CORPUS = 30  # 每个源的假结果条数（≥ 两页，翻页路径才走得到）
PAGE_SIZE = 20  # 与 papers.js 的 PAGE_SIZE 一致
# 最后一条 toast 的文本（带外括号，可直接 .includes）。toast 存活 3.6s，
# 连续两次导入时 querySelector 会取到**上一条**——2026-09-16 实测踩过。
LAST_TOAST = "([...document.querySelectorAll('#toast .toast-msg')].at(-1)?.textContent || '')"


def js_quote(s: str) -> str:
    """JS 字符串字面量（跨 evaluate 传值）。"""
    return json.dumps(s, ensure_ascii=False)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pdf_bytes() -> bytes:
    """造一份真 PDF（有文字层，ingest 能解析出正文）。"""
    import pymupdf

    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text(
        (72, 96),
        "这是一篇用于在线找论文 E2E 验收的测试论文正文：检索增强生成把外部知识引入生成过程。",
        fontname="china-s",
        fontsize=12,
    )
    return pdf.tobytes()


# ---------------------------------------------------------------------------
# 本地假源（扮 arXiv + OpenAlex + PDF 主机）
# ---------------------------------------------------------------------------


class _FakeSources(BaseHTTPRequestHandler):
    """假源 HTTP 服务（回环 http；下载侧只放行回环，见 download.py 模块头）。"""

    pdf = b""

    def log_message(self, *args):  # noqa: ANN002 - 静音 BaseHTTPRequestHandler 日志
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_pdf(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(self.pdf)))
        self.end_headers()
        self.wfile.write(self.pdf)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        parts = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parts.query)
        if parts.path == "/arxiv":
            self._arxiv(query)
        elif parts.path.startswith("/openalex/works"):
            self._openalex(parts.path, query)
        elif parts.path.startswith("/pdf/"):
            self._send_pdf()
        else:
            self._send(b"not found", "text/plain", 404)

    # ---- arXiv：Atom XML ----

    def _arxiv_entry_xml(self, n: int) -> str:
        pid = f"2401.{n:05d}"
        return (
            "<entry>"
            f"<id>http://arxiv.org/abs/{pid}v1</id>"
            f"<title>arXiv 测试论文 {n}：检索增强生成综述</title>"
            f"<published>2024-01-{(n % 28) + 1:02d}T00:00:00Z</published>"
            f"<author><name>作者甲{n}</name></author><author><name>作者乙{n}</name></author>"
            f"<summary>这是第 {n} 篇假论文的摘要，用于验收摘要展开交互。</summary>"
            f'<link rel="alternate" href="https://example.org/abs/{pid}"/>'
            "</entry>"
        )

    def _arxiv(self, query: dict) -> None:
        if "id_list" in query:
            pid = query["id_list"][0].split("v")[0]
            n = int(pid.split(".")[1])
            entries = self._arxiv_entry_xml(n)
        else:
            start = int(query.get("start", ["0"])[0])
            count = int(query.get("max_results", ["10"])[0])
            ids = range(start + 1, min(start + count, CORPUS) + 1)
            entries = "".join(self._arxiv_entry_xml(n) for n in ids)
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom">' + entries + "</feed>"
        )
        self._send(xml.encode("utf-8"), "application/atom+xml")

    # ---- OpenAlex：works JSON ----

    def _work(self, n: int, *, oa: bool) -> dict:
        wid = f"W2{n:09d}"
        return {
            "id": f"https://openalex.org/{wid}",
            "doi": f"https://doi.org/10.1000/e2e-{n}",
            "display_name": f"中文期刊测试论文 {n}：水库坝体稳定性分析",
            "publication_year": 2023,
            "authorships": [{"author": {"display_name": f"中文作者{n}"}}],
            "primary_location": {"source": {"display_name": "某某学报"}},
            # 倒排索引：词 → 位置（中文按单字拆，重建后是空格分隔的文本）
            "abstract_inverted_index": {
                "本文": [0],
                "研究": [1],
                f"第{n}篇": [2],
                "假论文": [3],
            },
            "open_access": {
                "is_oa": oa,
                "oa_url": f"{self.pdf_base}/{wid}.pdf" if oa else None,
            },
        }

    def _openalex(self, path: str, query: dict) -> None:
        # 降级开关：特定检索词让这一源整体 500（另一源照常）
        if BOOM_WORD in (query.get("search", [""])[0]):
            self._send(b'{"error": "boom"}', "application/json", 500)
            return
        if path.startswith("/openalex/works/"):
            wid = path.rsplit("/", 1)[-1]
            n = int(wid.lstrip("W").lstrip("2"))
            payload = self._work(n, oa=n != 1)  # 第 1 条无开放获取（禁用态验收用）
        else:
            page = int(query.get("page", ["1"])[0])
            per_page = int(query.get("per-page", ["10"])[0])
            start = (page - 1) * per_page
            # 全局第 1 条永远是"无开放获取"那条（交错后落在第 2 行）
            payload = {
                "results": [
                    self._work(n, oa=n != 1)
                    for n in range(start + 1, min(start + per_page, CORPUS) + 1)
                ]
            }
        self._send(json.dumps(payload).encode("utf-8"), "application/json")


class FakeSources:
    """假源的生命周期管理（后台线程 + 动态端口）。"""

    def __init__(self) -> None:
        self.pdf_base = ""
        self.port = 0
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.port = free_port()
        self.pdf_base = f"http://127.0.0.1:{self.port}/pdf"
        handler = type(
            "_Handler", (_FakeSources,), {"pdf": _pdf_bytes(), "pdf_base": self.pdf_base}
        )
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()

    def env(self) -> dict:
        """两个来源的 base URL 覆盖（调用时读取，见 arxiv.py/openalex.py）。"""
        base = f"http://127.0.0.1:{self.port}"
        return {
            "MIKASA_PAPERS_ARXIV_BASE": f"{base}/arxiv",
            "MIKASA_PAPERS_ARXIV_PDF_BASE": self.pdf_base,
            "MIKASA_PAPERS_OPENALEX_BASE": f"{base}/openalex",
        }


# ---------------------------------------------------------------------------
# 服务器与浏览器（与 chrome_model_settings.py 同款）
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
    """等 uvicorn 起来（就绪判定用只有本次构建才有的 papers 端点）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            body = api_get(port, "/api/papers/settings")
            if "has_api_key" in body:
                return
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            pass
        time.sleep(0.3)
    raise RuntimeError("服务器未就绪（日志见 tmpdir 下 serve.log）")


async def set_input(cdp, selector: str, value: str) -> None:
    """设 input 值并派发 input 事件（keyDirty 跟踪挂在 input 上）。"""
    await cdp.evaluate(
        f"(() => {{ const i = document.querySelector({js_quote(selector)}); "
        f"i.value = {js_quote(value)}; "
        f"i.dispatchEvent(new Event('input', {{bubbles: true}})); return true; }})()"
    )


async def wait_until(cdp, expr: str, what: str, timeout: float = 40.0) -> None:
    """轮询页面表达式直到真值；超时抛错（失败信息带轮询目标）。

    超时给得宽：arXiv 来源自带 3 秒节流（成功一次后补睡），一次检索
    可能要多等几秒。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await cdp.evaluate(expr):
            return
        await asyncio.sleep(0.25)
    raise RuntimeError(f"超时等不到：{what}")


async def search(cdp, q: str) -> None:
    """填词 → 点搜索 → 等结果行（或空态）出现。"""
    await set_input(cdp, "#paper-q", q)
    await cdp.evaluate("document.querySelector('#paper-search-btn').click(); true")
    await wait_until(
        cdp,
        "(() => { const s = document.querySelector('#paper-search-btn');"
        " return !s.disabled && (document.querySelectorAll('.paper-item').length > 0"
        " || document.querySelector('#paper-results .empty')); })()",
        f"检索完成：{q}",
    )


async def run(args):
    tmp = Path(tempfile.mkdtemp(prefix="papers-e2e-"))
    log(f"临时目录：{tmp}")
    userdata = tmp / "userdata"
    userdata.mkdir(parents=True)
    fake = FakeSources()
    fake.start()
    env = dict(os.environ)
    env["MIKASA_DATA_DIR"] = str(userdata)  # 隔离的生命线：所有写入落在 tmp
    env.update(fake.env())
    env.pop("OPENALEX_API_KEY", None)  # 不信宿主机残留的密钥
    server = None
    chrome = None
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

        profile = tempfile.mkdtemp(prefix="papers-chrome-")
        chrome = subprocess.Popen(
            [
                args.chrome,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                f"--remote-debugging-port={args.cdp_port}",
                f"--user-data-dir={profile}",
                "--window-size=1400,900",
                f"http://127.0.0.1:{port}/documents",
            ]
        )
        target = wait_json_list(args.cdp_port)
        async with CDP(target["webSocketDebuggerUrl"]) as cdp:
            await cdp.call("Page.enable")
            await cdp.call("Log.enable")
            for _ in range(60):
                state = await cdp.evaluate(
                    "document.readyState + '|' + String(!!document.querySelector('#paper-form'))"
                )
                if state == "complete|true":
                    break
                await asyncio.sleep(0.25)
            await asyncio.sleep(0.8)

            # ---- 0. 空库会弹首启引导，先关掉（否则挡着面板）----
            await cdp.evaluate(
                "(() => { const b = [...document.querySelectorAll('#onboard .btn')]"
                ".find(x => x.textContent.includes('开始使用'));"
                " if (b) b.click(); return true; })()"
            )
            await asyncio.sleep(0.3)

            # ---- 1. 检索：结果行渲染 ----
            await search(cdp, "检索增强")
            page1 = await cdp.evaluate("""(() => {
              const rows = [...document.querySelectorAll('.paper-item')];
              const first = rows[0];
              return {
                count: rows.length,
                title: first.querySelector('.paper-title').textContent,
                titleHref: first.querySelector('.paper-title').getAttribute('href'),
                src: first.querySelector('.paper-src').textContent,
                meta: first.querySelector('.paper-meta').textContent,
                moreShown: !document.querySelector('#paper-more').classList.contains('hidden'),
                status: document.querySelector('#paper-status').textContent,
                disabledImports: rows.filter(r =>
                  r.querySelector('.paper-actions .btn.primary').disabled).length,
              };
            })()""")

            # ---- 2. 摘要开合（展开/收起文案互切）----
            await cdp.evaluate(
                "document.querySelector('.paper-item .paper-actions .btn.ghost').click(); true"
            )
            abstract_open = await cdp.evaluate("""(() => {
              const row = document.querySelector('.paper-item');
              return {
                shown: !row.querySelector('.paper-abstract').classList.contains('hidden'),
                label: row.querySelector('.paper-actions .btn.ghost').textContent,
                text: row.querySelector('.paper-abstract').textContent.slice(0, 12),
              };
            })()""")

            # ---- 3. 加载更多：第二页追加 ----
            await cdp.evaluate("document.querySelector('#paper-more').click(); true")
            await wait_until(
                cdp,
                "document.querySelectorAll('.paper-item').length > 20",
                "第二页追加",
            )
            page2 = await cdp.evaluate("""(() => ({
              count: document.querySelectorAll('.paper-item').length,
              moreHidden: document.querySelector('#paper-more').classList.contains('hidden'),
              status: document.querySelector('#paper-status').textContent,
            }))()""")

            # ---- 4. 逐源降级：OpenAlex 假源 500，arXiv 结果照常 ----
            await search(cdp, f"水库坝 {BOOM_WORD}")
            degrade = await cdp.evaluate("""(() => ({
              count: document.querySelectorAll('.paper-item').length,
              status: document.querySelector('#paper-status').textContent,
            }))()""")
            # 回到正常结果，供导入用
            await search(cdp, "检索增强")

            # ---- 5. 导入第一篇（arXiv）→ 入库 → 树里出现 ----
            await cdp.evaluate(
                "document.querySelector('.paper-item .paper-actions .btn.primary').click(); true"
            )
            await wait_until(
                cdp,
                "(() => { const b = document.querySelector("
                "'.paper-item .paper-actions .btn.primary');"
                " return b.textContent === '已导入'; })()",
                "导入完成（按钮转为已导入）",
            )
            import1 = await cdp.evaluate("""(() => ({
              label: document.querySelector('.paper-item .paper-actions .btn.primary').textContent,
              status: document.querySelector('.paper-item .paper-status').textContent,
              toast: [...document.querySelectorAll('#toast .toast-msg')].at(-1)?.textContent || '',
              treeDocs: document.querySelectorAll('#kb-tree .doc-item').length,
            }))()""")
            docs_after_import = api_get(port, "/api/documents")["documents"]
            uploads = sorted(p.name for p in (userdata / "uploads").glob("*"))
            health_after_import = api_get(port, "/api/health")["documents"]

            # ---- 6. 重复导入同一篇：sha256 跳过（200 文案）----
            # 导入成功后按钮定格为禁用的「已导入」（防重复点击），所以跳过
            # 路径按用户的真实走法验收：重新检索 → 新结果行 → 再点导入。
            imported_btn_disabled = await cdp.evaluate(
                "document.querySelector('.paper-item .paper-actions .btn.primary').disabled"
            )
            await search(cdp, "检索增强")
            await cdp.evaluate(
                "document.querySelector('.paper-item .paper-actions .btn.primary').click(); true"
            )
            await wait_until(
                cdp,
                f"({LAST_TOAST}).includes('跳过')",
                "重复导入跳过提示",
            )
            import2 = await cdp.evaluate(LAST_TOAST)

            # ---- 7. 无开放获取那条：导入按钮禁用 ----
            no_oa = await cdp.evaluate("""(() => {
              const row = [...document.querySelectorAll('.paper-item')]
                .find(r => r.querySelector('.paper-src').textContent === 'OpenAlex');
              const btn = row.querySelector('.paper-actions .btn.primary');
              return {disabled: btn.disabled, title: btn.title,
                      meta: row.querySelector('.paper-meta').textContent};
            })()""")

            # ---- 8. OpenAlex 密钥：保存 → 热生效；清空 → 清除 ----
            await cdp.evaluate("document.querySelector('#paper-key-toggle').click(); true")
            key_row_shown = await cdp.evaluate(
                "!document.querySelector('#paper-key-row').classList.contains('hidden')"
            )
            await set_input(cdp, "#paper-key-input", FAKE_KEY)
            await cdp.evaluate("document.querySelector('#paper-key-save').click(); true")
            await wait_until(
                cdp,
                "document.querySelector('#paper-key-note').textContent.startsWith('已保存')",
                "密钥保存成功",
            )
            key_saved = api_get(port, "/api/papers/settings")
            env_file = (userdata / ".env").read_text(encoding="utf-8")
            key_ui = await cdp.evaluate("""(() => ({
              note: document.querySelector('#paper-key-note').textContent,
              cleared: document.querySelector('#paper-key-input').value === '',
              placeholder: document.querySelector('#paper-key-input').placeholder,
            }))()""")
            # 清空保存 = 清除（三段语义：动过且为空）
            await set_input(cdp, "#paper-key-input", "")
            await cdp.evaluate("document.querySelector('#paper-key-save').click(); true")
            await wait_until(
                cdp,
                "document.querySelector('#paper-key-note').textContent.startsWith('已清除')",
                "密钥清除",
            )
            key_cleared = api_get(port, "/api/papers/settings")

            # ---- 9. 布局：进阅读视图面板收起，关闭后回来 ----
            await cdp.evaluate("document.querySelector('#kb-tree .doc-item').click(); true")
            await wait_until(
                cdp,
                "document.querySelector('#papers-card').classList.contains('hidden')",
                "阅读视图下论文面板收起",
            )
            # 内嵌模式的关闭钮 = 阅读头的 ✕（aria-label「收起阅读区」）
            await cdp.evaluate("document.querySelector('#rd-close').click(); true")
            await asyncio.sleep(0.5)
            panel_back = await cdp.evaluate(
                "!document.querySelector('#papers-card').classList.contains('hidden')"
            )

            shot = None
            if args.out_shot:
                res = await cdp.call("Page.captureScreenshot", {"format": "png"})
                shot = res["data"]

            report = {
                "page1": page1,
                "abstractOpen": abstract_open,
                "page2": page2,
                "degrade": degrade,
                "import1": import1,
                "importedBtnDisabled": imported_btn_disabled,
                "docsAfterImport": len(docs_after_import),
                "healthAfterImport": health_after_import,
                "uploads": uploads,
                "import2": import2,
                "noOa": no_oa,
                "keyRowShown": key_row_shown,
                "keySaved": key_saved,
                "keyUi": key_ui,
                "envHasKey": "OPENALEX_API_KEY" in env_file,
                "keyCleared": key_cleared,
                "envKeyGone": "OPENALEX_API_KEY"
                not in (userdata / ".env").read_text(encoding="utf-8"),
                "panelBack": panel_back,
                "consoleErrors": cdp.errors,
            }
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if shot:
                with open(args.out_shot, "wb") as f:
                    f.write(base64.b64decode(shot))
                log(f"截图已写: {args.out_shot}")

            bad = []
            if page1["count"] != PAGE_SIZE:
                bad.append(f"第一页应为 {PAGE_SIZE} 条，实际 {page1['count']}")
            if "arXiv 测试论文" not in page1["title"]:
                bad.append(f"首条标题不对：{page1['title']}")
            if not page1["titleHref"].startswith("https://example.org/"):
                bad.append(f"标题没链到详情页：{page1['titleHref']}")
            if page1["src"] != "arXiv":
                bad.append(f"首条来源徽标不对：{page1['src']}")
            if "2024" not in page1["meta"] or "作者甲" not in page1["meta"]:
                bad.append(f"元信息行缺年份/作者：{page1['meta']}")
            if not page1["moreShown"]:
                bad.append("首屏应显示「加载更多」")
            if page1["disabledImports"] != 1:
                bad.append(f"应恰有 1 条禁用导入（无开放获取），实际 {page1['disabledImports']}")
            if not abstract_open["shown"] or abstract_open["label"] != "收起":
                bad.append(f"摘要展开失败：{abstract_open}")
            if page2["count"] != PAGE_SIZE * 2:
                bad.append(f"加载更多后应 {PAGE_SIZE * 2} 条，实际 {page2['count']}")
            if page2["moreHidden"]:
                bad.append("第二页取满后不应再显示「加载更多」")
            if degrade["count"] == 0:
                bad.append("单源失败时另一源的结果没出来")
            if "部分来源暂时不可用" not in degrade["status"] or "OpenAlex" not in degrade["status"]:
                bad.append(f"逐源降级提示缺失：{degrade['status']}")
            if import1["label"] != "已导入":
                bad.append(f"导入后按钮文案不对：{import1['label']}")
            if "入库成功" not in import1["toast"]:
                bad.append(f"导入 toast 不对：{import1['toast']}")
            if len(docs_after_import) != 1:
                bad.append(f"入库文档数应为 1，实际 {len(docs_after_import)}")
            if health_after_import != 1:
                bad.append(f"/api/health 文档数没跟上：{health_after_import}")
            if len(uploads) != 1 or "(arxiv 2401." not in uploads[0]:
                bad.append(f"uploads 落盘名不对：{uploads}")
            if not imported_btn_disabled:
                bad.append("导入成功后按钮没定格（防重复点的纪律）")
            if "跳过" not in import2:
                bad.append(f"重复导入没有跳过文案：{import2}")
            if not no_oa["disabled"]:
                bad.append("无开放获取那条的导入按钮没禁用")
            if "无全文" not in no_oa["meta"]:
                bad.append(f"无全文标记缺失：{no_oa['meta']}")
            if not key_row_shown:
                bad.append("密钥行没展开")
            if not key_saved.get("has_api_key"):
                bad.append("密钥保存后 has_api_key 仍为假")
            if not key_ui["cleared"] or "已保存" not in key_ui["placeholder"]:
                bad.append(f"保存后密钥框状态不对：{key_ui}")
            if key_cleared.get("has_api_key"):
                bad.append("清空保存后密钥没被清除")
            if not panel_back:
                bad.append("关闭阅读视图后论文面板没回来")
            if cdp.errors:
                bad.append("console 有错误")
            if bad:
                log("验收未过：" + "；".join(bad))
                return 1
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
    parser = argparse.ArgumentParser(description="无头 Chrome 在线找论文验收")
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
