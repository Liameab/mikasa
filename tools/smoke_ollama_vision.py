#!/usr/bin/env python
"""本机 Ollama **视觉**通道的真机冒烟（ADR-0027 的最后一环）。

识图链路此前只跑过**假多模态服务**的 E2E——"本机 Ollama 视觉模型"那条路
从没被自动化碰过（交接单 P7）。这个工具补的就是它：**检测到本机有视觉模型
才跑**，没有就打印跳过（本机默认没装 qwen2.5vl，6GB 的下载不替用户决定）。

它做的事：
  1. 找本机**具备视觉能力**的模型（`/api/show` 的 capabilities 里有 "vision"；
     老版本 Ollama 不给这个字段时退回名字匹配）；
  2. 用应用自己的 `OpenAICompatVision`（backend=local，免密钥）发一张**现造的
     棋盘格 PNG**，断言回复非空——"能连上但返回空"是最难查的一种失败
     （ADR-0027 的纪律：绝不静默返回空文本）；
  3. 把模型的回复打出来供人看一眼（是不是在描述这张图，机器判不了）。

用法：
  python tools/smoke_ollama_vision.py                 # 有视觉模型就跑，没有跳过
  python tools/smoke_ollama_vision.py --live          # 没模型 → 退出码 1
  python tools/smoke_ollama_vision.py --model minicpm-v
退出码：0 = 通过或跳过；1 = 断言失败（或 --live 下环境不满足）。
"""

import argparse
import json
import os
import struct
import sys
import urllib.error
import urllib.request
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mikasa.config.settings import VisionConfig  # noqa: E402
from mikasa.errors import ZhiwenError  # noqa: E402
from mikasa.providers.vision import OpenAICompatVision  # noqa: E402

BASE = "http://127.0.0.1:11434"

# 有视觉能力的模型名（老 Ollama 不给 capabilities 时按名字认；覆盖常见几个）
_VISION_NAME_HINTS = ("vl", "llava", "minicpm-v", "vision", "gemma3", "moondream")


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _get(path: str, timeout: float = 5.0):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _png(size: int = 96) -> bytes:
    """现造一张棋盘格 PNG（有明确结构，视觉模型不至于无话可说）。"""

    rows = []
    for y in range(size):
        row = b"\x00"
        for x in range(size):
            dark = ((x // 12) + (y // 12)) % 2
            row += b"\x20\x20\x20" if dark else b"\xf0\xec\xe4"
        rows.append(row)
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _vision_models() -> list[str]:
    """本机具备视觉能力的模型名（capabilities 优先，名字兜底）。"""
    try:
        names = [m.get("name", "") for m in _get("/api/tags").get("models", [])]
    except (urllib.error.URLError, OSError, ValueError):
        return []
    found: list[str] = []
    for name in names:
        if not name:
            continue
        try:
            info = _post("/api/show", {"model": name})
            caps = info.get("capabilities") or []
            if "vision" in caps:
                found.append(name)
                continue
        except (urllib.error.URLError, OSError, ValueError):
            pass  # 老版本没有 capabilities：走名字匹配
        if any(hint in name.lower() for hint in _VISION_NAME_HINTS):
            found.append(name)
    return found


def _post(path: str, payload: dict, timeout: float = 30.0):
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="本机视觉通道冒烟（Ollama）")
    parser.add_argument("--live", action="store_true", help="环境不满足时判失败（默认跳过）")
    parser.add_argument("--model", default="", help="指定模型（缺省自动挑一个视觉模型）")
    args = parser.parse_args()

    try:
        version = _get("/api/version", timeout=5).get("version", "?")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log(f"跳过：本机 Ollama 不可达（{type(exc).__name__}: {exc}）")
        return 1 if args.live else 0

    model = args.model.strip() or next(iter(_vision_models()), "")
    if not model:
        log("跳过：本机没有视觉模型（要跑这条请先 `ollama pull qwen2.5vl:7b`，约 6GB）")
        return 1 if args.live else 0

    log(f"Ollama {version} · 视觉模型 {model}")
    cfg = VisionConfig(
        backend="local",
        base_url=f"{BASE}/v1",
        model=model,
        max_tokens=256,
        timeout_seconds=120.0,
    )
    vision = OpenAICompatVision(cfg, max_retries=0)
    try:
        result = vision.describe(_png(), mime="image/png")
    except ZhiwenError as exc:
        log(f"识图失败：{exc}")
        return 1
    text = (result.text or "").strip()
    if not text:
        # ADR-0027 的核心纪律：绝不静默返回空文本（用户会以为"识别完成"）
        log("识图返回了**空文本**——这是最坏的一种失败（界面会显示识别成功但没有内容）")
        return 1
    log(f"模型回复（{len(text)} 字）：{text[:120]}")
    log("视觉通道冒烟通过（真 Ollama：非空回复）——回复内容是否准确请人工看一眼")
    return 0


if __name__ == "__main__":
    sys.exit(main())
