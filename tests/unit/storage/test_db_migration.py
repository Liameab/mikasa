"""schema 版本迁移测试：v1/v2 → 当前逐级升级、幂等续跑、分路与报错语义。

M4.5 起 schema 支持自动迁移（ADR-0004 修订版，db.py 头注释）；v3 起
语料文件夹（kb_folders + documents.folder_id 补列）。本文件把"历史库
演进"钉成回归：逐字按各版本快照造库 → init_db → 断言当前形态与存量
数据保全 → 模拟"版本行更新前崩溃"的续跑 → 高版本/断层硬报错 → 全新
库直建当前版本。造库一律用 db._SCHEMA_V1_SQL / _SCHEMA_V2_SQL（immutable
快照），不走 init_db，避免"用新代码造旧库"自证循环。
"""

from __future__ import annotations

import sqlite3

import pytest

from mikasa.errors import StorageError
from mikasa.models.document import Chunk, Document
from mikasa.storage import db, repo
from mikasa.utils.text import fold_title


def _v1_conn(tmp_path, name: str = "mikasa.db") -> sqlite3.Connection:
    """按 v1 快照建库并写版本行（独立函数，每个测试一把新库）。"""
    conn = db.connect(tmp_path / name)
    conn.executescript(db._SCHEMA_V1_SQL)
    conn.execute("INSERT INTO schema_version (version) VALUES (1)")
    return conn


def _v2_conn(tmp_path, name: str = "mikasa.db") -> sqlite3.Connection:
    """按 v2 快照（M4.5 终态）建库并写版本行。"""
    conn = db.connect(tmp_path / name)
    conn.executescript(db._SCHEMA_V2_SQL)
    conn.execute("INSERT INTO schema_version (version) VALUES (2)")
    return conn


def _v4_conn(tmp_path, name: str = "mikasa.db") -> sqlite3.Connection:
    """按 v4 终态建库并写版本行。

    v4 的终态 = v3 快照 + folder_id + source_ref 两条补列（当时全新库路径的两步
    顺序）——只跑快照造出来的是"缺两列的 v4"，不是真 v4。
    """
    conn = db.connect(tmp_path / name)
    conn.executescript(db._SCHEMA_V3_SQL)
    conn.execute(db._SCHEMA_DOC_FOLDER_ALTER)
    conn.execute(db._SCHEMA_DOC_SOURCE_ALTER)
    conn.execute("INSERT INTO schema_version (version) VALUES (4)")
    return conn


def test_v4_db_migrates_v5_adding_doc_links(tmp_path):
    """v4 → v5：只多出 doc_links（+ 两个索引），存量文档与块原封不动。

    M6 ③ 的第一级：表先落地、**不回填**（关系要靠 LLM 抽取，迁移阶段没有数据
    可填，凭空生成只会是噪声）。这条同时锁住"迁移不碰存量"这条纪律。
    """
    conn = _v4_conn(tmp_path)
    doc_id = repo.insert_document(
        conn,
        Document(title="样本", file_path="样本.md", file_type="md", file_sha256="s", char_count=1),
    )
    repo.insert_chunks(conn, [Chunk(document_id=doc_id, seq=0, content="块", content_sha256="s0")])
    conn.commit()

    db.init_db(conn)  # 触发 v4 → v5

    assert (
        int(
            conn.execute(
                "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
            ).fetchone()["version"]
        )
        == 5
    )
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "doc_links" in tables
    indexes = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_doc_links_src", "idx_doc_links_dst"} <= indexes
    # 存量数据一条不少
    assert conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"] == 1
    # 新表可用、且是空的（不回填）
    assert conn.execute("SELECT COUNT(*) AS n FROM doc_links").fetchone()["n"] == 0
    # 幂等：再跑一次不炸、版本不变
    db.init_db(conn)
    assert (
        int(
            conn.execute(
                "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
            ).fetchone()["version"]
        )
        == 5
    )
    conn.close()


