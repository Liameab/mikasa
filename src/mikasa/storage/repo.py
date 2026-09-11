"""数据访问层：documents / chunks / embeddings / qa / eval_runs 的 CRUD。

全部函数首参为 sqlite3.Connection：事务边界由调用方（ingest / pipeline）控制，
存储层不持有长连接（Web 多线程安全的前提）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable

import numpy as np

from mikasa.models.document import Chunk, Document

# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------


def _new_id(cur: sqlite3.Cursor) -> int:
    """INSERT 成功后取自增主键。typeshed 将 lastrowid 标为 int|None，实际恒非空。"""
    rowid = cur.lastrowid
    assert rowid is not None
    return rowid


def insert_document(conn: sqlite3.Connection, doc: Document) -> int:
    cur = conn.execute(
        """INSERT INTO documents (title, file_path, file_type, file_sha256, char_count)
           VALUES (?, ?, ?, ?, ?)""",
        (doc.title, doc.file_path, doc.file_type, doc.file_sha256, doc.char_count),
    )
    return _new_id(cur)


def update_document_after_ingest(
    conn: sqlite3.Connection,
    doc_id: int,
    *,
    ingest_status: str,
    chunk_count: int | None = None,
    error_message: str | None = None,
) -> None:
    conn.execute(
        """UPDATE documents SET ingest_status = ?,
                chunk_count = COALESCE(?, chunk_count),
                error_message = ?, updated_at = datetime('now')
           WHERE id = ?""",
        (ingest_status, chunk_count, error_message, doc_id),
    )


def list_documents(conn: sqlite3.Connection) -> list[Document]:
    rows = conn.execute("SELECT * FROM documents ORDER BY created_at DESC, id DESC").fetchall()
    return [_row_to_document(r) for r in rows]


def get_document(conn: sqlite3.Connection, doc_id: int) -> Document | None:
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    return _row_to_document(row) if row else None


def get_document_by_path(conn: sqlite3.Connection, file_path: str) -> Document | None:
    row = conn.execute("SELECT * FROM documents WHERE file_path = ?", (file_path,)).fetchone()
    return _row_to_document(row) if row else None


def get_document_by_sha(conn: sqlite3.Connection, file_sha256: str) -> Document | None:
    """按内容哈希查找（增量导入的幂等判定：同内容视为同文档）。"""
    row = conn.execute(
        "SELECT * FROM documents WHERE file_sha256 = ? ORDER BY id DESC LIMIT 1",
        (file_sha256,),
    ).fetchone()
    return _row_to_document(row) if row else None


def delete_document(conn: sqlite3.Connection, doc_id: int) -> None:
    # chunk/embedding 由外键 ON DELETE CASCADE 清理
    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))


def count_documents(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"])


def _row_to_document(row: sqlite3.Row) -> Document:
    return Document(
        id=row["id"],
        title=row["title"],
        file_path=row["file_path"],
        file_type=row["file_type"],
        file_sha256=row["file_sha256"],
        char_count=row["char_count"],
        chunk_count=row["chunk_count"],
        ingest_status=row["ingest_status"],
        error_message=row["error_message"],
        folder_id=row["folder_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# documents 组织属性（v3：文件夹归属 / 改名）
# ---------------------------------------------------------------------------
# folder_id 一律走 UPDATE 写入（见 models/document.py 的 folder_id 注释）：
# insert_document 的 SQL 不含此列，历史 schema（v1/v2）上插行不崩。
# 文件夹存在性由 API 层预检，本层只写库；外键 NO ACTION 兜底（容器不
# 级联文档，与 qa_folders → qa_sessions 先例一致）。


def move_document(conn: sqlite3.Connection, doc_id: int, folder_id: int | None) -> None:
    """文档移入文件夹（folder_id=None = 移回根；存在性由 API 层预检）。"""
    conn.execute("UPDATE documents SET folder_id = ? WHERE id = ?", (folder_id, doc_id))


def set_document_title(conn: sqlite3.Connection, doc_id: int, title: str) -> None:
    """文档改名（显示层：只改 documents.title，upload 副本文件名不动）。"""
    conn.execute("UPDATE documents SET title = ? WHERE id = ?", (title, doc_id))


# ---------------------------------------------------------------------------
# chunks
# ---------------------------------------------------------------------------


def insert_chunks(conn: sqlite3.Connection, chunks: Iterable[Chunk]) -> None:
    """批量插入（调用方保证 document_id 一致；事务由调用方包裹）。"""
    conn.executemany(
        """INSERT INTO chunks
               (document_id, seq, content, content_sha256, heading_path, page_number, tokens)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                c.document_id,
                c.seq,
                c.content,
                c.content_sha256,
                c.heading_path,
                c.page_number,
                json.dumps(c.tokens, ensure_ascii=False),
            )
            for c in chunks
        ],
    )


