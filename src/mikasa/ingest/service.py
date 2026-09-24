"""入库服务：源文件 → 清洗 → 结构分块 → SQLite → 向量化（增量语义）。

幂等与增量规则（面试可讲的设计点，详见 docs/architecture.md）：
  - 内容未变：任意文档的 file_sha256 已存在 → 跳过（重跑/重复导入零副作用）；
  - 同名文件内容已变：删除旧文档行（外键级联清 chunks/embeddings）后重建，
    实现"改了笔记再导入 = 原地更新"的直觉语义——重建行**保留组织属性**
    （文件夹归属 + 手动改过的标题，v3）：原地更新不该把用户整理好的分类
    打散，reindex 全量重建同理（按 uploads 路径映射回写）；
  - 顺序纪律（2026-09-17）：新副本**先在盘上就位**，旧行随后才删。文件操作
    是整条链路唯一会撞外部锁的一步（Windows 文件锁/只读副本），排在删行之后
    的话，失败就是"文档从界面消失、文件还在盘上"；
  - 嵌入失败：整个文档回滚并标记失败——保证向量矩阵与 chunk 表恒一致
    （IndexManager 对不一致直接报错，半成品会毒化全库，故宁回滚不残留）。

向量只对新增/变更文档追加（INSERT OR REPLACE），同模型增量、不重嵌全库；
`mikasa ingest --reindex` 提供全量重建路径。
"""

from __future__ import annotations

import json
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


@dataclass(frozen=True)
class _KeptProps:
    """重建时要继承的组织属性（同名替换与 reindex 共用）。

    用具名字段而不是元组：解包顺序写反是**静默错值**（标题和文件夹都是
    "看起来像那么回事"的值），字段名自证。新增被保留的属性时只改这里。
    """

    title: str
    folder_id: int | None
    source_ref: str | None


@dataclass
class IngestSummary:
    """一次 ingest 运行的汇总（CLI 展示与测试断言用）。"""

    ingested: list[str] = field(default_factory=list)  # 新入库/被替换
    skipped: list[str] = field(default_factory=list)  # 内容未变
    failed: list[tuple[str, str]] = field(default_factory=list)  # (文件, 原因)
    chunks_added: int = 0
    chars_added: int = 0