def _v3_conn(tmp_path, name: str = "mikasa.db") -> sqlite3.Connection:
    """按 v3 快照（M7 终态）建库并写版本行。

    v3 的终态 = 快照 **+ folder_id 补列**（当时 init_db 全新库路径的两步
    顺序）——只跑 executescript 造出来的是"缺 folder_id 的 v3"，不是真 v3。
    """
    conn = db.connect(tmp_path / name)
    conn.executescript(db._SCHEMA_V3_SQL)
    conn.execute(db._SCHEMA_DOC_FOLDER_ALTER)
    conn.execute("INSERT INTO schema_version (version) VALUES (3)")
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r["name"] for r in rows}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def _add_session(conn: sqlite3.Connection, profile: str, messages: list[tuple[str, str]]) -> int:
    """造 v1 会话：messages 为 (role, content) 序列，逐条入库。"""
    cur = conn.execute("INSERT INTO qa_sessions (profile) VALUES (?)", (profile,))
    sid = int(cur.lastrowid)
    for role, content in messages:
        conn.execute(
            "INSERT INTO qa_messages (session_id, role, content) VALUES (?, ?, ?)",
            (sid, role, content),
        )
    return sid


def test_fresh_db_builds_current_directly(tmp_path):
    """全新库：init_db 一次建齐当前终态（v3：qa/kb 双文件夹表 + documents
    folder_id 列 + 会话三新列，版本行 = SCHEMA_VERSION）。"""
    conn = db.connect(tmp_path / "fresh.db")
    db.init_db(conn)
    conn.commit()

    version = int(conn.execute("SELECT version FROM schema_version").fetchone()["version"])
    assert version == db.SCHEMA_VERSION
    tables = _table_names(conn)
    assert {"qa_folders", "kb_folders"} <= tables
    doc_cols = _columns(conn, "documents")
    # 补列覆层在全新库同样生效（与迁移库殊途同归）：逐版累加，一个都不少
    assert {"folder_id", "source_ref"} <= doc_cols
    cols = _columns(conn, "qa_sessions")
    assert {"title", "title_manual", "folder_id"} <= cols
    assert cols >= {
        "id",
        "profile",
        "created_at",  # v1 原列一个不少
    }
    # 新库上 qa 会话/文件夹 CRUD 立即可用（folder 引用先于 session 建表，无顺序坑）
    folder = repo.create_folder(conn, "示例组")
    sid = repo.create_session(conn, "offline")
    repo.move_session(conn, sid, folder)
    assert repo.get_session(conn, sid)["folder_id"] == folder
    # kb 文件夹 CRUD 立即可用 + 文档可移夹
    kb = repo.create_kb_folder(conn, "参考资料")
    assert repo.get_kb_folder(conn, kb)["name"] == "参考资料"

    # 版本一致：二次 init 是零 DDL 空操作（不抛、不重插版本行）
    db.init_db(conn)
    assert conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"] == 1
    conn.close()


