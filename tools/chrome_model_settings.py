#!/usr/bin/env python
"""无头 Chrome 模型设置 E2E 验收（设置面板「模型」段，ADR-0018）。

自起一台隔离服务器（offline profile + MIKASA_DATA_DIR 指向临时目录，
真实 Web 进程），在真实问答页的面板上走一遍用户流程：

  1. 齿轮开面板 → 「模型」段从服务端回填（offline 档显示 mock 模型名）；
  2. 点 DeepSeek 预设 → 地址/模型自动填好（不保存）；
  3. 粘贴密钥 → 保存并生效 → 轮询服务端确认模型已切换（**热生效**）；
  4. 宿主机侧断言落盘：覆盖层只有 llm 段且**不含密钥**，密钥在 .env；
  5. 错误路径：地址改成打不通的端口 → 「测试连接」出红色错误文案；
  6. **重启服务器** → 配置仍在（持久化闭环，覆盖层真的生效）；
  7. 刷新页面 → 面板字段由服务端回填（不是 localStorage）。
  全程收集 console 错误，有错退出码 1。

**为什么不用 --config 注入 data_dir**（chrome_corpus.py 的老办法）：
--config 会让 settings.config_path 非空 → 面板进入只读态（locked），
这条验收恰恰要验证"面板可写"，所以隔离必须走 MIKASA_DATA_DIR 环境变量。

用法：
  python tools/chrome_model_settings.py [--out-shot tools/shots/model-settings-e2e.png]
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
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.chrome_probe import CDP, log, wait_json_list  # noqa: E402

REPO_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIKASA_EXE = REPO_ROOT / ".venv" / "Scripts" / "mikasa.exe"

FAKE_KEY = "sk-e2e-fake-0001"  # 只写进临时数据目录，不可能是真密钥
BAD_URL = "http://127.0.0.1:9/v1"  # 9 号端口：必然连接拒绝，且不产生外部调用


def js_quote(s: str) -> str:
    """JS 字符串字面量（跨 evaluate 传值）。"""
    return json.dumps(s, ensure_ascii=False)


def free_port() -> int:
    """向系统要一个空闲端口（避免撞上残留的旧服务/旧 Chrome）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def api_get(port: int, path: str) -> dict:
    """宿主机侧直接读服务端 JSON（不经页面，用于断言真实状态）。"""
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as resp:
        return json.loads(resp.read().decode("utf-8"))


def start_server(port: int, env: dict, logfile) -> subprocess.Popen:
    """起隔离服务器（真实进程；数据目录由 MIKASA_DATA_DIR 决定）。"""
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
    """等 uvicorn 起来。

    就绪判定 = /api/settings/model 返回 200 且带 has_api_key 键——端口上
    若盘踞着旧构建的 Mikasa（没有这个端点，返回 404），绝不能误认成自己。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            body = api_get(port, "/api/settings/model")
            if "has_api_key" in body:
                return
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            pass
        time.sleep(0.3)
    raise RuntimeError("服务器未就绪（日志见 tmpdir 下 serve.log）")


async def set_input(cdp, selector: str, value: str) -> None:
    """设 input 值并派发 input 事件（密钥框的 keyDirty 跟踪挂在 input 上）。

    一律包 IIFE：evaluate 共享页面全局作用域，顶层 const 二次声明会
    SyntaxError（踩过）。
    """
    await cdp.evaluate(
        f"(() => {{ const i = document.querySelector({js_quote(selector)}); "
        f"i.value = {js_quote(value)}; "
        f"i.dispatchEvent(new Event('input', {{bubbles: true}})); return true; }})()"
    )


async def wait_until(cdp, expr: str, what: str, timeout: float = 25.0) -> None:
    """轮询页面表达式直到真值；超时抛错（失败信息带轮询目标）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await cdp.evaluate(expr):
            return
        await asyncio.sleep(0.2)
    raise RuntimeError(f"超时等不到：{what}")


