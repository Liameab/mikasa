"""入库服务：源文件 → 清洗 → 结构分块 → SQLite → 向量化（增量语义）。

幂等与增量规则（面试可讲的设计点，详见 docs/architecture.md）：
  - 内容未变：任意文档的 file_sha256 已存在 → 跳过（重跑/重复导入零副作用）；
  - 同名文件内容已变：删除旧文档行（外键级联清 chunks/embeddings）后重建，
    实现"改了笔记再导入 = 原地更新"的直觉语义——重建行**保留组织属性**
    （文件夹归属 + 手动改过的标题，v3）：原地更新不该把用户整理好的分类
    打散，reindex 全量重建同理（按 uploads 路径映射回写）；
  - 嵌入失败：整个文档回滚并标记失败——保证向量矩阵与 chunk 表恒一致
    （IndexManager 对不一致直接报错，半成品会毒化全库，故宁回滚不残留）。

向量只对新增/变更文档追加（INSERT OR REPLACE），同模型增量、不重嵌全库；
`mikasa ingest --reindex` 提供全量重建路径。
"""

from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

import numpy as np

from mikasa.config.settings import Settings
from mikasa.errors import ZhiwenError
from mikasa.index.tokenizer import get_tokenizer
from mikasa.ingest.chunker import chunk_paragraphs
from mikasa.ingest.loader import load_document
from mikasa.ingest.types import LoadedDocument
from mikasa.models.document import Chunk, Document
from mikasa.providers import get_embedding
from mikasa.providers.embedding import EmbeddingProvider
from mikasa.storage import repo
from mikasa.storage.db import open_db
from mikasa.storage.index_files import save_index_meta
from mikasa.utils.hashing import sha256_file, sha256_text
from mikasa.utils.logging import get_logger, redact

logger = get_logger("ingest")

_EMBED_BATCH = 32  # bge 系列单批上限足够小，规避 API 单请求长度限制


@dataclass
class IngestSummary:
    """一次 ingest 运行的汇总（CLI 展示与测试断言用）。"""

    ingested: list[str] = field(default_factory=list)  # 新入库/被替换
    skipped: list[str] = field(default_factory=list)  # 内容未变
    failed: list[tuple[str, str]] = field(default_factory=list)  # (文件, 原因)
    chunks_added: int = 0
    chars_added: int = 0

    @property
    def total_files(self) -> int:
        return len(self.ingested) + len(self.skipped) + len(self.failed)