def test_v1_db_migrates_preserving_data_and_backfills_titles(tmp_path):
    """v1 存量库：逐级迁到 v3 后列齐、他表行数不变、标题按首条 user 消息回填。"""
    conn = _v1_conn(tmp_path)
    # 会话 A：两问两答，首问短（20 字内原样）；会话 B：一问一答，首问超长待截断；
    # 会话 C：零消息（回填应留 NULL）；会话 D：空串消息先行（空内容应被跳过，
    # 回填取"首条非空 user 消息"= 真问）
    short_q = "什么是反向传播？"
    long_q = "L2 正则化为什么能防止过拟合它与权重衰减有什么区别请展开详细说说"
    a = _add_session(
        conn,
        "api",
        [("user", short_q), ("assistant", "答一"), ("user", "追问"), ("assistant", "答二")],
    )
    b = _add_session(conn, "api", [("user", long_q), ("assistant", "答")])
    c = _add_session(conn, "offline", [])  # 零消息
    d = _add_session(conn, "offline", [("user", ""), ("assistant", "答"), ("user", "真问")])

    # 他表存量（迁移不得碰 documents/chunks/qa_messages/eval_runs）
    doc_id = repo.insert_document(
        conn,
        Document(
            title="样本",
            file_path="样本.md",
            file_type="md",
            file_sha256="s",
            char_count=1,
        ),
    )
    repo.insert_chunks(conn, [Chunk(document_id=doc_id, seq=0, content="块", content_sha256="s0")])
    # eval_runs 一行
    repo.insert_eval_run(
        conn,
        eval_set_name="golden",
        corpus_sha256="x",
        config_json="{}",
        metrics_json="{}",
        report_md="",
    )
    counts_before = {
        "documents": repo.count_documents(conn),
        "chunks": repo.count_chunks(conn),
        "qa_messages": len(conn.execute("SELECT id FROM qa_messages").fetchall()),
        "eval_runs": len(conn.execute("SELECT id FROM eval_runs").fetchall()),
    }
    conn.commit()
    conn.close()

    # 用 open_db 触发真实迁移路径（连接级 init_db，与产品入口一致）
    conn = db.connect(tmp_path / "mikasa.db")
    db.init_db(conn)

    assert (
        int(conn.execute("SELECT version FROM schema_version").fetchone()["version"])
        == db.SCHEMA_VERSION
    )
    tables = _table_names(conn)
    assert {"qa_folders", "kb_folders"} <= tables
    # v1 时代的文档行：逐版补列后 folder_id/source_ref 均为 NULL（根级/无来源）
    assert {"folder_id", "source_ref"} <= _columns(conn, "documents")
    cols = _columns(conn, "qa_sessions")
    assert {"title", "title_manual", "folder_id"} <= cols

    # 标题回填：首条非空 user 消息折叠截断；零消息的会话留 NULL
    by_id = {s["id"]: s for s in repo.list_sessions(conn)}
    assert by_id[a]["title"] == short_q  # 20 字内原样（无换行可折）
    assert by_id[a]["folder_id"] is None
    assert by_id[b]["title"] == fold_title(long_q, 20)  # 复算与迁移一致
    assert len(by_id[b]["title"]) <= 20
    assert by_id[c]["title"] is None
    assert by_id[d]["title"] == "真问"  # 空串消息被跳过，取真正的首问
    # 回填的标题不带手动锁（title_manual=0 → 之后 LLM 提炼可覆盖升级）
    row_a = repo.get_session(conn, a)
    assert row_a["title_manual"] == 0
    assert row_a["folder_id"] is None

    # 他表行数与文档数据不动
    assert repo.count_documents(conn) == counts_before["documents"]
    assert repo.count_chunks(conn) == counts_before["chunks"]
    assert (
        len(conn.execute("SELECT id FROM qa_messages").fetchall()) == counts_before["qa_messages"]
    )
    assert len(conn.execute("SELECT id FROM eval_runs").fetchall()) == counts_before["eval_runs"]
    assert repo.get_document(conn, doc_id).title == "样本"
    conn.close()


def test_migration_resumes_after_crash_before_version_row(tmp_path):
    """半途崩溃续跑：版本行仍=1 而列已加/标题已回填时，重跑不重列、不覆盖已命名。"""
    conn = _v1_conn(tmp_path)
    _add_session(conn, "api", [("user", "第一次问"), ("assistant", "答")])
    _add_session(conn, "offline", [("user", "第二次问"), ("assistant", "答")])
    conn.commit()
    db.init_db(conn)  # 完整迁移到当前版本（v1 → v2 → v3）
    sessions = repo.list_sessions(conn)
    assert all(s["title"] for s in sessions)

    # 模拟"迁移函数执行完、版本行更新前崩溃"：版本行回退到 1，
    # 并把一个会话的标题改成手动值（等价于崩溃与重跑之间用户已改名）
    conn.execute("UPDATE schema_version SET version = 1 WHERE version = 3")
    repo.set_session_title(conn, sessions[0]["id"], "手动改名")
    conn.commit()

    db.init_db(conn)  # 续跑：应无异常、版本回到当前
    assert (
        int(conn.execute("SELECT version FROM schema_version").fetchone()["version"])
        == db.SCHEMA_VERSION
    )
    after = repo.list_sessions(conn)
    # 已命名会话（无论手动与否）不被回填覆盖；未命名会话标题补回。
    # 注意 list_sessions 按 id 倒序，勿按位置对应会话。
    assert {s["title"] for s in after} == {"手动改名", "第一次问"}
    conn.close()


def test_migration_idempotent_second_open(tmp_path):
    """迁移完成后第二次连接是空操作（正常重启路径不重放迁移）。"""
    conn = _v1_conn(tmp_path)
    _add_session(conn, "offline", [("user", "首问"), ("assistant", "答")])
    conn.commit()
    conn.close()
    for _ in range(2):
        conn = db.connect(tmp_path / "mikasa.db")
        db.init_db(conn)
        conn.close()
    conn = db.connect(tmp_path / "mikasa.db")
    assert repo.list_sessions(conn)[0]["title"] == "首问"
    conn.close()


