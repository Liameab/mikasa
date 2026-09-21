#!/usr/bin/env python
"""打包产物冒烟：**真启动冻结的 exe**，确认服务真的起来了（中文注释纪律）。

为什么必须有这一环（2026-09-20 v0.1.6 实测的教训）：源码全绿 ≠ 打包版能开。
v0.1.6 的发布包双击后弹"服务启动超时"，而 925 条单测、ruff、mypy 全是绿的——
因为故障只在冻结环境里存在：升级残留的 `_internal/websockets/` 空壳让
`import websockets` 变成没有 `__version__` 的命名空间包，uvicorn 加载
WebSocket 协议实现时 ImportError，**服务器线程当场死**。源码环境里那份
websockets 是完整的，永远复现不出来。

四件只有这一环能验的事：
  1. **双击等效**：用 `cmd /c start` 起进程（没有 std 句柄——真实双击的形态，
     也是 2026-09-15 sys.stdout=None 那个 bug 的复现条件）；
  2. **服务真的在监听**：轮询 /api/health（端口可能顺延，试一小段范围）；
  3. **日志干净 + 载荷里没有 WebSocket 空壳**（后者是这次事故的物证）；
  4. **离线入库**：进程带着"断网"的环境变量启动（HF_HUB_OFFLINE=1 + 端点指向
     不可达地址），上传一份文本必须成功——证明随包向量模型真的铺得到、用得
     上（ADR-0030）。模型没打进包或铺设坏了，这一步会以网络错误失败，而不是
     悄悄下 91MB 把冒烟骗绿。

用法：
  python tools/smoke_frozen.py [--exe dist/Mikasa/Mikasa.exe] [--timeout 75]
退出码：0 = 通过；1 = 启动失败/超时/日志有 traceback/载荷缺件/离线入库失败。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_EXE = REPO_ROOT / "dist" / "Mikasa" / "Mikasa.exe"

# 应用端口顺延的探测范围（8787 被占时它会 +1 ……）
_PORT_RANGE = range(8787, 8800)


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _pids_of(image: str) -> set[str]:
    """当前在跑的 Mikasa.exe PID 集合（tasklist，不依赖 psutil）。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=20,
        ).stdout
    except OSError:
        return set()
    pids = set()
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == image.lower():
            pids.add(parts[1])
    return pids


def _health(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return None


def _existing_mikasa() -> int | None:
    """返回"已经在跑的 Mikasa"所在端口（没有则 None）。

    **冒烟必须独占**：应用有单实例保护——探到已有 Mikasa 就只开窗口指向它、
    **不起自己的服务**（防两个进程写同一个库，见 entry.py 的 _pick_port）。
    有别的实例在场时跑冒烟，测的根本不是这份产物（本工具第一次实测就是这么
    假超时的：用户开着的 8787 一直应答，我启的那个进程连服务都没起）。
    """
    for port in _PORT_RANGE:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as resp:
                if json.loads(resp.read().decode("utf-8")).get("name") == "Mikasa":
                    return port
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return None


def _upload_text_offline(port: int, data_dir: Path) -> str | None:
    """往冻结实例传一份 txt（离线环境下）——返回错误说明，成功返回 None。

    这是"下载即用"承诺的**唯一硬证据**：入库会真的构造 fastembed 后端、加载
    ONNX 权重、算出向量。进程的环境变量已经把网断掉（见调用处），所以这一步
    成功 = 模型确实随包带到了、并且没走网络。手写 multipart：不为冒烟引入
    新的第三方依赖（httpx 在发布环境的 venv 里有，但工具要保持零依赖习惯）。
    """
    sample = data_dir / "smoke-embed.txt"
    sample.write_text("冒烟文本：验证随包向量模型能在断网环境下完成入库。", encoding="utf-8")
    boundary = "----mikasa-smoke-frozen"
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{sample.name}"\r\n'
        "Content-Type: text/plain\r\n\r\n"
    ).encode()
    body = head + sample.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/documents",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            if resp.status == 201:
                return None
            return f"离线入库返回了 {resp.status}（期望 201）"
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        return f"离线入库失败（HTTP {exc.code}）：{detail}"
    except (urllib.error.URLError, OSError) as exc:
        return f"离线入库失败：{exc}"


