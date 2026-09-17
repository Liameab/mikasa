"""SQLite 连接与建表（含 schema 版本迁移）。

- journal_mode=WAL：读写不互斥（Web 并发 + CLI 并行无压力）；
- foreign_keys=ON：文档删除级联 chunk/embedding、会话删除级联消息
  （均 ON DELETE CASCADE）；悬空引用一律以约束错误兜底（文件夹删除
  的 409 若被绕过，FK 是最后防线）；
- schema_version 表做最小迁移（ADR-0004 修订版，见 docs/design-decisions.md）：
  **全新库**直建当前版本 SCHEMA_VERSION；**低版本库**沿 _MIGRATIONS 逐级
  自动迁移——每级显式提交、迁移函数内部幂等（半途崩溃后重跑可从已
  完成处续走）、logger 留痕；**高版本库**仍硬报错：旧程序绝不读写新库。
  历史 schema 快照（_SCHEMA_V1_SQL / _SCHEMA_V2_SQL / _SCHEMA_V3_SQL）仅供
  迁移测试造旧库，不可变——schema 终态、历史快照、迁移函数三者同一 PR 落地。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from mikasa.errors import StorageError
from mikasa.utils.text import fold_title

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 4

# 初始化串行化锁（见 open_db 的说明）
_INIT_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# schema 定义（按段拼装：HEAD/TAIL 是 v1~v3 共享的公共段，qa 段各自独立）
# ---------------------------------------------------------------------------
# 纪律：HEAD/TAIL 里的表将来若被更高版本改动，必须先把它连同当时各版本
# 差异段的样子整体固化进历史快照（_SCHEMA_V1_SQL → _SCHEMA_V2_SQL，改名
# 滚动），并配套写迁移函数——任何人不得回改已有快照本身（immutable）。
#
# v3 对 documents 的 folder 归属采用 **ALTER 覆层** 而非改写 HEAD：
#   _SCHEMA_HEAD 里的 documents 永远保持 v1/v2 原形（无 folder_id），
#   v3 补列由 _SCHEMA_DOC_FOLDER_ALTER 追加——全新库直建与旧库迁移都走
#   同一条 ALTER（新库旧库殊途同归），快照因此逐字节不变、无需整表复制。

_SCHEMA_HEAD = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    file_path TEXT NOT NULL UNIQUE,          -- uploads/ 下的入库副本路径
    file_type TEXT NOT NULL,                 -- pdf | md | txt | docx
    file_sha256 TEXT NOT NULL,               -- 增量重建判断
    char_count INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    ingest_status TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,            -- 跨文档去重
    heading_path TEXT,                       -- "1. 模型 > 1.2 注意力"
    page_number INTEGER,
    tokens TEXT NOT NULL DEFAULT '[]',       -- 预分词 JSON（BM25 直接复用）
    UNIQUE(document_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_sha256 ON chunks(content_sha256);

CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    model TEXT NOT NULL,                     -- 向量所属嵌入模型（切换模型需重嵌）
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL,                    -- float32 little-endian
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings(model);
"""

# 文件夹自引用 + 会话引用都取外键默认行为（NO ACTION）：删除被引用的
# 文件夹会被约束拒绝——应用层先做 409 非空判定，这里只做兜底，绝不静默
# 级联删除用户会话（与 documents/qa_messages 的 CASCADE 有本质区别：
# 后者的子行是父行的组成部分，而文件夹是"容器"，容器不可连坐内容）。
_SCHEMA_QA_FOLDERS_DDL = """
CREATE TABLE IF NOT EXISTS qa_folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES qa_folders(id),   -- NULL=根级文件夹
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_qa_folders_parent ON qa_folders(parent_id);
"""

