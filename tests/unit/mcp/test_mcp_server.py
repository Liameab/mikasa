"""MCP server：JSON-RPC 分派、三个只读工具、stdio 分帧与 stdout 纪律。

offline profile 起真实链路（MockLLM + 纯 BM25 + 真 SQLite）：search / read
走完整检索与取块路径，ask 走"离线档如实说明"那条分支。生成侧的形状
（正文 [n] ↔ 引用清单一一对应）用注入的假 service 测——真模型那条路要密钥
与网络，不进 CI。

三个成功判据（谈判稿 §1）里能自动化的两条钉在这里：
search 拿回带出处的片段、ask 的 [n] 能对回文档与页码；第三条"agent 自己
先搜再读再答"要在 Claude Code 里人工走一遍（见 docs/usage-guide.md）。
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from typer.testing import CliRunner

import mikasa.cli as cli
import mikasa.mcp as mcp_mod
from mikasa.cli import app
from mikasa.config.settings import load_settings
from mikasa.errors import StorageError
from mikasa.ingest.service import IngestService
from mikasa.mcp import PROTOCOL_VERSION, McpServer, serve_stdio
from mikasa.models.answer import Answer, Citation
from mikasa.storage import repo
from mikasa.storage.db import open_db

runner = CliRunner()

# 两块>400 字的正文：offline 档 size=400，保证切出 ≥4 个 chunk，
# 这样"中间块有前后邻块"才有可测的前提（不依赖分块器具体切在哪）
_PARA = (
    "L2 正则化在损失函数中加入权重的平方和惩罚项，鼓励小而分散的权重，"
    "从而降低模型对训练集噪声的敏感度，这是最常用的抑制过拟合手段。"
)
_ATT = (
    "缩放点积注意力除以根号 dk，是为了防止点积随维度增大而方差过大，"
    "softmax 因此进入饱和区、梯度消失，除以根号 dk 把方差拉回 1 附近。"
)
NOTE = "# 机器学习笔记\n\n## 正则化\n\n" + _PARA * 12 + "\n\n## 注意力机制\n\n" + _ATT * 12 + "\n"


def _seed(tmp_path: Path, settings) -> None:
    note = tmp_path / "n.md"
    note.write_text(NOTE, encoding="utf-8")
    IngestService(settings).ingest_paths([tmp_path])


def _call(server: McpServer, name: str, args: dict, msg_id: int = 1) -> dict:
    """发一次 tools/call，返回 result（协议层错误直接让测试炸出来）。"""
    reply = server.handle(
        {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }
    )
    assert reply is not None
    assert "error" not in reply, reply
    return reply["result"]


def _text(result: dict) -> str:
    return "\n".join(block["text"] for block in result["content"])


class _FakeService:
    """假 AskService：只回固定答案，用来测生成侧的输出形状。"""

    def __init__(self, answer: Answer | None = None, error: Exception | None = None) -> None:
        self.answer_obj = answer
        self.error = error
        self.asked: list[str] = []

    def answer(self, question: str) -> Answer:
        self.asked.append(question)
        if self.error is not None:
            raise self.error
        assert self.answer_obj is not None
        return self.answer_obj

    def search(self, query: str, *, top_k: int = 5, document_id: int | None = None) -> list:
        return []


# ---------------------------------------------------------------------------
# 协议层
# ---------------------------------------------------------------------------


def test_initialize_echoes_known_version_and_declares_tools(offline_settings):
    server = McpServer(offline_settings)
    reply = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}},
        }
    )
    assert reply is not None
    result = reply["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION  # 认识就原样回
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "mikasa"


def test_initialize_falls_back_on_unknown_version(offline_settings):
    server = McpServer(offline_settings)
    reply = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "1999-01-01"},
        }
    )
    assert reply is not None
    assert reply["result"]["protocolVersion"] == PROTOCOL_VERSION


def test_notifications_get_no_reply(offline_settings):
    server = McpServer(offline_settings)
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled"}) is None


def test_unknown_method_is_method_not_found(offline_settings):
    server = McpServer(offline_settings)
    reply = server.handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}})
    assert reply is not None
    assert reply["error"]["code"] == -32601


def test_tools_list_is_exactly_three_readonly_tools(offline_settings):
    server = McpServer(offline_settings)
    reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert reply is not None
    tools = reply["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["search", "read", "ask"]
    # 只读承诺：写工具一个都不许出现（挡的是将来手滑加 ingest/delete）
    assert not {"ingest", "delete", "settings", "index"} & {t["name"] for t in tools}
    for tool in tools:
        assert tool["description"]
        assert tool["inputSchema"]["type"] == "object"
        assert tool["inputSchema"]["additionalProperties"] is False
        assert tool["inputSchema"]["required"]


def test_invalid_params_shape_is_rejected(offline_settings):
    """params / arguments 不是对象时回协议错误，而不是让 KeyError 冒上去。"""
    server = McpServer(offline_settings)
    first = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": "nope"})
    assert first is not None and first["error"]["code"] == -32602
    second = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "search", "arguments": "nope"},
        }
    )
    assert second is not None and second["error"]["code"] == -32602


def test_unknown_tool_is_invalid_params(offline_settings):
    server = McpServer(offline_settings)
    reply = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "ingest", "arguments": {"path": "x.md"}},
        }
    )
    assert reply is not None
    assert reply["error"]["code"] == -32602


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_returns_snippets_with_provenance(tmp_path, offline_settings):
    _seed(tmp_path, offline_settings)
    server = McpServer(offline_settings)

    result = _call(server, "search", {"query": "缩放点积注意力为什么要除以根号 dk"})
    assert result["isError"] is False
    payload = json.loads(_text(result))
    assert payload["hit_count"] == len(payload["hits"]) > 0
    assert any("缩放点积注意力" in hit["snippet"] for hit in payload["hits"])

    top = payload["hits"][0]
    assert top["rank"] == 1
    assert isinstance(top["chunk_id"], int)
    assert top["document_title"]  # 出处四要素：标题 / 章节 / 页码 / chunk_id
    assert "snippet" in top and "section" in top and "page" in top


def test_search_respects_top_k(tmp_path, offline_settings):
    _seed(tmp_path, offline_settings)
    server = McpServer(offline_settings)

    one = json.loads(_text(_call(server, "search", {"query": "正则化", "top_k": 1})))
    assert len(one["hits"]) == 1
    all_hits = json.loads(_text(_call(server, "search", {"query": "正则化", "top_k": 20})))
    assert len(all_hits["hits"]) > 1


def test_search_rejects_bad_arguments(tmp_path, offline_settings):
    _seed(tmp_path, offline_settings)
    server = McpServer(offline_settings)

    for args in (
        {},
        {"query": "   "},
        {"query": "正则化", "top_k": 0},
        {"query": "x", "top_k": True},
    ):
        result = _call(server, "search", args)
        assert result["isError"] is True, args


def test_search_without_hits_says_so_instead_of_returning_nothing(offline_settings):
    server = McpServer(offline_settings, service=_FakeService())
    result = _call(server, "search", {"query": "库里根本没有的话题"})
    assert result["isError"] is False
    assert "没有检索到" in _text(result)


def test_internal_error_is_reported_and_session_survives(offline_settings):
    """工具内部炸了（非 ZhiwenError）也不能打死会话：回 isError + 如实说明。"""

    class _Boom(_FakeService):
        def search(self, query: str, *, top_k: int = 5, document_id: int | None = None) -> list:
            raise RuntimeError("模拟内核炸了")

    server = McpServer(offline_settings, service=_Boom())
    result = _call(server, "search", {"query": "任意"})
    assert result["isError"] is True
    assert "内部错误" in _text(result) and "模拟内核炸了" in _text(result)
    assert server.handle({"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}}) is not None


def test_search_on_empty_kb_explains_what_to_do(offline_settings):
    """空库不是崩溃：给一句能照做的提示（失败提示要如实）。"""
    server = McpServer(offline_settings)
    result = _call(server, "search", {"query": "任何问题"})
    assert result["isError"] is True
    assert "知识库为空" in _text(result)


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


def test_read_returns_chunk_text_and_neighbours(tmp_path, offline_settings):
    _seed(tmp_path, offline_settings)
    server = McpServer(offline_settings)
    with open_db(offline_settings.db_path) as conn:
        doc_id = repo.list_documents(conn)[0].id
        chunk_ids = repo.chunk_ids_of_document(conn, doc_id)
    assert len(chunk_ids) >= 3, "夹具前提：这篇笔记至少要切成 3 块"

    # 中间块：前后各一个
    middle = json.loads(_text(_call(server, "read", {"chunk_id": chunk_ids[1]})))
    assert middle["chunk_id"] == chunk_ids[1]
    assert middle["text"]
    assert [item["relation"] for item in middle["context"]] == ["上一块", "下一块"]
    assert [item["chunk_id"] for item in middle["context"]] == [chunk_ids[0], chunk_ids[2]]

    # 首块：只有下一块
    first = json.loads(_text(_call(server, "read", {"chunk_id": chunk_ids[0]})))
    assert [item["relation"] for item in first["context"]] == ["下一块"]


def test_read_unknown_chunk_id_is_a_tool_error_and_session_survives(offline_settings):
    server = McpServer(offline_settings)
    result = _call(server, "read", {"chunk_id": 999999})
    assert result["isError"] is True
    assert "999999" in _text(result)
    # 一次失败的调用不许打死会话：下一条请求照常回复
    assert server.handle({"jsonrpc": "2.0", "id": 9, "method": "ping", "params": {}}) is not None


def test_read_rejects_non_integer_chunk_id(offline_settings):
    server = McpServer(offline_settings)
    result = _call(server, "read", {"chunk_id": True})  # bool 是 int 的子类，必须挡掉
    assert result["isError"] is True
    assert "整数" in _text(result)


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------


def test_ask_offline_says_what_is_available_instead_of_crashing(offline_settings):
    server = McpServer(offline_settings)
    result = _call(server, "ask", {"question": "缩放点积注意力为什么要除以根号 dk？"})
    assert result["isError"] is False  # 能力说明不是错误
    text = _text(result)
    assert "离线档" in text and "search" in text


def test_ask_returns_markers_and_verifiable_citations(api_settings):
    answer = Answer(
        question="缩放点积注意力为什么要除以根号 dk？",
        text="因为点积随维度增大而方差变大，softmax 会进饱和区[1]。",
        citations=[
            Citation(
                marker=1,
                chunk_id=7,
                document_title="机器学习笔记",
                section="2. 注意力机制",
                page=3,
                snippet="缩放点积注意力除以根号 dk……",
            )
        ],
        model="deepseek-chat",
    )
    fake = _FakeService(answer=answer)
    server = McpServer(api_settings, service=fake)  # type: ignore[arg-type]

    result = _call(server, "ask", {"question": answer.question})
    assert result["isError"] is False
    assert fake.asked == [answer.question]

    blocks = result["content"]
    assert blocks[0]["text"] == answer.text  # 正文原样，[n] 保留
    assert "[1]" in blocks[0]["text"]
    meta = json.loads(blocks[1]["text"].split("\n", 1)[1])
    assert meta["model"] == "deepseek-chat"
    assert meta["refused"] is False
    assert meta["citations"] == [
        {
            "marker": 1,
            "chunk_id": 7,
            "document_title": "机器学习笔记",
            "section": "2. 注意力机制",
            "page": 3,
        }
    ]


def test_ask_surfaces_expected_failures_as_tool_error(api_settings):
    server = McpServer(api_settings, service=_FakeService(error=StorageError("知识库为空")))
    result = _call(server, "ask", {"question": "任何问题"})
    assert result["isError"] is True
    assert "知识库为空" in _text(result)


# ---------------------------------------------------------------------------
# stdio 分帧
# ---------------------------------------------------------------------------


def test_stdio_loop_answers_one_json_object_per_line(monkeypatch, offline_settings):
    lines = [
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION},
            }
        ),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "2",
                "method": "tools/call",
                "params": {"name": "search", "arguments": {"query": "正则化"}},
            }
        ),
        "这不是 JSON",
        "123",  # 合法 JSON，但不是对象
        "",
    ]
    stdout = io.StringIO()
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(lines) + "\n"))
    monkeypatch.setattr("sys.stdout", stdout)

    serve_stdio(offline_settings)

    raw = stdout.getvalue()
    assert raw.endswith("\n")
    assert "知识库为空" in raw  # ensure_ascii=False：中文原样（客户端按 UTF-8 读）
    replies = [json.loads(line) for line in raw.splitlines()]
    # 四条回复：initialize、search、解析失败、非对象；通知不回
    assert len(replies) == 4
    assert replies[0]["result"]["serverInfo"]["name"] == "mikasa"
    assert replies[1]["id"] == "2"
    assert replies[1]["result"]["isError"] is True
    assert replies[2]["id"] is None
    assert replies[2]["error"]["code"] == -32700
    assert replies[3]["error"]["code"] == -32600


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------


def test_mcp_command_starts_stdio_with_stdout_left_clean(tmp_path, monkeypatch):
    """横幅必须走 stderr：stdout 上多一个字符，客户端就解析不了。"""
    settings = load_settings("offline", data_dir=tmp_path / "data")
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: settings)
    started: list[object] = []
    monkeypatch.setattr(mcp_mod, "serve_stdio", started.append)

    result = runner.invoke(app, ["mcp", "--profile", "offline"])

    assert result.exit_code == 0
    assert started == [settings]
    assert "MCP server 已就绪" in result.stderr
    assert "离线档" in result.stderr  # mock 档的前置提示
    # stdout 一个字符都不能有（result.output 是 stdout+stderr 的合并流，
    # 所以这里必须断言 stdout 本身）
    assert result.stdout == ""