def chunks_by_document(conn: sqlite3.Connection, doc_id: int) -> list[Chunk]:
    rows = conn.execute(
        "SELECT * FROM chunks WHERE document_id = ? ORDER BY seq", (doc_id,)
    ).fetchall()
    return [_row_to_chunk(r) for r in rows]


def chunk_by_id(conn: sqlite3.Connection, chunk_id: int) -> Chunk | None:
    row = conn.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
    return _row_to_chunk(row) if row else None


def all_chunks_ordered(conn: sqlite3.Connection) -> list[Chunk]:
    """全量 chunk（按 id 升序）：与向量矩阵的行序严格一致。"""
    rows = conn.execute("SELECT * FROM chunks ORDER BY id").fetchall()
    return [_row_to_chunk(r) for r in rows]


def chunk_ids_of_document(conn: sqlite3.Connection, doc_id: int) -> list[int]:
    """文档内 chunk 主键（按 seq 升序，与插入顺序一致）。"""
    rows = conn.execute(
        "SELECT id FROM chunks WHERE document_id = ? ORDER BY seq", (doc_id,)
    ).fetchall()
    return [int(r["id"]) for r in rows]


def document_title_map(conn: sqlite3.Connection) -> dict[int, str]:
    """document_id -> title（引用展示用；文档数远小于 chunk 数）。"""
    rows = conn.execute("SELECT id, title FROM documents").fetchall()
    return {int(r["id"]): r["title"] for r in rows}


def corpus_digest(conn: sqlite3.Connection) -> str:
    """语料指纹：按 chunk_id 升序对 (id, content_sha256) 对取哈希。

    与 Corpus.__post_init__ 的指纹算法逐字节一致（评测防错配）。
    混入 id 的原因：chunk 表 AUTOINCREMENT 在重建（--reindex）后 id 会
    整体平移不复用。只哈希内容序列时"内容没变但 id 全变"的库指纹不变，
    而黄金集的 gold_chunk_ids 已全部错位——静默错配。混入 id 后任何
    重建/增删都会触发指纹失配，强制重跑 tools/build_golden.py。
    """
    digest = hashlib.sha256()
    for row in conn.execute("SELECT id, content_sha256 FROM chunks ORDER BY id"):
        digest.update(f"{row['id']}:".encode())
        digest.update(row["content_sha256"].encode("utf-8"))
    return digest.hexdigest()