class IngestService:
    """一次运行持有一个 embedder；可被 CLI 与 Web 复用（每个实例一次生命周期）。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.ensure_dirs()
        self._tokenizer = get_tokenizer()
        # reindex 的组织属性暂存：{uploads 副本文件名: (标题, 文件夹 id)}。
        # reindex 先清空行再全量重导，ingest_one 重建新行时按名查回写；
        # 常规路径为空 dict，查不到即根级/默认标题（无副作用）。
        # **键必须是文件名而不是 file_path**（2026-09-11 修复的真实 bug）：
        # 库里 file_path 是历史值——实测 23 行里 21 行指向已废弃的旧项目
        # 路径（D:\Code\MyProject1\...，项目改名残留），而重导后的新路径
        # 必然不同 → 按完整路径做键永远查不到 → 每次 reindex 都把用户整理
        # 好的文件夹归属清成根级（用户实测"每次修改后文件夹都会变"）。
        # uploads 副本是扁平存放的（uploads_dir / file.name），文件名即稳定键。
        self._reindex_keep: dict[str, tuple[str, int | None]] = {}
        # 入库串行化：Web 上传是并发 POST（前端多选/重复选文件逐个 upload 不
        # await），而"查重 → 删旧行 → 插新行"在 SQLite 上不是原子链——2026-09-11
        # 实测并发同名上传撞 documents.file_path 唯一约束（IntegrityError → 500）。
        # serve 单进程是既定约束，进程内锁足够；RLock 供
        # reindex → ingest_paths → ingest_one 嵌套复用。
        self._lock = threading.RLock()
        embedding_cfg = settings.embedding
        self._embedding: EmbeddingProvider | None = (
            get_embedding(embedding_cfg) if embedding_cfg.backend != "none" else None
        )

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    def ingest_paths(self, paths: list[Path], *, force: bool = False) -> IngestSummary:
        """接收文件或目录（目录递归收集支持的类型，排序保证确定性）。"""
        with self._lock:  # 整批串行：批量重导期间不让并发上传插进同一批的半途
            return self._ingest_paths(paths, force=force)

    def _ingest_paths(self, paths: list[Path], *, force: bool) -> IngestSummary:
        summary = IngestSummary()
        for file in self._collect_files(paths):
            try:
                status, added_chunks, added_chars = self.ingest_one(file, force=force)
            except (ZhiwenError, OSError) as exc:
                summary.failed.append((str(file), redact(str(exc))))
                logger.error("入库失败 %s：%s", file, redact(str(exc)))
                continue
            if status == "skipped":
                summary.skipped.append(str(file))
                logger.info("跳过（内容未变）：%s", file)
            else:
                summary.ingested.append(str(file))
                summary.chunks_added += added_chunks
                summary.chars_added += added_chars
                logger.info("入库成功：%s", file)
        self._sync_meta()
        return summary

    def ingest_one(self, file: Path, *, force: bool = False) -> tuple[str, int, int]:
        """处理单个文件（加锁串行，理由见 __init__ 的 _lock 注释）。

        返回 (状态, 新增块数, 新增字符数)：ingested（含 replaced）/ skipped。
        异常上抛（由调用方决定记录还是终止）。
        """
        with self._lock:
            return self._ingest_one(file, force=force)

    def _ingest_one(self, file: Path, *, force: bool) -> tuple[str, int, int]:
        if not file.is_file():
            raise FileNotFoundError(f"文件不存在：{file}")
        sha = sha256_file(file)
        copy_path = self.settings.uploads_dir / file.name

        # 同内容已在库：直接跳过（先查再解析——大 PDF 解析很贵，重复上传别白付）
        with open_db(self.settings.db_path) as conn:
            duplicate = repo.get_document_by_sha(conn, sha)
        if duplicate is not None and not force:
            return "skipped", 0, 0

        # 解析新文件**必须在动库之前**（2026-09-11 修复）：解析失败（空文档 /
        # 损坏文件 / 无文本层的扫描件）时旧文档与其组织属性必须原样保留——
        # 曾经"先删旧行后解析"，重传一个坏文件就把用户改好的标题与文件夹
        # 归属一并清掉，uploads 旧副本还成了孤儿。
        loaded = load_document(file)
        if not loaded.paragraphs:
            raise ZhiwenError("文档为空（无任何可检索段落）")

        # 重建时的组织属性（文件夹归属 + 标题）：同名替换从被删行收走；
        # reindex 场景行已整体清空，靠 uploads 路径映射取回。None = 保持
        # 默认（根级 / 文件主名标题）。
        keep_title: str | None = None
        keep_folder: int | None = None
        with open_db(self.settings.db_path) as conn:
            existing = repo.get_document_by_path(conn, str(copy_path))
            if existing is not None:
                if existing.file_sha256 == sha and not force:
                    return "skipped", 0, 0
                # 同名更新：删行前先收走组织属性（文件夹 + 手动改过的标题）
                keep_title = existing.title
                keep_folder = existing.folder_id
                logger.info("检测到同名更新：替换文档 #%s（%s）", existing.id, file.name)
                if existing.id is not None:
                    repo.delete_document(conn, existing.id)
                    conn.commit()
            else:
                kept = self._reindex_keep.get(file.name)
                if kept is not None:
                    keep_title, keep_folder = kept

        copied = False
        displaced: Path | None = None  # 同名替换时被"让位"的旧副本
        if file.resolve() != copy_path.resolve():
            # 源文件就是入库副本（reindex 扫 uploads 的场景）：无需复制，
            # 否则 shutil 会报"同一文件"（真实 bug 的回归见 test_service）
            copy_path.parent.mkdir(parents=True, exist_ok=True)
            # 同名替换：旧副本先**改名让位**（同目录 rename 瞬时完成、不复制
            # 数据），入库成功后才真删。为什么不直接覆盖——旧行在上面已经
            # 删掉并提交，若随后嵌入阶段失败（API 限流/超时/维度不符），
            # 回滚会把新行和副本一起清掉，**旧文档在库与磁盘上同时消失**。
            # 2026-09-11 打包前审查发现：当天"解析提前"只挡住了坏文件，
            # 没挡住嵌入失败这条路径。
            if copy_path.is_file():
                displaced = copy_path.with_name(f"{copy_path.name}.replacing")
                displaced.unlink(missing_ok=True)
                copy_path.rename(displaced)
            try:
                shutil.copyfile(file, copy_path)  # 副本入库后再解析：原文件可随意移动
            except OSError:
                if displaced is not None:  # 连复制都没成：把旧副本放回去
                    displaced.rename(copy_path)
                raise
            copied = True

        doc_id: int | None = None
        try:
            doc_id, chunks, doc_title = self._chunk_and_persist(
                loaded, copy_path, sha, title=keep_title, folder_id=keep_folder
            )
            self._embed_chunks(chunks, doc_title)
            self._mark_done(doc_id, len(chunks))
        except Exception:
            # 回滚半成品：保证索引一致与幂等（重跑可完整重建）
            if doc_id is not None:
                with open_db(self.settings.db_path) as conn:
                    repo.delete_document(conn, doc_id)
                    conn.commit()
            # 只删**本次复制进来**的副本（2026-09-11 修复）：reindex 场景里源
            # 文件就是 uploads 副本，无条件 unlink 会把用户的源文件删掉——
            # reindex 开头已清空全部行，等于文档在库和磁盘上同时消失。
            if copied:
                copy_path.unlink(missing_ok=True)
            # 让位的旧副本放回原位：库里那行虽然没了，盘上的原件要保住，
            # 用户重传或 reindex 都能把它接回来（宁留文件，不留半成品）
            if displaced is not None:
                displaced.rename(copy_path)
            raise
        if displaced is not None:
            displaced.unlink(missing_ok=True)  # 成功：旧副本正式退役
        return "ingested", len(chunks), loaded.char_count

    def exclusive(self) -> threading.RLock:
        """把入库锁交给调用方用 `with` 包住一段外部操作。

        为什么需要：删除文档是"删行 + 按路径 unlink uploads 副本"两步，而 reindex
        会**按同一路径**重建新行。两者不互斥时，reindex 刚建好的行会用着一个已经被
        删掉的副本——`/file`、`/page/*`、下次 reindex 全部失效，文档等于凭空消失且
        不可恢复（2026-09-11 审查发现）。锁本身是 RLock，嵌套调用安全。
        """
        return self._lock

    def reindex(self) -> IngestSummary:
        """全量重建：清空语料后重新导入 data/uploads 下全部入库副本。

        场景：切换嵌入模型 / 分词器 / 分块参数后旧索引作废。
        重建保留每行的组织属性（标题/文件夹，按 uploads 路径映射回写）——
        重嵌语料不该把用户整理好的文件夹分类打散。
        """
        with self._lock:  # 清库到重灌完成之间不能被并发上传插队（会拿到 doomed 的 doc_id）
            return self._reindex()

    def _reindex(self) -> IngestSummary:
        # **先确认 uploads 里有东西，再动数据库。** 反过来写过一版，后果是：
        # 库已经删空并 commit，才发现 uploads 是空的 → 直接返回空 summary，
        # 命令输出 `ingested=[] skipped=[] failed=[]`、退出码 0，而用户的全部
        # 文档与向量已经无声消失（2026-09-11 审查实测）。触发它并不难：用户在
        # 资源管理器里清过一次 uploads、或换过 data 目录、或副本被误删。
        uploads = self.settings.uploads_dir
        files = sorted(uploads.rglob("*")) if uploads.is_dir() else []
        if not any(f.is_file() for f in files):
            raise ZhiwenError(
                f"uploads 目录里没有可重建的文件，reindex 已中止（避免清空现有知识库）：{uploads}\n"
                "如果确实要清空知识库，请逐篇删除文档；uploads 丢失时可从自己的原始资料重新上传。"
            )
        self._reindex_keep = {}
        with open_db(self.settings.db_path) as conn:
            for doc in repo.list_documents(conn):
                if doc.file_path:
                    # 用文件名做键：file_path 可能是脏历史值，但副本名稳定
                    self._reindex_keep[PureWindowsPath(doc.file_path).name] = (
                        doc.title,
                        doc.folder_id,
                    )
                if doc.id is not None:
                    repo.delete_document(conn, doc.id)
            conn.commit()
        summary = self.ingest_paths([uploads], force=True)
        self._sync_meta()
        return summary

    # ------------------------------------------------------------------
    # 内部步骤
    # ------------------------------------------------------------------

    def _collect_files(self, paths: list[Path]) -> list[Path]:
        from mikasa.ingest.loader import LOADERS

        files: list[Path] = []
        for path in paths:
            if path.is_dir():
                root = path.resolve()
                for candidate in sorted(path.rglob("*")):
                    if candidate.suffix.lower() not in LOADERS or not candidate.is_file():
                        continue
                    # 排除"自己的状态目录"（真实 bug，见 test_service.py）：
                    # 把 data/ 当笔记目录扫（如扫仓库根目录）会自噬——扫描途中
                    # 刚写入的 uploads 副本被再次发现并重复入库。判定：正在扫的
                    # 目录是 data/ 的祖先时，跳过 data/ 下的文件；显式扫 data/
                    # 或其子目录（如 reindex 扫 uploads）不受影响。
                    data_root = self.settings.data_dir.resolve()
                    if (
                        data_root.is_relative_to(root)
                        and root != data_root
                        and candidate.resolve().is_relative_to(data_root)
                    ):
                        continue
                    files.append(candidate)
            elif path.is_file():
                files.append(path)
            else:
                logger.warning("路径不存在，已忽略：%s", path)
        return files

    def _chunk_and_persist(
        self,
        loaded: LoadedDocument,
        copy_path: Path,
        sha: str,
        *,
        title: str | None = None,
        folder_id: int | None = None,
    ) -> tuple[int, list[Chunk], str]:
        """分块 + 落库。title/folder_id 是重建时的组织属性保留值（同名替换、
        reindex 场景由 ingest_one 传入）：title=None 用文件主名（默认语义），
        folder_id=None 落根级（新文档默认）。索引词空间用**显示标题**——
        用户改过的名字就是检索里看到的名字。"""
        cfg = self.settings.chunking
        specs = chunk_paragraphs(loaded.paragraphs, size=cfg.size, overlap=cfg.overlap)
        doc_title = title if title is not None else loaded.title
        chunks: list[Chunk] = []
        for seq, spec in enumerate(specs):
            # 正文原样保存；标题只在索引词空间前置（title augmentation：
            # 提升稀疏/稠密召回，而生成与展示不重复标题，见 architecture.md）
            index_text = self._index_text(doc_title, spec.heading_path, spec.content)
            chunks.append(
                Chunk(
                    document_id=-1,  # 占位，落库前替换为真实 id
                    seq=seq,
                    content=spec.content,
                    content_sha256=sha256_text(spec.content),
                    heading_path=spec.heading_path,
                    page_number=spec.page,
                    tokens=self._tokenizer(index_text),
                )
            )
        with open_db(self.settings.db_path) as conn:
            doc_id = repo.insert_document(
                conn,
                Document(
                    title=doc_title,
                    file_path=str(copy_path),
                    file_type=loaded.file_type,
                    file_sha256=sha,
                    char_count=loaded.char_count,
                ),
            )
            if folder_id is not None:
                # 组织属性回写：insert 不落 folder_id 列（历史 schema 兼容，
                # 见 repo.move_document），保留场景在同一事务内补 UPDATE
                repo.move_document(conn, doc_id, folder_id)
            for chunk in chunks:
                # frozen 模型的构造期改写：字段全量已知，仅差 document_id
                object.__setattr__(chunk, "document_id", doc_id)
            repo.insert_chunks(conn, chunks)
            ids = repo.chunk_ids_of_document(conn, doc_id)
            if len(ids) != len(chunks):
                raise RuntimeError("chunk 落库数不一致（内部错误，请报告）")
            for chunk, chunk_id in zip(chunks, ids, strict=True):
                object.__setattr__(chunk, "id", chunk_id)
            conn.commit()
        logger.info(
            "文档已入库：%s（%d 字符 → %d 块）", loaded.title, loaded.char_count, len(chunks)
        )
        # 把 doc_title 一并返回：向量侧要用**同一个**标题构造索引文本，
        # 各算各的迟早会漂移
        return doc_id, chunks, doc_title

    def _index_text(self, title: str, heading_path: str | None, content: str) -> str:
        """索引词空间：标题路径前置 + 正文——**BM25 与向量共用的检索表示**。

        `chunking.title_prefix=false` 时退回纯正文：配置项与文档（architecture.md
        中英两版、design-decisions）都把它当"可消融的旋钮"，但在此之前**没有任何
        代码读过它**，标题是无条件拼上去的（2026-09-11 审查发现）。
        """
        if not self.settings.chunking.title_prefix:
            return content
        head = f"《{title}》"
        if heading_path:
            head += f"｜{heading_path}"
        return f"{head}\n{content}"

    def _embed_chunks(self, chunks: list[Chunk], doc_title: str) -> None:
        if self._embedding is None:
            return  # 无向量模式（offline）：跳过，dense 路自然关闭
        # **向量也必须用索引词空间**（标题前置 + 正文），不是裸 content：
        # 本函数上方的注释与 architecture.md 都写明"标题前置提升稀疏/稠密两路
        # 召回"、`_index_text` 的 docstring 也写着"BM25/向量共用"——而实现里
        # dense 一直只看 content，标题从未进过向量（2026-09-11 审查发现）。
        # 注意：改了嵌入输入，**既有库需要 `mikasa ingest --reindex`** 才能让
        # 旧数据也带上标题。
        contents = [self._index_text(doc_title, c.heading_path, c.content) for c in chunks]
        batches: list[np.ndarray] = []
        for start in range(0, len(contents), _EMBED_BATCH):
            batches.append(self._embedding.embed_documents(contents[start : start + _EMBED_BATCH]))
        matrix = batches[0] if len(batches) == 1 else np.concatenate(batches, axis=0)
        chunk_ids: list[int] = []
        for chunk in chunks:
            assert chunk.id is not None  # 落库后已回填（见 _chunk_and_persist）
            chunk_ids.append(chunk.id)
        with open_db(self.settings.db_path) as conn:
            repo.save_embeddings(conn, self.settings.embedding.model, chunk_ids, matrix)
            conn.commit()

    def _mark_done(self, doc_id: int, chunk_count: int) -> None:
        with open_db(self.settings.db_path) as conn:
            repo.update_document_after_ingest(
                conn, doc_id, ingest_status="done", chunk_count=chunk_count
            )
            conn.commit()

    def _sync_meta(self) -> None:
        """跑批结束后把 DB 状态落成 meta.json（doctor/评测的指纹来源）。"""
        with open_db(self.settings.db_path) as conn:
            chunk_count = repo.count_chunks(conn)
            digest = repo.corpus_digest(conn)
        embedding_model = self.settings.embedding.model if self._embedding is not None else None
        save_index_meta(
            self.settings,
            chunk_count=chunk_count,
            embedding_model=embedding_model,
            embedding_dim=self._embedding_dim(),
            corpus_sha256=digest,
        )

    def _embedding_dim(self) -> int | None:
        if self._embedding is None:
            return None
        with open_db(self.settings.db_path) as conn:
            row = conn.execute(
                "SELECT dim FROM embeddings ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return int(row["dim"]) if row else None