async def run(args):
    tmp = Path(tempfile.mkdtemp(prefix="model-settings-e2e-"))
    userdata = tmp / "userdata"
    userdata.mkdir(parents=True)
    env = dict(os.environ)
    env["MIKASA_DATA_DIR"] = str(userdata)  # 隔离的生命线：所有写入落在 tmp
    server = None
    chrome = None
    try:
        if args.server_port is None:
            args.server_port = free_port()
        if args.cdp_port is None:
            args.cdp_port = free_port()
        port = args.server_port
        logfile = open(tmp / "serve.log", "w", encoding="utf-8")  # noqa: SIM115 - 进程生命周期内持有
        server = start_server(port, env, logfile)
        try:
            wait_server(port)
        except RuntimeError:
            log("服务器启动失败，serve.log 尾：")
            log((tmp / "serve.log").read_text(encoding="utf-8")[-3000:])
            raise

        url = f"http://127.0.0.1:{port}/"
        profile = tempfile.mkdtemp(prefix="model-settings-chrome-")
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
        async with CDP(target["webSocketDebuggerUrl"]) as cdp:
            await cdp.call("Page.enable")
            await cdp.call("Log.enable")
            for _ in range(60):
                state = await cdp.evaluate(
                    "document.readyState + '|' + String(!!document.querySelector('#btn-settings'))"
                )
                if state == "complete|true":
                    break
                await asyncio.sleep(0.25)
            await asyncio.sleep(0.8)

            # ---- 0. 空库会弹首启引导，先关掉（否则截图全是它）----
            await cdp.evaluate(
                "(() => { const b = [...document.querySelectorAll('#onboard .btn')]"
                ".find(x => x.textContent.includes('开始使用'));"
                " if (b) b.click(); return true; })()"
            )
            await asyncio.sleep(0.3)

            # ---- 1. 开面板：「模型」段由服务端回填（offline 档 = mock 模型名）----
            await cdp.evaluate("document.querySelector('#btn-settings').click(); true")
            await wait_until(
                cdp,
                "(() => { const m = document.querySelector('#s-model');"
                " return !!m && m.value !== ''; })()",
                "面板模型字段回填（GET /api/settings/model）",
            )
            panel_visible = await cdp.evaluate(
                "!document.querySelector('#settings-panel').classList.contains('hidden')"
            )
            initial = await cdp.evaluate("""(() => ({
              model: document.querySelector('#s-model').value,
              baseUrl: document.querySelector('#s-base-url').value,
              chips: [...document.querySelectorAll('#s-provider .s-chip')]
                .map(c => c.dataset.preset),
              keyRowHidden: document.querySelector('#s-api-key').closest('.s-row')
                .classList.contains('hidden'),
            }))()""")

            # ---- 2. 点 DeepSeek 预设：只回填，不保存 ----
            await cdp.evaluate(
                "document.querySelector('#s-provider .s-chip[data-preset=\"deepseek\"]')"
                ".click(); true"
            )
            await asyncio.sleep(0.2)
            preset_filled = await cdp.evaluate("""(() => ({
              model: document.querySelector('#s-model').value,
              baseUrl: document.querySelector('#s-base-url').value,
              keyRowHidden: document.querySelector('#s-api-key').closest('.s-row')
                .classList.contains('hidden'),
            }))()""")
            health_before = api_get(port, "/api/health")["llm_model"]

            # ---- 3. 粘贴密钥 → 保存并生效 → 热生效断言 ----
            await set_input(cdp, "#s-api-key", FAKE_KEY)
            await cdp.evaluate("document.querySelector('#s-save-btn').click(); true")
            await wait_until(
                cdp,
                "fetch('/api/settings/model').then(r => r.json())"
                ".then(d => d.model === 'deepseek-chat')",
                "保存后服务端模型已切到 deepseek-chat（热生效）",
            )
            await asyncio.sleep(0.3)
            after_save = await cdp.evaluate("""(() => ({
              model: document.querySelector('#s-model').value,
              keyCleared: document.querySelector('#s-api-key').value === '',
              chipOn: document.querySelector('#s-provider .s-chip.on')?.dataset.preset || '',
              hint: document.querySelector('#s-model-hint').textContent.slice(0, 24),
            }))()""")
            health_after = api_get(port, "/api/health")["llm_model"]

            # ---- 4. 落盘断言（宿主机侧读文件）----
            overlay = (userdata / "config.yaml").read_text(encoding="utf-8")
            env_file = (userdata / ".env").read_text(encoding="utf-8")

            # ---- 5. 错误路径：打不通的地址 → 测试连接出红色文案 ----
            await set_input(cdp, "#s-base-url", BAD_URL)
            await cdp.evaluate("document.querySelector('#s-test-btn').click(); true")
            await wait_until(
                cdp,
                "(() => { const n = document.querySelector('#s-test-result');"
                " return n.classList.contains('error') && n.textContent.length > 0; })()",
                "测试连接失败文案（错误路径）",
            )
            test_error = await cdp.evaluate("document.querySelector('#s-test-result').textContent")
            # 失败路径不落盘：服务端仍是保存时的配置
            health_after_error = api_get(port, "/api/health")["llm_model"]

            # ---- 6.「视觉模型」段（M6 ②）：初始未接入 → 切 SiliconFlow → 保存 ----
            vision_initial = await cdp.evaluate("""(() => ({
              chips: [...document.querySelectorAll('#v-provider .s-chip')]
                .map(c => c.dataset.preset),
              chipOn: document.querySelector('#v-provider .s-chip.on')?.dataset.preset || '',
              baseUrlRowHidden: document.querySelector('#v-base-url').closest('.s-row')
                .classList.contains('hidden'),
            }))()""")
            await cdp.evaluate(
                "document.querySelector('#v-provider .s-chip[data-preset=\"siliconflow\"]')"
                ".click(); true"
            )
            await asyncio.sleep(0.2)
            vision_preset = await cdp.evaluate("""(() => ({
              model: document.querySelector('#v-model').value,
              baseUrl: document.querySelector('#v-base-url').value,
              keyRowHidden: document.querySelector('#v-api-key').closest('.s-row')
                .classList.contains('hidden'),
            }))()""")
            await cdp.evaluate("document.querySelector('#v-save-btn').click(); true")
            await wait_until(
                cdp,
                "fetch('/api/settings/vision').then(r => r.json()).then(d => d.backend === 'api')",
                "视觉段保存后服务端已切换（热生效）",
            )
            vision_saved = await cdp.evaluate(
                "document.querySelector('#v-provider .s-chip.on')?.dataset.preset || ''"
            )
            # ★ 红线（端到端版）：写视觉段**不能把模型段抹掉**（覆盖层是同一份 YAML）
            overlay_after_vision = (userdata / "config.yaml").read_text(encoding="utf-8")
            model_after_vision = api_get(port, "/api/settings/model")

            shot = None
            if args.out_shot:
                res = await cdp.call("Page.captureScreenshot", {"format": "png"})
                shot = res["data"]

            # ---- 6. 重启服务器：配置持久（覆盖层真的在生效）----
            stop_server(server)
            logfile.close()
            logfile = open(tmp / "serve2.log", "w", encoding="utf-8")  # noqa: SIM115
            server = start_server(port, env, logfile)
            wait_server(port)
            health_restart = api_get(port, "/api/health")["llm_model"]
            model_after_restart = api_get(port, "/api/settings/model")

            # ---- 7. 刷新页面：面板字段由服务端回填 ----
            await cdp.call("Page.reload", {"ignoreCache": True})
            await asyncio.sleep(2.0)
            await cdp.evaluate("document.querySelector('#btn-settings').click(); true")
            await wait_until(
                cdp,
                "(() => { const m = document.querySelector('#s-model');"
                " return !!m && m.value === 'deepseek-chat'; })()",
                "刷新后面板回填服务端配置",
            )
            after_reload = await cdp.evaluate("""(() => ({
              baseUrl: document.querySelector('#s-base-url').value,
              hasKey: document.querySelector('#s-api-key').placeholder.includes('已保存'),
              chipOn: document.querySelector('#s-provider .s-chip.on')?.dataset.preset || '',
              vChipOn: document.querySelector('#v-provider .s-chip.on')?.dataset.preset || '',
              vModel: document.querySelector('#v-model').value,
            }))()""")
            await asyncio.sleep(0.8)

            report = {
                "panelVisible": panel_visible,
                "initial": initial,
                "healthBefore": health_before,
                "presetFilled": preset_filled,
                "afterSave": after_save,
                "healthAfter": health_after,
                "healthAfterError": health_after_error,
                "healthAfterRestart": health_restart,
                "modelAfterRestart": model_after_restart,
                "overlayHasKey": FAKE_KEY in overlay,
                "overlayHasModel": "deepseek-chat" in overlay,
                "envFileHasKey": "DEEPSEEK_API_KEY" in env_file,
                "testError": test_error[:80],
                "visionInitial": vision_initial,
                "visionPreset": vision_preset,
                "visionSaved": vision_saved,
                "modelAfterVision": model_after_vision["model"],
                "afterReload": after_reload,
                "consoleErrors": cdp.errors,
            }
            print(json.dumps(report, ensure_ascii=False))
            if shot:
                with open(args.out_shot, "wb") as f:
                    f.write(base64.b64decode(shot))
                log(f"截图已写: {args.out_shot}")

            bad = []
            if not panel_visible:
                bad.append("设置面板没打开")
            if initial["keyRowHidden"]:
                bad.append("offline 档密钥行不应隐藏（mock 不是 local）")
            if preset_filled["model"] != "deepseek-chat":
                bad.append(f"预设没填模型名: {preset_filled['model']}")
            if preset_filled["baseUrl"] != "https://api.deepseek.com":
                bad.append(f"预设没填地址: {preset_filled['baseUrl']}")
            if preset_filled["keyRowHidden"]:
                bad.append("DeepSeek 预设下密钥行被误隐藏")
            if health_after != "deepseek-chat":
                bad.append(f"保存后 health 仍是 {health_after}（热生效失败）")
            if not after_save["keyCleared"]:
                bad.append("保存后密钥框没清空")
            if after_save["chipOn"] != "deepseek":
                bad.append(f"保存后选中芯片不对: {after_save['chipOn']}")
            if FAKE_KEY in overlay:
                bad.append("密钥泄进了 config.yaml（必须只在 .env）")
            if "deepseek-chat" not in overlay:
                bad.append("覆盖层没有写入模型名")
            if "DEEPSEEK_API_KEY" not in env_file:
                bad.append(".env 没有写入密钥变量")
            if not test_error:
                bad.append("错误路径没有报错文案")
            if health_after_error != "deepseek-chat":
                bad.append("测试连接失败却改了服务端配置")
            if health_restart != "deepseek-chat":
                bad.append(f"重启后配置丢失: {health_restart}")
            if not model_after_restart.get("has_api_key"):
                bad.append("重启后 has_api_key 为假（密钥没读回来）")
            if after_reload["baseUrl"] != "https://api.deepseek.com":
                bad.append(f"刷新后回填地址不对: {after_reload['baseUrl']}")
            if not after_reload["hasKey"]:
                bad.append("刷新后密钥状态提示不对")
            # ---- 视觉段（M6 ②）----
            if vision_initial["chips"] != ["none", "siliconflow", "ollama", "custom"]:
                bad.append(f"视觉段芯片不对: {vision_initial['chips']}")
            if vision_initial["chipOn"] != "none":
                bad.append(f"offline 档视觉段应默认「未接入」，实际 {vision_initial['chipOn']}")
            if not vision_initial["baseUrlRowHidden"]:
                bad.append("「未接入」时地址行应隐藏（没什么可填）")
            if vision_preset["model"] != "Qwen/Qwen2.5-VL-32B-Instruct":
                bad.append(f"视觉预设没填模型名: {vision_preset['model']}")
            if vision_preset["baseUrl"] != "https://api.siliconflow.cn/v1":
                bad.append(f"视觉预设没填地址: {vision_preset['baseUrl']}")
            if vision_preset["keyRowHidden"]:
                bad.append("SiliconFlow 预设下密钥行被误隐藏")
            if vision_saved != "siliconflow":
                bad.append(f"视觉段保存后选中芯片不对: {vision_saved}")
            if "vision:" not in overlay_after_vision:
                bad.append("覆盖层没有写入视觉段")
            # ★ 红线：写视觉段不能抹掉模型段（整个 E2E 里最值钱的一条）
            if "deepseek-chat" not in overlay_after_vision:
                bad.append("保存视觉段把模型段从覆盖层里抹掉了")
            if model_after_vision["model"] != "deepseek-chat":
                bad.append(f"视觉段保存后模型段被改动: {model_after_vision['model']}")
            if after_reload["vChipOn"] != "siliconflow":
                bad.append(f"重启+刷新后视觉段没回填: {after_reload['vChipOn']}")
            if after_reload["vModel"] != "Qwen/Qwen2.5-VL-32B-Instruct":
                bad.append(f"重启+刷新后视觉模型名没回填: {after_reload['vModel']}")
            if cdp.errors:
                bad.append("console 有错误")
            if bad:
                log("验收未过：" + "；".join(bad))
                return 1
            return 0
    finally:
        if server is not None:
            stop_server(server)
        if chrome is not None:
            chrome.terminate()
            try:
                chrome.wait(timeout=5)
            except subprocess.TimeoutExpired:
                chrome.kill()


def main():
    parser = argparse.ArgumentParser(description="无头 Chrome 模型设置验收")
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