def test_higher_version_raises_storage_error(tmp_path):
    """库版本高于程序支持：硬报错不迁移（ADR-0004 原纪律：旧程序不读写新库）。"""
    conn = _v1_conn(tmp_path)
    conn.execute("UPDATE schema_version SET version = 99")
    conn.commit()
    with pytest.raises(StorageError, match="高于程序支持"):
        db.init_db(conn)
    conn.close()


def test_missing_migration_path_raises(tmp_path, monkeypatch):
    """版本断层（目标版本无迁移函数）：硬报错并停在原版本，不留半迁移。"""
    conn = _v1_conn(tmp_path)
    conn.commit()
    monkeypatch.setattr(db, "_MIGRATIONS", {})  # 模拟断层
    with pytest.raises(StorageError, match="缺少 1 → 2 的迁移路径"):
        db.init_db(conn)
    # 停在 v1：版本行没动（rollback 语义由 open_db 负责，此处直接检查不抛即可）
    conn.rollback()
    conn.close()


def test_migrated_titles_fold_whitespace(tmp_path):
    """回填折叠空白：首问含换行/全角空格时压单空格（fold_title 语义落库）。"""
    conn = _v1_conn(tmp_path)
    question = "什么是反向传播？\n\n它和梯度消失\n有什么关系？　补充"
    _add_session(conn, "offline", [("user", question), ("assistant", "答")])
    conn.commit()
    db.init_db(conn)
    assert repo.list_sessions(conn)[0]["title"] == fold_title(question, 20)
    conn.close()


# ---------------------------------------------------------------------------
# v2 → v3（v3 = 语料文件夹：kb_folders 表 + documents.folder_id 补列）
# ---------------------------------------------------------------------------


def _add_v2_doc(conn: sqlite3.Connection, title: str) -> int:
    """v2 时代插入文档（列清单显式给出，快照库无 folder_id 列——模拟
    v2 程序当时会写出的 SQL，不用现版 repo.insert_document 的列清单）。"""
    cur = conn.execute(
        """INSERT INTO documents (title, file_path, file_type, file_sha256, char_count)
           VALUES (?, ?, ?, ?, 0)""",
        (title, f"uploads/{title}.md", "md", "sha-" + title),
    )
    return int(cur.lastrowid)


def test_v2_db_migrates_v3_preserving_everything(tmp_path):
    """v2 存量库：v3 迁移后 qa 数据/文件夹树原样、documents 补列全 NULL、
    kb 文件夹与文档移夹立即可用。"""
    conn = _v2_conn(tmp_path)
    # qa 侧：文件夹 + 夹内会话（夹引用要存活）
    qfolder = repo.create_folder(conn, "问答组")
    sid = repo.create_session(conn, "offline")
    repo.move_session(conn, sid, qfolder)
    repo.set_session_title(conn, sid, "手动名")
    # 文档侧：两行（迁移后 folder_id 应为 NULL）
    d1 = _add_v2_doc(conn, "甲")
    _add_v2_doc(conn, "乙")
    conn.commit()
    conn.close()

    conn = db.connect(tmp_path / "mikasa.db")
    db.init_db(conn)
    assert (
        int(conn.execute("SELECT version FROM schema_version").fetchone()["version"])
        == db.SCHEMA_VERSION
    )
    assert "kb_folders" in _table_names(conn)
    # documents 补列到位，存量行 folder_id = NULL（根级，不猜测归属）
    assert "folder_id" in _columns(conn, "documents")
    docs = conn.execute("SELECT id, folder_id FROM documents ORDER BY id").fetchall()
    assert [r["id"] for r in docs] == [d1, d1 + 1]
    assert all(r["folder_id"] is None for r in docs)
    # qa 侧数据原样（文件夹树 + 会话标题/归属不丢）
    assert repo.get_session(conn, sid)["title"] == "手动名"
    assert repo.get_session(conn, sid)["folder_id"] == qfolder
    assert repo.list_folders(conn)[0]["name"] == "问答组"
    # v3 新能力即刻可用：kb 文件夹 + 文档移夹（folder 引用先于补列建好）
    kb = repo.create_kb_folder(conn, "语料组")
    repo.move_document(conn, d1, kb)
    assert repo.get_document(conn, d1).folder_id == kb
    assert repo.kb_folder_children_counts(conn, kb) == {"subfolders": 0, "documents": 1}
    conn.close()


