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


def test_corpus_fingerprint_detects_id_shift_with_same_count(offline_settings):
    """reindex 后 chunk 总数不变、id 整体平移时，指纹必须变。

    这是"引用变死 id"的根因：Web 的内存快照若只比数量，就会以为库没变、
    继续用旧快照发引用——`citation.chunk_id` 在库里已不存在（/api/chunks/{id}
    404、高亮跳转全废），标题也退化成"未知文档"。
    """
    from mikasa.models.document import Chunk, Document

    def _chunks(n: int, doc_id: int) -> list[Chunk]:
        return [
            Chunk(document_id=doc_id, seq=i, content=f"块{i}", content_sha256=f"s{i}")
            for i in range(n)
        ]

    with open_db(offline_settings.db_path) as conn:
        doc_id = repo.insert_document(
            conn,
            Document(
                title="样本", file_path="样本.md", file_type="md", file_sha256="sha", char_count=1
            ),
        )
        repo.insert_chunks(conn, _chunks(3, doc_id))
        first = repo.corpus_fingerprint(conn)

        # 模拟 `mikasa ingest --reindex`：整表重建，数量一样但 id 后移
        conn.execute("DELETE FROM chunks")
        repo.insert_chunks(conn, _chunks(3, doc_id))
        second = repo.corpus_fingerprint(conn)

    assert first[0] == second[0], "本用例的前提是 chunk 数不变"
    assert first != second, "id 平移了而指纹没变 → 快照不重建，引用会指向死 id"


def test_corpus_fingerprint_stable_when_unchanged(offline_settings):
    """没有变更时指纹必须稳定——否则每个请求都重建索引，检索直接变慢。"""
    from mikasa.models.document import Chunk, Document

    with open_db(offline_settings.db_path) as conn:
        doc_id = repo.insert_document(
            conn,
            Document(
                title="样本", file_path="样本.md", file_type="md", file_sha256="sha", char_count=1
            ),
        )
        repo.insert_chunks(
            conn, [Chunk(document_id=doc_id, seq=0, content="块", content_sha256="s0")]
        )
        assert repo.corpus_fingerprint(conn) == repo.corpus_fingerprint(conn)


def test_corpus_fingerprint_on_empty_db(offline_settings):
    """空库也要有确定的指纹（MAX(id) 为 NULL，COALESCE 兜住成 0）。"""
    with open_db(offline_settings.db_path) as conn:
        assert repo.corpus_fingerprint(conn) == (0, 0)


# ---------------------------------------------------------------------------
# 文档知识链（M6 ③，schema v5 的 doc_links）
# ---------------------------------------------------------------------------


def _two_docs(conn) -> tuple[int, int]:
    """两篇最小文档（甲、乙）——知识链的测试样例。"""
    from mikasa.models.document import Document

    a = repo.insert_document(
        conn,
        Document(title="甲", file_path="a.md", file_type="md", file_sha256="a", char_count=1),
    )
    b = repo.insert_document(
        conn,
        Document(title="乙", file_path="b.md", file_type="md", file_sha256="b", char_count=1),
    )
    return a, b


def test_replace_doc_links_replaces_llm_edges_and_keeps_manual(offline_settings):
    """重跑抽取：llm 边按**本次结果替换**，人工边原样留着（③ 的核心口径）。"""
    with open_db(offline_settings.db_path) as conn:
        a, b = _two_docs(conn)
        added = repo.replace_doc_links(
            conn,
            a,
            [
                {
                    "dst_doc_id": b,
                    "relation": "同一主题",
                    "evidence": "都在讲正则化",
                    "confidence": 0.8,
                },
                {
                    "dst_doc_id": b,
                    "relation": "方法被借鉴",
                    "evidence": "乙借用了甲的做法",
                    "confidence": 0.6,
                },
            ],
        )
        assert added == 2
        # 人工确认过的一条（源端是 a）
        conn.execute(
            """INSERT INTO doc_links (src_doc_id, dst_doc_id, relation, source)
               VALUES (?, ?, '老师指定', 'manual')""",
            (a, b),
        )
        # 重跑：这次只抽出一条、且关系不同 → llm 边被替换，manual 留着
        assert (
            repo.replace_doc_links(
                conn,
                a,
                [
                    {
                        "dst_doc_id": b,
                        "relation": "前置知识",
                        "evidence": "先读乙",
                        "confidence": 0.9,
                    }
                ],
            )
            == 1
        )
        got = {(r["relation"], r["source"]) for r in repo.links_for_document(conn, a)}
        assert got == {("前置知识", "llm"), ("老师指定", "manual")}

        # 同一 (src,dst,relation) 重复写入不会翻倍（UNIQUE + OR IGNORE 兜住）
        repo.replace_doc_links(conn, a, [{"dst_doc_id": b, "relation": "前置知识"}])
        rows = [r for r in repo.links_for_document(conn, a) if r["relation"] == "前置知识"]
        assert len(rows) == 1


def test_links_for_document_covers_both_directions_with_titles(offline_settings):
    """列表要同时给出"我指向谁"与"谁指向我"，并带另一端文档名（④ 的图也吃它）。"""
    with open_db(offline_settings.db_path) as conn:
        a, b = _two_docs(conn)
        repo.replace_doc_links(
            conn, a, [{"dst_doc_id": b, "relation": "同一主题", "confidence": 0.7}]
        )
        out = repo.links_for_document(conn, a)
        assert [(r["direction"], r["other_title"]) for r in out] == [("out", "乙")]
        back = repo.links_for_document(conn, b)
        assert [(r["direction"], r["other_title"]) for r in back] == [("in", "甲")]


def test_delete_doc_links_clears_both_directions(offline_settings):
    """删文档时连带清两个方向，否则列表里会出现指向已删文档的死链。"""
    with open_db(offline_settings.db_path) as conn:
        a, b = _two_docs(conn)
        repo.replace_doc_links(conn, a, [{"dst_doc_id": b, "relation": "同一主题"}])
        repo.replace_doc_links(conn, b, [{"dst_doc_id": a, "relation": "前置知识"}])
        repo.delete_doc_links(conn, a)
        assert repo.links_for_document(conn, b) == []
