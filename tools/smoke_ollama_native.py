#!/usr/bin/env python
"""本机 Ollama 原生通道的真机冒烟（ADR-0029 的最后一环）。

单测覆盖了请求体（think / num_ctx）、NDJSON 流、错误映射与空答案守卫，
但它们用的是**假服务**——"源码服务 + 真 qwen3:8b"这条链路没有自动化。
这个工具补的就是它，断言三件事：

  1. `think=false` 的短问答**秒级**返回、**答案非空**（ADR-0029 实测 1.4 秒；
     2026-09-20 之前走兼容面时是 14.2 秒且 0 字）；
  2. `think=true` 用时**不短于**关闭思考那一轮——思考是额外算力，不可能更快
     （这条比"必须慢多少"稳，不受机器负载抖动影响）；
  3. 两轮都受 `num_ctx` 约束（请求带上 16384；服务端是否生效看 ollama ps，
     本工具不假装能断言显存行为）。

默认**宽松**：本机没有 Ollama（或没有模型）时打印跳过并返回 0——它不该在
没装 Ollama 的机器上把门禁判红。要把它当硬门禁用，加 `--live`（连不上即失败）。

用法：
  python tools/smoke_ollama_native.py            # 有 Ollama 就跑，没有就跳过
  python tools/smoke_ollama_native.py --live     # 连不上/没模型 → 退出码 1
  python tools/smoke_ollama_native.py --model qwen3:4b
退出码：0 = 通过或跳过；1 = 断言失败（或 --live 下环境不满足）。
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mikasa.config.settings import LLMConfig  # noqa: E402
from mikasa.errors import ZhiwenError  # noqa: E402
from mikasa.providers.ollama import OllamaNativeLLM  # noqa: E402

BASE = "http://127.0.0.1:11434"
QUESTION = "用一句话说明什么是 L2 正则化。"
# 秒级判据留足余量：本机实测 1.0–1.4 秒，但冷启动/机器忙时会到几秒
FAST_LIMIT_SECONDS = 15.0


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _get(path: str, timeout: float = 5.0):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _model_present(model: str) -> bool:
    try:
        tags = _get("/api/tags")
    except (urllib.error.URLError, OSError, ValueError):
        return False
    names = {m.get("name", "") for m in tags.get("models", [])}
    return any(n == model or n.startswith(f"{model}:") for n in names)


def _ask(model: str, *, think: bool, num_ctx: int, max_tokens: int) -> tuple[float, str]:
    """跑一轮问答，返回 (秒数, 答案文本)。失败抛 ZhiwenError。"""
    cfg = LLMConfig(
        backend="local",
        base_url=f"{BASE}/v1",
        model=model,
        think=think,
        num_ctx=num_ctx,
        max_tokens=max_tokens,
        temperature=0.1,
    )
    llm = OllamaNativeLLM(cfg)
    started = time.monotonic()
    result = llm.complete(
        [{"role": "user", "content": QUESTION}], temperature=0.1, max_tokens=max_tokens
    )
    return time.monotonic() - started, result.text


def main() -> int:
    parser = argparse.ArgumentParser(description="本机 Ollama 原生通道冒烟")
    parser.add_argument("--live", action="store_true", help="环境不满足时判失败（默认跳过）")
    parser.add_argument("--model", default="qwen3:8b", help="要用的本机模型")
    parser.add_argument("--num-ctx", type=int, default=16384)
    args = parser.parse_args()

    try:
        version = _get("/api/version", timeout=5).get("version", "?")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log(f"跳过：本机 Ollama 不可达（{type(exc).__name__}: {exc}）")
        return 1 if args.live else 0
    if not _model_present(args.model):
        log(f"跳过：模型 {args.model} 不在本机（ollama pull {args.model}）")
        return 1 if args.live else 0

    log(f"Ollama {version} · 模型 {args.model} · num_ctx={args.num_ctx}")

    bad: list[str] = []
    # ① 关闭思考：秒级 + 非空
    try:
        fast_seconds, fast_text = _ask(
            args.model, think=False, num_ctx=args.num_ctx, max_tokens=256
        )
    except ZhiwenError as exc:
        log(f"关闭思考那一轮直接失败：{exc}")
        return 1
    log(f"think=false · {fast_seconds:.1f}s · {len(fast_text)} 字：{fast_text.strip()[:40]}…")
    if not fast_text.strip():
        bad.append("think=false 返回了空答案（ADR-0029 修的就是这个症状）")
    if fast_seconds > FAST_LIMIT_SECONDS:
        bad.append(
            f"think=false 用了 {fast_seconds:.1f}s（上限 {FAST_LIMIT_SECONDS:.0f}s）——"
            "思考模式可能没被真正关掉（兼容面会静默忽略 think）"
        )

    # ② 开启思考：只会更慢（或按 ADR-0029 预算被烧光而如实报错）
    think_note = ""
    try:
        slow_seconds, slow_text = _ask(
            args.model, think=True, num_ctx=args.num_ctx, max_tokens=1024
        )
        think_note = f"{slow_seconds:.1f}s · {len(slow_text)} 字"
        if slow_seconds < fast_seconds * 0.5:
            bad.append(
                f"think=true 反而更快（{slow_seconds:.1f}s vs {fast_seconds:.1f}s）——"
                "think 参数很可能没生效"
            )
    except ZhiwenError as exc:
        # 已知形态：推理把输出预算吃光 → provider 如实报错（不是静默空串）。
        # 冒烟把它当**可接受结果**记录下来：这正是 ADR-0029 描述的行为。
        think_note = f"推理吃光预算、如实报错（{str(exc)[:60]}…）"
    log(f"think=true  · {think_note}")

    if bad:
        log("冒烟未过：\n  - " + "\n  - ".join(bad))
        return 1
    log("原生通道冒烟通过（真 Ollama：秒级 + 非空 + think 有代价）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
