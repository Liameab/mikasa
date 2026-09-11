"""数据访问层测试：会话列表、评测记录单查、报告回填与向量模型分布。

list_sessions / get_eval_run / update_eval_run_report 服务 Web 会话页
与评测报告页；embedding_models_in_db 是 doctor 索引一致性的判定输入。
"""

from __future__ import annotations

from mikasa.storage import repo
from mikasa.storage.db import open_db


def _sessions(offline_settings) -> list[dict]:
    with open_db(offline_settings.db_path) as conn:
        return repo.list_sessions(conn)


def test_list_sessions_empty_db(offline_settings):
    assert _sessions(offline_settings) == []


def test_list_sessions_orders_newest_first_with_message_count(offline_settings):
    with open_db(offline_settings.db_path) as conn:
        s1 = repo.create_session(conn, "offline")
        s2 = repo.create_session(conn, "offline")
        # s1 两问两答，s2 一问一答
        for question in ("第一问", "第二问"):
            repo.insert_qa_message(conn, session_id=s1, role="user", content=question)
            repo.insert_qa_message(conn, session_id=s1, role="assistant", content="答")
        repo.insert_qa_message(conn, session_id=s2, role="user", content="只问")
        repo.insert_qa_message(conn, session_id=s2, role="assistant", content="只答")

    sessions = _sessions(offline_settings)
    assert [s["id"] for s in sessions] == [s2, s1]  # 新会话在前
    by_id = {s["id"]: s for s in sessions}
    assert by_id[s1]["message_count"] == 4
    assert by_id[s2]["message_count"] == 2
    assert by_id[s1]["profile"] == "offline"
    assert "created_at" in by_id[s1]


def test_get_session_exists_and_missing(offline_settings):
    with open_db(offline_settings.db_path) as conn:
        s = repo.create_session(conn, "offline")
        assert repo.get_session(conn, s)["id"] == s
        assert repo.get_session(conn, 9999) is None


def test_new_session_untitled_and_unfoldered(offline_settings):
    """M4.5 新列默认：新建会话 title=NULL / title_manual=0 / folder_id=NULL。"""
    with open_db(offline_settings.db_path) as conn:
        s = repo.create_session(conn, "offline")
        row = repo.get_session(conn, s)
        assert row["title"] is None  # NULL=未命名，展示层回退 #id
        assert row["title_manual"] == 0
        assert row["folder_id"] is None  # NULL=根（未归入文件夹）


def test_embedding_models_in_db_distribution(offline_settings):
    """向量行数按模型分组：空库 / 单模型 / 混嵌残留（doctor 判定输入）。"""
    import numpy as np

    from mikasa.models.document import Chunk, Document

    def _chunks(n: int, doc_id: int) -> list[Chunk]:
        return [
            Chunk(document_id=doc_id, seq=i, content=f"块{i}", content_sha256=f"s{i}")
            for i in range(n)
        ]

    with open_db(offline_settings.db_path) as conn:
        assert repo.embedding_models_in_db(conn) == {}  # 空库

        doc_id = repo.insert_document(
            conn,
            Document(
                title="样本",
                file_path="样本.md",
                file_type="md",
                file_sha256="sha",
                char_count=1,
            ),
        )
        repo.insert_chunks(conn, _chunks(3, doc_id))
        ids = repo.chunk_ids_of_document(conn, doc_id)

        # 单模型 → 一个键；再混入另一模型 → 两个键（doctor 判"部分迁移"）
        repo.save_embeddings(conn, "BAAI/bge-m3", ids, np.zeros((3, 4), dtype=np.float32))
        assert repo.embedding_models_in_db(conn) == {"BAAI/bge-m3": 3}

        repo.clear_embeddings(conn, "BAAI/bge-m3")
        repo.save_embeddings(conn, "BAAI/bge-m3", ids[:2], np.zeros((2, 4), dtype=np.float32))
        repo.save_embeddings(
            conn, "BAAI/bge-small-zh-v1.5", ids[2:], np.zeros((1, 2), dtype=np.float32)
        )
        assert repo.embedding_models_in_db(conn) == {
            "BAAI/bge-m3": 2,
            "BAAI/bge-small-zh-v1.5": 1,
        }


def test_eval_run_insert_get_update_roundtrip(offline_settings):
    with open_db(offline_settings.db_path) as conn:
        assert repo.get_eval_run(conn, 1) is None  # 不存在 → None（Web 404 依据）

        run_id = repo.insert_eval_run(
            conn,
            eval_set_name="golden",
            corpus_sha256="abc123",
            config_json='{"profile": "offline"}',
            metrics_json='{"recall@10": 1.0}',
            report_md="",  # 占位：跑完才渲染报告回填
        )
        assert run_id == 1

        before = repo.get_eval_run(conn, run_id)
        assert before is not None
        assert before["report_md"] == ""

        repo.update_eval_run_report(conn, run_id, "# 评测报告\n全绿。")
        after = repo.get_eval_run(conn, run_id)
        assert after["report_md"] == "# 评测报告\n全绿。"

        # 回填不改变其他列
        assert after["metrics_json"] == before["metrics_json"]
        assert after["corpus_sha256"] == before["corpus_sha256"]