class IngestService:
    """一次运行持有一个 embedder；可被 CLI 与 Web 复用（每个实例一次生命周期）。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.ensure_dirs()
        self._tokenizer = get_tokenizer()
        # reindex 的组织属性暂存：{uploads 副本文件名: _KeptProps(标题, 文件夹, 来源)}。
        # reindex 先清空行再全量重导，ingest_one 重建新行时按名查回写；
        # 常规路径为空 dict，查不到即根级/默认标题（无副作用）。
        # **键必须是文件名而不是 file_path**（2026-09-11 修复的真实 bug）：
        # 库里 file_path 是历史值——实测 23 行里 21 行指向已废弃的旧项目
        # 路径（D:\Code\MyProject1\...，项目改名残留），而重导后的新路径
        # 必然不同 → 按完整路径做键永远查不到 → 每次 reindex 都把用户整理
        # 好的文件夹归属清成根级（用户实测"每次修改后文件夹都会变"）。
        # uploads 副本是扁平存放的（uploads_dir / file.name），文件名即稳定键。
        self._reindex_keep: dict[str, _KeptProps] = {}
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

    def ingest_one(
        self,
        file: Path,
        *,
        force: bool = False,
        source_ref: str | None = None,
        title: str | None = None,
    ) -> tuple[str, int, int]:
        """处理单个文件（加锁串行，理由见 __init__ 的 _lock 注释）。

        返回 (状态, 新增块数, 新增字符数)：ingested（含 replaced）/ skipped。
        异常上抛（由调用方决定记录还是终止）。

        source_ref 由论文导入（形如 "arxiv:2401.12345"）与笔记链路
        （"note:<key>"）传入：它是四条入库链路里**唯一**知道"这个文件从哪来"
        的地方，其余链路（上传/CLI/reindex）一律 None。

        title 只由笔记链路传入（用户在编辑器里写的标题）：文件主名对笔记是
        "标题-key"这种内部名，不能当显示标题；且正文首个 `#` 标题也不该劫持
        用户填的标题。**它是"显式标题"层，优先级高于同名替换继承来的旧标题**
        （否则编辑时改标题永远不生效）——见 _ingest_one 里的回填点。

        注意 title 与 force 是**配套**的：内容未变时（同名同 sha）非 force 会
        在 title 回填**之前**就返回 skipped，于是"只改标题"静默不生效。笔记
        链路两处调用都同时传了 force=True。
        """
        with self._lock:
            return self._ingest_one(file, force=force, source_ref=source_ref, title=title)

    def _ingest_one(
        self,
        file: Path,
        *,
        force: bool,
        source_ref: str | None = None,
        title: str | None = None,
    ) -> tuple[str, int, int]:
        if not file.is_file():
            raise FileNotFoundError(f"文件不存在：{file}")
        sha = sha256_file(file)
        copy_path = self.settings.uploads_dir / file.name

        # 同内容已在库：直接跳过（先查再解析——大 PDF 解析很贵，重复上传别白付）。
        # **但只跳"已完成"的那一行**：进程被杀（关窗/Ctrl-C/断电）时行会停在
        # pending，不跳的话重传/重存一律被去重挡下——用户看到的"重传也没用"
        # 就是这么来的（2026-09-20 全量审查实测：这类行的自然修复动作全被堵死，
        # 而缺向量还会把全库 dense 路拖成只剩 BM25）。非 done → 落到下面重建。
        with open_db(self.settings.db_path) as conn:
            duplicate = repo.get_document_by_sha(conn, sha)
        if duplicate is not None and not force:
            if duplicate.ingest_status == "done":
                return "skipped", 0, 0
            logger.warning(
                "同内容的文档 #%s 处于 %s 状态（上次入库没走完）——重做这一篇",
                duplicate.id,
                duplicate.ingest_status,
            )

        # 解析新文件**必须在动库之前**（2026-09-11 修复）：解析失败（空文档 /
        # 损坏文件 / 无文本层的扫描件）时旧文档与其组织属性必须原样保留——
        # 曾经"先删旧行后解析"，重传一个坏文件就把用户改好的标题与文件夹
        # 归属一并清掉，uploads 旧副本还成了孤儿。
        loaded = load_document(file)
        if not loaded.paragraphs:
            raise ZhiwenError("文档为空（无任何可检索段落）")

        # 重建时的组织属性（文件夹归属 + 标题）：同名替换从旧行收走（旧行本尊
        # 要等文件就位后才删，见下）；reindex 场景行已整体清空，靠 uploads
        # 路径映射取回。None = 保持默认（根级 / 文件主名标题）。
        keep_title: str | None = None
        keep_folder: int | None = None
        keep_source: str | None = source_ref  # 本次显式带来的来源优先（导入链路）
        replaced_id: int | None = None  # 同名替换待删的旧行（文件就位后才删，见下）
        with open_db(self.settings.db_path) as conn:
            existing = repo.get_document_by_path(conn, str(copy_path))
            if existing is None:
                # 写侧与读侧同口径（2026-09-20 修）：file_path 可能是**历史脏值**
                # （仓库改名 / data 搬家都留下过），只按全路径找会把"这份文件的新
                # 版本"判成新文件 → 插出第二行：多一篇重复、丢文件夹与标题、索引
                # 里还是旧正文，而读侧按名兜底照样读得出来，用户看到的就是同一份
                # 文档出现两次。找到就地把这个键修回真实副本路径（写侧的脏键在
                # 这儿，修法也落在这儿，别让每个调用方各自打补丁）。
                existing = repo.get_document_by_upload_name(conn, copy_path.name)
                if existing is not None:
                    assert existing.id is not None  # 从库里读出来的行，主键必在
                    logger.info(
                        "修复历史脏 file_path：文档 #%s 的 %r → %r",
                        existing.id,
                        existing.file_path,
                        str(copy_path),
                    )
                    repo.set_document_file_path(conn, existing.id, str(copy_path))
                    # 后面只用到标题/文件夹/来源/哈希，都不受这次路径修复影响
            if existing is not None:
                # 内容未变 + 同名 → 跳过。**同样只跳 done**：这一道闸在 sha 那一道
                # 之后（同名不同 sha 会走到这里），pending/failed 的行两处都要放行，
                # 否则"重传自愈"只成功一半（2026-09-20 审查：第一道修完这条仍在挡）
                if existing.file_sha256 == sha and not force:
                    if existing.ingest_status == "done":
                        return "skipped", 0, 0
                    logger.warning(
                        "同名文档 #%s 处于 %s 状态（上次入库没走完）——重做这一篇",
                        existing.id,
                        existing.ingest_status,
                    )
                # 同名更新：先收走组织属性（文件夹 + 手动改过的标题），**旧行
                # 此刻不动**——它要等新副本在盘上就位之后才删。让位 rename 是
                # 整条链路唯一会撞文件锁的一步（Windows 上任何打开着旧副本的
                # 程序都能让 WinError 32），排在删行之后的话，失败就是"文档从
                # 界面消失、文件还在盘上"，要等 reindex 才回得来（笔记还会降级
                # 成普通文档）——2026-09-16 记录，2026-09-17 按此顺序修掉。
                keep_title = existing.title
                keep_folder = existing.folder_id
                if keep_source is None:
                    # 来源随文件一起继承：同名替换（用户重传同一份论文 PDF）
                    # 不该把"它来自哪条在线记录"这条事实弄丢
                    keep_source = existing.source_ref
                logger.info("检测到同名更新：替换文档 #%s（%s）", existing.id, file.name)
                replaced_id = existing.id
            else:
                kept = self._reindex_keep.get(file.name)
                if kept is not None:
                    keep_title, keep_folder, keep_source = (
                        kept.title,
                        kept.folder_id,
                        kept.source_ref,
                    )

        # 显式标题优先级最高（笔记链路），必须压过同名替换继承来的旧标题，
        # 否则"编辑笔记时改标题"永远不生效。放在这里而不是入库后补
        # set_document_title：_chunk_and_persist 用 doc_title 构造索引词空间
        # （《标题》｜标题路径），事后改标题会让"显示名"与"索引里的名字"漂移
        # ——改完名搜不到新名字就是这么来的。
        if title is not None:
            keep_title = title

        copied = False
        displaced: Path | None = None  # 同名替换时被"让位"的旧副本
        if file.resolve() != copy_path.resolve():
            # 源文件就是入库副本（reindex 扫 uploads 的场景）：无需复制，
            # 否则 shutil 会报"同一文件"（真实 bug 的回归见 test_service）
            copy_path.parent.mkdir(parents=True, exist_ok=True)
            # 同名替换：旧副本先**改名让位**（同目录 rename 瞬时完成、不复制
            # 数据），入库成功后才真删。为什么不直接覆盖——旧行在文件就位后
            # 就删掉并提交了，若随后嵌入阶段失败（API 限流/超时/维度不符），
            # 回滚会把新副本清掉：直接覆盖 = **旧内容在磁盘上也不复存在**，
            # 文档在库与磁盘上同时消失。2026-09-11 打包前审查发现：当天
            # "解析提前"只挡住了坏文件，没挡住嵌入失败这条路径。
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
            if replaced_id is not None:
                # 新副本已就位，这才删旧行（顺序纪律见上面"旧行此刻不动"）。
                # 放进 try 是为了让下面的回滚把让位副本放回去：删除失败时
                # 库与盘都得原样（这里是纯 DB 操作，失败基本只剩库损坏）
                with open_db(self.settings.db_path) as conn:
                    repo.delete_document(conn, replaced_id)
                    conn.commit()
            doc_id, chunks, doc_title = self._chunk_and_persist(
                loaded,
                copy_path,
                sha,
                title=keep_title,
                folder_id=keep_folder,
                source_ref=keep_source,
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
            # 成功之后的收尾：**绝不能让它把成功翻成失败**。旧副本若是只读或被别的
            # 程序占着（Windows 文件锁），unlink 会抛 WinError 5/32 —— 原先它裸奔在
            # try 之外，于是"新内容其实已经入库"的请求被翻成 400「入库失败」，用户
            # 重试就多一篇（2026-09-16 对抗性实测）。
            # 删不掉就留着：它是**旧正文**的副本、名字带 .replacing 后缀，
            # 后缀过滤会让 reindex 永远看不到它（纯垃圾，不复活）。
            try:
                displaced.unlink(missing_ok=True)
            except OSError:
                logger.warning("旧副本未能退役（不影响本次入库，可手工删除）：%s", displaced)
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
        # 上次中断留下的映射先装上：本次重建会沿用它把标题/文件夹/标记收回来
        self._reindex_keep = self._load_stale_keep()
        with open_db(self.settings.db_path) as conn:
            docs = repo.list_documents(conn)
            lost = self._docs_without_copies(docs, uploads)
            if lost:
                # **第二道闸（2026-09-20）**：每行都得在 uploads 里找得到副本。
                # 只挡"uploads 全空"不够——清库之后才发现某一行的副本不在，那一行
                # 就再也重建不出来（笔记尤其：uploads 副本是它唯一的副本，文件删了
                # 连原文都没了）。宁可在动手前停下来，让用户先恢复或先显式删掉那几篇。
                names = "\n".join(f"  · {d.title}" for d in lost[:10])
                more = f"\n  …（共 {len(lost)} 篇）" if len(lost) > 10 else ""
                raise ZhiwenError(
                    f"这些文档在 uploads 里找不到副本，reindex 已中止（清库后它们无法重建）：\n"
                    f"{names}{more}\n"
                    f"请先从备份恢复 data/uploads/；若确实不再需要，请在知识库页逐篇删除后再重建。"
                )
            for doc in docs:
                if doc.file_path:
                    # 用文件名做键：file_path 可能是脏历史值，但副本名稳定
                    self._reindex_keep[PureWindowsPath(doc.file_path).name] = _KeptProps(
                        title=doc.title,
                        folder_id=doc.folder_id,
                        source_ref=doc.source_ref,
                    )
            # **落盘必须在删行之前**：清库与重导之间被杀（关窗/Ctrl-C/断电）时，
            # 映射只存在于内存里会随进程消失——重跑 reindex 能把内容收回来，
            # 标题/文件夹/note: 标记却全部回落到默认（2026-09-20 全量审查实测）。
            self._save_keep()
            for doc in docs:
                if doc.id is not None:
                    repo.delete_document(conn, doc.id)
            conn.commit()
        summary = self.ingest_paths([uploads], force=True)
        self._remove_keep()  # 重建成功才删：中途失败要留下现场给下一次收
        self._sync_meta()
        return summary

    # ---- reindex 的组织属性映射：落盘 / 读回（见 _reindex 的说明） ----

    def _keep_path(self) -> Path:
        return self.settings.index_dir / "reindex-keep.json"

    def _save_keep(self) -> None:
        """清库前把组织属性映射写到盘上（tmp + 原子替换）。"""
        path = self._keep_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            name: {"title": p.title, "folder_id": p.folder_id, "source_ref": p.source_ref}
            for name, p in self._reindex_keep.items()
        }
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _load_stale_keep(self) -> dict[str, _KeptProps]:
        """读上次中断留下的映射（没有/读不出来都返回空，不打断本次重建）。"""
        path = self._keep_path()
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            logger.warning("残留的 reindex 属性映射读不出来，忽略：%s", path)
            return {}
        out: dict[str, _KeptProps] = {}
        for name, props in raw.items():
            if isinstance(props, dict):
                out[str(name)] = _KeptProps(
                    title=str(props.get("title", "")),
                    folder_id=props.get("folder_id"),
                    source_ref=props.get("source_ref"),
                )
        if out:
            logger.warning(
                "发现上次未完成 reindex 留下的属性映射（%d 条）："
                "本次重建会沿用它恢复标题/文件夹/标记",
                len(out),
            )
        return out

    def _remove_keep(self) -> None:
        self._keep_path().unlink(missing_ok=True)

    def _docs_without_copies(self, docs: list[Document], uploads: Path) -> list[Document]:
        """哪些文档行在 uploads 里找不到对应副本（reindex 前的安全闸）。

        两跳解析与读侧 `_resolve_upload_file` 同口径：
          ① 按 file_path 的**文件名**比（file_path 本身可能是历史脏值，
             而副本名稳定——与"同名替换判定按名兜底"同一取舍）；
          ② 回退按标题：uploads 下 stem 与标题相同的候选，再用 sha256 验身
             （只看名字不验身，同名不同内容会被误判成"副本还在"）。
        """
        files = [p for p in uploads.iterdir() if p.is_file()] if uploads.is_dir() else []
        names = {p.name for p in files}
        by_stem: dict[str, list[Path]] = {}
        for path in files:
            by_stem.setdefault(path.stem, []).append(path)

        lost: list[Document] = []
        for doc in docs:
            if doc.file_path and PureWindowsPath(doc.file_path).name in names:
                continue
            candidates = by_stem.get(doc.title or "", [])
            if doc.file_sha256 and any(sha256_file(p) == doc.file_sha256 for p in candidates):
                continue
            lost.append(doc)
        return lost

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
        source_ref: str | None = None,
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
            if source_ref is not None:
                # 同上（v4）：只有论文导入会带来源，"已在库中"标记靠它（ADR-0020）
                repo.set_document_source_ref(conn, doc_id, source_ref)
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

    def refresh_title_index(self, doc_id: int, *, new_title: str) -> int:
        """改名后刷新该文档所有 chunk 的 tokens（BM25 词空间里的《标题》前缀）。

        **为什么必须刷新**：tokens 是"入库那一刻"按标题算出来的；只改显示标题的话，
        新名字在 BM25 里零命中、**旧名字仍然命中**（引用卡却显示新名字）。
        同一个用户动作两条路径结果不同：笔记编辑器保存走 `ingest_one(title=...)`
        （正确），树里的「重命名」走 PATCH（原先只改显示名）——2026-09-20 全量审查实测。

        **向量不在此列**：重算向量要整篇重嵌，改名的代价不该那么大。向量侧仍是
        旧标题的表示（这一条如实记在 limitations-and-failures.md）。
        """
        with self._lock, open_db(self.settings.db_path) as conn:
            chunks = repo.chunks_by_document(conn, doc_id)
            rows = [
                (
                    # tokens 在库里是 **JSON 文本**（与 repo.insert_chunks 同口径，
                    # 直接绑 list 会被 sqlite3 拒掉：type 'list' is not supported）
                    json.dumps(
                        self._tokenizer(self._index_text(new_title, c.heading_path, c.content)),
                        ensure_ascii=False,
                    ),
                    c.id,
                )
                for c in chunks
                if c.id is not None
            ]
            if not rows:
                return 0
            conn.executemany("UPDATE chunks SET tokens = ? WHERE id = ?", rows)
            conn.commit()
        self._sync_meta()  # 词空间变了：快照里的指纹跟着变（评测/doctor 都看它）
        logger.info(
            "改名后已刷新索引词空间：doc #%s → 《%s》（%d 块）", doc_id, new_title, len(rows)
        )
        return len(rows)

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

    def sync_meta(self) -> None:
        """公开的 meta.json 同步入口（Web 入库尾链用）。

        为什么需要它：meta.json 原先只在 CLI 路径写（`ingest_paths` / `reindex`），
        而 Web 的上传 / 论文导入 / 笔记保存三条链路直调 `ingest_one`，从不经过
        那里——纯网页建的库没有 meta.json，`doctor` 的"三方一致性"检查第一行就
        静默跳过（缺向量、混模型都发现不了；2026-09-20 全量审查实测）。
        """
        self._sync_meta()

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