_SCHEMA_QA_SESSIONS_V2 = """
CREATE TABLE IF NOT EXISTS qa_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile TEXT,
    title TEXT,                                 -- 会话标题（NULL=未命名）
    title_manual INTEGER NOT NULL DEFAULT 0,    -- 1=用户手动命名，自动提炼永不覆盖
    folder_id INTEGER REFERENCES qa_folders(id),  -- 所属文件夹（NULL=根）
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# 语料库文件夹（v3）：结构语义与 qa_folders 完全同构——自引用父级、
# 外键取默认 NO ACTION（删除被引用的文件夹会被约束拒绝，应用层先做
# 409 非空判定、这里只兜底），容器绝不级联删除文档。
_SCHEMA_KB_FOLDERS_DDL = """
CREATE TABLE IF NOT EXISTS kb_folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES kb_folders(id),   -- NULL=根级文件夹
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_kb_folders_parent ON kb_folders(parent_id);
"""

# v3 补列覆层：documents.folder_id（NULL=根级）。ALTER 没有 IF NOT EXISTS，
# 幂等由迁移函数内的 _column_names 守卫负责（全新库直建路径也走本段，
# 保证新库与迁移库的终态逐列一致）。NO ACTION：删除文件夹不级联文档。
_SCHEMA_DOC_FOLDER_ALTER = """
ALTER TABLE documents ADD COLUMN folder_id INTEGER REFERENCES kb_folders(id);
"""

# v4 补列覆层：documents.source_ref（形如 "arxiv:2401.12345" / "core:72543"；
# NULL=非导入文档）。语义 = "这篇文档的来源标识"，两个消费方：
#   - 论文导入："arxiv:<id>" / "core:<id>"，「找论文」页据此标「已在库中」
#     （精确匹配，见 ADR-0020）；
#   - 知识库页写的笔记："note:<key>"，前端据此识别"哪些行可编辑正文"
#     （见 ADR-0021）。
# 两个前缀互不冲突：论文页只做 arxiv:/core: 的精确匹配。
# **不加唯一约束**：同一篇论文内容变了（出版商换了 PDF）会重新导入成新行，
# 唯一约束会让那次导入直接失败；查询取"任一条命中"即可。
# 也不加索引：本列只在搜索页做一次 `IS NOT NULL` 全表集合查询，文档量级
# 下全表扫描是零成本，加索引反而要连带迁移复杂度。
_SCHEMA_DOC_SOURCE_ALTER = """
ALTER TABLE documents ADD COLUMN source_ref TEXT;
"""

_SCHEMA_TAIL = """
CREATE TABLE IF NOT EXISTS qa_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER REFERENCES qa_sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,                      -- user | assistant
    content TEXT NOT NULL,
    citations_json TEXT,                     -- assistant 消息的 Citation 序列化
    refused INTEGER,                         -- 0/1 拒答标记
    latency_ms_json TEXT,                    -- 延迟分段
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_qa_session ON qa_messages(session_id);

CREATE TABLE IF NOT EXISTS eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    eval_set_name TEXT NOT NULL,
    corpus_sha256 TEXT,
    config_json TEXT NOT NULL,               -- profile/模型/参数全量快照（复现依据）
    metrics_json TEXT NOT NULL,
    report_md TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# v1 历史快照：与 v2/v3 的差异在 qa 段（qa_sessions 无标题/文件夹三列、
