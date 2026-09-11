"""qa 会话管理 repo 测试（M4.5）：文件夹树 CRUD、标题原子规则、级联删除。

与 test_repo.py 的分工：那里测"旧有"的会话/评测查询，本文件专测
M4.5 新增的会话管理面——文件夹增删改移、后代/计数查询工具、标题
三路径锁语义（title_manual）、会话移动与级联删除、list_sessions 新键。
"""

from __future__ import annotations

import sqlite3

import pytest

from mikasa.storage import repo
from mikasa.storage.db import open_db

# ---------------------------------------------------------------------------
# 文件夹树
# ---------------------------------------------------------------------------


def test_folder_crud_roundtrip(offline_settings):
    with open_db(offline_settings.db_path) as conn:
        assert repo.list_folders(conn) == []  # 空库无文件夹

        root = repo.create_folder(conn, "深度学习")
        child = repo.create_folder(conn, "注意力机制", parent_id=root)
        # 平铺列表（id 升序），键 = 表列全集
        rows = repo.list_folders(conn)
        assert [r["id"] for r in rows] == [root, child]
        assert {r["name"] for r in rows} == {"深度学习", "注意力机制"}
        assert rows[0]["parent_id"] is None and rows[1]["parent_id"] == root
        assert "created_at" in rows[0]
        assert repo.get_folder(conn, root)["name"] == "深度学习"
        assert repo.get_folder(conn, child)["parent_id"] == root
        assert repo.get_folder(conn, 9999) is None

        repo.rename_folder(conn, child, "Transformer")
        assert repo.get_folder(conn, child)["name"] == "Transformer"

        # 移动：根 → root 下（parent 存在性由 API 预检，repo 不校验）
        repo.move_folder(conn, child, root)
        assert repo.get_folder(conn, child)["parent_id"] == root


def test_folder_descendants_include_self_and_deep_chain(offline_settings):
    with open_db(offline_settings.db_path) as conn:
        root = repo.create_folder(conn, "根")
        a = repo.create_folder(conn, "A", parent_id=root)
        b = repo.create_folder(conn, "B", parent_id=a)  # root → A → B 三层
        side = repo.create_folder(conn, "旁支", parent_id=root)
        leaf = repo.create_folder(conn, "叶", parent_id=b)

        assert set(repo.folder_descendant_ids(conn, root)) == {root, a, b, leaf, side}
        assert repo.folder_descendant_ids(conn, b) == [b, leaf]  # 含自身（id 升序）
        assert repo.folder_descendant_ids(conn, leaf) == [leaf]
        # 从 a 看：旁支不在其后代里（防环判据的分支隔离）
        assert side not in repo.folder_descendant_ids(conn, a)


def test_folder_children_counts(offline_settings):
    with open_db(offline_settings.db_path) as conn:
        root = repo.create_folder(conn, "根")
        assert repo.folder_children_counts(conn, root) == {"subfolders": 0, "sessions": 0}

        repo.create_folder(conn, "子", parent_id=root)
        repo.create_folder(conn, "孙", parent_id=root)
        sid = repo.create_session(conn, "offline")
        repo.move_session(conn, sid, root)
        assert repo.folder_children_counts(conn, root) == {"subfolders": 2, "sessions": 1}


def test_delete_folder_empty_ok_and_referenced_raises(offline_settings):
    """空文件夹可删；仍被会话/子文件夹引用时以约束错误兜底（409 的 DB 防线）。"""
    with open_db(offline_settings.db_path) as conn:
        empty = repo.create_folder(conn, "空夹")
        repo.delete_folder(conn, empty)
        assert repo.get_folder(conn, empty) is None

        root = repo.create_folder(conn, "根")
        # 被会话引用：FK 拒绝（应用层 409 未拦的兜底）
        sid = repo.create_session(conn, "offline")
        repo.move_session(conn, sid, root)
        with pytest.raises(sqlite3.IntegrityError):
            repo.delete_folder(conn, root)
        # 被子文件夹引用：同样拒绝
        child = repo.create_folder(conn, "子", parent_id=root)
        with pytest.raises(sqlite3.IntegrityError):
            repo.delete_folder(conn, root)
        repo.delete_folder(conn, child)  # 先删子 → 根仍被会话引用
        with pytest.raises(sqlite3.IntegrityError):
            repo.delete_folder(conn, root)


# ---------------------------------------------------------------------------
# 会话：标题三路径锁语义 + 移动 + 级联删除
# ---------------------------------------------------------------------------


