"""入库服务单测：幂等 / 同名替换 / 失败回滚 / reindex。

这些语义是"工程感"的直接证据：重复导入零副作用、
改了笔记再导入 = 原地更新、嵌入失败不残留半成品。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from mikasa.errors import ProviderError, ZhiwenError
from mikasa.ingest.service import IngestService
from mikasa.models.document import Chunk
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


def test_force_bypasses_content_dedup_so_same_body_coexist(tmp_path, offline_settings):
    """force=True 时"异名同内容"各自成篇（笔记链路依赖的逃生口）。

    默认的跨文件内容去重对语料是好事（备份副本不污染检索），但笔记的第二篇
    可能就是同一份内容（或编辑成与他人同内容）：走默认语义会被**静默跳过**
    ——接口照样返回成功、库里却没有那一行。force 绕过它，同时仍走同名替换。
    """
    _write_note(tmp_path, "b1.md", CONTENT_A)
    _write_note(tmp_path, "b2.md", CONTENT_A)
    svc = _svc(offline_settings)
    svc.ingest_one(tmp_path / "b1.md", force=True)
    svc.ingest_one(tmp_path / "b2.md", force=True)
    with open_db(offline_settings.db_path) as conn:
        assert repo.count_documents(conn) == 2


def test_explicit_title_beats_inherited_title_and_reaches_index(tmp_path, offline_settings):
    """显式 title 压过同名替换继承的旧标题，且**在分块时就生效**。

    笔记编辑改标题走的就是这条路。两条都错不得：
      - 让旧标题胜出 → 编辑时改标题永远不生效（keep_title 继承旧行）；
      - 改成"入库后再 set_document_title" → 索引词空间（BM25 与向量共用的
        检索表示）里留的仍是旧名字 → 改完名搜不到新名字。
    标题用纯 ASCII 标记词：jieba 会把中文词组切开，子串断言不可靠。
    """
    note = _write_note(tmp_path, "a.md", CONTENT_A)
    svc = _svc(offline_settings)
    svc.ingest_one(note, title="FirstTitleA")

    # 同名替换：内容变了才走替换（sha 相同会被跳过），标题也一并换掉
    _write_note(tmp_path, "a.md", CONTENT_A + "\n## 补记\n\n新增一段可检索的正文。\n")
    svc.ingest_one(note, title="SecondTitleB")

    with open_db(offline_settings.db_path) as conn:
        docs = repo.list_documents(conn)
        assert len(docs) == 1, "同名替换必须是原地更新"
        assert docs[0].title == "SecondTitleB"
        tokens = " ".join(
            t for chunk in repo.chunks_by_document(conn, docs[0].id) for t in chunk.tokens
        )
    assert "SecondTitleB" in tokens
    assert "FirstTitleA" not in tokens


# ---------------------------------------------------------------------------
# 失败路径
# ---------------------------------------------------------------------------


def test_embed_failure_rolls_back_document_and_copy(tmp_path, offline_settings, monkeypatch):
    note = _write_note(tmp_path, "a.md", CONTENT_A)
    svc = _svc(offline_settings)

    def boom(chunks, doc_title):  # 挂在实例上 → 不绑定 self，签名与调用点一致
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


def test_reindex_aborts_when_a_copy_is_missing(tmp_path, offline_settings):
    """副本少了一个 → reindex **中止**，绝不清库（2026-09-20 补的第二道闸）。

    第一道闸只挡"uploads 全空"，挡不住"少了一个副本"——而那才是更容易被忽略的
    丢数据路径：清库 → 重灌时那一行没有来源 → 永久消失（笔记尤其：uploads 副本
    就是它唯一的副本，原文也跟着没了）。
    """
    _write_note(tmp_path, "a.md", CONTENT_A)
    _write_note(tmp_path, "b.md", "# 第二篇\n\n朴素贝叶斯假设特征条件独立。")
    svc = _svc(offline_settings)
    svc.ingest_paths([tmp_path])
    (offline_settings.uploads_dir / "b.md").unlink()  # 模拟清理工具/误删

    with pytest.raises(ZhiwenError, match="找不到副本"):
        svc.reindex()

    with open_db(offline_settings.db_path) as conn:
        assert repo.count_documents(conn) == 2  # 库原样未动，两篇都还在


def test_reindex_accepts_a_copy_found_by_title(tmp_path, offline_settings):
    """副本被改名（stem 仍等于标题）不算丢——与读侧同口径的第二跳，别误报。"""
    _write_note(tmp_path, "a.md", CONTENT_A)
    svc = _svc(offline_settings)
    svc.ingest_paths([tmp_path])
    with open_db(offline_settings.db_path) as conn:
        title = repo.list_documents(conn)[0].title
    renamed = offline_settings.uploads_dir / "a.md"
    renamed.rename(offline_settings.uploads_dir / f"{title}.md")

    rebuilt = svc.reindex()  # 不抛即通过（按标题 + sha 验身找到副本）

    assert len(rebuilt.ingested) == 1


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


def test_stale_file_path_still_replaces_instead_of_duplicating(tmp_path, offline_settings):
    """历史脏 file_path 下重传同名文件 = 替换，不是新增（2026-09-20 修）。

    真实来路：仓库改名 / `data/` 搬家会让库里 file_path 指向废弃的旧目录
    （本项目实测 23 行里 21 行如此）。**读侧**一直按文件名兜底，那份文档照样
    读得出来；而**写侧**当时只按全路径找旧行 → 判成"新文件" → 同名替换变成
    插第二行：多一篇重复、丢组织属性、索引里还是旧正文。

    修法在写侧（脏键在那儿）：按全路径找不到就按名兜底，找到先把键修回真实
    副本路径，再走原本的同名替换。
    """
    note = _write_note(tmp_path, "a.md", CONTENT_A)
    svc = _svc(offline_settings)
    svc.ingest_paths([tmp_path])
    stale = r"D:\Code\MyProject1\data\uploads\a.md"  # 改名前的旧项目路径
    with open_db(offline_settings.db_path) as conn:
        doc = repo.list_documents(conn)[0]
        folder = repo.create_kb_folder(conn, "论文")
        repo.set_document_title(conn, doc.id, "用户整理名")
        repo.move_document(conn, doc.id, folder)
        repo.set_document_file_path(conn, doc.id, stale)  # 模拟历史脏值

    note.write_text(CONTENT_A + "\n## 附录\n\n补充一段。\n", encoding="utf-8")
    result = svc.ingest_paths([tmp_path])

    assert result.ingested == [str(note)]
    with open_db(offline_settings.db_path) as conn:
        docs = repo.list_documents(conn)
        assert len(docs) == 1  # 关键：替换而不是插第二行
        assert docs[0].title == "用户整理名"  # 组织属性照旧继承
        assert docs[0].folder_id == folder
        assert docs[0].chunk_count == 3  # 内容确已更新
        # 顺带把脏键修回了真实副本路径（下一跳不必再兜底）
        assert docs[0].file_path == str(offline_settings.uploads_dir / "a.md")


def test_upload_name_lookup_treats_wildcards_literally(tmp_path, offline_settings):
    """文件名里的 `%` / `_` 不能被当通配符（所以这条查找不走 SQL LIKE）。"""
    _write_note(tmp_path, "100%_笔记.md", CONTENT_A)
    svc = _svc(offline_settings)
    svc.ingest_paths([tmp_path])
    with open_db(offline_settings.db_path) as conn:
        assert repo.get_document_by_upload_name(conn, "100%_笔记.md") is not None
        assert repo.get_document_by_upload_name(conn, "100XY笔记.md") is None
        assert repo.get_document_by_upload_name(conn, "100%Z笔记.md") is None


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


def test_reindex_keeps_source_ref(tmp_path, offline_settings):
    """reindex 后论文来源标识（source_ref）不丢——v4 的同一个坑。

    reindex 会清空全部行再重建；来源若不进保留清单，每次换嵌入模型都会
    把"这篇论文来自哪条在线记录"清成 NULL → 「找论文」页的"已在库中"标记
    集体失效（folder_id 当年就是这么丢的）。
    """
    _write_note(tmp_path, "a.md", CONTENT_A)
    svc = _svc(offline_settings)
    svc.ingest_paths([tmp_path])
    with open_db(offline_settings.db_path) as conn:
        doc = repo.list_documents(conn)[0]
        repo.set_document_source_ref(conn, doc.id, "arxiv:2401.12345")
        conn.commit()

    rebuilt = svc.reindex()
    assert len(rebuilt.ingested) == 1
    with open_db(offline_settings.db_path) as conn:
        doc = repo.list_documents(conn)[0]
        assert doc.source_ref == "arxiv:2401.12345"
        assert repo.list_source_refs(conn) == {"arxiv:2401.12345"}


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

    def boom(self, chunks, doc_title):
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


def test_same_name_reupload_embed_failure_keeps_old_copy(tmp_path, offline_settings, monkeypatch):
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

    def boom(self, chunks, doc_title):
        raise ProviderError("模拟嵌入服务故障（429）")

    monkeypatch.setattr(IngestService, "_embed_chunks", boom)
    with pytest.raises(ProviderError):
        svc.ingest_one(changed)

    assert copy_path.is_file(), "旧副本必须放回原位（否则文档彻底消失）"
    assert copy_path.read_text(encoding="utf-8") == original_text, "放回的应是旧版内容"
    assert not list(offline_settings.uploads_dir.glob("*.replacing")), "让位文件不能残留"


@pytest.mark.skipif(
    os.name != "nt", reason="POSIX 的 rename 不受文件占用影响，触发条件是 Windows 文件锁"
)
def test_same_name_replace_keeps_row_when_old_copy_locked(tmp_path, offline_settings):
    """同名替换时旧副本被别的程序占着：**旧行必须留在库里**（2026-09-17 修复）。

    旧顺序是"先删旧行并提交，再做让位 rename"：rename 撞 Windows 文件锁
    （WinError 32，任何打开着该副本的程序都会造成）时异常上抛，而旧行早已
    删除——文档从界面消失、文件却还在盘上，要等一次 reindex 才能回来（笔记
    还会顺带降级成普通文档，因为标记在丢掉的行走里）。用户什么都没改，却看到
    文档不见了。
    修复 = 顺序反过来：新副本先在盘上就位，旧行只在文件操作全部成功后才删。
    这一步失败时库与盘都原样不动，错误如实上抛（用户重试即可）。
    """
    note = _write_note(tmp_path, "笔记.md", CONTENT_A)
    svc = _svc(offline_settings)
    svc.ingest_one(note)
    copy_path = offline_settings.uploads_dir / "笔记.md"
    assert copy_path.is_file()

    changed = _write_note(tmp_path, "笔记.md", CONTENT_A.replace("Adam", "AdamW 优化器"))
    with open_db(offline_settings.db_path) as conn:
        before = repo.list_documents(conn)

    with open(copy_path, "rb"), pytest.raises(OSError):  # 句柄占用 → 让位 rename 必失败
        svc.ingest_one(changed)

    with open_db(offline_settings.db_path) as conn:
        after = repo.list_documents(conn)
    assert [d.id for d in after] == [d.id for d in before], "旧行必须原样在库（修复前这里会是空表）"
    assert copy_path.read_text(encoding="utf-8") == CONTENT_A, "旧副本必须原样在位"


def test_reindex_with_empty_uploads_refuses_instead_of_wiping(tmp_path, offline_settings):
    """uploads 为空时 reindex 必须**中止**，而不是把现有库清空。

    反过来的顺序（先把行删干净、commit，才发现 uploads 里没东西可灌）会让命令
    输出一个空 summary、退出码 0，而用户的全部文档与向量已经无声消失
    （2026-09-11 审查实测）。用户清过一次 uploads、换过 data 目录、副本被误删
    都会命中。所以先查 uploads 再动数据库。
    """
    _write_note(tmp_path, "a.md", CONTENT_A)
    svc = _svc(offline_settings)
    svc.ingest_paths([tmp_path])
    with open_db(offline_settings.db_path) as conn:
        assert repo.count_documents(conn) == 1

    # 模拟 uploads 副本被清掉（源目录 tmp_path 不影响，reindex 只看 uploads）
    for f in offline_settings.uploads_dir.rglob("*"):
        if f.is_file():
            f.unlink()

    with pytest.raises(ZhiwenError, match="uploads"):
        svc.reindex()

    with open_db(offline_settings.db_path) as conn:
        assert repo.count_documents(conn) == 1, "库必须原封不动"


def test_index_text_honours_title_prefix_knob(offline_settings):
    """`chunking.title_prefix=false` 必须真的改变索引词空间。

    配置项与三处文档（architecture.md 中英、design-decisions）都把它当"可消融的
    旋钮"，但在此之前**没有任何代码读过它**——标题是无条件拼上去的
    （2026-09-11 审查发现）。
    """
    svc = _svc(offline_settings)

    on = svc._index_text("论文", "1. 章", "正文")
    assert on.startswith("《论文》｜1. 章"), on

    svc.settings.chunking = svc.settings.chunking.model_copy(update={"title_prefix": False})
    assert svc._index_text("论文", "1. 章", "正文") == "正文"


def test_embedding_uses_index_text_not_raw_content(offline_settings):
    """向量必须嵌入**索引词空间**（标题前置 + 正文），不是裸 content。

    代码注释与 architecture.md 都写着"标题前置提升稀疏/稠密两路召回"、
    `_index_text` 的 docstring 也写着"BM25/向量共用"，而实现里 dense 一直只看
    content——标题从未进过向量（2026-09-11 审查发现）。
    """
    import numpy as np

    seen: list[list[str]] = []

    class _Spy:
        model = "spy"
        dim = 2

        def embed_documents(self, texts: list[str]) -> np.ndarray:
            seen.append(list(texts))
            return np.zeros((len(texts), 2), dtype=np.float32)

    svc = _svc(offline_settings)
    svc._embedding = _Spy()
    # 先落一篇真文档：_embed_chunks 会把向量写回库，chunk 没有对应的
    # documents 行会撞外键（测试夹具的坑，不是被测逻辑的问题）
    from mikasa.models.document import Document

    with open_db(offline_settings.db_path) as conn:
        doc_id = repo.insert_document(
            conn,
            Document(
                title="论文标题", file_path="a.md", file_type="md", file_sha256="sha", char_count=1
            ),
        )
        repo.insert_chunks(
            conn,
            [
                Chunk(
                    document_id=doc_id,
                    seq=0,
                    content="正文内容",
                    content_sha256="s",
                    heading_path="1. 章",
                )
            ],
        )
        chunk = repo.chunks_by_document(conn, doc_id)[0]

    svc._embed_chunks([chunk], "论文标题")

    assert seen and seen[0][0].startswith("《论文标题》｜1. 章")
    assert "正文内容" in seen[0][0]
