#!/usr/bin/env python
"""无头 Chrome「找论文」页 E2E 验收（M8，独立页 + 多源 + 筛选 + 详情）。

自起一台隔离服务器（offline profile + MIKASA_DATA_DIR 指向临时目录），
三个来源全部用**本地假源**替换（arXiv/OpenAlex/CORE 的 base URL 都是调用
时读环境变量），假源同时扮三家的检索 API、单条查询与 PDF 主机（回环 http
——正是 papers/download.py 给 E2E 留的逃生门）。真实 Web 进程 + 真实浏览器
走一遍用户流程：

  1. 三栏布局齐备；来源列表由 `/api/papers/sources` 渲染（三个复选框）；
  2. 检索 → 结果按**三源轮转**出现（arXiv/OpenAlex/CORE 各一）；
  3. **筛选真的发出去了**：改年份/换排序后，从假源的 `/__recorded` 断言
     该源收到的查询里确实带上了条件（不靠脆弱的 DOM 反推）；
  4. **能力对齐**：只勾 arXiv 时"被引最多"排序整项禁用（它没有被引数据），
     并给出说明文案；
  5. 「加载更多」按**窗口大小**推进 offset → 两页零重复；
  6. 点结果 → 右侧详情出现完整摘要/被引/来源；点「导入知识库」→ 入库 →
     结果行与详情面板同时转「已在库中」+ 去提问/去知识库出口；
  7. 无开放获取的那条：导入按钮提前禁用；重复导入走 sha256 跳过；
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
CORPUS = 30  # 假源每个源的条数（≥ 两页，翻页路径才走得到）
PAGE_SIZE = 20  # 与 papers-page.js 的 PAGE_SIZE 一致
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
        "这是一篇用于找论文 E2E 验收的测试论文正文：检索增强生成把外部知识引入生成过程。",
        fontname="china-s",
        fontsize=12,
    )
    return pdf.tobytes()


# ---------------------------------------------------------------------------
# 本地假源（扮 arXiv + OpenAlex + CORE + PDF 主机）
# ---------------------------------------------------------------------------


class _FakeSources(BaseHTTPRequestHandler):
    """假源 HTTP 服务（回环 http；下载侧只放行回环，见 download.py 模块头）。

    **记录每个来源收到的查询串**（`/__recorded` 可读）：筛选参数有没有真的
    发出去，只能从上游这一侧断言——前端 DOM 说不了实话。
    """

    pdf = b""
    pdf_base = ""
    recorded: dict[str, list[str]] = {}

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
        if parts.path == "/__recorded":
            body = json.dumps(self.recorded).encode("utf-8")
            self._send(body, "application/json")
            return
        if parts.path == "/arxiv":
            self._record("arxiv", parts.query)
            self._arxiv(query)
        elif parts.path.startswith("/openalex/works"):
            self._record("openalex", parts.query)
            self._openalex(parts.path, query)
        elif parts.path.startswith("/core/search"):
            self._record("core", parts.query)
            self._core_search(query)
        elif parts.path.startswith("/core/works/"):
            self._record("core", parts.query)
            self._core_fetch(parts.path)
        elif parts.path.startswith("/pdf/"):
            self._send_pdf()
        else:
            self._send(b"not found", "text/plain", 404)

    @classmethod
    def _record(cls, source: str, query: str) -> None:
        cls.recorded.setdefault(source, []).append(query)

    # ---- arXiv：Atom XML ----

    def _arxiv_entry_xml(self, n: int) -> str:
        pid = f"2401.{n:05d}"
        return (
            "<entry>"
            f"<id>http://arxiv.org/abs/{pid}v1</id>"
            f"<title>arXiv 测试论文 {n}：检索增强生成综述</title>"
            f"<published>2024-01-{(n % 28) + 1:02d}T00:00:00Z</published>"
            f"<author><name>作者甲{n}</name></author><author><name>作者乙{n}</name></author>"
            f"<summary>这是第 {n} 篇假论文的摘要，用于验收摘要与详情面板。</summary>"
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
            "cited_by_count": n * 3,
            "authorships": [{"author": {"display_name": f"中文作者{n}"}}],
            "primary_location": {"source": {"display_name": "某某学报"}},
            "abstract_inverted_index": {"本文": [0], "研究": [1], f"第{n}篇": [2], "假论文": [3]},
            "open_access": {"is_oa": oa, "oa_url": f"{self.pdf_base}/{wid}.pdf" if oa else None},
            "language": "zh",
        }

    def _openalex(self, path: str, query: dict) -> None:
        if path.startswith("/openalex/works/"):
            wid = path.rsplit("/", 1)[-1]
            n = int(wid.lstrip("W").lstrip("2"))
            payload = self._work(n, oa=n != 1)  # 第 1 条无开放获取（禁用态验收用）
        else:
            page = int(query.get("page", ["1"])[0])
            per_page = int(query.get("per-page", ["10"])[0])
            start = (page - 1) * per_page
            payload = {
                "results": [
                    self._work(n, oa=n != 1)
                    for n in range(start + 1, min(start + per_page, CORPUS) + 1)
                ]
            }
        self._send(json.dumps(payload).encode("utf-8"), "application/json")

    # ---- CORE：works JSON（注意路径带尾斜杠，与 core.py 的实现一致）----

    def _core_work(self, n: int, *, pdf: bool) -> dict:
        return {
            "id": 7000 + n,
            "title": f"CORE 测试论文 {n}：开放获取仓储里的研究",
            "authors": [{"name": f"CORE 作者{n}"}],
            "yearPublished": 2022,
            "citationCount": n * 5,
            "abstract": f"这是 CORE 第 {n} 条假记录的摘要。",
            "doi": f"https://doi.org/10.1000/core-{n}",
            "downloadUrl": f"{self.pdf_base}/core-{n}.pdf" if pdf else "",
            "journals": [],
            "publisher": "CORE 出版社",
            "language": {"code": "en", "name": "English"},
            "links": [{"type": "reader", "url": f"https://core.ac.uk/reader/{7000 + n}"}],
        }

    def _core_search(self, query: dict) -> None:
        offset = int(query.get("offset", ["0"])[0])
        limit = int(query.get("limit", ["10"])[0])
        # 第 1 条无 downloadUrl：证明"CORE 全 OA"没有被写死（导入按钮应禁用）
        payload = {
            "results": [
                self._core_work(n, pdf=n != 1)
                for n in range(offset + 1, min(offset + limit, CORPUS) + 1)
            ]
        }
        self._send(json.dumps(payload).encode("utf-8"), "application/json")

    def _core_fetch(self, path: str) -> None:
        n = int(path.rsplit("/", 1)[-1]) - 7000
        self._send(json.dumps(self._core_work(n, pdf=n != 1)).encode("utf-8"), "application/json")


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
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), _FakeSources)
        _FakeSources.pdf = _pdf_bytes()
        _FakeSources.pdf_base = self.pdf_base
        _FakeSources.recorded = {}
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()

    def env(self) -> dict:
        """三个来源的 base URL 覆盖（调用时读取，见各来源模块头）。"""
        base = f"http://127.0.0.1:{self.port}"
        return {
            "MIKASA_PAPERS_ARXIV_BASE": f"{base}/arxiv",
            "MIKASA_PAPERS_ARXIV_PDF_BASE": self.pdf_base,
            "MIKASA_PAPERS_OPENALEX_BASE": f"{base}/openalex",
            "MIKASA_PAPERS_CORE_BASE": f"{base}/core",
        }

    def recorded(self) -> dict:
        return _FakeSources.recorded


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
    """等 uvicorn 起来（就绪判定用只有本次构建才有的来源目录端点）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            body = api_get(port, "/api/papers/sources")
            if body.get("sources"):
                return
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            pass
        time.sleep(0.3)
    raise RuntimeError("服务器未就绪（日志见 tmpdir 下 serve.log）")