def count_chunks(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"])


def corpus_fingerprint(conn: sqlite3.Connection) -> tuple[int, int]:
    """语料指纹：(chunk 数, 最大 chunk id)——给检索快照判断"要不要重建"。

    **为什么不能只比数量**：`mikasa ingest --reindex` 会整表重建，chunk 总数
    可能**一个不差**而 id 全部平移（AUTOINCREMENT 只增不减）。Web 进程的内存
    快照若按数量判等，就会以为"库没变"而继续用旧快照——回答里给出的
    `citation.chunk_id` 在库里已不存在（`/api/chunks/{id}` 404、高亮跳转全废），
    引用标题也退化成"未知文档"（旧 chunk 的 document_id 已失效），要重启服务
    才恢复。带上 max(id) 即可识别这种整体平移（2026-09-11 跨进程审查复现）。
    """
    row = conn.execute("SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS m FROM chunks").fetchone()
    return int(row["n"]), int(row["m"])


def _row_to_chunk(row: sqlite3.Row) -> Chunk:
    tokens: list[str] = json.loads(row["tokens"]) if row["tokens"] else []
    return Chunk(
        id=row["id"],
        document_id=row["document_id"],
        seq=row["seq"],
        content=row["content"],
        content_sha256=row["content_sha256"],
        heading_path=row["heading_path"],
        page_number=row["page_number"],
        tokens=tokens,
    )


# ---------------------------------------------------------------------------
# embeddings（向量 BLOB 与 numpy 矩阵互转）
# ---------------------------------------------------------------------------


def save_embeddings(
    conn: sqlite3.Connection,
    model: str,
    chunk_ids: list[int],
    matrix: np.ndarray,
) -> None:
    """整库覆盖式保存（先清该模型旧向量再写入，事务外负责）。"""
    conn.executemany(
        "INSERT OR REPLACE INTO embeddings (chunk_id, model, dim, vector) VALUES (?, ?, ?, ?)",
        [
            (cid, model, int(matrix.shape[1]), matrix[i].tobytes())
            for i, cid in enumerate(chunk_ids)
        ],
    )


def clear_embeddings(conn: sqlite3.Connection, model: str) -> None:
    conn.execute("DELETE FROM embeddings WHERE model = ?", (model,))


def load_embedding_matrix(
    conn: sqlite3.Connection, model: str
) -> tuple[list[int], np.ndarray] | None:
    """按 chunk_id 升序读取某模型的向量矩阵；该模型未嵌入返回 None。

    返回 (chunk_ids, matrix)：与 all_chunks_ordered 的 id 序对齐，
    上层用 chunk_id → 行号映射做余弦检索。
    """
    rows = conn.execute(
        "SELECT chunk_id, dim, vector FROM embeddings WHERE model = ? ORDER BY chunk_id",
        (model,),
    ).fetchall()
    if not rows:
        return None
    dim = rows[0]["dim"]
    chunk_ids: list[int] = []
    blobs: list[bytes] = []
    for r in rows:
        chunk_ids.append(r["chunk_id"])
        blobs.append(bytes(r["vector"]))
    matrix = np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(len(rows), dim)
    return chunk_ids, matrix


def embedding_models_in_db(conn: sqlite3.Connection) -> dict[str, int]:
    """库内各嵌入模型的向量行数（model → 行数）。

    embeddings 表 chunk_id 主键 → 正常库应只有当前模型一个键；多键 =
    混嵌残留（切 embedding 模型后没重嵌干净）。doctor 索引一致性据此
    区分"整库错模型"与"部分迁移"两种形态（见 ADR-0014 维度迁移纪律）。
    """
    rows = conn.execute("SELECT model, COUNT(*) AS n FROM embeddings GROUP BY model").fetchall()
    return {r["model"]: int(r["n"]) for r in rows}


# ---------------------------------------------------------------------------
# qa 会话/消息
# ---------------------------------------------------------------------------


def create_session(conn: sqlite3.Connection, profile: str) -> int:
    cur = conn.execute("INSERT INTO qa_sessions (profile) VALUES (?)", (profile,))
    return _new_id(cur)


def list_sessions(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """会话列表（按创建倒序，附消息数与标题/文件夹——Web 会话树的数据源）。

    title/folder_id 对同一 session_id 恒为同组行取值（按主键分组），
    SQLite 宽松分组语义下与显式 GROUP BY 结果一致。
    """
    rows = conn.execute(
        """SELECT s.id, s.profile, s.created_at, s.title, s.folder_id,
                  COUNT(m.id) AS message_count
             FROM qa_sessions s LEFT JOIN qa_messages m ON m.session_id = s.id
            GROUP BY s.id ORDER BY s.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_session(conn: sqlite3.Connection, session_id: int) -> dict | None:
    """单会话（续问端点校验存在性，不存在返回 None → Web 404）。"""
    row = conn.execute("SELECT * FROM qa_sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def insert_qa_message(
    conn: sqlite3.Connection,
    *,
    session_id: int | None,
    role: str,
    content: str,
    citations_json: str | None = None,
    refused: bool | None = None,
    latency_ms_json: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> None:
    conn.execute(
        """INSERT INTO qa_messages
               (session_id, role, content, citations_json, refused,
                latency_ms_json, prompt_tokens, completion_tokens)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            session_id,
            role,
            content,
            citations_json,
            1 if refused else None if refused is None else 0,
            latency_ms_json,
            prompt_tokens,
            completion_tokens,
        ),
    )


def messages_by_session(conn: sqlite3.Connection, session_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM qa_messages WHERE session_id = ? ORDER BY id", (session_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def first_user_message(conn: sqlite3.Connection, session_id: int) -> str | None:
    """会话首条非空 user 消息原文——自动标题与 LLM 提炼的统一事实源。

    空白内容（fold_title 折叠后为空）视同不存在；返回 None = 会话还没有
    任何可提炼的提问（调用方据此决定不命名/报 400）。
    """
    row = conn.execute(
        """SELECT content FROM qa_messages
             WHERE session_id = ? AND role = 'user'
               AND content IS NOT NULL AND trim(content) <> ''
             ORDER BY id LIMIT 1""",
        (session_id,),
    ).fetchone()
    return str(row["content"]) if row else None


# ---- qa 会话管理（M4.5）：标题原子规则 + 文件夹树 ----------------
#
# 标题三条路径与锁语义（title_manual）：
#   - 自动截断补名（问答流程每轮调用）→ auto_title_if_untitled：
#     仅 title_manual=0 AND title IS NULL 时写入 → 首轮命名后不随轮次漂移；
#     手动清除后 title 回 NULL，下轮自动重新补名（闭环）；
#   - LLM 提炼落库（suggest_title）→ apply_suggested_title：
#     title_manual=0 即可写入 → 可覆盖先前的截断兜底标题（升级语义）；
#   - 手动命名/清除（Web PATCH）→ set_session_title：非空即锁 manual=1，
#     自动路径（上两者）从此一律让路；None/空串=清除并解锁。
# 文件夹删除的非空 409、移动防环 404/409 判定在 API 层做（本层只提供
# 查询工具 folder_descendant_ids / folder_children_counts）。


def set_session_title(conn: sqlite3.Connection, session_id: int, title: str | None) -> None:
    """手动命名/清除（Web PATCH）。非空 → 落库并锁 title_manual=1（自动
    提炼永不覆盖手动命名）；None/空串 → 标题清 NULL 并解锁（回到未命名，
    问答流程下轮自动重新补名）。"""
    if title and title.strip():
        conn.execute(
            "UPDATE qa_sessions SET title = ?, title_manual = 1 WHERE id = ?",
            (title.strip(), session_id),
        )
    else:
        conn.execute(
            "UPDATE qa_sessions SET title = NULL, title_manual = 0 WHERE id = ?",
            (session_id,),
        )


def auto_title_if_untitled(conn: sqlite3.Connection, session_id: int, title: str) -> bool:
    """自动截断标题的原子落库（问答流程在首条消息入库后调用）。

    WHERE 条件（title_manual=0 AND title IS NULL）即锁语义本身，返回
    是否真的写入：无标题会话补名后不再随新轮次刷新；手动改名后的会话
    不再覆盖；手动清除后下轮重新补名。
    """
    cur = conn.execute(
        """UPDATE qa_sessions SET title = ?
             WHERE id = ? AND title_manual = 0 AND title IS NULL""",
        (title, session_id),
    )
    return cur.rowcount > 0


def apply_suggested_title(conn: sqlite3.Connection, session_id: int, title: str) -> bool:
    """LLM 提炼标题的原子落库（suggest_title 服务调用）。

    与 auto_title_if_untitled 的差别：不要求 title IS NULL——截断兜底先落
    库后，LLM 提炼成功需要"升级覆盖"它；title_manual=0 守卫保证手动
    命名（=1）永不被自动覆盖。返回是否真的写入。
    """
    cur = conn.execute(
        """UPDATE qa_sessions SET title = ?
             WHERE id = ? AND title_manual = 0""",
        (title, session_id),
    )
    return cur.rowcount > 0


def move_session(conn: sqlite3.Connection, session_id: int, folder_id: int | None) -> None:
    """会话移入文件夹（folder_id=None = 移回根）。文件夹存在性由 API 层预检。"""
    conn.execute("UPDATE qa_sessions SET folder_id = ? WHERE id = ?", (folder_id, session_id))


def delete_session(conn: sqlite3.Connection, session_id: int) -> None:
    # 消息由外键 ON DELETE CASCADE 清理（同 documents → chunks 先例）
    conn.execute("DELETE FROM qa_sessions WHERE id = ?", (session_id,))


def create_folder(conn: sqlite3.Connection, name: str, parent_id: int | None = None) -> int:
    """新建文件夹（parent_id=None 为根级）。"""
    cur = conn.execute("INSERT INTO qa_folders (name, parent_id) VALUES (?, ?)", (name, parent_id))
    return _new_id(cur)


def list_folders(conn: sqlite3.Connection) -> list[dict]:
    """全部文件夹（id 升序，平铺）——嵌套结构由前端按 parent_id 组树。"""
    rows = conn.execute("SELECT * FROM qa_folders ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def get_folder(conn: sqlite3.Connection, folder_id: int) -> dict | None:
    """单文件夹（PATCH/DELETE 的 404 判定输入）。"""
    row = conn.execute("SELECT * FROM qa_folders WHERE id = ?", (folder_id,)).fetchone()
    return dict(row) if row else None


def rename_folder(conn: sqlite3.Connection, folder_id: int, name: str) -> None:
    """文件夹改名（名字非空由 API schema 校验，本层只做写入）。"""
    conn.execute("UPDATE qa_folders SET name = ? WHERE id = ?", (name, folder_id))


def move_folder(conn: sqlite3.Connection, folder_id: int, parent_id: int | None) -> None:
    """文件夹移动到新父级（None=移回根）。防环判据在 API 层（用
    folder_descendant_ids 判定 409），本层只写库——漏判成环时递归
    查询会触达 SQLite 递归上限报错兜底，不会无限递归。"""
    conn.execute("UPDATE qa_folders SET parent_id = ? WHERE id = ?", (parent_id, folder_id))


def delete_folder(conn: sqlite3.Connection, folder_id: int) -> None:
    """删除**空**文件夹（含子文件夹或会话时由 API 409 挡在前面；万一绕过，
    外键约束以 IntegrityError 兜底——文件夹是容器，绝不级联连坐内容）。"""
    conn.execute("DELETE FROM qa_folders WHERE id = ?", (folder_id,))


def folder_descendant_ids(conn: sqlite3.Connection, folder_id: int) -> list[int]:
    """文件夹自身 + 全部后代的 id 列表（WITH RECURSIVE 树遍历，id 升序）。

    含自身是有意为之：防环判据"新 parent 不得是自身或任何后代"一条
    `parent_id in folder_descendant_ids(...)` 即完成；前端"移动菜单里
    自身+后代灰显"也复用本函数。

    **必须用 UNION 而不是 UNION ALL**：防环校验（读 parent 链 → 判断 → 写回）
    不是原子的，两个并发移动请求互相认父时两边都能通过校验，库里就真的留下
    一个环。`UNION ALL` 不去重，遇到环会**无限递归**——SQLite 没有递归深度上限，
    该请求线程与连接会永久卡死并持续吃内存（2026-09-11 审查发现）。`UNION`
    去重后环会在第二圈收敛，最坏结果是遍历早停，不是挂死。
    """
    rows = conn.execute(
        """WITH RECURSIVE subtree(id) AS (
               SELECT id FROM qa_folders WHERE id = ?
               UNION  -- 不是 UNION ALL：去重才能防环（见下）
               SELECT f.id FROM qa_folders f JOIN subtree s ON f.parent_id = s.id
           )
           SELECT id FROM subtree ORDER BY id""",
        (folder_id,),
    ).fetchall()
    return [int(r["id"]) for r in rows]


def folder_children_counts(conn: sqlite3.Connection, folder_id: int) -> dict[str, int]:
    """文件夹的直接子项计数（删除时的 409 文案素材：子文件夹数 + 会话数）。

    只数直接子项：子孙文件夹的会话归那棵子树自己的删除流程管。
    """
    subfolders = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM qa_folders WHERE parent_id = ?", (folder_id,)
        ).fetchone()["n"]
    )
    sessions = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM qa_sessions WHERE folder_id = ?", (folder_id,)
        ).fetchone()["n"]
    )
    return {"subfolders": subfolders, "sessions": sessions}


