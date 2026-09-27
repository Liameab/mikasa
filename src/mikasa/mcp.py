"""MCP server（stdio）：把知识库封成 agent 可调用的三个**只读**工具。

为什么手写 JSON-RPC、不引官方 `mcp` SDK（2026-09-27 拍板）：本仓要保证
**打包版也能用** `mikasa mcp`，引 SDK 就得把它一起塞进 PyInstaller
（安装包变大 + hiddenimports 手工维护），而 stdio 传输的协议面很小——
换行分隔的 JSON-RPC 2.0 + 四个方法，自己实现足够稳，且零新依赖、
CI 与打包版同轨。

谈判稿（`tech-roadmap-reply.md` §5）锁死的六条，这里是落地：

  1. **三个工具而不是两个**：`search`（片段 + 出处）→ `read`（读全文）→
     `ask`（带 [n] 的答案 + 引用清单）。缺 `read` 会让 agent 断链——
     search 之后发现证据不足，没有任何手段补读；
  2. **只做 stdio**：HTTP transport 要接 ADR-0033 的口令门与会话语义，是另一件事；
  3. **独立入口**：`mikasa mcp` 是一次性子进程，与 `serve` 那个"单进程 +
     内存快照 + 任务槽"的长期服务生命周期模型不同，不复用同一个进程；
  4. **只读**：不给 ingest / delete / settings——"agent 用你的库"必须没有破坏面；
  5. **档位从配置来**（`--profile` / `--config`），不在工具参数里；
  6. **探测不到模型时不许崩**：offline（mock）档下 `ask` 返回一句"仅 search
     可用"，而不是抛异常打断 agent 会话。

stdout 纪律：本模块的 stdout **只跑 JSON-RPC**。任何诊断一律走 stderr——
日志的 console handler 已经指向 stderr，但 rich 的 Console 必须显式
`Console(stderr=True)`，否则一行横幅就能让客户端解析失败。
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from mikasa import __version__
from mikasa.config.settings import Settings
from mikasa.errors import StorageError, ZhiwenError
from mikasa.pipeline.ask import AskService
from mikasa.storage import repo
from mikasa.storage.db import open_db
from mikasa.utils.logging import get_logger

logger = get_logger("mcp")

# 本 server 说的协议版本。客户端给别的版本时：认识的版本原样回（由客户端
# 决定是否继续），不认识的回本值——这是规范里"服务端选版本"的写法。
PROTOCOL_VERSION = "2025-06-18"
_KNOWN_PROTOCOL_VERSIONS = (PROTOCOL_VERSION, "2025-03-26", "2024-11-05")

# JSON-RPC 2.0 标准错误码（只用到这四个）
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603

# 工具名即"只读白名单"：写工具（ingest/delete/settings）**一个都不提供**，
# 分派前先查这张表，将来有人加工具也会先撞上这条注释。
_READ_ONLY_TOOLS = ("search", "read", "ask")

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "search",
        "description": (
            "在本地知识库里检索（BM25 + 向量 + RRF 融合），返回带出处的原文片段，"
            "不生成答案。拿到 chunk_id 后可以用 read 读该块及其前后各一块的全文；"
            "需要成段的答案（带 [n] 引用）时用 ask。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索问题（中文或英文）"},
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 5,
                    "description": "返回条数，默认 5",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read",
        "description": (
            "按 chunk_id 读取原文：该块全文 + 同一文档里前后各一块（补上下文）。"
            "chunk_id 来自 search 的结果；块可能已被删除或重建索引，那时会明确报错。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "integer", "minimum": 1, "description": "search 给的 chunk_id"}
            },
            "required": ["chunk_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ask",
        "description": (
            "知识库问答：检索后用配置好的模型生成回答，正文里的 [n] 与返回的引用清单"
            "一一对应（含文档、章节、页码、chunk_id），可逐条回原文核对。"
            "证据不足时按纪律拒答（refused=true）。离线档（mock 模型）不可用，"
            "会返回一句说明而不是报错。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"question": {"type": "string", "description": "问题（中文或英文）"}},
            "required": ["question"],
            "additionalProperties": False,
        },
    },
]


def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _tool_result(texts: list[str], *, is_error: bool = False) -> dict[str, Any]:
    """工具结果：一个或多个 text 块（MCP 会按块展示给模型）。"""
    return {
        "content": [{"type": "text", "text": text} for text in texts],
        "isError": is_error,
    }


def _require_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise StorageError(f"参数 {key} 必须是非空字符串")
    return value.strip()


def _require_int(args: dict[str, Any], key: str) -> int:
    value = args.get(key)
    # bool 是 int 的子类：True 传进来会静默变成 chunk_id=1，必须挡掉
    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageError(f"参数 {key} 必须是整数")
    return value


class McpServer:
    """三个只读工具 + JSON-RPC 分派。**一个实例 = 一次 stdio 会话**。

    AskService 只建一次（`service` 惰性属性）：它内部缓存索引快照
    （IndexManager），长驻子进程里反复 search/ask 不会每次重建 BM25。
    """

    def __init__(self, settings: Settings, *, service: AskService | None = None) -> None:
        """service 是测试注入点（与 `AskService(llm=...)` 同款）。"""
        self.settings = settings
        self._service = service

    @property
    def service(self) -> AskService:
        if self._service is None:
            self._service = AskService(self.settings)
        return self._service

    # ------------------------------------------------------------------
    # JSON-RPC 分派（纯函数式：不读 stdin、不写 stdout，单测直接喂字典）
    # ------------------------------------------------------------------

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """处理一条消息；**通知**（没有 id）返回 None——协议规定不许回复。"""
        method = message.get("method")
        msg_id = message.get("id")
        if msg_id is None:  # notifications/initialized、notifications/cancelled…
            logger.debug("收到通知：%s", method)
            return None

        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(msg_id, _INVALID_PARAMS, "params 必须是对象")

        if method == "initialize":
            return _result(msg_id, self._initialize(params))
        if method == "ping":
            return _result(msg_id, {})
        if method == "tools/list":
            return _result(msg_id, {"tools": _TOOLS})
        if method == "tools/call":
            return self._tools_call(msg_id, params)
        return _error(msg_id, _METHOD_NOT_FOUND, f"不支持的方法：{method}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        version = requested if requested in _KNOWN_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "mikasa", "version": __version__},
        }

    def _tools_call(self, msg_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _error(msg_id, _INVALID_PARAMS, "arguments 必须是对象")
        if name not in _READ_ONLY_TOOLS:
            return _error(
                msg_id,
                _INVALID_PARAMS,
                f"未知工具：{name!r}（可用：{' / '.join(_READ_ONLY_TOOLS)}）",
            )

        handler = getattr(self, f"tool_{name}")
        try:
            texts = handler(args)
        except ZhiwenError as exc:
            # 可预期的失败（知识库为空 / chunk_id 不存在 / 参数不合法）：
            # 作为**工具结果**回给 agent（isError），而不是 JSON-RPC 错误——
            # 后者在多数客户端里等于"这条链路坏了"，agent 学不到该怎么改；
            # 前者它读得到"知识库为空，请先 ingest"，能自己决定下一步。
            return _result(msg_id, _tool_result([str(exc)], is_error=True))
        except Exception as exc:  # noqa: BLE001 - 一次工具调用失败绝不能打死整个会话
            logger.exception("工具 %s 执行失败", name)
            return _result(
                msg_id, _tool_result([f"内部错误：{type(exc).__name__}: {exc}"], is_error=True)
            )
        return _result(msg_id, _tool_result(texts))

    # ------------------------------------------------------------------
    # 三个工具
    # ------------------------------------------------------------------

    def tool_search(self, args: dict[str, Any]) -> list[str]:
        """search(query, top_k=5)：检索片段 + 出处（JSON 文本）。"""
        query = _require_str(args, "query")
        top_k = args.get("top_k", 5)
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
            raise StorageError("参数 top_k 必须是 1~20 的整数")

        hits = self.service.search(query, top_k=top_k)
        if not hits:
            return [f"没有检索到与「{query}」相关的内容——库里可能没有这个话题，换个说法再试。"]

        with open_db(self.settings.db_path) as conn:
            titles = repo.document_title_map(conn)
        payload = [
            {
                "chunk_id": hit.chunk.id,
                "rank": hit.rank,
                "document_id": hit.chunk.document_id,
                "document_title": titles.get(hit.chunk.document_id, "未知文档"),
                "section": hit.chunk.heading_path,
                "page": hit.chunk.page_number,
                "snippet": hit.chunk.snippet,
            }
            for hit in hits
        ]
        return [
            json.dumps(
                {"query": query, "hit_count": len(payload), "hits": payload},
                ensure_ascii=False,
                indent=2,
            )
        ]

    def tool_read(self, args: dict[str, Any]) -> list[str]:
        """read(chunk_id)：该块全文 + 同文档前后各一块。"""
        chunk_id = _require_int(args, "chunk_id")
        with open_db(self.settings.db_path) as conn:
            chunk = repo.chunk_by_id(conn, chunk_id)
            if chunk is None:
                raise StorageError(
                    f"没有 id 为 {chunk_id} 的块——可能已被删除或索引已重建，"
                    "请重新 search 拿新的 id。"
                )
            titles = repo.document_title_map(conn)
            siblings = repo.chunks_by_document(conn, chunk.document_id)

        index = next((i for i, item in enumerate(siblings) if item.id == chunk_id), None)
        if index is None:  # 理论不可达：块在，却不在自己文档的列表里
            siblings, index = [], 0

        context = []
        for offset, relation in ((-1, "上一块"), (1, "下一块")):
            position = index + offset
            if 0 <= position < len(siblings):
                neighbour = siblings[position]
                context.append(
                    {
                        "chunk_id": neighbour.id,
                        "relation": relation,
                        "section": neighbour.heading_path,
                        "page": neighbour.page_number,
                        "text": neighbour.content,
                    }
                )

        payload = {
            "chunk_id": chunk.id,
            "document_id": chunk.document_id,
            "document_title": titles.get(chunk.document_id, "未知文档"),
            "section": chunk.heading_path,
            "page": chunk.page_number,
            "seq": chunk.seq,
            "text": chunk.content,
            "context": context,
        }
        return [json.dumps(payload, ensure_ascii=False, indent=2)]

    def tool_ask(self, args: dict[str, Any]) -> list[str]:
        """ask(question)：带 [n] 的答案 + 引用清单（两个 text 块）。"""
        question = _require_str(args, "question")
        if self.settings.llm.backend == "mock":
            # 离线档的承诺是零外部调用，mock 模型的回答只是演示用的固定串——
            # 与其让 agent 把假答案当真，不如如实说清本档能干什么。
            return [
                "当前为离线档（mock 模型），ask 不可用——本档只用 search 检索原文、"
                "read 读全文。需要生成式回答请切 api / local 档后重启本服务。"
            ]

        answer = self.service.answer(question)
        citations = [
            {
                "marker": cite.marker,
                "chunk_id": cite.chunk_id,
                "document_title": cite.document_title,
                "section": cite.section,
                "page": cite.page,
            }
            for cite in answer.citations
        ]
        meta = {
            "model": answer.model,
            "refused": answer.refused,
            "citations": citations,
        }
        return [
            answer.text,
            "引用清单（正文 [n] → 出处）：\n" + json.dumps(meta, ensure_ascii=False, indent=2),
        ]


def _write(stream: TextIO, message: dict[str, Any]) -> None:
    """写一条 JSON-RPC：**单行** + 立即 flush。

    stdio 传输按换行分帧，且客户端在等回复——攒批或漏 flush 都会表现为"卡住"。
    """
    stream.write(json.dumps(message, ensure_ascii=False) + "\n")
    stream.flush()


def _force_utf8_stdio() -> None:
    """stdin/stdout 一律 UTF-8，且 stdout 不做换行翻译。

    中文 Windows 的默认编码是 cp936：客户端发来的中文问题会被解成乱码，
    而 stdout 的 `\\n` 会被翻译成 `\\r\\n`——两者都属于"看着通了、实际坏了"，
    且在别的机器上复现不出来。规范要求 UTF-8，这里钉死。
    """
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):  # 测试里被换成 StringIO 时没有这个方法
            stream.reconfigure(encoding="utf-8", newline="\n", errors="replace")


def serve_stdio(settings: Settings) -> None:
    """跑一次 stdio 会话：逐行读 JSON-RPC、逐条回复，直到 stdin 关闭。"""
    _force_utf8_stdio()
    server = McpServer(settings)
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            _write(out, _error(None, _PARSE_ERROR, f"JSON 解析失败：{exc}"))
            continue
        if not isinstance(message, dict):
            _write(out, _error(None, _INVALID_REQUEST, "一条消息必须是 JSON 对象"))
            continue
        try:
            reply = server.handle(message)
        except Exception as exc:  # noqa: BLE001 - 分派层也不许把会话打死
            logger.exception("处理消息失败")
            reply = _error(message.get("id"), _INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
        if reply is not None:
            _write(out, reply)
