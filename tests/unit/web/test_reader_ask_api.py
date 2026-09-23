"""阅读器「边看边问」端点测试（A 档 c，2026-09-22）。

被测契约：POST /api/documents/{id}/ask/stream
  - 帧序与问答页同构（meta → delta×n → done，delta 拼接 === done.text），
    但 **meta 帧没有 session_id 键**（这条路不建会话）、多一个 sources；
  - **不落库**：qa_sessions / qa_messages 一行都不许多；
  - scope=doc 只看本篇、scope=all 全库且标 current_doc；
  - 失败语义：文档不存在 404 / 本篇无正文 400（都在流开始前）；
    空问题 → 恰一帧 error（HTTP 仍是 200，与问答页同口径）；
    选中文字超长 → 422 且文案是中文。

解析器 _parse_sse 与 test_ask_stream.py 同构（与前端 js 的帧切分一致）。
"""

from __future__ import annotations

import json
from pathlib import Path

from mikasa.ingest.service import IngestService
from mikasa.storage import repo
from mikasa.storage.db import open_db

OTHER_NOTE = """# 螺旋锚笔记

## 承载力

循环荷载作用下螺旋锚的承载力随加载次数下降，位移逐渐累积。
"""


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """SSE 文本 → [(event, data_json)]：与前端同构的帧切分。"""
    frames: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event = ""
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data_lines.append(line.removeprefix("data: "))
        frames.append((event, json.loads("\n".join(data_lines))))
    return frames


def _first_doc(c, needle: str = "") -> dict:
    """取（含 needle 的）第一篇文档。"""
    docs = c.get("/api/documents").json()["documents"]
    if needle:
        docs = [d for d in docs if needle in d["title"]]
    assert docs, f"库里没有匹配 {needle!r} 的文档"
    return docs[0]


def _ask(c, doc_id: int, question: str, **extra):
    return c.post(f"/api/documents/{doc_id}/ask/stream", json={"question": question, **extra})


def test_reader_stream_frames_and_sources_shape(seeded_client):
    c, _ = seeded_client
    doc = _first_doc(c, "机器学习笔记")
    resp = _ask(c, doc["id"], "缩放点积注意力除以根号 dk 是为了什么")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = _parse_sse(resp.text)

    kinds = [kind for kind, _ in frames]
    assert kinds[0] == "meta"
    assert kinds[-1] == "done"
    assert set(kinds[1:-1]) == {"delta"}

    meta = frames[0][1]
    assert "session_id" not in meta, "阅读器这条路不建会话：meta 帧不许带 session_id"
    assert meta["document_id"] == doc["id"]
    assert meta["scope"] == "doc"
    assert meta["title"] == doc["title"]
    assert isinstance(meta["sources"], list) and meta["sources"], "meta 帧就该有相关片段"
    assert all(s["current_doc"] and not s["cited"] for s in meta["sources"])

    done = frames[-1][1]
    answer = done["answer"]
    concat = "".join(data["text"] for kind, data in frames if kind == "delta")
    assert concat == answer["text"]  # 协议对偶：拼接恒等
    assert "session_id" not in done

    # 相关片段的键集锁死（前端按这套字段渲染；改动必须同步改前端与本文档）
    expected_keys = {
        "chunk_id",
        "document_id",
        "document_title",
        "section",
        "page",
        "snippet",
        "rank",
        "cited",
        "marker",
        "current_doc",
    }
    assert {key for key in done["sources"][0]} == expected_keys
    cited = [s for s in done["sources"] if s["cited"]]
    assert cited, "答案引用了资料，done 帧必须标出被引用的片段"
    assert {s["marker"] for s in cited} == {c_["marker"] for c_ in answer["citations"]}
    assert all(s["document_id"] == doc["id"] for s in done["sources"])


