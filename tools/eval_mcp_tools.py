"""MCP 工具调用评测：本机 8B 自己决定调哪个工具，跑 10 题并逐题记账。

要回答的问题（谈判稿 §4-B）：把知识库挂成 MCP 工具之后，**agent 真的会用吗**？

- 工具选择：先 search 还是直接 ask？证据不足时会换词再搜、补 read 吗？
- 调用次数：每题几轮、几次调用（成本直觉）。
- 引用可验证性：ask 的 [n] 与引用清单是否一一对应、chunk_id 能不能 read 回来。
- 失败模式：参数不合 schema、工具报错、空检索、该拒答却凭记忆作答。

驱动者是**本机 qwen3:8b**（Ollama 原生 /api/chat 的 tools 字段），不是脚本写死
的策略——写死的策略测不出"工具选择"。真实客户端（Claude Code）那条轨迹见
docs/usage-guide.md，本脚本是与它互补、可重复的场次。

用法::

    MIKASA_DATA_DIR=build/eval-local/data \\
        python tools/eval_mcp_tools.py --profile local --out build/mcp-tool-eval.json
    MIKASA_DATA_DIR=... python tools/eval_mcp_tools.py --only q001   # 先冒烟一题

跑批前先 `ollama ps`：本机 8GB 卡上别的模型占着显存，qwen3:8b 会被反复驱逐。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from mikasa.config.settings import load_settings
from mikasa.providers.ollama import ollama_api_root

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN = REPO_ROOT / "evals" / "golden_set.json"

# 驾驶员的系统提示词：保持最小——工具语义主要靠工具自己的 description。
# 写进报告里，因为"工具选择"的数字只有在知道给过什么指示时才有意义。
SYSTEM_PROMPT = (
    "你是会用工具回答问题的助手。规则：\n"
    "1) 涉及知识库内容时必须调用工具，不要凭记忆回答；\n"
    "2) 想先看看有什么原文用 search；拿到 chunk_id 要读全文用 read；\n"
    "3) 需要成段回答、带出处的用 ask；\n"
    "4) 工具说没有相关内容时，就如实告诉用户知识库里没有，不要编造。"
)

# 加固版提示词（--strict-tools）：首轮冒烟暴露"这题我会就不查库"后补的对照臂。
# 与默认版的差别只有一处措辞：明确堵掉"凭记忆作答"这条路。
STRICT_SYSTEM_PROMPT = (
    "你是会用工具回答问题的助手。规则：\n"
    "1) **即使你认为自己知道答案**，也必须先调用工具从知识库取内容，"
    "回答只能基于工具返回的原文；\n"
    "2) 想先看看有什么原文用 search；拿到 chunk_id 要读全文用 read；\n"
    "3) 需要成段回答、带出处的用 ask；\n"
    "4) 工具说没有相关内容时，就如实告诉用户知识库里没有，不要编造。"
)

# 终答里认定"如实说不知道"的朴素判据（只用于打分那一列，原始文本全部留档）
_NO_INFO_HINTS = ("没有相关", "未提及", "没有提到", "无法回答", "知识库中没", "未找到")
# ask 返回两个 text 块（答案 + 引用清单），这里是第二块的开头（见 mcp.py）
ASK_CITATION_HEADER = "引用清单（正文"
_MARKER_RE = re.compile(r"\[(\d+)\]")


def select_items(items: list[dict[str, Any]], *, n_answerable: int, n_unanswerable: int):
    """按规则（不是手挑）选题：各难度取前几个 + 四类不可答各取一个。

    确定性选择是为了可复跑：同一份 golden_set 永远选出同一批题。
    """
    answerable = [i for i in items if i["kind"] == "answerable"]
    chosen = []
    per_difficulty = max(1, n_answerable // 3)
    for difficulty in ("easy", "medium", "hard"):
        pool = [i for i in answerable if i.get("difficulty") == difficulty]
        chosen += pool[:per_difficulty]

    unanswerable = [i for i in items if i["kind"] != "answerable"]
    by_reason: dict[str, list[dict[str, Any]]] = {}
    for item in unanswerable:
        by_reason.setdefault(str(item.get("reason")), []).append(item)
    # 先每类一个，不够再从剩下的补（顺序固定：unrelated → insufficient → bait）
    order = ["unrelated", "insufficient", "hallucination_bait"]
    picked: list[dict[str, Any]] = [by_reason[r][0] for r in order if by_reason.get(r)]
    rest = [i for i in unanswerable if i not in picked]
    picked += rest[: max(0, n_unanswerable - len(picked))]
    return chosen[:n_answerable] + picked[:n_unanswerable]


class McpClient:
    """一次 stdio 会话的最小 MCP 客户端（换行分帧的 JSON-RPC 2.0）。"""

    def __init__(self, profile: str, *, log_path: Path) -> None:
        env = {**os.environ, "PYTHONUTF8": "1"}
        self._log = log_path.open("w", encoding="utf-8")
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "mikasa", "mcp", "--profile", profile],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._log,
            text=True,
            encoding="utf-8",
            cwd=REPO_ROOT,
            env=env,
        )
        # 写出的帧必须只有 \n：Popen 没有 newline 参数（那是 open() 的），
        # 文本模式下 Windows 会把 \n 翻成 \r\n，服务端按 \n 分帧会留下尾随 \r。
        if self._proc.stdin:
            self._proc.stdin.reconfigure(newline="\n")
        self._id = 0

    def _call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self._proc.stdin and self._proc.stdout
        self._id += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            message["params"] = params
        self._proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()
        line = self._proc.stdout.readline()
        if not line:
            raise RuntimeError(f"MCP 服务没有回复（{method}）——stderr 见 {self._log.name}")
        reply = json.loads(line)
        if "error" in reply:
            raise RuntimeError(f"MCP 报错：{reply['error'].get('message')}")
        return reply["result"]

    def handshake(self) -> list[dict[str, Any]]:
        self._call(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "eval", "version": "0"},
            },
        )
        return self._call("tools/list")["tools"]

    def call_tool(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """调一次工具，返回（拼接后的文本，是否 isError）。"""
        result = self._call("tools/call", {"name": name, "arguments": args})
        text = "\n".join(block.get("text", "") for block in result.get("content", []))
        return text, bool(result.get("isError"))

    def close(self) -> None:
        if self._proc.stdin:
            self._proc.stdin.close()
        self._proc.wait(timeout=10)
        self._log.close()


def ollama_chat(root: str, body: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    # 注意 ollama_api_root 返回的已经以 /api 结尾（…/api），这里只接 /chat
    request = urllib.request.Request(
        root + "/chat",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def citations_from_ask(text: str) -> dict[str, Any] | None:
    """从 ask 的第二个 text 块里抠出引用清单。

    **必须先按表头切块再找 `{`**：答案正文里就有 LaTeX 花括号
    （`$ w = (X^T X)^{-1} $`），直接从整段文本找首个 `{` 会解析到正文上去。
    """
    _, header, rest = text.partition(ASK_CITATION_HEADER)
    if not header:
        return None
    start = rest.find("{")
    if start < 0:
        return None
    try:
        return json.loads(rest[start:])
    except json.JSONDecodeError:
        return None


def probe_ask(client: McpClient, item: dict[str, Any]) -> dict[str, Any]:
    """不让模型参与，直接调一次 ask：引用可验证性那一栏的数据来源。"""
    started = time.monotonic()
    failures: list[str] = []
    try:
        text, is_error = client.call_tool("ask", {"question": item["question"]})
    except Exception as exc:  # noqa: BLE001 —— 协议层异常也留档
        text, is_error = f"{type(exc).__name__}: {exc}", True
    if is_error:
        failures.append(f"ask 返回错误：{text[:120]}")
    answer_text, _, _ = text.partition(ASK_CITATION_HEADER)
    meta = citations_from_ask(text)
    check = check_citations(client, answer_text, meta)
    return {
        "id": item["id"],
        "question": item["question"],
        "kind": item["kind"],
        "difficulty": item.get("difficulty"),
        "reason": item.get("reason"),
        "ask_refused": None if meta is None else meta.get("refused"),
        "answer": answer_text.strip(),
        "citations": check["citations"],
        "citations_verified": check["verified"],
        "out_of_range": check["out_of_range"],
        "failure_modes": failures,
        "elapsed_s": round(time.monotonic() - started, 1),
    }


def check_citations(
    client: McpClient, answer_text: str, ask_meta: dict[str, Any] | None
) -> dict[str, Any]:
    """引用核对（agent 臂与 ask 探针共用）：[n] 是否越界、chunk_id 能否 read 回来。"""
    markers = sorted({int(m) for m in _MARKER_RE.findall(answer_text)})
    citations = (ask_meta or {}).get("citations") or []
    cite_markers = sorted({int(c.get("marker") or 0) for c in citations})
    out_of_range = [m for m in markers if m not in cite_markers] if citations else []
    verified = 0
    for cite in citations:
        chunk_id = cite.get("chunk_id")
        if not isinstance(chunk_id, int):
            continue
        try:
            _, is_error = client.call_tool("read", {"chunk_id": chunk_id})
        except Exception:  # noqa: BLE001 —— 读不回来就是读不回来，记一笔
            is_error = True
        verified += 0 if is_error else 1
    return {
        "markers": markers,
        "citations": cite_markers,
        "out_of_range": out_of_range,
        "verified": verified,
    }


def run_question(
    client: McpClient,
    settings,
    item: dict[str, Any],
    tools: list[dict[str, Any]],
    *,
    system_prompt: str,
    max_rounds: int,
    timeout: float,
) -> dict[str, Any]:
    """一题：agent 循环 → 记账（工具序列 / 引用核对 / 失败模式）。"""
    root = ollama_api_root(settings.llm.base_url)
    options: dict[str, Any] = {}
    if settings.llm.num_ctx:
        options["num_ctx"] = settings.llm.num_ctx
    options["temperature"] = 0.0  # 评测要可复跑，别引入采样抖动

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": item["question"]},
    ]
    calls: list[dict[str, Any]] = []
    ask_meta: dict[str, Any] | None = None
    failures: list[str] = []
    final_answer = ""
    started = time.monotonic()

    for round_index in range(1, max_rounds + 1):
        body = {
            "model": settings.llm.model,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "think": False,
        }
        if options:
            body["options"] = options
        try:
            reply = ollama_chat(root, body, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 —— 评测脚本，网络/模型异常都记成失败模式
            failures.append(f"第 {round_index} 轮 /api/chat 失败：{type(exc).__name__}: {exc}")
            break

        message = reply.get("message") or {}
        raw_calls = message.get("tool_calls") or []
        if not raw_calls:
            final_answer = (message.get("content") or "").strip()
            break

        messages.append(
            {"role": "assistant", "content": message.get("content") or "", "tool_calls": raw_calls}
        )
        for raw in raw_calls:
            function = raw.get("function") or {}
            name = function.get("name") or ""
            args = function.get("arguments")
            if isinstance(args, str):  # 有的模型把参数发成 JSON 字符串
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    failures.append(f"参数不是合法 JSON：{args!r}")
                    args = None
            if not isinstance(args, dict):
                failures.append(f"{name} 的参数不是对象：{args!r}")
                messages.append(
                    {"role": "tool", "tool_name": name, "content": "参数必须是 JSON 对象"}
                )
                calls.append(
                    {
                        "round": round_index,
                        "name": name,
                        "args": args,
                        "ok": False,
                        "error": "参数不是对象",
                    }
                )
                continue

            call_started = time.monotonic()
            try:
                text, is_error = client.call_tool(name, args)
            except Exception as exc:  # noqa: BLE001 —— 协议层异常也留档
                text, is_error = f"{type(exc).__name__}: {exc}", True
            elapsed = round(time.monotonic() - call_started, 2)
            calls.append(
                {
                    "round": round_index,
                    "name": name,
                    "args": args,
                    "ok": not is_error,
                    "error": text if is_error else None,
                    "elapsed_s": elapsed,
                }
            )
            if is_error:
                failures.append(f"{name} 返回错误：{text[:120]}")
            if name == "ask" and not is_error:
                parsed = citations_from_ask(text)
                if parsed is not None:
                    ask_meta = parsed
            messages.append({"role": "tool", "tool_name": name, "content": text})
    else:
        failures.append(f"跑满 {max_rounds} 轮仍未给出终答")
        final_answer = "(未收敛)"

    # 引用核对：终答里的 [n] ↔ ask 的引用清单，且每个 chunk_id 都能 read 回来
    check = check_citations(client, final_answer, ask_meta)
    markers = check["markers"]
    citations = check["citations"]
    out_of_range = check["out_of_range"]
    verified = check["verified"]

    says_no_info = any(hint in final_answer for hint in _NO_INFO_HINTS)
    asked = any(c["name"] == "ask" and c["ok"] for c in calls)
    if item["kind"] == "answerable":
        if not asked:
            verdict = "未走 ask" + ("（只搜了）" if any(c["ok"] for c in calls) else "（没调工具）")
        elif out_of_range:
            verdict = "引用越界"
        elif citations and verified < len(citations):
            verdict = "引用无法全部读回"
        else:
            verdict = "OK"
    else:  # 不可答：看它是如实说没有，还是凭记忆作答
        if ask_meta is not None and ask_meta.get("refused"):
            verdict = "OK（ask 拒答）" if says_no_info or not markers else "拒答但终答未说明"
        elif not calls:
            verdict = "凭记忆作答（未调工具）"
        elif asked:
            verdict = "ask 未拒答" if markers else "ask 未拒答（无引用）"
        elif says_no_info:
            verdict = "OK（检索后如实说没有）"
        else:
            verdict = "有工具但终答未说明"

    return {
        "id": item["id"],
        "question": item["question"],
        "kind": item["kind"],
        "difficulty": item.get("difficulty"),
        "reason": item.get("reason"),
        "rounds": max((c["round"] for c in calls), default=0),
        "calls": calls,
        "tool_sequence": [c["name"] for c in calls],
        "final_answer": final_answer,
        "markers": markers,
        "ask_refused": None if ask_meta is None else ask_meta.get("refused"),
        "citations": citations,
        "citations_verified": verified,
        "out_of_range": out_of_range,
        "says_no_info": says_no_info,
        "failure_modes": failures,
        "verdict": verdict,
        "elapsed_s": round(time.monotonic() - started, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="MCP 工具调用评测（本机 8B 驱动）")
    parser.add_argument("--profile", default="local")
    parser.add_argument("--only", help="只跑某一题（冒烟用），如 q001")
    parser.add_argument("--max-rounds", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--n-answerable", type=int, default=6)
    parser.add_argument("--n-unanswerable", type=int, default=4)
    parser.add_argument(
        "--strict-tools", action="store_true", help="用加固版提示词（堵'凭记忆作答'）"
    )
    parser.add_argument("--probe-ask", action="store_true", help="不让模型参与，直接调 ask 验引用")
    parser.add_argument("--out", default="build/mcp-tool-eval.json")
    args = parser.parse_args()

    if not os.environ.get("MIKASA_DATA_DIR"):
        print("请先设 MIKASA_DATA_DIR（评测用隔离库，如 build/eval-local/data）", file=sys.stderr)
        return 2

    settings = load_settings(args.profile)
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    items = select_items(
        golden["items"], n_answerable=args.n_answerable, n_unanswerable=args.n_unanswerable
    )
    if args.only:
        items = [i for i in items if i["id"] == args.only]
        if not items:
            print(f"选题里没有 {args.only}", file=sys.stderr)
            return 2

    out_path = REPO_ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = out_path.with_suffix(".server.log")
    system_prompt = STRICT_SYSTEM_PROMPT if args.strict_tools else SYSTEM_PROMPT

    client = McpClient(args.profile, log_path=log_path)
    try:
        tools = client.handshake()
        ollama_tools = [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["inputSchema"],
                },
            }
            for tool in tools
        ]
        print(f"模型 {settings.llm.model}｜工具 {[t['name'] for t in tools]}｜{len(items)} 题")
        results = []
        if args.probe_ask:
            # agent 臂里 8B 从不调 ask（首轮实测），"引用可验证性"这一栏因此只能
            # 由探针补：直接调 ask，看工具本身给出的引用站不站得住。
            for item in items:
                record = probe_ask(client, item)
                results.append(record)
                print(
                    f"{record['id']} {record['kind'][:4]:4} 拒答={record['ask_refused']} "
                    f"引用{record['citations']} 可读回 {record['citations_verified']}"
                    f"/{len(record['citations'])} 越界{record['out_of_range']} "
                    f"{record['elapsed_s']}s",
                    flush=True,
                )
            payload = {
                "mode": "probe-ask",
                "profile": args.profile,
                "model": settings.llm.model,
                "data_dir": os.environ.get("MIKASA_DATA_DIR"),
                "results": results,
            }
            out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            bad = [r["id"] for r in results if r["out_of_range"] or r["failure_modes"]]
            print(f"\n越界或报错：{bad or '无'}；明细 → {out_path}")
            return 0
        for item in items:
            record = run_question(
                client,
                settings,
                item,
                ollama_tools,
                system_prompt=system_prompt,
                max_rounds=args.max_rounds,
                timeout=args.timeout,
            )
            results.append(record)
            print(
                f"{record['id']} {record['kind'][:4]:4} 调用{len(record['calls'])}次 "
                f"{'→'.join(record['tool_sequence']) or '（无）':28} "
                f"{record['verdict']}  {record['elapsed_s']}s",
                flush=True,
            )
    finally:
        client.close()

    payload = {
        "profile": args.profile,
        "model": settings.llm.model,
        "strict_tools": args.strict_tools,
        "system_prompt": system_prompt,
        "max_rounds": args.max_rounds,
        "data_dir": os.environ.get("MIKASA_DATA_DIR"),
        "results": results,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    total_calls = sum(len(r["calls"]) for r in results)
    bad = [r["id"] for r in results if not r["verdict"].startswith("OK")]
    print(f"\n共 {total_calls} 次工具调用；非 OK：{bad or '无'}")
    print(f"明细 → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
