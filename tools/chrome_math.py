#!/usr/bin/env python
"""无头 Chrome 公式渲染 E2E 验收（2026-09-20，中文注释纪律）。

**为什么必须进浏览器**：`smoke_math.mjs` 用真 KaTeX 在 node 里跑 `renderToString`
——那验的是"字符串渲染"，验不到三件只有浏览器知道的事：
  1. `globalThis.katex` 在页面里真的可用（脚本标签加载成功、执行顺序对）；
  2. **字体真的加载并生效**（woff2 的 Content-Type 在本机 Python 的 mimetypes 里
     查不到，静态服务回的是 application/octet-stream——浏览器认不认只能实测）；
  3. KaTeX 的 CSS 真的生效（样式标签顺序、路径）。
任何一条坏掉，公式都会掉成回退字形或裸 LaTeX，而后端测试完全看不见。

做法：自带一台假 OpenAI 兼容服务（照 chrome_note_ocr.py），让它流式返回一段
**带公式的真实排版样本**（块级 / 行内 / 货币误判 / 代码里的 $），页面走真实
SSE 问答链路渲染，然后断言：
  - `.katex` 元素出现且数量对得上；
  - 浏览器**确实去取了** KaTeX 字体（performance 资源条目）；
  - 货币 `$5`/`$10` 保持字面（没被当公式吞掉）；
  - 围栏代码里的 `$HOME` 保持字面；
  - console 零错误（字体解码失败会在这里现形）。

用法：
  python tools/chrome_math.py [--out-shot tools/shots/math-e2e.png]
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
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.chrome_corpus import (  # noqa: E402
    free_port,
    wait_server,
    wait_until,
)
from tools.chrome_probe import CDP, log, wait_json_list  # noqa: E402
from tools.fake_llm import make_handler, serve  # noqa: E402

REPO_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIKASA_EXE = REPO_ROOT / ".venv" / "Scripts" / "mikasa.exe"

# 假模型吐出的"回答"：刻意混入四类样本，一次把公式管线的边界都压上
ANSWER = """\
泰勒展开把函数写成幂级数：

$$e^x = 1 + \\frac{x^2}{2!} + \\cdots$$

行内也要能认：阶乘 $n!$、区间 $(-1, 1]$、递推 $2n+1$。

但价格 $5到$10 与编号 $1000 不是公式，代码里的 $HOME 也不是：

```bash
echo $HOME
```
"""

_CONFIG_TEMPLATE = """\
# 公式渲染 E2E 专用配置：local 档（自由问答开关可用）+ LLM 指向假服务。
profile: local
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
  backend: local          # 免密钥（占位钥匙），端点 = 本机假服务
  base_url: http://127.0.0.1:{fake_port}/v1
  api_key_env: ""
  model: fake-math-1
  temperature: 0.0
  max_tokens: 1024

embedding:
  backend: none

judge:
  enabled: false