def test_reader_ask_creates_no_session(seeded_client):
    """即问即散：问答页那条"每问必建会话"的路在这里绝不走。"""
    c, settings = seeded_client
    doc = _first_doc(c, "机器学习笔记")
    assert c.get("/api/sessions").json()["sessions"] == []

    resp = _ask(c, doc["id"], "L2 正则化为什么能防止过拟合？")
    assert _parse_sse(resp.text)[-1][0] == "done"

    assert c.get("/api/sessions").json()["sessions"] == []
    with open_db(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM qa_sessions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM qa_messages").fetchone()[0] == 0


def test_reader_scope_doc_vs_all(seeded_client):
    """默认只看本篇；切 all 才带回别的文档（并标 current_doc=False）。"""
    c, settings = seeded_client
    src = settings.data_dir.parent / "notes"
    (src / "other.md").write_text(OTHER_NOTE, encoding="utf-8")
    IngestService(settings).ingest_paths([src])

    doc = _first_doc(c, "机器学习笔记")
    question = "循环荷载作用下螺旋锚的承载力如何变化？"

    narrow = _parse_sse(_ask(c, doc["id"], question).text)
    assert all(s["current_doc"] for s in narrow[-1][1]["sources"]), "本篇口径不许带别的文档"

    wide = _parse_sse(_ask(c, doc["id"], question, scope="all").text)
    assert wide[0][1]["scope"] == "all"
    others = [s for s in wide[-1][1]["sources"] if not s["current_doc"]]
    assert others, "全库口径应带回别的文档的片段"
    assert all(s["document_title"] == "螺旋锚笔记" for s in others)


def test_reader_missing_document_404(seeded_client):
    c, _ = seeded_client
    resp = _ask(c, 9999, "任何问题")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "文档不存在（可能已被删除）"


def test_reader_pending_document_400(client):
    """本篇还没有可检索的正文 → 400 且文案可照做（切全库/看识别状态）。"""
    c, settings = client
    with open_db(settings.db_path) as conn:
        cur = conn.execute(
            """INSERT INTO documents (title, file_path, file_type, file_sha256)
               VALUES ('未入库', 'x.md', 'md', 'sha-x')"""
        )
        doc_id = int(cur.lastrowid)
        conn.commit()

    resp = _ask(c, doc_id, "任何问题")
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "全库" in detail and "识别状态" in detail

    # scope=all 时放行：问的是全库，这篇空不空与问题无关
    wide = _ask(c, doc_id, "任何问题", scope="all")
    assert wide.status_code == 200


def test_reader_blank_question_single_error_frame(seeded_client):
    """空白问题：恰一帧 error、零 meta、HTTP 200（与 /api/ask/stream 同口径）。"""
    c, _ = seeded_client
    doc = _first_doc(c, "机器学习笔记")
    resp = _ask(c, doc["id"], "   ")
    assert resp.status_code == 200
    frames = _parse_sse(resp.text)
    assert [kind for kind, _ in frames] == ["error"]
    assert "问题为空" in frames[0][1]["message"]


def test_reader_context_too_long_422_chinese(seeded_client):
    """选中文字超长 → 422，且文案是中文（不是 pydantic 的英文原句）。"""
    c, _ = seeded_client
    doc = _first_doc(c, "机器学习笔记")
    resp = _ask(c, doc["id"], "问题", context="选" * 1001)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    blob = json.dumps(detail, ensure_ascii=False)
    assert "选中的文字过长" in blob
    assert "String should have" not in blob  # 英文默认文案不许漏到界面


def test_reader_context_reaches_llm_messages(tmp_path: Path, monkeypatch):
    """选中文字真的进了提示词：替身 LLM 记录 messages，断言段序与内容。

    这里**自己起 app**而不是用 client 夹具：AppServices 在 create_app 时就
    把 AskService（含 LLM）构造好了，夹具建完再 monkeypatch 是打不中的。
    """
    from fastapi.testclient import TestClient

    from mikasa.config.settings import load_settings
    from mikasa.web.app import create_app

    captured: list[list[dict]] = []

    class _Recorder:
        model = "recorder"

        def stream(self, messages, *, temperature, max_tokens):
            captured.append(list(messages))
            yield "摩擦系数 μ 描述接触面的粗糙程度。[1]"

        def complete(self, messages, *, temperature, max_tokens):
            raise AssertionError("阅读器链路应当走流式")

    monkeypatch.setattr("mikasa.pipeline.ask.get_llm", lambda config: _Recorder())
    settings = load_settings("offline", data_dir=tmp_path / "data")
    src = tmp_path / "notes"
    src.mkdir(exist_ok=True)
    (src / "note.md").write_text("# 笔记\n\n摩擦系数 μ 描述接触面的粗糙程度。\n", encoding="utf-8")
    IngestService(settings).ingest_paths([src])

    with TestClient(create_app(settings)) as c:
        doc = _first_doc(c, "笔记")
        resp = _ask(
            c, doc["id"], "这里说的 μ 是什么意思？", context="摩擦系数 μ 描述接触面的粗糙程度"
        )
        assert _parse_sse(resp.text)[-1][0] == "done"

    assert captured, "替身 LLM 没有被调用"
    user_message = next(m["content"] for m in captured[0] if m["role"] == "user")
    assert "摩擦系数 μ 描述接触面的粗糙程度" in user_message
    assert user_message.index("【正在阅读的段落】") < user_message.index("【资料片段】")


def test_reader_sources_carry_titles_and_rank_order(seeded_client):
    """片段带文档标题与检索名次（前端列表按它排序/标注来源）。"""
    c, _ = seeded_client
    doc = _first_doc(c, "机器学习笔记")
    frames = _parse_sse(_ask(c, doc["id"], "L2 正则化为什么能防止过拟合？").text)
    sources = frames[-1][1]["sources"]
    assert sources, "应当有命中"
    assert [s["rank"] for s in sources] == list(range(1, len(sources) + 1))
    assert all(s["document_title"] == "机器学习笔记" for s in sources)
    assert all(s["snippet"] for s in sources)


def test_reader_ask_does_not_touch_repo_messages(seeded_client):
    """再确认一次"不落库"是表级事实：连 repo 层的消息表都不许有行。"""
    c, settings = seeded_client
    doc = _first_doc(c, "机器学习笔记")
    _ask(c, doc["id"], "L2 正则化为什么能防止过拟合？")
    with open_db(settings.db_path) as conn:
        assert repo.messages_by_session(conn, 1) == []