async def set_input(cdp, selector: str, value: str) -> None:
    """设 input 值并派发 input/change 事件（前端 onchange 挂在 change 上）。"""
    await cdp.evaluate(
        f"(() => {{ const i = document.querySelector({js_quote(selector)}); "
        f"i.value = {js_quote(value)}; "
        f"i.dispatchEvent(new Event('input', {{bubbles: true}})); "
        f"i.dispatchEvent(new Event('change', {{bubbles: true}})); return true; }})()"
    )


async def wait_until(cdp, expr: str, what: str, timeout: float = 40.0) -> None:
    """轮询页面表达式直到真值；超时抛错（失败信息带轮询目标）。

    超时给得宽：arXiv 来源自带 3 秒节流、CORE 自持 2 秒节流，一次检索
    可能要多等几秒。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await cdp.evaluate(expr):
            return
        await asyncio.sleep(0.25)
    raise RuntimeError(f"超时等不到：{what}")


async def wait_idle(cdp, timeout: float = 40.0) -> None:
    """等"没有在途检索"（前端 busy 期间搜索按钮禁用）。

    **不做这一步会让整条验收失真**（2026-09-16 踩过）：点完搜索就去点
    「加载更多」，新检索还没落地，翻页接在**上一次检索**的结果后面 →
    列表混着两套窗口，看起来像"翻页重复"，其实是测试自己抢跑。
    """
    await wait_until(
        cdp,
        "!document.querySelector('#paper-search-btn').disabled",
        "检索空闲（无在途请求）",
        timeout,
    )


async def search(cdp, q: str) -> None:
    """填词 → 点搜索 → **等这一轮真的跑完**再返回。"""
    await wait_idle(cdp)
    await set_input(cdp, "#paper-q", q)
    await cdp.evaluate("document.querySelector('#paper-search-btn').click(); true")
    await asyncio.sleep(0.15)  # 让 busy 立起来（太快的话下面这步会立刻通过）
    await wait_idle(cdp)
    await wait_until(
        cdp,
        "document.querySelectorAll('.paper-item').length > 0"
        " || document.querySelector('#paper-results .empty')",
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
                "--window-size=1600,900",
                f"http://127.0.0.1:{port}/papers",
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

            # ---- 0. 空库会弹首启引导，先关掉（否则挡着页面）----
            await cdp.evaluate(
                "(() => { const b = [...document.querySelectorAll('#onboard .btn')]"
                ".find(x => x.textContent.includes('开始使用'));"
                " if (b) b.click(); return true; })()"
            )
            await asyncio.sleep(0.3)

            # ---- 1. 三栏布局 + 来源目录渲染 ----
            await wait_until(
                cdp,
                "document.querySelectorAll('#p-sources input[data-source]').length === 3",
                "来源复选框由目录渲染（3 个）",
            )
            layout = await cdp.evaluate("""(() => {
              const main = document.querySelector('main.papers-layout');
              const cols = getComputedStyle(main).gridTemplateColumns.split(' ').length;
              return {
                cols,
                navActive: document.querySelector('nav.main a.active')?.dataset.page || '',
                sourceLabels: [...document.querySelectorAll('#p-sources .p-check span')]
                  .map(n => n.textContent),
                checked: document.querySelectorAll('#p-sources input:checked').length,
              };
            })()""")

            # ---- 2. 检索：三源轮转（arXiv/OpenAlex/CORE 各一）----
            await search(cdp, "水库坝")
            page1 = await cdp.evaluate("""(() => {
              const rows = [...document.querySelectorAll('.paper-item')];
              return {
                count: rows.length,
                sources: rows.slice(0, 3).map(r => r.querySelector('.paper-src').textContent),
                firstTitle: rows[0]?.querySelector('.paper-title')?.textContent || '',
                firstMeta: rows[0]?.querySelector('.paper-meta')?.textContent || '',
                snippetShown: !!rows[0]?.querySelector('.paper-snippet'),
                hasCites: (rows[0]?.querySelector('.paper-meta')?.textContent || '')
                  .includes('被引'),
                moreShown: !document.querySelector('#paper-more').classList.contains('hidden'),
              };
            })()""")

            # ---- 3. 筛选真的发出去了（从假源侧断言，不看 DOM）----
            await wait_idle(cdp)
            recorded_before = len(fake.recorded().get("openalex", []))
            await set_input(cdp, "#p-date-from", "2021-01-01")
            await wait_idle(cdp)  # 改筛选会自动重搜：等它真的跑完
            oa_queries = fake.recorded().get("openalex", [])[recorded_before:]
            year_sent = any(
                "from_publication_date%3A2021-01-01" in q or "from_publication_date:2021-01-01" in q
                for q in oa_queries
            )
            if not year_sent:
                log(f"DEBUG openalex 收到的查询（共 {len(oa_queries)} 条）：{oa_queries[-3:]}")

            # ---- 4. 能力对齐：只勾 arXiv → "被引最多"禁用且有说明 ----
            await cdp.evaluate("""(() => {
              const boxes = [...document.querySelectorAll('#p-sources input[data-source]')];
              for (const b of boxes) if (b.dataset.source !== 'arxiv') b.click();
              return true;
            })()""")
            await wait_idle(cdp)
            caps = await cdp.evaluate("""(() => {
              const cited = document.querySelector('#p-sorts button[data-sort="cited"]');
              return {
                citedDisabled: cited.disabled,
                sortNote: document.querySelector('#p-sort-note').textContent,
                oaChecked: document.querySelector('#p-oa-only').checked,
                oaDisabled: document.querySelector('#p-oa-only').disabled,
                oaNote: document.querySelector('#p-oa-note').textContent,
              };
            })()""")
            # 恢复全选，供后续用例
            await cdp.evaluate("""(() => {
              const boxes = [...document.querySelectorAll('#p-sources input[data-source]')];
              for (const b of boxes) if (!b.checked) b.click();
              return true;
            })()""")
            await wait_idle(cdp)

            # ---- 5. 加载更多：按窗口大小推进 → 两页零重复 ----
            await search(cdp, "水库坝")
            await cdp.evaluate("document.querySelector('#paper-more').click(); true")
            await wait_until(
                cdp,
                "document.querySelectorAll('.paper-item').length > 20",
                "第二页追加",
            )
            paging = await cdp.evaluate("""(() => {
              const refs = [...document.querySelectorAll('.paper-item')]
                .map(r => r.dataset.ref);
              const uniq = new Set(refs);
              const dup = refs.filter((r, i) => refs.indexOf(r) !== i);
              return {count: refs.length, unique: uniq.size, dup: [...new Set(dup)].slice(0, 8),
                      firstPageRefs: refs.slice(0, 4), secondPageRefs: refs.slice(20, 24)};
            })()""")

            # 截图点：结果满屏、筛选栏完整（末尾那张会拍在在途检索的瞬间，是空列表）
            shot = None
            if args.out_shot:
                await search(cdp, "水库坝")
                await asyncio.sleep(0.3)
                res = await cdp.call("Page.captureScreenshot", {"format": "png"})
                shot = res["data"]

            # ---- 6. 详情面板 + 导入 + 已在库中 + 出口 ----
            await cdp.evaluate("document.querySelectorAll('.paper-item')[0].click(); true")
            detail = await cdp.evaluate("""(() => ({
              title: document.querySelector('#paper-detail .pd-title')?.textContent || '',
              body: document.querySelector('#paper-detail .pd-body')?.textContent || '',
              hasImport: !!document.querySelector('#paper-detail .btn.primary'),
            }))()""")
            await cdp.evaluate("document.querySelector('#paper-detail .btn.primary').click(); true")
            await wait_until(
                cdp,
                "(() => { const p = document.querySelector('#paper-detail .paper-inlib');"
                " return !!p; })()",
                "导入后详情面板转为已入库",
            )
            imported = await cdp.evaluate("""(() => ({
              rowBadge: !!document.querySelector('.paper-item[data-ref]:first-child .paper-inlib'),
              badges: document.querySelectorAll('.paper-item .paper-inlib').length,
              exits: [...document.querySelectorAll('#paper-detail .pd-actions a')]
                .map(a => a.textContent),
              toast: [...document.querySelectorAll('#toast .toast-msg')].at(-1)?.textContent || '',
            }))()""")
            docs_after = api_get(port, "/api/documents")["documents"]
            health = api_get(port, "/api/health")["documents"]

            # ---- 7. 无开放获取那条：导入按钮提前禁用 ----
            # 只看 CORE（它的第 1 条假记录故意不带 downloadUrl），点第一条
            await cdp.evaluate("""(() => {
              const boxes = [...document.querySelectorAll('#p-sources input[data-source]')];
              for (const b of boxes) if (b.dataset.source !== 'core' && b.checked) b.click();
              return true;
            })()""")
            await asyncio.sleep(0.4)
            await search(cdp, "水库坝")
            await cdp.evaluate("document.querySelectorAll('.paper-item')[0].click(); true")
            await asyncio.sleep(0.3)
            no_oa = await cdp.evaluate("""(() => {
              const btn = document.querySelector('#paper-detail .btn.primary');
              return {
                src: document.querySelector('.paper-item .paper-src')?.textContent || '',
                exists: !!btn,
                disabled: btn ? btn.disabled : null,
                title: btn ? btn.title : '',
                cites: document.querySelector('#paper-detail .paper-cites')?.textContent || '',
              };
            })()""")
            # 恢复全选
            await cdp.evaluate("""(() => {
              const boxes = [...document.querySelectorAll('#p-sources input[data-source]')];
              for (const b of boxes) if (!b.checked) b.click();
              return true;
            })()""")
            await asyncio.sleep(0.3)

            # ---- 8. 密钥：保存 → 生效；清空 → 清除 ----
            await cdp.evaluate("document.querySelector('#paper-key-toggle').click(); true")
            await set_input(cdp, "#paper-key-input", FAKE_KEY)
            await cdp.evaluate("document.querySelector('#paper-key-save').click(); true")
            await wait_until(
                cdp,
                "document.querySelector('#paper-key-note').textContent.startsWith('已保存')",
                "密钥保存成功",
            )
            key_saved = api_get(port, "/api/papers/settings")
            await set_input(cdp, "#paper-key-input", "")
            await cdp.evaluate("document.querySelector('#paper-key-save').click(); true")
            await wait_until(
                cdp,
                "document.querySelector('#paper-key-note').textContent.startsWith('已清除')",
                "密钥清除",
            )
            key_cleared = api_get(port, "/api/papers/settings")

            report = {
                "layout": layout,
                "page1": page1,
                "yearSent": year_sent,
                "caps": caps,
                "paging": paging,
                "detail": detail,
                "imported": imported,
                "docsAfterImport": len(docs_after),
                "healthAfterImport": health,
                "noOa": no_oa,
                "keySaved": key_saved,
                "keyCleared": key_cleared,
                "consoleErrors": cdp.errors,
            }
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if shot:
                with open(args.out_shot, "wb") as f:
                    f.write(base64.b64decode(shot))
                log(f"截图已写: {args.out_shot}")

            bad = []
            if layout["cols"] != 3:
                bad.append(f"三栏布局没生效（grid 列数 {layout['cols']}）")
            if layout["navActive"] != "papers":
                bad.append(f"顶栏没有高亮「找论文」：{layout['navActive']}")
            if layout["checked"] != 3 or layout["sourceLabels"] != ["arXiv", "OpenAlex", "CORE"]:
                bad.append(f"来源目录渲染不对：{layout}")
            if page1["count"] != PAGE_SIZE:
                bad.append(f"第一页应为 {PAGE_SIZE} 条，实际 {page1['count']}")
            if page1["sources"] != ["arXiv", "OpenAlex", "CORE"]:
                bad.append(f"首三条不是三源轮转：{page1['sources']}")
            if not page1["snippetShown"] or page1["firstMeta"].count("·") < 2:
                bad.append(f"结果行信息不全：{page1}")
            if not page1["hasCites"]:
                bad.append("结果行没有显示被引数据")
            if not page1["moreShown"]:
                bad.append("首屏应显示「加载更多」")
            if not year_sent:
                bad.append("年份筛选没有真的发到上游（openalex 收到的查询里没有该条件）")
            if not caps["citedDisabled"]:
                bad.append("只选 arXiv 时「被引最多」应整项禁用")
            if "被引" not in caps["sortNote"]:
                bad.append(f"禁用原因没有说明：{caps['sortNote']}")
            if not caps["oaChecked"] or not caps["oaDisabled"]:
                bad.append(f"全 OA 来源下「只看开放获取」应为已勾选且禁用：{caps}")
            if "已自动满足" not in caps["oaNote"]:
                bad.append(f"「已满足」与「不支持」的文案没分开：{caps['oaNote']}")
            if paging["count"] != paging["unique"]:
                bad.append(f"翻页出现重复结果：{paging}")
            if not detail["title"] or not detail["body"] or not detail["hasImport"]:
                bad.append(f"详情面板内容不全：{detail}")
            if not imported["rowBadge"] or imported["badges"] < 1:
                bad.append(f"导入后结果行没标「已在库中」：{imported}")
            if "去提问" not in imported["exits"] or "去知识库" not in imported["exits"]:
                bad.append(f"导入后没有出口：{imported['exits']}")
            if "入库成功" not in imported["toast"]:
                bad.append(f"导入 toast 不对：{imported['toast']}")
            if len(docs_after) != 1 or health != 1:
                bad.append(f"入库文档数不对：docs={len(docs_after)} health={health}")
            if not no_oa["exists"] or not no_oa["disabled"]:
                bad.append(f"无开放获取的 CORE 记录应禁用导入按钮：{no_oa}")
            if "不提供" not in no_oa["cites"] and "被引" not in no_oa["cites"]:
                bad.append(f"CORE 被引数据没显示：{no_oa['cites']}")
            if not key_saved.get("has_api_key"):
                bad.append("密钥保存后 has_api_key 仍为假")
            if key_cleared.get("has_api_key"):
                bad.append("清空保存后密钥没被清除")
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
    parser = argparse.ArgumentParser(description="无头 Chrome 找论文页验收")
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