"""


_FAKE = make_handler(ANSWER)  # 假模型端点类（calls 里留取证记录）


async def run(args):
    tmp = Path(tempfile.mkdtemp(prefix="math-e2e-"))
    fake = None
    server = None
    chrome = None
    try:
        args.server_port = args.server_port or free_port()
        args.cdp_port = args.cdp_port or free_port()
        args.fake_port = args.fake_port or free_port()

        fake = serve(_FAKE, args.fake_port)
        log(f"假模型服务：http://127.0.0.1:{args.fake_port}/v1/chat/completions")

        data_dir = (tmp / "data").as_posix()
        cfg = tmp / "serve.yaml"
        cfg.write_text(
            _CONFIG_TEMPLATE.format(data_dir=data_dir, fake_port=args.fake_port),
            encoding="utf-8",
        )
        with open(tmp / "serve.log", "w", encoding="utf-8") as logf:
            server = subprocess.Popen(
                [
                    str(MIKASA_EXE),
                    "serve",
                    "--profile",
                    "local",
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

        url = f"http://127.0.0.1:{args.server_port}/"
        profile = tempfile.mkdtemp(prefix="math-chrome-")
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
            await cdp.call("Runtime.enable")
            await cdp.call("Page.reload")
            await asyncio.sleep(0.4)
            await wait_until(cdp, "document.readyState === 'complete'", "问答页加载")
            await wait_until(
                cdp, "!!document.querySelector('#question')", "提问框就绪", timeout=25.0
            )
            await asyncio.sleep(1.0)
            if await cdp.evaluate("!!document.querySelector('.onboard-mask')"):
                await cdp.evaluate(
                    "[...document.querySelectorAll('.onboard-card button')]"
                    ".find(b => b.textContent.includes('开始使用'))?.click()"
                )
                await wait_until(cdp, "!document.querySelector('.onboard-mask')", "首启引导关闭")

            # 1) 切到自由问答并提问（kb 要有语料；free 不需要，正合适验渲染）
            await cdp.evaluate("document.querySelector('#mode-free').click(); true")
            await cdp.evaluate(
                "(() => { const n = document.querySelector('#question');"
                " n.value = '泰勒展开是什么';"
                " n.dispatchEvent(new Event('input', {bubbles:true})); return true; })()"
            )
            await cdp.evaluate("document.querySelector('#send').click(); true")
            try:
                # 样本里公式共 **4** 处（1 块级 + 3 行内）——阈值跟着样本走，
                # 数错一个就会白等一分钟（本工具第一版实测）
                await wait_until(
                    cdp,
                    "document.querySelectorAll('.msg.assistant .katex').length >= 4",
                    "公式渲染成 KaTeX",
                    timeout=60.0,
                )
            except RuntimeError:
                # 超时失败必须带现场：是"回答没到"还是"到了但没渲染"，两种病因
                # 修法完全不同（本工具第一版就栽在只有一句"超时"上）
                diag = await cdp.evaluate("""(() => {
                  const b = document.querySelector('.msg.assistant .bubble');
                  return {
                    msgs: document.querySelectorAll('.msg.assistant').length,
                    katex: b ? b.querySelectorAll('.katex').length : -1,
                    text: b ? b.textContent.slice(0, 200) : '(没有回答气泡)',
                    katexGlobal: typeof globalThis.katex,
                  };
                })()""")
                log("超时现场：" + json.dumps(diag, ensure_ascii=False))
                raise
            await asyncio.sleep(1.2)  # 等字体请求与流式收尾

            metrics = await cdp.evaluate("""(() => {
              const bubble = document.querySelector('.msg.assistant .bubble');
              const fonts = performance.getEntriesByType('resource')
                .filter(e => e.name.includes('/vendor/katex/fonts/'));
              // 残迹判据要排除 .katex 子树：KaTeX 的 HTML+MathML 输出里带一个
              // <annotation> 存着源 LaTeX，直接读 textContent 会把渲染成功的
              // 公式误判成"没渲染"（本工具第一版实测踩到）
              const clone = bubble.cloneNode(true);
              clone.querySelectorAll('.katex').forEach(n => n.remove());
              return {
                katex: bubble.querySelectorAll('.katex').length,
                katexDisplay: bubble.querySelectorAll('.katex-display').length,
                rawLatex: (clone.textContent.match(/\\$\\$|\\\\frac/g) || []).length,
                currency: bubble.textContent.includes('$5到$10'),
                codeDollar: bubble.textContent.includes('$HOME'),
                fontFiles: fonts.length,
                fontNames: fonts.slice(0, 3).map(f => f.name.split('/').pop()),
                fontStatus: document.fonts.status,
                katexLoaded: typeof globalThis.katex === 'object',
              };
            })()""")
            log("指标：" + json.dumps(metrics, ensure_ascii=False))

            if metrics["katex"] < 4:
                bad.append(f"KaTeX 元素太少（样本里应有 4 处）：{metrics}")
            if metrics["katexDisplay"] < 1:
                bad.append("块级公式没有 .katex-display（$$…$$ 未被识别为块级）")
            if metrics["rawLatex"]:
                bad.append(f"正文里还有未渲染的 LaTeX 残迹：{metrics['rawLatex']}")
            if not metrics["currency"]:
                bad.append("货币写法 $5到$10 被误当公式吃掉了")
            if not metrics["codeDollar"]:
                bad.append("代码块里的 $HOME 被误渲染了")
            if not metrics["katexLoaded"]:
                bad.append("globalThis.katex 不可用（脚本没加载或顺序不对）")
            # 字体：浏览器必须**真的去取**这些文件（KaTeX 按需加载字体）
            if metrics["fontFiles"] < 1:
                bad.append("一个 KaTeX 字体文件都没请求——公式会掉成回退字形")

            if args.out_shot:
                res = await cdp.call("Page.captureScreenshot", {"format": "png"})
                Path(args.out_shot).write_bytes(base64.b64decode(res["data"]))
                log(f"截图已写: {args.out_shot}")
            await asyncio.sleep(1.0)
            if cdp.errors:
                bad.append("console 有错误：" + "；".join(cdp.errors[:3]))

        if bad:
            log("验收未过：\n  - " + "\n  - ".join(bad))
            log("服务日志尾：\n" + (tmp / "serve.log").read_text(encoding="utf-8")[-1500:])
            return 1
        log("公式渲染 E2E 验收通过（KaTeX 元素 / 字体请求 / 货币与代码不误伤）")
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
    parser = argparse.ArgumentParser(description="无头 Chrome 公式渲染 E2E 验收")
    parser.add_argument("--out-shot", default=None, help="截图输出路径（PNG）")
    parser.add_argument("--server-port", type=int, default=None)
    parser.add_argument("--cdp-port", type=int, default=None)
    parser.add_argument("--fake-port", type=int, default=None)
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
