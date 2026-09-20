#!/usr/bin/env python
"""无头 Chrome 评测页 E2E 验收（自动题库 + 评测全流程，中文注释纪律）。

为什么自带一台假模型：自动出题要调几十次 LLM、整场评测再调几十次，真机
local 档一轮要几分钟且结果不可复现。这里起一台**本机回环的假 OpenAI 兼容
服务**，把配置里的 llm 指过去——与找论文页用假源（chrome_papers.py）同一
套路：被测的是**前端与 Web 链路的真实行为**，模型输出只是让流程能动起来。

走一遍完整用户流程：
  1. /eval 亮出两份题库：内置示例语料（63 题，默认选中）与「我的资料」
     （未生成、置灰）；
  2. 点「为我的资料生成题库」→ 出题面板出现、进度轮询推进 → 完成后
     「我的资料」变为可用且自动切为当前，提示写明"只能横向比"；
  3. 开始评测 → 任务面板 → 完成后历史列表出现记录、报告自动打开，
     标题写明题库名 mikasa-auto（证明确实用的是自动题库）；
  4. 切回内置题库 → 提示回到人工题库口径；
  5. 全程 console 零错误（含未捕获异常；Runtime.enable + reload 才能捕获
     模块求值期抛错——探针纪律见 chrome_probe.py）。

用法：
  python tools/chrome_eval.py [--out-shot tools/shots/eval-e2e.png]
  [--server-port N] [--cdp-port N] [--fake-port N] [--chrome <路径>]
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
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mikasa.config.settings import load_settings  # noqa: E402
from mikasa.ingest.service import IngestService  # noqa: E402
from tools.chrome_corpus import (  # noqa: E402
    free_port,
    js_quote,
    wait_server,
    wait_until,
)
from tools.chrome_probe import CDP, log, wait_json_list  # noqa: E402

REPO_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIKASA_EXE = REPO_ROOT / ".venv" / "Scripts" / "mikasa.exe"

# 出题与评测的自报题数（端点默认值 24 + 8）；进度条分母即 32
SYNTH_TOTAL = 32

_CONFIG_TEMPLATE = """\
# 评测页 E2E 专用配置：offline.yaml 的等价物，只把 LLM 指向本机假服务。
# 与 offline 一致的取舍：BM25 单路、无重排、无裁判、无跨语言/双语增强
# ——保证 E2E 确定性且零外部依赖。
profile: offline
data_dir: {data_dir}

chunking:
  size: 400
  overlap: 50
  title_prefix: true

retrieval:
  bm25_top_k: 20
  dense_top_k: 20
  dense_enabled: false
  fusion_top_k: 10
  fusion_k: 60

reranker:
  backend: none

llm:
  backend: local          # OpenAI 兼容客户端（免密钥放行），端点 = 本机假服务
  base_url: http://127.0.0.1:{fake_port}/v1
  api_key_env: ""
  model: fake-eval-1
  temperature: 0.0
  max_tokens: 512

embedding:
  backend: none

judge:
  enabled: false