def test_auto_title_writes_once_until_cleared(offline_settings):
    """auto_title_if_untitled：补名一次后不随轮次漂移；手动清除后下轮可重补。"""
    with open_db(offline_settings.db_path) as conn:
        sid = repo.create_session(conn, "offline")
        # 无标题 → 写入并返回 True
        assert repo.auto_title_if_untitled(conn, sid, "第一轮标题") is True
        assert repo.get_session(conn, sid)["title"] == "第一轮标题"
        # 已有标题 → 不覆盖（新轮次不再漂移）
        assert repo.auto_title_if_untitled(conn, sid, "第二轮标题") is False
        assert repo.get_session(conn, sid)["title"] == "第一轮标题"
        # 手动清除（回未命名）→ 下轮可重新补名
        repo.set_session_title(conn, sid, None)
        assert repo.auto_title_if_untitled(conn, sid, "清后重补") is True
        assert repo.get_session(conn, sid)["title"] == "清后重补"


def test_manual_title_locks_auto_paths(offline_settings):
    """手动命名锁 title_manual=1：截断补名与 LLM 提炼都让路；清除解锁。"""
    with open_db(offline_settings.db_path) as conn:
        sid = repo.create_session(conn, "offline")
        repo.set_session_title(conn, sid, "手动名")
        assert repo.get_session(conn, sid)["title_manual"] == 1
        assert repo.auto_title_if_untitled(conn, sid, "截断名") is False
        assert repo.apply_suggested_title(conn, sid, "AI 名") is False
        assert repo.get_session(conn, sid)["title"] == "手动名"

        # 清除：标题 NULL、manual 回 0（再走自动路径就放行）
        repo.set_session_title(conn, sid, None)
        assert repo.get_session(conn, sid)["title"] is None
        assert repo.get_session(conn, sid)["title_manual"] == 0
        # 空串与纯空白同样视为清除
        repo.set_session_title(conn, sid, "再手改")
        repo.set_session_title(conn, sid, "   ")
        assert repo.get_session(conn, sid)["title"] is None


def test_suggested_title_upgrades_fallback_but_not_manual(offline_settings):
    """LLM 提炼可覆盖先前的截断兜底标题（title_manual=0 即可），手动锁除外。"""
    with open_db(offline_settings.db_path) as conn:
        sid = repo.create_session(conn, "offline")
        repo.auto_title_if_untitled(conn, sid, "截断兜底：反向传播")
        assert repo.apply_suggested_title(conn, sid, "反向传播的精髓") is True
        assert repo.get_session(conn, sid)["title"] == "反向传播的精髓"
        # manual 锁后提炼不落库
        repo.set_session_title(conn, sid, "手改")
        assert repo.apply_suggested_title(conn, sid, "提炼名") is False
        assert repo.get_session(conn, sid)["title"] == "手改"


def test_move_session_between_folders_and_root(offline_settings):
    with open_db(offline_settings.db_path) as conn:
        f1 = repo.create_folder(conn, "夹一")
        f2 = repo.create_folder(conn, "夹二")
        sid = repo.create_session(conn, "offline")
        assert repo.get_session(conn, sid)["folder_id"] is None  # 新会话在根

        repo.move_session(conn, sid, f1)
        assert repo.get_session(conn, sid)["folder_id"] == f1
        repo.move_session(conn, sid, f2)
        assert repo.get_session(conn, sid)["folder_id"] == f2
        repo.move_session(conn, sid, None)  # 移回根
        assert repo.get_session(conn, sid)["folder_id"] is None


def test_delete_session_cascades_messages(offline_settings):
    """删会话级联删消息（DB ON DELETE CASCADE）；文件夹内会话删后文件夹还在。"""
    with open_db(offline_settings.db_path) as conn:
        folder = repo.create_folder(conn, "夹")
        sid = repo.create_session(conn, "api")
        repo.move_session(conn, sid, folder)
        repo.insert_qa_message(conn, session_id=sid, role="user", content="问")
        repo.insert_qa_message(conn, session_id=sid, role="assistant", content="答")

        repo.delete_session(conn, sid)
        assert repo.get_session(conn, sid) is None
        assert repo.messages_by_session(conn, sid) == []  # 消息级联清空
        assert repo.get_folder(conn, folder) is not None  # 文件夹不连坐
        assert repo.folder_children_counts(conn, folder) == {"subfolders": 0, "sessions": 0}


def test_list_sessions_carries_title_and_folder(offline_settings):
    """list_sessions 新键（title/folder_id）——Web 会话树的数据契约（超集）。"""
    with open_db(offline_settings.db_path) as conn:
        folder = repo.create_folder(conn, "示例")
        sid = repo.create_session(conn, "offline")
        repo.move_session(conn, sid, folder)
        repo.set_session_title(conn, sid, "反向传播推导")
        row = repo.list_sessions(conn)[0]
        assert row["title"] == "反向传播推导"
        assert row["folder_id"] == folder
        # 未命名会话照常出列表（title NULL——旧前端展示回退键依旧可用）
        sid2 = repo.create_session(conn, "offline")
        by_id = {s["id"]: s for s in repo.list_sessions(conn)}
        assert by_id[sid2]["title"] is None
        assert by_id[sid2]["folder_id"] is None