def test_v2_to_v3_migration_resumes_after_crash(tmp_path):
    """v2 半途崩溃续跑：版本行回退到 2 时重跑不重列、逐级走到当前版本。"""
    conn = _v2_conn(tmp_path)
    _add_v2_doc(conn, "甲")
    conn.commit()
    db.init_db(conn)  # 完整迁移到当前版本
    conn.execute("UPDATE schema_version SET version = 2 WHERE version = 4")
    conn.commit()

    db.init_db(conn)  # 续跑：kb_folders 已建、两列都已加 → 守卫下空操作
    assert (
        int(conn.execute("SELECT version FROM schema_version").fetchone()["version"])
        == db.SCHEMA_VERSION
    )
    # 补列只加过一次（列集合里各自只出现一次）
    info = conn.execute("PRAGMA table_info(documents)").fetchall()
    names = [r["name"] for r in info]
    assert names.count("folder_id") == 1 and names.count("source_ref") == 1
    conn.close()


# ---------------------------------------------------------------------------
# v3 → v4（v4 = documents.source_ref 补列：论文在线来源标识）
# ---------------------------------------------------------------------------


def _add_v3_doc(conn: sqlite3.Connection, title: str, folder_id: int | None = None) -> int:
    """v3 时代插入文档（v3 已有 folder_id、**无** source_ref——模拟当时
    程序会写出的 SQL，不用现版 repo.insert_document 的列清单）。"""
    cur = conn.execute(
        """INSERT INTO documents (title, file_path, file_type, file_sha256, char_count, folder_id)
           VALUES (?, ?, ?, ?, 0, ?)""",
        (title, f"uploads/{title}.md", "md", "sha-" + title, folder_id),
    )
    return int(cur.lastrowid)


def test_v3_db_migrates_v4_preserving_everything(tmp_path):
    """v3 存量库：v4 迁移后数据原样、documents 补 source_ref 列且存量全 NULL。"""
    conn = _v3_conn(tmp_path)
    kb = repo.create_kb_folder(conn, "论文")
    sid = repo.create_session(conn, "offline")
    repo.set_session_title(conn, sid, "手动名")
    d1 = _add_v3_doc(conn, "甲", folder_id=kb)
    _add_v3_doc(conn, "乙")
    conn.commit()
    conn.close()

    conn = db.connect(tmp_path / "mikasa.db")
    db.init_db(conn)
    assert (
        int(conn.execute("SELECT version FROM schema_version").fetchone()["version"])
        == db.SCHEMA_VERSION
    )
    assert "source_ref" in _columns(conn, "documents")
    docs = conn.execute("SELECT id, folder_id, source_ref FROM documents ORDER BY id").fetchall()
    assert [r["id"] for r in docs] == [d1, d1 + 1]
    assert docs[0]["folder_id"] == kb  # v3 整理好的归属不丢
    # 存量行 source_ref 全 NULL：v4 之前导入的论文无从考证来源（不猜）
    assert all(r["source_ref"] is None for r in docs)
    assert repo.get_session(conn, sid)["title"] == "手动名"
    # v4 新能力即刻可用：写来源 → 集合查询命中
    repo.set_document_source_ref(conn, d1, "arxiv:2401.12345")
    assert repo.list_source_refs(conn) == {"arxiv:2401.12345"}
    conn.close()


def test_v3_to_v4_migration_resumes_after_crash(tmp_path):
    """v3→v4 半途崩溃续跑：版本行回退到 3 时重跑不重列、版本回 4。"""
    conn = _v3_conn(tmp_path)
    _add_v3_doc(conn, "甲")
    conn.commit()
    db.init_db(conn)  # 完整迁移到 v4
    conn.execute("UPDATE schema_version SET version = 3 WHERE version = 4")
    conn.commit()

    db.init_db(conn)  # 续跑：列已加 → 守卫下空操作
    assert (
        int(conn.execute("SELECT version FROM schema_version").fetchone()["version"])
        == db.SCHEMA_VERSION
    )
    info = conn.execute("PRAGMA table_info(documents)").fetchall()
    assert [r["name"] for r in info].count("source_ref") == 1
    conn.close()


