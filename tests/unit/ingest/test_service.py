"""入库服务单测：幂等 / 同名替换 / 失败回滚 / reindex。

这些语义是"工程感"的直接证据：重复导入零副作用、
改了笔记再导入 = 原地更新、嵌入失败不残留半成品。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mikasa.errors import ProviderError, ZhiwenError
from mikasa.ingest.service import IngestService
from mikasa.storage import repo
from mikasa.storage.db import open_db

CONTENT_A = """# 机器学习笔记

## 注意力机制

缩放点积注意力除以根号 dk 是为了防止点积随维度增大而方差过大。

## 优化器

Adam 用一阶矩与二阶矩的估计自适应调整学习率。
"""


def _write_note(dir_: Path, name: str, content: str) -> Path:
    path = dir_ / name
    path.write_text(content, encoding="utf-8")
    return path


def _svc(offline_settings):
    svc = IngestService(offline_settings)
    offline_settings.ensure_dirs()
    return svc


# ---------------------------------------------------------------------------
# 幂等与增量
# ---------------------------------------------------------------------------


def test_ingest_counts_and_idempotent_skip(tmp_path, offline_settings):
    note = _write_note(tmp_path, "a.md", CONTENT_A)
    svc = _svc(offline_settings)

    first = svc.ingest_paths([tmp_path])
    assert first.ingested == [str(note)] and first.failed == []
    assert first.chunks_added == 2 and first.chars_added > 0

    again = svc.ingest_paths([tmp_path])
    assert again.ingested == []
    assert again.skipped == [str(note)]
    assert again.chunks_added == 0

    with open_db(offline_settings.db_path) as conn:
        assert repo.count_documents(conn) == 1
        assert repo.count_chunks(conn) == 2


def test_force_reingests(tmp_path, offline_settings):
    note = _write_note(tmp_path, "a.md", CONTENT_A)
    svc = _svc(offline_settings)
    svc.ingest_paths([tmp_path])
    forced = svc.ingest_paths([tmp_path], force=True)
    assert forced.ingested == [str(note)]  # force 重灌
    with open_db(offline_settings.db_path) as conn:
        assert repo.count_documents(conn) == 1  # 同名替换而非新增
        assert repo.count_chunks(conn) == 2


def test_same_name_content_changed_replaces_in_place(tmp_path, offline_settings):
    note = _write_note(tmp_path, "a.md", CONTENT_A)
    svc = _svc(offline_settings)
    svc.ingest_paths([tmp_path])

    # 改内容：加一段 → 再导入应更新而非报重
    changed = CONTENT_A + "\n## 损失函数\n\n交叉熵来自极大似然估计。\n"
    note.write_text(changed, encoding="utf-8")
    result = svc.ingest_paths([tmp_path])
    assert result.ingested == [str(note)]
    with open_db(offline_settings.db_path) as conn:
        docs = repo.list_documents(conn)
        assert len(docs) == 1  # 行数不变
        assert docs[0].chunk_count == 3  # 块数反映新内容


def test_duplicate_content_different_names_skipped_once(tmp_path, offline_settings):
    """字节级去重：异名同内容的备份/副本只入库一份，不污染语料（同名才算更新）。"""
    _write_note(tmp_path, "b1.md", CONTENT_A)
    _write_note(tmp_path, "b2.md", CONTENT_A)
    svc = _svc(offline_settings)
    result = svc.ingest_paths([tmp_path])
    assert len(result.ingested) == 1
    assert result.skipped == [str(tmp_path / "b2.md")]  # 内容相同 → 跳过
    with open_db(offline_settings.db_path) as conn:
        assert repo.count_documents(conn) == 1
        assert repo.count_chunks(conn) == 2


# ---------------------------------------------------------------------------
# 失败路径
# ---------------------------------------------------------------------------


def test_embed_failure_rolls_back_document_and_copy(tmp_path, offline_settings, monkeypatch):
    note = _write_note(tmp_path, "a.md", CONTENT_A)
    svc = _svc(offline_settings)

    def boom(self):
        raise RuntimeError("模拟嵌入服务故障")

    monkeypatch.setattr(svc, "_embed_chunks", boom)
    with pytest.raises(RuntimeError, match="模拟嵌入服务故障"):
        svc.ingest_one(note)

    with open_db(offline_settings.db_path) as conn:
        assert repo.count_documents(conn) == 0  # 文档回滚
        assert repo.count_chunks(conn) == 0
    uploads = list(offline_settings.uploads_dir.rglob("*"))
    assert uploads == []  # 半成品副本也清除


def test_empty_document_reported_as_failure(tmp_path, offline_settings):
    _write_note(tmp_path, "空.md", "# 只有标题")
    svc = _svc(offline_settings)
    result = svc.ingest_paths([tmp_path])
    assert result.ingested == []
    assert len(result.failed) == 1
    assert "空" in result.failed[0][0]
    with open_db(offline_settings.db_path) as conn:
        assert repo.count_documents(conn) == 0


def test_missing_file_raises(tmp_path, offline_settings):
    svc = _svc(offline_settings)
    with pytest.raises(FileNotFoundError):
        svc.ingest_one(tmp_path / "不存在.md")


# ---------------------------------------------------------------------------
# reindex
# ---------------------------------------------------------------------------


def test_reindex_rebuilds_from_uploads(tmp_path, offline_settings):
    _write_note(tmp_path, "a.md", CONTENT_A)
    _write_note(tmp_path, "b.md", "# 第二篇\n\n朴素贝叶斯假设特征条件独立。")
    svc = _svc(offline_settings)
    first = svc.ingest_paths([tmp_path])
    assert len(first.ingested) == 2

    rebuilt = svc.reindex()
    assert len(rebuilt.ingested) == 2  # uploads 副本全量重建
    with open_db(offline_settings.db_path) as conn:
        assert repo.count_documents(conn) == 2


def test_content_sha_is_sha256_of_raw_bytes(tmp_path, offline_settings):
    note = _write_note(tmp_path, "a.md", CONTENT_A)
    svc = _svc(offline_settings)
    svc.ingest_paths([tmp_path])
    expected = hashlib.sha256(note.read_bytes()).hexdigest()
    with open_db(offline_settings.db_path) as conn:
        doc = repo.list_documents(conn)[0]
    assert doc.file_sha256 == expected


# ---------------------------------------------------------------------------
# v3：组织属性（改名/文件夹归属）在重灌语义下保留
# ---------------------------------------------------------------------------


def test_same_name_replace_keeps_folder_and_title(tmp_path, offline_settings):
    """同名替换重灌（用户更新了同一份笔记）：UI 里改的名 + 归的夹不丢。

    替换语义先删旧行再重建——若新行不带回组织属性，用户整理过的语料
    会在每次改笔记后被打回根级未命名。这里是"重灌不拆家"的回归断言。
    """
    note = _write_note(tmp_path, "a.md", CONTENT_A)
    svc = _svc(offline_settings)
    svc.ingest_paths([tmp_path])
    with open_db(offline_settings.db_path) as conn:
        folder = repo.create_kb_folder(conn, "论文")
        doc = repo.list_documents(conn)[0]
        repo.set_document_title(conn, doc.id, "用户整理名")
        repo.move_document(conn, doc.id, folder)

    note.write_text(CONTENT_A + "\n## 附录\n\n补充一段。\n", encoding="utf-8")
    result = svc.ingest_paths([tmp_path])
    assert result.ingested == [str(note)]
    with open_db(offline_settings.db_path) as conn:
        docs = repo.list_documents(conn)
        assert len(docs) == 1  # 同名替换而非新增
        assert docs[0].title == "用户整理名"  # 改名保留
        assert docs[0].folder_id == folder  # 文件夹归属保留
        assert docs[0].chunk_count == 3  # 内容确已更新（不是幂等跳过）


def test_reindex_keeps_folder_and_title(tmp_path, offline_settings):
    """reindex（换嵌入模型时的全量重建）后组织属性原样恢复。

    reindex 从 uploads 副本重新走全量入库；若保留映射只在内存里而不
    按行恢复，重建后文件夹树会整个塌成根级——v3 回归锚点。
    """
    _write_note(tmp_path, "a.md", CONTENT_A)
    svc = _svc(offline_settings)
    svc.ingest_paths([tmp_path])
    with open_db(offline_settings.db_path) as conn:
        folder = repo.create_kb_folder(conn, "论文")
        doc = repo.list_documents(conn)[0]
        repo.set_document_title(conn, doc.id, "整理名")
        repo.move_document(conn, doc.id, folder)

    rebuilt = svc.reindex()
    assert len(rebuilt.ingested) == 1  # 全量重建发生（非空跑）
    with open_db(offline_settings.db_path) as conn:
        doc = repo.list_documents(conn)[0]
        assert doc.title == "整理名"
        assert doc.folder_id == folder


def test_reindex_keeps_folder_when_file_path_is_stale(tmp_path, offline_settings):
    """reindex 的保留键必须是文件名而非 file_path（2026-09-11 修复的真实 bug）。

    库里 file_path 可能是脏历史值（项目改名后指向旧目录，实测 21/23 行），
    按完整路径做键时 reindex 永远查不到保留信息 → 用户整理好的文件夹归属
    每次重灌都被清成根级（用户实测："每次修改后文件夹都会变"）。
    """

    from mikasa.ingest.service import IngestService
    from mikasa.storage import repo
    from mikasa.storage.db import open_db

    src = tmp_path / "src"
    src.mkdir()
    (src / "甲笔记.md").write_text("# 甲笔记\n\n甲笔记的正文内容。", encoding="utf-8")

    svc = IngestService(offline_settings)
    svc.ingest_paths([src])
    with open_db(offline_settings.db_path) as conn:
        doc_id = repo.list_documents(conn)[0].id
        folder_id = repo.create_kb_folder(conn, "论文夹")
        repo.move_document(conn, doc_id, folder_id)
        repo.set_document_title(conn, doc_id, "我改过的名字")
        conn.commit()

    # 把 file_path 改写成指向已废弃旧目录的历史形态（复刻真实脏数据）
    with open_db(offline_settings.db_path) as conn:
        conn.execute(
            "UPDATE documents SET file_path = ? WHERE id = ?",
            (r"D:\Code\MyProject1\data\uploads\甲笔记.md", doc_id),
        )
        conn.commit()

    svc.reindex()
    with open_db(offline_settings.db_path) as conn:
        doc = repo.list_documents(conn)[0]
    assert doc.folder_id == folder_id, "重灌后文件夹归属必须保留"
    assert doc.title == "我改过的名字", "重灌后用户改过的标题必须保留"


def test_reindex_failure_keeps_uploads_source(tmp_path, offline_settings, monkeypatch):
    """reindex 失败绝不能删掉 uploads 里的源文件（2026-09-11 修复）。

    reindex 先清空全部文档行，再把 uploads 当输入重灌——此时"源文件"就是
    uploads 副本本身。旧实现失败回滚时无条件 unlink 副本，于是文档在库里
    和磁盘上同时消失（永久丢失，只能靠外部备份）。修复 = 只删本次真正
    复制进来的副本。
    """
    note = _write_note(tmp_path, "a.md", CONTENT_A)
    svc = _svc(offline_settings)
    svc.ingest_one(note)  # 源在 tmp_path → 副本落进 uploads
    copy_path = offline_settings.uploads_dir / "a.md"
    assert copy_path.is_file()

    def boom(self, chunks):
        raise ProviderError("模拟嵌入服务故障（如 429）")

    monkeypatch.setattr(IngestService, "_embed_chunks", boom)
    summary = IngestService(offline_settings).reindex()

    assert summary.failed, "嵌入故障应记入 failed 而不是静默吞掉"
    assert copy_path.is_file(), "reindex 失败不能删掉用户的 uploads 源文件"


def test_same_name_bad_reupload_keeps_old_document(tmp_path, offline_settings):
    """同名重传一个坏文件：旧文档与其组织属性必须原样保留（2026-09-11 修复）。

    旧实现"先删旧行、后解析新文件"：重传空文档（或损坏/无文本层的扫描件）
    时旧行早已删除并提交，用户改的标题与文件夹归属随异常一起丢掉。
    修复 = 解析并校验通过后才动库。
    """
    good = _write_note(tmp_path, "笔记.md", CONTENT_A)
    svc = _svc(offline_settings)
    svc.ingest_one(good)
    with open_db(offline_settings.db_path) as conn:
        doc_id = repo.list_documents(conn)[0].id
        folder_id = repo.create_kb_folder(conn, "论文夹")
        repo.move_document(conn, doc_id, folder_id)
        repo.set_document_title(conn, doc_id, "我改过的名字")
        conn.commit()

    bad = _write_note(tmp_path, "笔记.md", "# 只有标题")  # 解析期判空
    with pytest.raises(ZhiwenError):
        svc.ingest_one(bad)

    with open_db(offline_settings.db_path) as conn:
        rows = repo.list_documents(conn)
    assert len(rows) == 1, "坏文件不能把旧文档清掉"
    assert rows[0].title == "我改过的名字", "用户改过的标题必须保留"
    assert rows[0].folder_id == folder_id, "文件夹归属必须保留"
    assert (offline_settings.uploads_dir / "笔记.md").is_file(), "uploads 旧副本必须保留"


def test_same_name_reupload_embed_failure_keeps_old_copy(
    tmp_path, offline_settings, monkeypatch
):
    """同名重传时嵌入失败：盘上的旧副本必须保住（2026-09-11 打包前审查修复）。

    旧行在解析通过后就被删掉并提交，若随后嵌入阶段失败（API 限流/超时），
    旧实现会把新副本也 unlink —— 文档在库里和磁盘上同时消失。现在旧副本
    先改名让位（同目录 rename，不复制数据），失败则原样放回。
    """
    note = _write_note(tmp_path, "笔记.md", CONTENT_A)
    svc = _svc(offline_settings)
    svc.ingest_one(note)
    copy_path = offline_settings.uploads_dir / "笔记.md"
    assert copy_path.is_file()
    original_text = copy_path.read_text(encoding="utf-8")

    # 同名、内容已变的新文件；让嵌入阶段失败
    changed = _write_note(tmp_path, "笔记.md", CONTENT_A.replace("Adam", "AdamW 优化器"))

    def boom(self, chunks):
        raise ProviderError("模拟嵌入服务故障（429）")

    monkeypatch.setattr(IngestService, "_embed_chunks", boom)
    with pytest.raises(ProviderError):
        svc.ingest_one(changed)

    assert copy_path.is_file(), "旧副本必须放回原位（否则文档彻底消失）"
    assert copy_path.read_text(encoding="utf-8") == original_text, "放回的应是旧版内容"
    assert not list(offline_settings.uploads_dir.glob("*.replacing")), "让位文件不能残留"
