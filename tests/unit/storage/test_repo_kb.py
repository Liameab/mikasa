"""语料文件夹 repo 测试（v3）：kb_folders 树 CRUD + 文档组织属性写入。

与 test_repo_sessions.py 分工：qa 侧会话树在那里测，本文件专测 v3 新增
的 kb 侧镜像套——文件夹增删改移、后代/计数查询、文档移夹（move_document）、
文档改名（set_document_title），以及"文件夹是容器绝不级联删文档"的约束。
"""

from __future__ import annotations

import sqlite3

import pytest

from mikasa.models.document import Document
from mikasa.storage import repo
from mikasa.storage.db import open_db


def _doc(conn, title: str, doc_id: int | None = None) -> int:
    """插入一行文档（file_path 唯一避免唯一键撞车）。"""
    return repo.insert_document(
        conn,
        Document(
            title=title,
            file_path=f"uploads/{title}-{doc_id or ''}.md",
            file_type="md",
            file_sha256="sha-" + title,
            char_count=1,
        ),
    )


# ---------------------------------------------------------------------------
# 文件夹树
# ---------------------------------------------------------------------------


def test_kb_folder_crud_roundtrip(offline_settings):
    with open_db(offline_settings.db_path) as conn:
        assert repo.list_kb_folders(conn) == []  # 空库无文件夹

        root = repo.create_kb_folder(conn, "论文")
        child = repo.create_kb_folder(conn, "Transformer", parent_id=root)
        rows = repo.list_kb_folders(conn)
        assert [r["id"] for r in rows] == [root, child]
        assert {r["name"] for r in rows} == {"论文", "Transformer"}
        assert rows[0]["parent_id"] is None and rows[1]["parent_id"] == root
        assert "created_at" in rows[0]
        assert repo.get_kb_folder(conn, root)["name"] == "论文"
        assert repo.get_kb_folder(conn, child)["parent_id"] == root
        assert repo.get_kb_folder(conn, 9999) is None

        repo.rename_kb_folder(conn, child, "Attention Is All You Need")
        assert repo.get_kb_folder(conn, child)["name"] == "Attention Is All You Need"

        # 移动：root 下 → 根级 → 另一父级（parent 存在性由 API 预检，repo 不校验）
        repo.move_kb_folder(conn, child, None)
        assert repo.get_kb_folder(conn, child)["parent_id"] is None
        repo.move_kb_folder(conn, child, root)
        assert repo.get_kb_folder(conn, child)["parent_id"] == root


def test_kb_folder_descendants_include_self_and_deep_chain(offline_settings):
    with open_db(offline_settings.db_path) as conn:
        root = repo.create_kb_folder(conn, "根")
        a = repo.create_kb_folder(conn, "A", parent_id=root)
        b = repo.create_kb_folder(conn, "B", parent_id=a)
        side = repo.create_kb_folder(conn, "旁支", parent_id=root)
        leaf = repo.create_kb_folder(conn, "叶", parent_id=b)

        assert set(repo.kb_folder_descendant_ids(conn, root)) == {root, a, b, leaf, side}
        assert repo.kb_folder_descendant_ids(conn, b) == [b, leaf]  # 含自身
        assert side not in repo.kb_folder_descendant_ids(conn, a)


def test_kb_folder_children_counts_subfolders_and_documents(offline_settings):
    with open_db(offline_settings.db_path) as conn:
        root = repo.create_kb_folder(conn, "根")
        assert repo.kb_folder_children_counts(conn, root) == {"subfolders": 0, "documents": 0}

        repo.create_kb_folder(conn, "子", parent_id=root)
        repo.create_kb_folder(conn, "孙", parent_id=root)
        doc = _doc(conn, "甲")
        repo.move_document(conn, doc, root)
        assert repo.kb_folder_children_counts(conn, root) == {"subfolders": 2, "documents": 1}
        # 计数只数直接子项："子"夹自己空着就是零（根的文档不跨级算进来）
        sub = repo.create_kb_folder(conn, "子夹", parent_id=root)
        repo.create_kb_folder(conn, "孙夹", parent_id=sub)
        assert repo.kb_folder_children_counts(conn, sub) == {"subfolders": 1, "documents": 0}


def test_delete_kb_folder_empty_ok_and_referenced_raises(offline_settings):
    """空文件夹可删；仍被文档/子文件夹引用时以约束错误兜底（409 的 DB 防线）。"""
    with open_db(offline_settings.db_path) as conn:
        empty = repo.create_kb_folder(conn, "空夹")
        repo.delete_kb_folder(conn, empty)
        assert repo.get_kb_folder(conn, empty) is None

        root = repo.create_kb_folder(conn, "根")
        # 被文档引用：FK 拒绝——文件夹是容器，绝不级联删文档（语料不连坐）
        doc = _doc(conn, "甲")
        repo.move_document(conn, doc, root)
        with pytest.raises(sqlite3.IntegrityError):
            repo.delete_kb_folder(conn, root)
        assert repo.get_document(conn, doc) is not None  # 文档安然无恙
        # 被子文件夹引用：同样拒绝
        child = repo.create_kb_folder(conn, "子", parent_id=root)
        with pytest.raises(sqlite3.IntegrityError):
            repo.delete_kb_folder(conn, root)
        repo.delete_kb_folder(conn, child)  # 先删子 → 根仍被文档引用
        with pytest.raises(sqlite3.IntegrityError):
            repo.delete_kb_folder(conn, root)
        repo.move_document(conn, doc, None)  # 文档移走后空根可删
        repo.delete_kb_folder(conn, root)
        assert repo.get_kb_folder(conn, root) is None


# ---------------------------------------------------------------------------
# 文档组织属性：移夹 + 改名
# ---------------------------------------------------------------------------


def test_move_document_between_folders_and_root(offline_settings):
    with open_db(offline_settings.db_path) as conn:
        f1 = repo.create_kb_folder(conn, "夹一")
        f2 = repo.create_kb_folder(conn, "夹二")
        doc = _doc(conn, "甲")
        assert repo.get_document(conn, doc).folder_id is None  # 新文档在根级

        repo.move_document(conn, doc, f1)
        assert repo.get_document(conn, doc).folder_id == f1
        repo.move_document(conn, doc, f2)
        assert repo.get_document(conn, doc).folder_id == f2
        repo.move_document(conn, doc, None)  # 移回根级
        assert repo.get_document(conn, doc).folder_id is None


def test_set_document_title_only_touches_display(offline_settings):
    """改名只改 documents.title——upload 副本文件名不动（file_path 原样）。"""
    with open_db(offline_settings.db_path) as conn:
        doc = _doc(conn, "旧名")
        repo.set_document_title(conn, doc, "新名")
        row = repo.get_document(conn, doc)
        assert row.title == "新名"
        assert row.file_path == "uploads/旧名-.md"  # 副本路径不受影响