# ---------------------------------------------------------------------------
# kb 文件夹（v3：语料文档文件夹树——qa 文件夹套的镜像）
# ---------------------------------------------------------------------------
# 语义与 qa_folders 完全同构：自引用树、NO ACTION 外键兜底、删除非空
# 由 API 层 409 挡、防环判据 folder_descendant_ids、计数文案换 documents。
# 刻意镜像平铺而非泛化一个"文件夹"实现：两棵树表名/计数口径不同，
# 显式重复让每处语义一目了然（同 repo 文件的 qa 套惯例）。


def create_kb_folder(conn: sqlite3.Connection, name: str, parent_id: int | None = None) -> int:
    """新建语料文件夹（parent_id=None 为根级）。"""
    cur = conn.execute("INSERT INTO kb_folders (name, parent_id) VALUES (?, ?)", (name, parent_id))
    return _new_id(cur)


def list_kb_folders(conn: sqlite3.Connection) -> list[dict]:
    """全部语料文件夹（id 升序平铺）——嵌套结构由前端按 parent_id 组树。"""
    rows = conn.execute("SELECT * FROM kb_folders ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def get_kb_folder(conn: sqlite3.Connection, folder_id: int) -> dict | None:
    """单文件夹（PATCH/DELETE 的 404 判定输入）。"""
    row = conn.execute("SELECT * FROM kb_folders WHERE id = ?", (folder_id,)).fetchone()
    return dict(row) if row else None


def rename_kb_folder(conn: sqlite3.Connection, folder_id: int, name: str) -> None:
    conn.execute("UPDATE kb_folders SET name = ? WHERE id = ?", (name, folder_id))


def move_kb_folder(conn: sqlite3.Connection, folder_id: int, parent_id: int | None) -> None:
    """移动到新父级（None=移回根）。防环判据在 API 层，本层只写库。"""
    conn.execute("UPDATE kb_folders SET parent_id = ? WHERE id = ?", (parent_id, folder_id))


def delete_kb_folder(conn: sqlite3.Connection, folder_id: int) -> None:
    """删除**空**文件夹（含子文件夹或文档时由 API 409 挡在前面；万一绕过，
    外键约束以 IntegrityError 兜底——文件夹是容器，绝不级联删除文档）。"""
    conn.execute("DELETE FROM kb_folders WHERE id = ?", (folder_id,))


def kb_folder_descendant_ids(conn: sqlite3.Connection, folder_id: int) -> list[int]:
    """文件夹自身 + 全部后代的 id 列表（WITH RECURSIVE，同 qa 套语义）。

    UNION（非 UNION ALL）的理由见 folder_descendant_ids：去重是防环的最后一道保险。
    """
    rows = conn.execute(
        """WITH RECURSIVE subtree(id) AS (
               SELECT id FROM kb_folders WHERE id = ?
               UNION  -- 不是 UNION ALL：去重才能防环（见下）
               SELECT f.id FROM kb_folders f JOIN subtree s ON f.parent_id = s.id
           )
           SELECT id FROM subtree ORDER BY id""",
        (folder_id,),
    ).fetchall()
    return [int(r["id"]) for r in rows]


def kb_folder_children_counts(conn: sqlite3.Connection, folder_id: int) -> dict[str, int]:
    """文件夹的直接子项计数（删除时的 409 文案素材：子文件夹数 + 文档数）。

    只数直接子项：子孙文件夹的文档归那棵子树自己的删除流程管。
    """
    subfolders = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM kb_folders WHERE parent_id = ?", (folder_id,)
        ).fetchone()["n"]
    )
    documents = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM documents WHERE folder_id = ?", (folder_id,)
        ).fetchone()["n"]
    )
    return {"subfolders": subfolders, "documents": documents}


# ---------------------------------------------------------------------------
# eval_runs
# ---------------------------------------------------------------------------


def insert_eval_run(
    conn: sqlite3.Connection,
    *,
    eval_set_name: str,
    corpus_sha256: str | None,
    config_json: str,
    metrics_json: str,
    report_md: str,
) -> int:
    cur = conn.execute(
        """INSERT INTO eval_runs
               (eval_set_name, corpus_sha256, config_json, metrics_json, report_md)
           VALUES (?, ?, ?, ?, ?)""",
        (eval_set_name, corpus_sha256, config_json, metrics_json, report_md),
    )
    return _new_id(cur)


def list_eval_runs(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute("SELECT * FROM eval_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_eval_run(conn: sqlite3.Connection, run_id: int) -> dict | None:
    """单次评测记录（Web 评测报告页用，含 report_md）。"""
    row = conn.execute("SELECT * FROM eval_runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def update_eval_run_report(conn: sqlite3.Connection, run_id: int, report_md: str) -> None:
    """评测完成后回填 report_md（先插占位行、跑完再写，Web 轮询可区分状态）。"""
    conn.execute("UPDATE eval_runs SET report_md = ? WHERE id = ?", (report_md, run_id))