# 无 qa_folders 表）与 documents（无 folder_id 列，v1/v2 同形——共享的
# _SCHEMA_HEAD 原样保留）。迁移测试用它逐字造旧库；immutable，改动属篡改。
_SCHEMA_V1_SQL = (
    _SCHEMA_HEAD
    + """
CREATE TABLE IF NOT EXISTS qa_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""
    + _SCHEMA_TAIL
)

# v2 历史快照（M4.5 终态）：在 v1 之上多 qa_folders 表 + 会话三新列。
# v3 冻结：与 v3 的差异只剩 documents.folder_id 列（v2 没有）。
# v2→v3 迁移测试用本快照逐字造库；immutable，改动属篡改。
_SCHEMA_V2_SQL = _SCHEMA_HEAD + _SCHEMA_QA_FOLDERS_DDL + _SCHEMA_QA_SESSIONS_V2 + _SCHEMA_TAIL

# v3 历史快照（M7 终态）：表结构与 v4 相同（v4 只在 documents 上补了一列，
# 走 ALTER 覆层、不动表定义）。**v3 的终态 = 本快照 + _SCHEMA_DOC_FOLDER_ALTER**
# ——造 v3 旧库的测试要先 executescript 本快照、再执行那条 ALTER（与当时
# init_db 的全新库路径同一顺序）。immutable，改动属篡改。
_SCHEMA_V3_SQL = (
    _SCHEMA_HEAD
    + _SCHEMA_KB_FOLDERS_DDL
    + _SCHEMA_QA_FOLDERS_DDL
    + _SCHEMA_QA_SESSIONS_V2
    + _SCHEMA_TAIL
)

# 当前终态（v4）：**只**在全新库上执行。历史库绝不跑本 SQL——IF NOT EXISTS
# 只会"跳过已存在的表"，绝不会给旧表补列（补列是 ALTER 的活，归 _MIGRATIONS
# 与 init_db 里那两条走 _ensure_column 的 ALTER 覆层）。
_SCHEMA_SQL = _SCHEMA_V3_SQL


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """幂等补列：列已存在就跳过（SQLite 的 ADD COLUMN 没有 IF NOT EXISTS）。

    全新库直建路径必须用它而不是把 ALTER 拼进 executescript——那个脚本不是
    一个事务，若 ALTER 生效后、版本行写入前进程被杀（断电/taskkill），下次
    启动版本行仍缺失 → 重跑直建 → 撞 "duplicate column name" → **库永久打不开**
    （2026-09-11 修复；迁移路径本就有等价的 _column_names 守卫）。
    """
    names = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in names:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError as exc:
            # 并发首次建库：两个连接都看到"列不存在"，都去 ALTER，慢的那个撞
            # "duplicate column name"。此时列已经在了，忽略即可——这正是本函数
            # 想要的结果（2026-09-11 并发首连测试暴露）。
            if "duplicate column name" not in str(exc):
                raise


def connect(db_path: Path) -> sqlite3.Connection:
    """打开（必要时创建）数据库连接。连接即用即关，不做进程级缓存。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    _enable_wal(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _enable_wal(conn: sqlite3.Connection, attempts: int = 5) -> None:
    """切换 WAL，撞上"数据库正忙"就退避重试。

    **为什么必须重试**：WAL 切换要拿一个短暂的排他锁，而它**不走 busy timeout**
    （timeout 只作用于普通读写）。多个线程同时对**尚不存在**的库文件首连时，
    实测 160 次里 82 次直接抛 `database is locked`；库文件已存在时 0 次失败
    （2026-09-11 审查实测）。Web 端"首次运行 + 并发上传 + 后台评测"就能凑齐。

    最终仍失败也**不抛**：默认的 rollback journal 一样能用，只是并发差些——
    为这件事让整个应用起不来，代价远大于收益。
    """
    for attempt in range(attempts):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError:
            if attempt == attempts - 1:
                logging.getLogger("mikasa.storage.db").warning(
                    "无法切换到 WAL 日志模式（%s），继续用默认模式——并发写入可能变慢",
                    conn.execute("PRAGMA journal_mode").fetchone()[0],
                )
                return
            time.sleep(0.05 * (attempt + 1))


# ---------------------------------------------------------------------------
# 版本分路：全新直建 / 一致空操作 / 低版本逐级迁移 / 高版本硬报错
# ---------------------------------------------------------------------------


def init_db(conn: sqlite3.Connection) -> None:
    """建表 + 记录 schema 版本；已初始化时按库内版本分路。

    分路前**只**建 schema_version 表：任何全量 DDL 都要等版本判定后、
    按各自路径执行（全新库跑终态 SQL；历史库只跑迁移函数）。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version INTEGER PRIMARY KEY,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    row = conn.execute(
        "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if row is None:
        # 全新库：一次性建齐当前终态并写入版本行
        conn.executescript(_SCHEMA_SQL)
        # ALTER 单独走幂等补列（理由见 _ensure_column 的 docstring）。
        # 逐版累加：新库要补齐**所有**历史版本的补列，终态才与迁移库一致。
        _ensure_column(conn, "documents", "folder_id", _SCHEMA_DOC_FOLDER_ALTER)
        _ensure_column(conn, "documents", "source_ref", _SCHEMA_DOC_SOURCE_ALTER)
        # **幂等写入**：并发首次建库时两个连接都会走到这里（都看到"没有版本行"），
        # 一个先插入，另一个撞 UNIQUE constraint failed: schema_version.version
        # ——原本是 500（2026-09-11 并发首连测试暴露）。OR IGNORE 让后来者静默成为
        # 空操作：它想写的那个版本行已经在库里了，这正是期望结果。
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
        return
    version = int(row["version"])
    if version == SCHEMA_VERSION:
        return  # 版本一致：零 DDL 空操作
    if version > SCHEMA_VERSION:
        # ADR-0004 原纪律不变：旧程序绝不读写新库
        raise StorageError(
            f"数据库 schema 版本高于程序支持：库内 {version}，程序最高 {SCHEMA_VERSION}。"
            "请升级Mikasa后重试；先用旧版程序打开会破坏数据。"
        )
    _upgrade(conn, version)


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """PRAGMA table_info 的列名集合（ALTER 幂等守卫——SQLite 没有
    ADD COLUMN IF NOT EXISTS）。

    table 只接受模块内常量（f-string 注入点），绝不接外部输入。
    """
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def _upgrade(conn: sqlite3.Connection, from_version: int) -> None:
    """沿 _MIGRATIONS 逐级迁移：每级执行迁移函数 → 更新版本行 → 提交。

    每级都提交一次：任一级失败后库停在"版本行=上一级"，重跑只重放
    未完成的那级（其内部幂等，见 _migrate_* docstring）。
    """
    while from_version < SCHEMA_VERSION:
        target = from_version + 1
        migrate = _MIGRATIONS.get(target)
        if migrate is None:
            raise StorageError(
                f"数据库 schema 缺少 {from_version} → {target} 的迁移路径，已停在 {from_version}。"
                "数据未损坏，仍可用对应旧版程序打开；请备份 data/ 后向项目反馈。"
            )
        logger.info("schema 迁移开始：%d → %d", from_version, target)
        migrate(conn)
        conn.execute(
            "UPDATE schema_version SET version = ?, applied_at = datetime('now') WHERE version = ?",
            (target, from_version),
        )
        conn.commit()
        logger.info("schema 迁移完成：%d → %d", from_version, target)
        from_version = target


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """v1 → v2：会话管理升级（文件夹树 + 标题列）。

    三步各自幂等——任一步半途崩溃后重跑可从已完成处续走：
      1) 建 qa_folders 表（v1 库没有；IF NOT EXISTS 天然幂等）；
      2) qa_sessions 补 title / title_manual / folder_id 三列（ALTER 没有
         IF NOT EXISTS，用 _column_names 守卫，只补缺的列）；
      3) 标题回填：既有会话取各自**首条 user 消息**折叠截断（fold_title
         20 字），无消息的会话留 NULL（NULL=未命名，之后问答流程自动补名）。
    DDL 在 SQLite 各自即时生效，回填 UPDATE 到函数末尾统一提交——崩溃
    丢的只是未提交回填，重跑时 title IS NULL 条件让已填会话自然跳过。
    """
    conn.executescript(_SCHEMA_QA_FOLDERS_DDL)
    cols = _column_names(conn, "qa_sessions")
    if "title" not in cols:
        conn.execute("ALTER TABLE qa_sessions ADD COLUMN title TEXT")
    if "title_manual" not in cols:
        conn.execute("ALTER TABLE qa_sessions ADD COLUMN title_manual INTEGER NOT NULL DEFAULT 0")
    if "folder_id" not in cols:
        conn.execute(
            "ALTER TABLE qa_sessions ADD COLUMN folder_id INTEGER REFERENCES qa_folders(id)"
        )
    conn.commit()

    rows = conn.execute(
        """SELECT s.id FROM qa_sessions s
             WHERE s.title IS NULL AND EXISTS (
                 SELECT 1 FROM qa_messages m
                  WHERE m.session_id = s.id AND m.role = 'user'
                    AND m.content IS NOT NULL AND trim(m.content) <> ''
             )
             ORDER BY s.id"""
    ).fetchall()
    for r in rows:
        first = conn.execute(
            """SELECT content FROM qa_messages
                 WHERE session_id = ? AND role = 'user'
                   AND content IS NOT NULL AND trim(content) <> ''
                 ORDER BY id LIMIT 1""",
            (r["id"],),
        ).fetchone()
        assert first is not None  # EXISTS 守卫保证命中
        conn.execute(
            "UPDATE qa_sessions SET title = ? WHERE id = ?",
            (fold_title(first["content"], 20), r["id"]),
        )
    conn.commit()


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """v2 → v3：语料文档文件夹（kb_folders 表 + documents.folder_id 列）。

    与 v1→v2 的文件夹先例同构，两步各自幂等：
      1) 建 kb_folders 表（IF NOT EXISTS 天然幂等）；
      2) documents 补 folder_id 列（ALTER 没有 IF NOT EXISTS，用
         _column_names 守卫，只在缺列时补——崩溃重跑不重列）。
    不需要数据回填：既有文档的 folder_id 一律 NULL = 根级（文件夹归属
    只由用户主动整理产生，不猜测）。DDL 在 SQLite 各自即时生效，末尾
    统一提交——崩溃丢的只是未提交尾巴，重跑空操作安全。
    """
    conn.executescript(_SCHEMA_KB_FOLDERS_DDL)
    if "folder_id" not in _column_names(conn, "documents"):
        conn.execute(_SCHEMA_DOC_FOLDER_ALTER)
    conn.commit()


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """v3 → v4：documents.source_ref（在线来源标识，供「找论文」页标"已在库中"）。

    单步、幂等：ALTER 没有 IF NOT EXISTS，用 _column_names 守卫只补缺列
    （崩溃重跑不重列）。**不需要数据回填**：v4 之前导入的文档没有来源信息
    ——当时的实现把 source+id 拼进文件名后就丢掉了，无从考证，一律 NULL。
    代价是历史导入的论文在「找论文」页不会标"已在库中"（重新导入会被
    sha256 去重挡下并提示已存在，不会产生重复文档）——如实接受，不猜。
    """
    if "source_ref" not in _column_names(conn, "documents"):
        conn.execute(_SCHEMA_DOC_SOURCE_ALTER)
    conn.commit()


# 迁移表：{目标版本: 迁移函数}。版本断层（缺 key）= 硬报错，不留半迁移状态。
# 定义在迁移函数之后（模块级 dict 求值时函数须已定义）。
_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _migrate_v1_to_v2,
    3: _migrate_v2_to_v3,
    4: _migrate_v3_to_v4,
}


@contextmanager
def open_db(db_path: Path) -> Iterator[sqlite3.Connection]:
    """open + init 一步到位（库不存在即创建），退出时提交并关闭连接。

    注意：原生 sqlite3.Connection 的上下文管理器只做 commit/rollback、
    并不 close——Windows 上不显式关闭会锁住 .db 文件（删除/临时目录清理失败），
    Web 长进程还会持续泄漏 fd。因此统一用本上下文管理器收口连接生命周期。
    """
    conn = connect(db_path)
    try:
        # 进程内串行化初始化：并发首请求时多个线程会同时走到"没有版本行"这个
        # 判断上，然后一起建表、一起插版本行。SQL 层已各自幂等（IF NOT EXISTS /
        # OR IGNORE / duplicate column 容错），这把锁只是把无谓的相互撞车省掉。
        # **只覆盖本进程**——跨进程（serve + CLI）靠上面那层幂等 SQL 兜底。
        with _INIT_LOCK:
            init_db(conn)
        yield conn
        conn.commit()  # 调用方已 commit 则此处为空操作（幂等）
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