"""


def _fake_reply(system: str, user: str) -> str:
    """按提示词分流给固定文案：出题（可答/不可答）与问答各一条。

    出题系统提示词含「出题人」（synth._SYSTEM）；不可答模板的 user 里含
    「回答不了」。问答（评测作答）给一条带 [1] 引用的短答，让引用链路也
    真实走过一遍。
    """
    if "出题人" in system:
        if "回答不了" in user:
            return "这些资料里没有涉及的那类防护措施是什么？"
        return "这份资料提到的关键参数或方法是什么？"
    return "根据资料中的说明：[1] 这是端到端验收用的模拟回答。"


class _FakeLLMHandler(BaseHTTPRequestHandler):
    """假 OpenAI 兼容端点：POST /v1/chat/completions（非流式）。"""

    calls = 0  # 类级计数：收尾时打印，便于判断"到底调没调模型"

    def log_message(self, *args):  # noqa: D102 - 静音 http.server 的 stderr 噪声
        pass

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 的接口名
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        messages = body.get("messages") or []
        system = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
        user = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
        _FakeLLMHandler.calls += 1
        payload = {
            "id": "chatcmpl-fake-eval",
            "object": "chat.completion",
            "created": 0,
            "model": body.get("model", "fake-eval-1"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": _fake_reply(system, user)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _start_fake_llm(port: int) -> ThreadingHTTPServer:
    """起假模型服务（守护线程；finally 里 shutdown）。"""
    server = ThreadingHTTPServer(("127.0.0.1", port), _FakeLLMHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def click(cdp, selector: str, what: str) -> None:
    """点击元素（找不到即失败——比静默返回更有诊断价值）。"""
    ok = await cdp.evaluate(
        f"(() => {{ const n = document.querySelector({js_quote(selector)}); "
        "if (!n) return false; n.click(); return true; })()"
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


async def run(args):
    tmp = Path(tempfile.mkdtemp(prefix="eval-e2e-"))
    fake = None
    server = None
    chrome = None
    try:
        args.server_port = args.server_port or free_port()
        args.cdp_port = args.cdp_port or free_port()
        args.fake_port = args.fake_port or free_port()

        # 0) 假模型服务先起好：App 一起来（含预检）就能用
        fake = _start_fake_llm(args.fake_port)
        log(f"假模型服务：http://127.0.0.1:{args.fake_port}/v1/chat/completions")

        # 1) 隔离配置 + 灌示例语料（内置题库的 47 道可答题与它逐块对齐）
        data_dir = (tmp / "data").as_posix()
        cfg = tmp / "serve.yaml"
        cfg.write_text(
            _CONFIG_TEMPLATE.format(data_dir=data_dir, fake_port=args.fake_port),
            encoding="utf-8",
        )
        settings = load_settings("offline", cfg)
        log("灌入示例语料…")
        IngestService(settings).ingest_paths([REPO_ROOT / "sample-corpus"])

        # 2) 起隔离服务器（真实 Web 进程；--profile 与 --config 并用同 chrome_notes）
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

        url = f"http://127.0.0.1:{args.server_port}/eval"
        profile = tempfile.mkdtemp(prefix="eval-chrome-")
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
            # 未捕获异常（模块求值期抛错等）走 Runtime.exceptionThrown，不是
            # Log.entryAdded；且只在加载那一刻抛出 → 连上后必须重载一次
            await cdp.call("Runtime.enable")
            await cdp.call("Page.reload")
            await asyncio.sleep(0.4)
            await wait_until(cdp, "document.readyState === 'complete'", "评测页加载")
            await wait_until(
                cdp,
                "document.querySelectorAll('#golden-banks button').length === 2",
                "题库按钮渲染",
                timeout=25.0,
            )
            # 首启引导：本 E2E 库非空（57 块）不该弹；弹出的窗口会挡住真实点击
            if await cdp.evaluate("!!document.querySelector('.onboard-mask')"):
                await click_text(cdp, ".onboard-card button", "开始使用")
                await wait_until(cdp, "!document.querySelector('.onboard-mask')", "首启引导关闭")
            await asyncio.sleep(0.6)  # 等 loadBanks 渲染后的 hint 更新

            # 1) 默认态：内置题库 63 题可点且选中；「我的资料」未生成、置灰
            banks = await cdp.evaluate(
                "(() => [...document.querySelectorAll('#golden-banks button')]"
                ".map(b => ({text: b.textContent, disabled: b.disabled,"
                " primary: b.classList.contains('primary')})))()"
            )
            if "内置示例语料（63 题）" not in banks[0]["text"]:
                bad.append(f"内置题库按钮文案不对：{banks[0]['text']!r}")
            if banks[0]["disabled"] or not banks[0]["primary"]:
                bad.append(f"内置题库应可用且默认选中：{banks[0]}")
            if "我的资料（未生成）" not in banks[1]["text"] or not banks[1]["disabled"]:
                bad.append(f"「我的资料」初始应未生成且置灰：{banks[1]}")
            hint = await cdp.evaluate("document.querySelector('#corpus-hint').textContent")
            if "63 题人工题库" not in hint:
                bad.append(f"内置题库提示文案不对：{hint!r}")

            # 2) 自动出题：面板出现 → 轮询把进度推到完成 → 题库清单刷新并切过去
            await click(cdp, "#synth-bank", "为我的资料生成题库按钮")
            await wait_until(
                cdp, "!document.querySelector('#synth-panel').hidden", "出题面板出现", timeout=15.0
            )
            await wait_until(
                cdp,
                """(() => { const b = document.querySelectorAll('#golden-banks button')[1];
                    return !!b && !b.disabled && b.classList.contains('primary'); })()""",
                "自动题库就绪并选中",
                timeout=180.0,
            )
            auto_text = await cdp.evaluate(
                "document.querySelectorAll('#golden-banks button')[1].textContent"
            )
            if "我的资料（" not in auto_text or "题）" not in auto_text:
                bad.append(f"自动题库按钮没显示题数：{auto_text!r}")
            ratio = await cdp.evaluate("document.querySelector('#synth-ratio').textContent.trim()")
            done_s, _, total_s = ratio.partition(" / ")
            if not (done_s.strip().isdigit() and total_s.strip().isdigit()):
                bad.append(f"出题进度数字格式不对：{ratio!r}")
            elif int(done_s) != int(total_s) or int(total_s) != SYNTH_TOTAL:
                bad.append(f"出题结束时进度不是满格：{ratio!r}（应 {SYNTH_TOTAL} / {SYNTH_TOTAL}）")
            meta = await cdp.evaluate("document.querySelector('#synth-meta').textContent")
            if "道可答题" not in meta or "道不可答题" not in meta:
                bad.append(f"出题收尾文案没报两类题数：{meta!r}")
            hint = await cdp.evaluate("document.querySelector('#corpus-hint').textContent")
            if "自动生成" not in hint or "横向比较" not in hint:
                bad.append(f"自动题库提示没写明「只能横向比」：{hint!r}")

            # 3) 用自动题库跑一场评测：任务面板 → 历史出现记录 → 报告自动打开
            await click(cdp, "#start-eval", "开始评测按钮")
            await wait_until(
                cdp,
                "!document.querySelector('#job-panel').hidden",
                "评测进度面板出现",
                timeout=15.0,
            )
            await wait_until(
                cdp,
                "document.querySelectorAll('#run-list .run-item').length >= 1",
                "评测完成后历史列表出现记录",
                timeout=300.0,
            )
            # 列表先渲染、报告是随后一次异步打开（openRun 还要再取一遍详情）
            # ——不等标题落定就读，会读到空（本工具第一版实测踩到的竞态）
            await wait_until(
                cdp,
                "document.querySelector('#report-head').textContent.length > 0",
                "报告自动打开",
                timeout=30.0,
            )
            run_name = await cdp.evaluate(
                "document.querySelector('#run-list .run-item .r-name').textContent"
            )
            if "mikasa-auto" not in run_name:
                bad.append(f"历史记录没标明用的是自动题库：{run_name!r}")
            pill = await cdp.evaluate(
                "document.querySelector('#run-list .run-item .pill').textContent"
            )
            if "完成" not in pill:
                bad.append(f"评测记录状态不是完成：{pill!r}")
            head = await cdp.evaluate("document.querySelector('#report-head').textContent")
            if "mikasa-auto" not in head:
                bad.append(f"报告标题没写明题库名：{head!r}")
            body_len = await cdp.evaluate(
                "document.querySelector('#report-body').textContent.length"
            )
            if body_len < 200:
                bad.append(f"报告正文没渲染出来（{body_len} 字符）")
            # 报告表格的渲染细节（renderMarkdown，只服务评测报告页）：
            # 分隔行不能被当成数据行（"| --- | --- |" 曾整行显示在表里），
            # 单元格里的 **粗体** 要真的加粗（黄金集行用它标"自动生成"）
            table_shape = await cdp.evaluate(
                """(() => {
                  const t = document.querySelector('#report-body table');
                  if (!t) return null;
                  const cells = [...t.querySelectorAll('td,th')].map((c) => c.textContent.trim());
                  return {
                    rows: t.querySelectorAll('tbody tr').length,
                    dashCells: cells.filter((c) => /^-{3,}$/.test(c)).length,
                    strong: t.querySelectorAll('strong').length,
                  };
                })()"""
            )
            if not table_shape or table_shape["rows"] < 1:
                bad.append(f"报告里没有渲染出表格：{table_shape}")
            else:
                if table_shape["dashCells"]:
                    bad.append(f"表格把 --- 分隔行当数据行显示：{table_shape}")
                if table_shape["strong"] < 1:
                    bad.append(f"单元格里的粗体没生效：{table_shape}")

            # 4) 切回内置题库：提示回到人工题库口径
            await click_text(cdp, "#golden-banks button", "内置示例语料")
            hint = await cdp.evaluate("document.querySelector('#corpus-hint').textContent")
            if "63 题人工题库" not in hint:
                bad.append(f"切回内置题库后提示没跟着换：{hint!r}")

            # 5) 截图 + console 错误收尾
            if args.out_shot:
                res = await cdp.call("Page.captureScreenshot", {"format": "png"})
                Path(args.out_shot).write_bytes(base64.b64decode(res["data"]))
                log(f"截图已写: {args.out_shot}")
            await asyncio.sleep(1.5)  # 静默给 1.5s 消化异步报错
            if cdp.errors:
                bad.append("console 有错误：" + "；".join(cdp.errors[:3]))

        log(f"假模型共被调用 {_FakeLLMHandler.calls} 次")
        if bad:
            log("验收未过：\n  - " + "\n  - ".join(bad))
            log("服务日志尾：\n" + (tmp / "serve.log").read_text(encoding="utf-8")[-2000:])
            return 1
        log("评测页 E2E 验收通过（自动题库 + 评测全流程）")
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
        if fake is not None:
            fake.shutdown()
            fake.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="无头 Chrome 评测页 E2E 验收")
    parser.add_argument("--out-shot", default=None, help="截图输出路径（PNG）")
    parser.add_argument("--server-port", type=int, default=None, help="隔离服务器端口")
    parser.add_argument("--cdp-port", type=int, default=None, help="Chrome 调试端口")
    parser.add_argument("--fake-port", type=int, default=None, help="假模型服务端口")
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
