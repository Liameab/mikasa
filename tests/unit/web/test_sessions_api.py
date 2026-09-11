"""会话管理端点测试（M4.5 P3）：文件夹树契约 + 会话改名/移动/删除 + 标题提炼。

按已批准计划逐契约：防环（自身/孙级）、非空删除计数文案、手动命名锁、
移动/移回根、suggest 三态（截断兜底落库 / 手动锁不落库 / 无提问 400）、
字段缺省不动（PATCH {} = no-op）。MockLLM（offline）上标题行为可预测：
首轮问答即自动截断命名，suggest 走截断兜底而非 LLM。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mikasa.config.settings import load_settings
from mikasa.errors import ProviderError
from mikasa.providers.llm import Completion
from mikasa.web.app import create_app

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


class _ApiLLM:
    """api 档替身：complete 同时服务 free 问答与标题提炼两条路径。

    complete 是 AskService 的唯一 LLM 出口（free 问答与 suggest_title 共用），
    按 messages 首条区分：标题提炼的 system 提示词含"标题"字样（与
    pipeline 单元测试同判据），其余视为 free 问答正文。stream 只做形态
    完整（本文件不触发）。与 conftest._WebFreeLLM 同构——tests/ 无
    __init__.py，独立存放避免跨模块 import。
    """

    model = "fake-suggest"

    def __init__(
        self,
        title: str = "标题：「反向传播的精髓」",
        answer: str = "反向传播的核心是链式法则。",
        boom: bool = False,
    ) -> None:
        self.title = title
        self.answer = answer
        self.boom = boom
        self.complete_calls: list[tuple[list[dict], float, int]] = []

    def stream(self, messages, *, temperature, max_tokens):  # type: ignore[no-untyped-def]
        yield "第一段"
        yield "第二段"

    def complete(self, messages, *, temperature, max_tokens):  # type: ignore[no-untyped-def]
        self.complete_calls.append((messages, temperature, max_tokens))
        if self.boom and messages[0]["role"] == "system" and "标题" in messages[0]["content"]:
            raise ProviderError("上游 500（模拟）")
        if messages[0]["role"] == "system" and "标题" in messages[0]["content"]:
            return Completion(text=self.title)
        return Completion(text=self.answer)


@pytest.fixture()
def suggest_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """api profile + 替身 LLM：标题提炼的 LLM 分支端到端（零网络零密钥）。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek-0000")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-test-silicon-0000")
    settings = load_settings("api", data_dir=tmp_path / "data")
    settings.ensure_dirs()
    llm = _ApiLLM()
    monkeypatch.setattr("mikasa.pipeline.ask.get_llm", lambda config: llm)
    with TestClient(create_app(settings)) as c:
        yield c, settings, llm


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _mkfolder(c, name: str, parent_id: int | None = None) -> dict:
    """POST /api/folders 并断言 201，返回 folder dict。"""
    body = {"name": name}
    if parent_id is not None:
        body["parent_id"] = parent_id
    resp = c.post("/api/folders", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["folder"]


def _new_session(c, question: str = "L2 正则化是什么？") -> int:
    """seeded 语料上问一轮，返回新会话 id（mock 自动命名已生效）。"""
    resp = c.post("/api/ask", json={"question": question})
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


# ---------------------------------------------------------------------------
# 文件夹：创建 / 列出 / 改名 / 移动防环 / 删除
# ---------------------------------------------------------------------------


def test_folders_start_empty_and_create_root(client):
    c, _ = client
    assert c.get("/api/folders").json() == {"folders": []}
    folder = _mkfolder(c, "机器学习")
    assert folder["name"] == "机器学习"
    assert folder["parent_id"] is None
    assert c.get("/api/folders").json()["folders"] == [folder]


def test_create_folder_child_and_validation(client):
    c, _ = client
    root = _mkfolder(c, "复习")
    child = _mkfolder(c, "数学", parent_id=root["id"])
    assert child["parent_id"] == root["id"]

    resp = c.post("/api/folders", json={"name": "孤儿", "parent_id": 9999})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "父文件夹不存在"

    blank = c.post("/api/folders", json={"name": "   "})
    assert blank.status_code == 400
    assert blank.json()["detail"] == "文件夹名不能为空"

    empty = c.post("/api/folders", json={"name": ""})
    assert empty.status_code == 422  # pydantic min_length 先挡

    too_long = c.post("/api/folders", json={"name": "长" * 51})
    assert too_long.status_code == 422


def test_patch_folder_rename_noop_and_404(client):
    c, _ = client
    folder = _mkfolder(c, "旧名")
    resp = c.patch(f"/api/folders/{folder['id']}", json={"name": "新名"})
    assert resp.status_code == 200
    assert resp.json()["folder"]["name"] == "新名"

    # 字段缺省 = 不动：空 PATCH 是 no-op
    noop = c.patch(f"/api/folders/{folder['id']}", json={})
    assert noop.status_code == 200
    assert noop.json()["folder"]["name"] == "新名"

    blank = c.patch(f"/api/folders/{folder['id']}", json={"name": "  "})
    assert blank.status_code == 400

    missing = c.patch("/api/folders/9999", json={"name": "x"})
    assert missing.status_code == 404
    assert missing.json()["detail"] == "文件夹不存在"


def test_folder_move_and_cycle_prevention(client):
    """防环契约：新父 ∈ 自身∪后代 → 409；移回根恒允许。"""
    c, _ = client
    a = _mkfolder(c, "A")
    a1 = _mkfolder(c, "A1", parent_id=a["id"])
    _mkfolder(c, "A1-1", parent_id=a1["id"])
    b = _mkfolder(c, "B")

    self_move = c.patch(f"/api/folders/{a1['id']}", json={"parent_id": a1["id"]})
    assert self_move.status_code == 409
    assert self_move.json()["detail"] == "不能把文件夹移入自身或其子文件夹"

    descendant_move = c.patch(f"/api/folders/{a['id']}", json={"parent_id": a1["id"]})
    assert descendant_move.status_code == 409  # a1 是 a 的后代：入 a1 即成环

    missing_parent = c.patch(f"/api/folders/{b['id']}", json={"parent_id": 9999})
    assert missing_parent.status_code == 404

    cross = c.patch(f"/api/folders/{a1['id']}", json={"parent_id": b["id"]})
    assert cross.status_code == 200
    assert cross.json()["folder"]["parent_id"] == b["id"]

    unroot = c.patch(f"/api/folders/{a1['id']}", json={"parent_id": None})
    assert unroot.status_code == 200
    assert unroot.json()["folder"]["parent_id"] is None


def test_delete_folder_nonempty_409_with_counts(client):
    c, _ = client
    a = _mkfolder(c, "A")
    _mkfolder(c, "A1", parent_id=a["id"])
    resp = c.delete(f"/api/folders/{a['id']}")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "1 个子文件夹" in detail and "0 个会话" in detail


def test_delete_folder_empty_ok_and_404(client):
    c, _ = client
    folder = _mkfolder(c, "空夹")
    resp = c.delete(f"/api/folders/{folder['id']}")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": folder["id"], "name": "空夹"}
    assert c.get("/api/folders").json()["folders"] == []

    missing = c.delete("/api/folders/9999")
    assert missing.status_code == 404


# ---------------------------------------------------------------------------
# 会话：改名（手动锁）/ 移动 / 删除
# ---------------------------------------------------------------------------


def test_patch_session_title_manual_lock_and_clear(seeded_client):
    """改名 → 锁手动（suggest 不再落库）；显式 null 清除解锁 → 恢复自动补名。"""
    c, _ = seeded_client
    session_id = _new_session(c)
    assert session_id is not None

    resp = c.patch(f"/api/sessions/{session_id}", json={"title": "机器学习基础"})
    assert resp.status_code == 200
    assert resp.json()["session"]["title"] == "机器学习基础"

    # 手动命名锁：suggest 给出建议但不落库
    suggested = c.post(f"/api/sessions/{session_id}/title/suggest", json={})
    assert suggested.status_code == 200
    body = suggested.json()
    assert body["applied"] is False
    assert body["title"] == "L2 正则化是什么？"
    assert c.get("/api/sessions").json()["sessions"][0]["title"] == "机器学习基础"

    # 显式 null = 清除解锁：suggest 重新落库（截断兜底 applied=true）
    cleared = c.patch(f"/api/sessions/{session_id}", json={"title": None})
    assert cleared.status_code == 200
    assert cleared.json()["session"]["title"] is None
    re_suggest = c.post(f"/api/sessions/{session_id}/title/suggest", json={})
    assert re_suggest.status_code == 200
    assert re_suggest.json()["applied"] is True
    assert re_suggest.json()["title"] == "L2 正则化是什么？"
    assert c.get("/api/sessions").json()["sessions"][0]["title"] == "L2 正则化是什么？"


def test_patch_session_title_validation(seeded_client):
    c, _ = seeded_client
    session_id = _new_session(c)
    blank = c.patch(f"/api/sessions/{session_id}", json={"title": "   "})
    assert blank.status_code == 400
    assert "清除请传 null" in blank.json()["detail"]
    too_long = c.patch(f"/api/sessions/{session_id}", json={"title": "长" * 101})
    assert too_long.status_code == 422


def test_patch_session_move_unroot_and_noop(seeded_client):
    """folder_id 单发不碰标题；null 移回根；字段缺省不动。"""
    c, _ = seeded_client
    session_id = _new_session(c)
    folder = _mkfolder(c, "会话夹")
    resp = c.patch(f"/api/sessions/{session_id}", json={"folder_id": folder["id"]})
    assert resp.status_code == 200
    session = resp.json()["session"]
    assert session["folder_id"] == folder["id"]
    assert session["title"] == "L2 正则化是什么？"  # 移夹不碰标题

    # 列表携带 folder_id（前端组树数据源）
    listed = c.get("/api/sessions").json()["sessions"][0]
    assert listed["folder_id"] == folder["id"]
    assert listed["title"] == "L2 正则化是什么？"

    unroot = c.patch(f"/api/sessions/{session_id}", json={"folder_id": None})
    assert unroot.json()["session"]["folder_id"] is None

    missing_folder = c.patch(f"/api/sessions/{session_id}", json={"folder_id": 9999})
    assert missing_folder.status_code == 404
    assert missing_folder.json()["detail"] == "文件夹不存在"

    missing_session = c.patch("/api/sessions/9999", json={"title": "x"})
    assert missing_session.status_code == 404
    assert missing_session.json()["detail"] == "会话不存在"


def test_patch_session_folder_only_preserves_manual_title(seeded_client):
    """手动标题的会话被移动后，标题与锁不丢（字段缺省 = 该属性不动）。"""
    c, _ = seeded_client
    session_id = _new_session(c)
    c.patch(f"/api/sessions/{session_id}", json={"title": "锁定名"})
    folder = _mkfolder(c, "夹")
    moved = c.patch(f"/api/sessions/{session_id}", json={"folder_id": folder["id"]})
    assert moved.json()["session"]["title"] == "锁定名"
    assert moved.json()["session"]["folder_id"] == folder["id"]
    # 锁仍在：suggest 不落库
    suggested = c.post(f"/api/sessions/{session_id}/title/suggest", json={})
    assert suggested.json()["applied"] is False


def test_delete_session_cascades_and_folder_survives(seeded_client):
    """删会话：消息级联清、列表消失；会话所在文件夹不受连坐。"""
    c, _ = seeded_client
    session_id = _new_session(c)
    c.post(
        "/api/ask",
        json={"question": "那早停呢？", "session_id": session_id},
    )  # 两轮 → 4 条消息
    folder = _mkfolder(c, "夹")
    c.patch(f"/api/sessions/{session_id}", json={"folder_id": folder["id"]})

    resp = c.delete(f"/api/sessions/{session_id}")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": session_id, "title": "L2 正则化是什么？"}

    assert c.get(f"/api/sessions/{session_id}/messages").status_code == 404  # 消息连坐
    assert session_id not in [s["id"] for s in c.get("/api/sessions").json()["sessions"]]
    folders = c.get("/api/folders").json()["folders"]
    assert [f["id"] for f in folders] == [folder["id"]]  # 文件夹活着

    missing = c.delete("/api/sessions/9999")
    assert missing.status_code == 404


# ---------------------------------------------------------------------------
# 标题提炼：三态 + apply 预览
# ---------------------------------------------------------------------------


def test_suggest_without_question_is_400(seeded_client):
    """无提问会话：400 业务错误。

    注意造法：2026-09-11 起"失败的提问"不再留下空会话（要修的那个 bug 正是
    "守卫/空库报错也会先建会话"），所以这里直接建一条会话，而不是靠失败的
    free 提问——后者现在会被清理掉（见 test_free_guard_failure_leaves_no_session）。
    """
    from mikasa.storage import repo
    from mikasa.storage.db import open_db

    c, settings = seeded_client
    with open_db(settings.db_path) as conn:
        empty_session_id = repo.create_session(conn, settings.profile)

    suggest = c.post(f"/api/sessions/{empty_session_id}/title/suggest", json={})
    assert suggest.status_code == 400
    assert "还没有提问" in suggest.json()["detail"]


def test_free_guard_failure_leaves_no_session(seeded_client):
    """free+offline 被守卫拒绝：不能留下空会话（2026-09-11 修复）。

    旧行为是"先建会话、问答失败后行留空"——用户每被拦一次，会话树里就多
    一条什么都没有的对话（与 CLI chat 的"早失败不落审计轨迹"纪律相悖）。
    """
    c, _ = seeded_client
    before = c.get("/api/sessions").json()["sessions"]

    resp = c.post("/api/ask", json={"question": "讲讲太阳系", "mode": "free"})
    assert resp.status_code == 400  # ConfigError：free 需 api/local

    after = c.get("/api/sessions").json()["sessions"]
    assert len(after) == len(before), "失败不该留下空会话"


def test_suggest_unknown_session_404(seeded_client):
    c, _ = seeded_client
    resp = c.post("/api/sessions/9999/title/suggest", json={})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "会话不存在"


def test_suggest_apply_false_previews_only(seeded_client):
    """apply=false：只回建议不落库（配合手动锁语义，值上等价于建议态）。"""
    c, _ = seeded_client
    session_id = _new_session(c)
    resp = c.post(f"/api/sessions/{session_id}/title/suggest", json={"apply": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is False
    assert body["title"] == "L2 正则化是什么？"  # mock 档 = 首问截断
    assert c.get("/api/sessions").json()["sessions"][0]["title"] == "L2 正则化是什么？"


def test_suggest_api_llm_title_cleaned_and_applied(suggest_client):
    """api 档：LLM 提炼（≤16 字清洗）→ 落库覆盖截断标题，applied=true。"""
    c, settings, llm = suggest_client
    # 自由问答建会话（fake.stream 两段）→ 自动截断标题已就位
    ask = c.post("/api/ask", json={"question": "什么是反向传播？", "mode": "free"})
    assert ask.status_code == 200, ask.text
    session_id = ask.json()["session_id"]

    resp = c.post(f"/api/sessions/{session_id}/title/suggest", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"title": "反向传播的精髓", "applied": True}
    assert c.get("/api/sessions").json()["sessions"][0]["title"] == "反向传播的精髓"

    assert len(llm.complete_calls) == 2  # 问答正文一次 + 标题提炼恰一次
    messages, temperature, max_tokens = llm.complete_calls[-1]  # 最后一次 = suggest
    assert messages[0]["role"] == "system" and "标题" in messages[0]["content"]
    assert messages[-1]["content"] == "【问题】什么是反向传播？\n【回答】反向传播的核心是链式法则。"
    assert temperature == settings.llm.temperature
    assert max_tokens == 24


def test_suggest_api_provider_error_falls_back(suggest_client, monkeypatch):
    """LLM 失败：回退截断标题且 applied=true（提炼失败不拖挂页面流程）。"""
    c, _, llm = suggest_client
    llm.boom = True
    ask = c.post("/api/ask", json={"question": "什么是反向传播？", "mode": "free"})
    session_id = ask.json()["session_id"]
    resp = c.post(f"/api/sessions/{session_id}/title/suggest", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is True
    assert body["title"] == "什么是反向传播？"  # 截断兜底
    assert c.get("/api/sessions").json()["sessions"][0]["title"] == "什么是反向传播？"