def _listening_pid(port: int) -> str | None:
    """该端口上 LISTENING 的进程 PID（netstat -ano）。

    为什么要它：用户可能自己正开着 Mikasa（很可能就在 8787），**别的实例**的
    /api/health 一样会 200——不核对 PID 的话，冒烟会拿别人的服务当自己的成绩。
    """
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, errors="replace", timeout=20
        ).stdout
    except OSError:
        return None
    for line in out.splitlines():
        parts = line.split()
        if (
            len(parts) >= 5
            and parts[0].upper() == "TCP"
            and parts[3] == "LISTENING"
            and parts[1].endswith(f":{port}")
        ):
            return parts[-1]
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="打包产物冒烟（真启动冻结 exe）")
    parser.add_argument("--exe", default=str(DEFAULT_EXE), help="冻结产物的 exe 路径")
    parser.add_argument("--timeout", type=float, default=75.0, help="等就绪的秒数")
    args = parser.parse_args()

    exe = Path(args.exe)
    if not exe.is_file():
        log(f"[x] 找不到冻结产物：{exe}（先跑 pyinstaller packaging/Mikasa.spec）")
        return 1

    running = _existing_mikasa()
    if running is not None:
        log(
            f"[x] 端口 {running} 上已经有一个 Mikasa 在跑——先关掉它再跑冒烟。\n"
            "    应用有单实例保护：已有实例在场时它只开窗口、不起自己的服务，\n"
            "    那样测的是那个实例，不是这份产物。"
        )
        return 1

    data_dir = Path(tempfile.mkdtemp(prefix="smoke-frozen-"))
    keep_tmp = False  # 失败时置真：保留数据目录与日志供排查
    bad: list[str] = []
    launched: set[str] = set()
    before = _pids_of(exe.name)
    try:
        # 载荷体检：打包里不该有 websockets 空壳（v0.1.6 事故的物证）。
        # 排除是刻意的（spec 里写明），这里把它钉住——重新被"顺手带上"就红。
        stale = exe.parent / "_internal" / "websockets"
        if stale.exists():
            bad.append(
                f"载荷里有 websockets 目录（{stale}）：uvicorn 会去加载它的协议实现，"
                "一份不完整的 websockets 会把服务器线程打崩（见 limitations §四）"
            )

        # 载荷体检（二）：随包向量模型必须在（ADR-0030）。少了它，新用户第一次
        # 上传文档要联网下 91MB——国内直连 huggingface.co 常超时，整条入库链以
        # 网络异常的样子失败（2026-09-15 实测）。
        embed_root = exe.parent / "_internal" / "models" / "embed"
        onnx_files = sorted(embed_root.rglob("*.onnx")) if embed_root.is_dir() else []
        if len(onnx_files) != 1:
            # 0 份 = 模型没打进去；2 份 = blobs/ 没清、同一份权重收了两遍
            # （2026-09-21 CI 实测：zip 因此从 165MB 涨到 263.6MB）
            bad.append(
                f"载荷里的模型权重有 {len(onnx_files)} 份（应当只有 1 份，{embed_root}）："
                "0 份先跑 python tools/fetch_embed_model.py；"
                "2 份是 blobs/ 第二副本没清"
            )

        env = dict(os.environ, MIKASA_DATA_DIR=str(data_dir))
        # **把网断掉**再启动：随包模型若没铺上/铺坏了，入库会以网络错误失败——
        # 而不是悄悄联网下 91MB、把"离线可用"这件事验成绿的（ADR-0030）。
        env["HF_HUB_OFFLINE"] = "1"
        env["HF_ENDPOINT"] = "http://127.0.0.1:1"  # 不可达：真去调就立刻失败
        env["HF_HUB_DISABLE_XET"] = "1"
        # 双击等效：cmd /c start 起进程 = **没有 std 句柄**，与用户双击 exe 同形
        subprocess.run(["cmd", "/c", "start", "", str(exe)], env=env, check=False, timeout=30)
        time.sleep(2.0)
        launched = _pids_of(exe.name) - before
        if not launched:
            bad.append("进程没起来（cmd start 之后 tasklist 里没有新 PID）")

        health: dict | None = None
        deadline = time.time() + args.timeout
        port_hit = 0
        while time.time() < deadline:
            for port in _PORT_RANGE:
                got = _health(port)
                if got is None:
                    continue
                if _listening_pid(port) in launched:  # 必须是我们拉起的那个进程
                    health, port_hit = got, port
                    break
                # 别人的实例（用户自己开着的那份）照样 200 —— 继续往后扫，
                # 别在这里 break：一 break 就再也轮不到我们的顺延端口
                # （本工具的第一次实测就是这么假超时的）
            if health is not None:
                break
            time.sleep(1.0)

        if health is None:
            bad.append(f"{args.timeout:.0f} 秒内没有端口报健康（服务没起来）")
        else:
            log(
                f"健康检查：端口 {port_hit} · profile={health.get('profile')} · "
                f"LLM={health.get('llm_model')} · 文档 {health.get('documents')} 篇"
            )
            # 离线入库（ADR-0030）：模型随包 + 不联网 = 新用户传完文档就能问
            err = _upload_text_offline(port_hit, data_dir)
            if err is None:
                log("离线入库：通过（随包向量模型可用，全程未联网）")
            else:
                bad.append(err)

        # 日志体检：启动期任何 traceback 都是"服务线程死过"的痕迹，
        # 哪怕某个端口恰好在应答（比如旧进程）。
        app_log = data_dir / "logs" / "mikasa.log"
        if app_log.is_file():
            text = app_log.read_text(encoding="utf-8", errors="replace")
            if "Traceback" in text:
                tail = text.strip().splitlines()[-6:]
                bad.append("应用日志里有 traceback：\n    " + "\n    ".join(tail))

        if bad:
            log("冒烟未过：\n  - " + "\n  - ".join(bad))
            # **失败要留现场**：应用日志就是这个工具的验尸报告，不能随临时目录一起删
            # （首次实测踩到：只报"没端口报健康"，日志已被 rmtree 清空，白跑一轮）
            if app_log.is_file():
                tail = app_log.read_text(encoding="utf-8", errors="replace").strip()
                log("应用日志尾（现场）：\n" + (tail[-1500:] if tail else "（空：启动期没有输出）"))
            log(f"数据目录（保留，供排查）：{data_dir}")
            keep_tmp = True
            return 1
        log("打包产物冒烟通过（双击等效启动 → 服务就绪 → 日志干净）")
        return 0
    finally:
        # 只杀本次拉起来的那些 PID：绝不用 /IM 按名字杀（用户自己开着的那份
        # 会被一起带走——本工具第一条纪律）
        for pid in sorted(launched):
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, check=False)
        if not keep_tmp:  # 失败时保留现场（见失败分支的注释）
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
