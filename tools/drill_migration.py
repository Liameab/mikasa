"""M4.5 真实库迁移演练：data/mikasa.db schema v1 → v2（首次真实迁移）。

手工步骤（演练前须已备份，见 data/mikasa.db.bak）：
  1. 首次 open_db 应触发迁移——日志出现「schema 迁移开始：1 → 2」；
  2. 断言：版本行=2、qa_folders 空表就位、27 个会话标题 = 各自首条 user
     消息 fold_title(20)（无消息会话留 NULL）、title_manual 全 0、
     folder_id 全 NULL、qa_messages/documents/chunks 行数不变；
  3. 二次 open_db（版本已匹配）零迁移日志——幂等重入无副作用。

真实库损坏前的备份、版本/计数快照见演练时的命令留痕（ADR-0004 修订段）。
运行：.venv/Scripts/python.exe tools/drill_migration.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from mikasa.storage.db import open_db
from mikasa.utils.text import fold_title

DB = Path(__file__).resolve().parent.parent / "data" / "mikasa.db"
MIGRATION_MARK = "schema 迁移开始：1 → 2"
PREV_COUNTS = {"qa_messages": 88, "documents": 21, "chunks": 52}  # v1 备份实测值


class _BoxHandler(logging.Handler):
    """把 INFO 级日志收进列表，演练断言用（不干扰全局日志配置）。"""

    def __init__(self, box: list[str]) -> None:
        super().__init__(level=logging.INFO)
        self.box = box

    def emit(self, record: logging.LogRecord) -> None:
        self.box.append(self.format(record))


def _snapshot(conn) -> dict:
    version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    sessions = conn.execute(
        """
        SELECT s.id, s.profile, s.title, s.title_manual, s.folder_id,
               (SELECT m.content FROM qa_messages m
                 WHERE m.session_id = s.id AND m.role = 'user'
                 ORDER BY m.id LIMIT 1) AS first_q
          FROM qa_sessions s ORDER BY s.id
        """
    ).fetchall()
    counts = {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("qa_messages", "documents", "chunks", "qa_folders")
    }
    return {"version": version, "sessions": sessions, "counts": counts}


def _check(sessions, counts: dict) -> None:
    # 行数不变量：v1 备份实测 27/88/21/52，迁移只准加列不准动行
    for table, expected in PREV_COUNTS.items():
        actual = counts[table]
        assert actual == expected, f"{table} 行数变了：{actual} ≠ {expected}"
    assert counts["qa_folders"] == 0, "迁移后 qa_folders 应为空表"

    n_filled = n_null = 0
    for row in sessions:
        sid, profile, title, manual, folder_id, first_q = row
        assert manual == 0, f"session {sid} title_manual 应为 0（迁移不锁手动名）"
        assert folder_id is None, f"session {sid} folder_id 应为 NULL（v1 无文件夹归属）"
        if first_q is None:  # 无消息会话：标题必须留 NULL（未命名态）
            n_null += 1
            assert title is None, f"session {sid} 无消息却回填了标题 {title!r}"
            continue
        n_filled += 1
        expected = fold_title(first_q, 20)
        assert title == expected, (
            f"session {sid}（profile={profile}）标题 ≠ 首问截断：\n"
            f"  title   = {title!r}\n  expected= {expected!r}"
        )
    print(f"  会话 {len(sessions)} 条：{n_filled} 条回填标题、{n_null} 条无消息留 NULL")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    assert DB.exists(), f"真实库不存在：{DB}（演练对象是 data/mikasa.db，不是 .bak）"

    box: list[str] = []
    logging.getLogger("mikasa").addHandler(_BoxHandler(box))

    # ── 第一次打开：触发迁移 ──
    with open_db(DB) as conn:
        first = _snapshot(conn)
        applied_at = conn.execute(
            "SELECT applied_at FROM schema_version WHERE version = 2"
        ).fetchone()[0]
    migrated_lines = [ln for ln in box if "schema 迁移" in ln]
    assert migrated_lines, "首次打开未触发迁移，日志里没有「schema 迁移」行"
    assert MIGRATION_MARK in box, "不是从 v1 迁移（应见「schema 迁移开始：1 → 2」）"
    assert first["version"] == 2
    print("第一次 open_db（触发迁移）：")
    for ln in migrated_lines:
        print("  " + ln)
    print(f"  schema_version → {first['version']}（applied_at={applied_at}）")
    _check(first["sessions"], first["counts"])

    # ── 第二次打开：版本匹配应零迁移（幂等重入） ──
    box.clear()
    with open_db(DB) as conn:
        second = _snapshot(conn)
    second_migrated = [ln for ln in box if "schema 迁移" in ln]
    assert not second_migrated, f"二次打开不该再迁移：{second_migrated}"
    assert second["version"] == 2
    print("第二次 open_db（版本匹配）：无任何迁移日志，版本仍 = 2 ✓")

    print("\n演练通过：真实库 v1 → v2 迁移成功，行数/标题/空表全部符合预期。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