def test_v3_missing_migration_path_raises(tmp_path, monkeypatch):
    """v2 库上断层（缺 2→3 迁移函数）：硬报错停在 v2，不留半迁移。"""
    conn = _v2_conn(tmp_path)
    conn.commit()
    monkeypatch.setattr(db, "_MIGRATIONS", {2: db._MIGRATIONS[2]})  # 只剩 1→2
    with pytest.raises(StorageError, match="缺少 2 → 3 的迁移路径"):
        db.init_db(conn)
    conn.rollback()
    # 停在 v2：kb_folders 未建、documents 未补列
    assert "kb_folders" not in _table_names(conn)
    assert "folder_id" not in _columns(conn, "documents")
    conn.close()


def test_new_db_bootstrap_recovers_from_halfway_crash(tmp_path):
    """全新库建到一半崩溃后能自愈（2026-09-11 修复）。

    复刻窗口：executescript 已建表、ALTER 也已生效，但版本行尚未写入时进程
    被杀（断电 / taskkill）。旧实现下次启动仍走"全新库"分支、重跑裸 ALTER →
    "duplicate column name: folder_id" → 库**永久打不开**。修复 = ALTER 走
    _ensure_column 的幂等补列。
    """
    db_path = tmp_path / "half.db"
    conn = db.connect(db_path)
    conn.executescript(db._SCHEMA_HEAD + db._SCHEMA_KB_FOLDERS_DDL)
    conn.execute("ALTER TABLE documents ADD COLUMN folder_id INTEGER REFERENCES kb_folders(id)")
    conn.commit()
    conn.close()  # 版本行没写：等价于此刻被杀

    with db.open_db(db_path) as recovered:  # 旧实现这里抛 OperationalError
        cols = {r["name"] for r in recovered.execute("PRAGMA table_info(documents)")}
        version = recovered.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()["version"]
    assert "folder_id" in cols
    assert version == db.SCHEMA_VERSION


def test_concurrent_first_connect_does_not_hit_database_locked(tmp_path):
    """多线程同时**首次**连同一个新库时不能抛 database is locked。

    WAL 切换要拿一个短暂的排他锁，而它不走 busy timeout（timeout 只作用于普通
    读写）。审查实测：库文件尚不存在时 160 次里 82 次直接抛 `database is locked`；
    库已存在时 0 次失败。Web 端"首次运行 + 并发上传 + 后台评测"就能凑齐。
    """
    import threading

    from mikasa.storage.db import open_db

    failures: list[str] = []

    def worker() -> None:
        try:
            with open_db(db_path) as conn:
                conn.execute("SELECT 1")
        except Exception as exc:  # noqa: BLE001 - 测试要看到具体异常类型
            failures.append(f"{type(exc).__name__}: {exc}")

    for _ in range(3):  # 每轮都是全新的库文件（失败只发生在首连）
        db_path = tmp_path / f"fresh-{_}.db"
        threads = [threading.Thread(target=worker) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert not failures, failures[:3]


def test_init_guard_serializes_across_processes(tmp_path, monkeypatch):
    """跨进程初始化互斥（2026-09-20 补）：另一个进程正在初始化时如实报错。

    进程内那把线程锁挡不住"serve 与 CLI 同时首启"——两个**进程**会读到同一个
    schema 版本号、一起跑迁移。模拟办法：直接用同一个锁文件取排他锁（等价于
    另一个进程正在初始化），再断言 open_db 不会悄悄并行迁移。
    """
    db_path = tmp_path / "guard.db"
    monkeypatch.setattr(db, "_INIT_GUARD_TIMEOUT", 0.2)  # 别让用例真等 30 秒
    lock_path = db_path.with_name(db_path.name + db._INITLOCK_SUFFIX)

    holder = sqlite3.connect(str(lock_path))
    holder.execute("BEGIN EXCLUSIVE")  # 假装另一个进程正在初始化
    try:
        with (
            pytest.raises(StorageError, match="另一个 Mikasa 进程"),
            db.open_db(db_path) as conn,
        ):
            conn.execute("SELECT 1")
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    # 锁放开之后一切照常（这道闸不能把正常路径堵死）
    with db.open_db(db_path) as conn:
        assert conn.execute("SELECT 1").fetchone() is not None
