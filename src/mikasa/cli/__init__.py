"""Mikasa 命令行入口（typer + rich）。

命令一览：
  init    初始化数据目录与环境检查引导
  doctor  环境体检（依赖/密钥/索引一致性），CI 冒烟用
  ingest / list / index stats / ask / chat   —— M1 提供
  eval    评测体系                                 —— M2 提供
  serve   Web 服务                                 —— M3 提供
"""

from __future__ import annotations

import contextlib
import json
import sys
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Literal

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from mikasa import __version__
from mikasa.config.settings import (
    VALID_PROFILES,
    Settings,
    load_dotenv_file,
    load_settings,
    resource_root,
)
from mikasa.errors import ConfigError, EvalError, ZhiwenError
from mikasa.ingest.service import IngestService, IngestSummary
from mikasa.providers.ollama import fetch_ollama_tags as _fetch_ollama_tags
from mikasa.providers.ollama import (
    ollama_api_root as _ollama_api_root,  # noqa: F401 - 兼容别名（单测按旧名引用）
)
from mikasa.utils.logging import default_log_file, get_logger, setup_logging

app = typer.Typer(
    name="mikasa",
    help="Mikasa —— 带引用溯源与自动化评测的个人学习知识库问答系统",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    rich_markup_mode="rich",
)

logger = get_logger("cli")

PROFILE_OPT = Annotated[
    str,
    typer.Option(
        "--profile",
        "-p",
        help=f"运行 profile（{' / '.join(VALID_PROFILES)}）",
        show_default=True,
    ),
]

CONFIG_OPT = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="显式指定配置文件（绕过默认查找链）"),
]

# 问答模式：字面量字面写在顶层（不 import pipeline）——--help 保持轻量，
# 合法值由服务层 ConfigError 兜底（见 ADR-0013）
MODE_OPT = Annotated[
    Literal["kb", "free"],
    typer.Option("--mode", "-m", help="kb=知识库检索（缺省）；free=直连 LLM 自由作答"),
]


@app.command()
def init(
    profile: PROFILE_OPT = "api",
    config: CONFIG_OPT = None,
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="数据目录（默认 <仓库>/data）"),
    ] = None,
) -> None:
    """初始化数据目录并生成引导说明。"""
    settings = load_settings(profile, config, data_dir=data_dir)
    settings.ensure_dirs()
    from rich.console import Console

    console = Console()
    console.print(f"[bold green]✔[/] 数据目录就绪：{settings.data_dir}")
    console.print(f"[bold green]✔[/] 当前 profile：[cyan]{settings.profile}[/]")
    console.print(f"[bold green]✔[/] LLM：{settings.llm.backend} / {settings.llm.model}")
    console.print(
        f"[bold green]✔[/] Embedding：{settings.embedding.backend} / {settings.embedding.model}"
    )
    console.print(
        "\n[bold]下一步：[/]\n"
        "  1. 编辑 .env 填入密钥（复制 .env.example，仅 api profile 需要）\n"
        "  2. 导入语料：  mikasa ingest <文件或目录>\n"
        '  3. 提问：      mikasa ask "你的问题" --show-sources\n'
        "  4. 体检：      mikasa doctor\n"
        "  5. Web 界面：  mikasa serve\n"
        '\n零密钥体验请用：mikasa ask --profile offline "……"'
    )


# ---------------------------------------------------------------------------
# doctor 本地探测函数（模块级：单测可 monkeypatch；doctor 从不真连外部服务）
# ---------------------------------------------------------------------------

# `_fetch_ollama_tags` / `_ollama_api_root` 已迁 providers/ollama.py（顶部 import，
# 以 `as` 保留原名）：Web 设置面板也要列本机模型，被 web 依赖的代码不能住在
# cli 里。doctor 调用与单测的 monkeypatch.setattr(cli, ...) 行为不变。


def _check_fastembed_import() -> None:
    """fastembed 可导入性检查。

    不实例化 TextEmbedding——构造即触发模型下载（~100MB），doctor 的
    职责只到"依赖在位"，下载交给首次 ingest/提问的运行时。
    """
    try:
        from fastembed import TextEmbedding  # noqa: F401
    except ImportError as exc:
        raise RuntimeError('缺少 fastembed（本地嵌入）：pip install -e ".[local]"') from exc


def _check_index_consistency(settings: Settings) -> None:
    """doctor 索引检查本体：快照 × 库内向量 × 当前配置三方一致。

    embeddings 表 chunk_id 主键 → 每库单模型语义；切 embedding 模型
    （api bge-m3 1024 维 / local bge-small 512 维）不重嵌会让 dense
    静默降级或查询崩溃，前置拦截在体检（见 ADR-0014 维度迁移纪律）。
    offline（backend=none）无向量路，一致性由 ingest 语义保证，不深究。
    模块级函数：单测直调可精确断言各错配形态（经 rich 表格会断行）。
    """
    from mikasa.storage.db import open_db
    from mikasa.storage.index_files import load_index_meta
    from mikasa.storage.repo import count_chunks, embedding_models_in_db

    meta = load_index_meta(settings)
    if meta is None:
        return  # 尚未建库不算错误
    if not settings.db_path.is_file():
        raise RuntimeError("索引文件存在但 mikasa.db 缺失——请重跑 ingest")
    if settings.embedding.backend == "none":
        return

    expected = settings.embedding.model
    with open_db(settings.db_path) as conn:
        counts = embedding_models_in_db(conn)  # model → 向量行数
        rows = counts.get(expected, 0)
        chunk_n = count_chunks(conn)
        dims = {int(r["dim"]) for r in conn.execute("SELECT DISTINCT dim FROM embeddings")}

    if not counts:
        raise RuntimeError("库内没有任何向量，但索引快照已存在——请执行 mikasa ingest --reindex")
    if meta.get("embedding_model") != expected:
        raise RuntimeError(
            f"索引快照的 embedding 模型（{meta.get('embedding_model')}）≠ 当前配置"
            f"（{expected}）：不同模型维度不可混用——请执行 mikasa ingest --reindex"
        )
    if set(counts) != {expected}:
        raise RuntimeError(
            f"库内混有非当前模型的残留向量（{counts}）——请执行 mikasa ingest --reindex"
        )
    if rows != chunk_n:
        raise RuntimeError(
            f"当前模型的向量行数（{rows}）≠ chunk 数（{chunk_n}）：存在缺向量或"
            "残留重复——请执行 mikasa ingest --reindex"
        )
    if len(dims) != 1:
        raise RuntimeError(f"库内向量维度不单一（{sorted(dims)}）——请执行 mikasa ingest --reindex")
    if meta.get("embedding_dim") is not None and dims != {meta["embedding_dim"]}:
        raise RuntimeError(
            f"快照维度（{meta['embedding_dim']}）≠ 库内维度（{dims.pop()}）"
            "——请执行 mikasa ingest --reindex"
        )


@app.command()
def doctor(
    profile: PROFILE_OPT = "api",
    config: CONFIG_OPT = None,
) -> None:
    """环境体检：版本 / 依赖 / 密钥 / 本地推理依赖 / 索引一致性。"""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    console.print(f"[bold]Mikasa {__version__} — 环境体检（profile={profile}）[/]\n")

    results: list[tuple[str, bool, str]] = []

    def check(name: str, fn: Callable[[], object], critical: bool = False) -> None:
        try:
            fn()
            results.append((name, True, "OK"))
        except Exception as exc:  # noqa: BLE001 - 体检要收集全部失败
            results.append((name, False, f"{type(exc).__name__}: {exc}"))

    # 1. Python 版本（requires-python>=3.11 已在安装时保证下限；这里只拦未验证的上限）
    def _py() -> None:
        if sys.version_info >= (3, 14):
            raise RuntimeError("尚未验证 Python 3.14，建议使用 3.11-3.13")

    check("Python 版本", _py, critical=True)

    # 2. 依赖可导入性（doctor 是硬门禁：任一项失败都会汇总红行并 exit 1，
    #    "告警"语义由失败文案承载——缺失提示装什么、去哪装）
    def _jieba() -> None:
        # 与运行时同一探针入口（内部已静音 jieba 的 pkg_resources 弃用告警）
        from mikasa.index.tokenizer import get_tokenizer_name

        if get_tokenizer_name() != "jieba":
            raise RuntimeError(
                "jieba 不可用，分词已降级为 bigram（见 docs/limitations-and-failures.md）"
            )

    def _pymupdf() -> None:
        import pymupdf  # noqa: F401

    def _docx() -> None:
        import docx  # noqa: F401

    def _numpy() -> None:
        import numpy  # noqa: F401

    check("jieba（中文分词）", _jieba)
    check("PyMuPDF（PDF 解析）", _pymupdf)
    check("python-docx（DOCX 解析）", _docx)
    check("numpy（向量检索）", _numpy)

    # 3. 配置加载
    settings = load_settings(profile, config)
    check("配置加载", lambda: settings.ensure_dirs())

    # 4. 密钥（脱敏显示；三档各有合格线：mock 无需 / local 免密钥 / api 严格，
    #    见 ADR-0014——Ollama /v1 不校验 Authorization，local 从密钥检查豁免）
    def _key() -> None:
        if not settings.llm.api_key:
            raise RuntimeError(f"缺少 {settings.llm.api_key_env or 'API key'}（见 .env.example）")

    if settings.llm.backend == "mock":
        results.append(("LLM 密钥", True, "mock 模式无需密钥"))
    elif settings.llm.backend == "local":
        results.append(("LLM 密钥", True, "local（Ollama）免密钥：兼容端点接受任意占位 key"))
    else:
        check("LLM 密钥", _key)

    def _embed_key() -> None:
        if not settings.embedding.api_key:
            raise RuntimeError(
                f"缺少 {settings.embedding.api_key_env or 'API key'}（见 .env.example）"
            )

    if settings.embedding.backend == "api":
        check("Embedding 密钥", _embed_key)
    elif settings.embedding.backend == "local":
        results.append(("Embedding 密钥", True, "local（fastembed CPU）无需密钥"))
    else:
        results.append(("Embedding 密钥", True, "none 模式无需密钥（纯 BM25 路）"))

    # 5. 本地推理依赖（按 backend 门控：仅 local 才查 Ollama/fastembed，
    #    offline/api 零感知。探测走上面的模块级函数，单测可整体替换）
    if settings.llm.backend == "local":

        def _ollama_base_url() -> str:
            # base_url 配置可缺省为 None：local 探测前先行收窄，
            # 空值本身就是配置错误（指向修配置而非连服务）
            base_url = settings.llm.base_url
            if not base_url:
                raise RuntimeError(
                    "配置缺 base_url（应为 http://localhost:11434/v1）——请检查 profile"
                )
            return base_url

        def _ollama_model() -> None:
            models = _fetch_ollama_tags(_ollama_base_url())
            if settings.llm.model not in models:
                installed = "、".join(models) if models else "无"
                raise RuntimeError(
                    f"模型 {settings.llm.model} 尚未拉取（当前已装：{installed}）。\n"
                    f"  请执行：ollama pull {settings.llm.model}"
                )

        check("Ollama 服务连通", lambda: _fetch_ollama_tags(_ollama_base_url()))
        check("Ollama 模型已拉取", _ollama_model)
    if settings.embedding.backend == "local":
        check("fastembed（本地嵌入）", _check_fastembed_import)

    # 6. 数据目录可写
    def _writable() -> None:
        probe = settings.data_dir / ".write_probe"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()

    check("数据目录可写", _writable)

    # 7. 索引一致性：快照 × 库内向量 × 当前配置三方对齐（探测本体
    #    _check_index_consistency 在模块级，见上；这里只包 check 契约）
    def _index() -> None:
        _check_index_consistency(settings)

    check("索引一致性", _index)

    table = Table(title="检查结果")
    table.add_column("检查项")
    table.add_column("状态")
    table.add_column("说明")
    for name, ok, detail in results:
        table.add_row(name, "[green]✔[/]" if ok else "[red]✘[/]", detail)
    console.print(table)

    failed = [r for r in results if not r[1]]  # (name, ok, detail)：元组索引易错，勿改顺序
    if failed:
        console.print(
            f"\n[red]共 {len(failed)} 项未通过。[/] 排查建议：\n"
            "  - 密钥问题：在仓库根目录执行  copy .env.example .env  后填写\n"
            '  - 依赖问题：pip install -e ".[local]"（本地推理需要）\n'
            "  - 不接任何模型时请使用：mikasa doctor --profile offline"
        )
        raise typer.Exit(code=1)
    console.print("\n[bold green]全部通过 ✔[/]")


# ---------------------------------------------------------------------------
# M1：文档管理 / 索引状态 / 问答
# ---------------------------------------------------------------------------


def _print_summary(console: Console, summary: IngestSummary) -> None:
    """入库汇总的表格输出。"""
    table = Table(title="导入结果")
    table.add_column("状态")
    table.add_column("数量")
    table.add_row("新导入/更新", str(len(summary.ingested)))
    table.add_row("跳过（内容未变）", str(len(summary.skipped)))
    table.add_row("失败", f"[red]{len(summary.failed)}[/]")
    table.add_row("新增块数", str(summary.chunks_added))
    console.print(table)
    for file, reason in summary.failed:
        # 文件名/原因走 Text：含小写方括号（如 notes[ab].md）会被 rich 当标记吞字
        line = Text("  ✘ ", style="red")
        line.append(f"{file}\n     {reason}")
        console.print(line)
    if summary.failed:
        console.print("[red]部分文件导入失败，详见上方原因（密钥/格式/网络问题）。[/]")


@app.command()
def ingest(
    files: Annotated[
        list[Path] | None,
        typer.Argument(
            help="要导入的文件或目录（可多个；目录递归扫描）。缺省 = 增量重扫 data/uploads"
        ),
    ] = None,
    reindex: Annotated[
        bool,
        typer.Option("--reindex", help="全量重建：清空语料后重导 data/uploads"),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="强制重导（忽略'内容未变'跳过）"),
    ] = False,
    profile: PROFILE_OPT = "api",
    config: CONFIG_OPT = None,
) -> None:
    """导入资料并建索引（md/txt/pdf/docx，重复导入自动跳过/替换）。"""
    console = Console()
    settings = load_settings(profile, config)
    service = IngestService(settings)

    if reindex:
        if files:
            console.print("[yellow]--reindex 会全量重建，忽略多余路径参数，仅重导 uploads[/]")
        summary = service.reindex()
    else:
        summary = service.ingest_paths(files or [settings.uploads_dir], force=force)
    _print_summary(console, summary)
    if summary.failed:
        raise typer.Exit(code=1)


@app.command(name="list")
def list_documents(
    profile: PROFILE_OPT = "api",
    config: CONFIG_OPT = None,
) -> None:
    """列出已入库文档（最近优先）。"""
    from mikasa.storage import repo
    from mikasa.storage.db import open_db

    console = Console()
    settings = load_settings(profile, config)
    with open_db(settings.db_path) as conn:
        docs = repo.list_documents(conn)
    if not docs:
        console.print("[yellow]知识库为空。导入资料：mikasa ingest <文件或目录>[/]")
        return
    table = Table(title=f"文档列表（共 {len(docs)} 篇）")
    for col in ("ID", "标题", "类型", "字符", "块数", "状态", "更新时间"):
        table.add_column(col)
    for doc in docs:
        table.add_row(
            str(doc.id),
            Text(doc.title),  # 标题是用户数据：含 [x] 会被 rich markup 吞字
            doc.file_type,
            f"{doc.char_count:,}",
            str(doc.chunk_count),
            doc.ingest_status,
            (doc.updated_at or "")[:19],
        )
    console.print(table)


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="问题（引号包裹）")],
    show_sources: Annotated[
        bool, typer.Option("--show-sources", "-s", help="同时打印命中的资料片段")
    ] = False,
    mode: MODE_OPT = "kb",
    profile: PROFILE_OPT = "api",
    config: CONFIG_OPT = None,
) -> None:
    """单轮提问：kb=基于知识库回答（无据拒答）；free=直连 LLM 自由作答。

    free 模式跳过检索直接使用接入的模型（api / local profile），
    回答不带引用、不拒答——offline（mock）下会报错提示。
    """
    from mikasa.pipeline.ask import AskService

    console = Console()
    settings = load_settings(profile, config)
    try:
        answer = AskService(settings).ask(question, mode=mode)
    except ZhiwenError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from exc
    _print_answer(console, answer, show_sources=show_sources, mode=mode)


def _print_answer(console: Console, answer, *, show_sources: bool, mode: str = "kb") -> None:
    """答案 + 引用表（ask / chat 共用；mode 控制 kb 专属文案不误伤 free）。

    LLM 正文与引用 snippet 一律经 Text 输出（不做 rich markup 解析）：
    论文/代码类回答常含 arr[i]、[x] 这类方括号，rich 会把它们当标记**静默
    吞掉**（2026-09-11 实测："用 arr[i] 表示" 渲染成 "用 arr 表示"，
    "paper[i]notes.md" 渲染成 "papernotes.md"），落库文本正确、只有屏幕失真。
    """
    console.print(Panel(Text(answer.text), title=f"Mikasa（{answer.model}）", border_style="green"))
    if answer.refused:
        console.print("[dim]（已拒答：资料中找不到可支撑答案的依据）[/]")
        return
    # free 无引用是常态：黄色"格式异常"警示只属于 kb（kb 的引用是纪律）
    if mode == "kb" and not answer.citations:
        console.print("[yellow]（注意：回答中未检出引用标记——格式解析见评测报告）[/]")
    for citation in answer.citations:
        section = f"｜{citation.section}" if citation.section else ""
        page = f"（第 {citation.page} 页）" if citation.page else ""
        head = Text("  ")
        head.append(f"[{citation.marker}]", style="cyan")
        head.append(f" {citation.document_title}{section}{page}")
        console.print(head)
        snippet = citation.snippet[:100] + ("…" if len(citation.snippet) > 100 else "")
        console.print(Text("      " + snippet, style="dim"))
    if show_sources:
        console.print("\n[bold]本次检索命中的资料片段：[/]")
        for citation in answer.citations:
            console.print(Text(f"── [{citation.marker}] 引用的原始块 snippet 见上", style="dim"))
    lat = answer.latency_ms
    if lat:
        if "retrieve" in lat:  # kb：检索/重排/生成三段（逐字口径不变）
            # "共"不能 sum(全部段)：generate 从**检索前**开始计时，已含
            # retrieve/rerank/translate，直接相加会重复计数（2026-09-11 修正）
            total = lat.get("generate", 0.0) + lat.get("translate_answer", 0.0)
            console.print(
                f"[dim]延迟：检索 {lat.get('retrieve', 0):.0f}ms / "
                f"重排 {lat.get('rerank', 0):.0f}ms / 生成 {lat.get('generate', 0):.0f}ms"
                f"（共 {total:.0f}ms）[/]"
            )
        else:  # free：仅生成段（latency 无 retrieve 键即 free，见 ADR-0013）
            console.print(f"[dim]延迟：生成 {lat.get('generate', 0):.0f}ms[/]")


@app.command()
def chat(
    mode: MODE_OPT = "kb",
    profile: PROFILE_OPT = "api",
    config: CONFIG_OPT = None,
) -> None:
    """多轮对话：kb 带历史摘要（无据拒答）；free 直连 LLM 自由问答。

    模式同 ask 的 --mode；quit/exit/退出 结束。
    """
    from mikasa.pipeline.ask import AskService
    from mikasa.storage import repo
    from mikasa.storage.db import open_db

    console = Console()
    settings = load_settings(profile, config)
    try:  # free+mock 在首轮就拦（先于建会话），早失败不落审计轨迹
        service = AskService(settings)
        service._guard_mode(mode)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from exc
    with open_db(settings.db_path) as conn:
        session_id = repo.create_session(conn, settings.profile)
    banner = "[bold]Mikasa对话模式[/]（Ctrl+C 或输入 quit 退出）"
    if mode == "free":
        banner += "[dim] · free 自由问答（不引用知识库）[/]"
    console.print(banner + "\n")
    try:
        while True:
            question = console.input("[cyan]你 > [/]").strip()
            if not question or question.lower() in ("quit", "exit", "退出"):
                break
            try:
                answer = service.chat(session_id, question, mode=mode)
            except ZhiwenError as exc:
                console.print(f"[red]{exc}[/]")
                break
            _print_answer(console, answer, show_sources=False, mode=mode)
    except (KeyboardInterrupt, EOFError):
        console.print("\n再见 👋")


# ---- eval 子命令组（M2：run 执行评测 / list 查看历史） ----

_eval_app = typer.Typer(
    help="自动化评测：黄金集 → 检索层/生成层指标 + 语义裁判（offline 零密钥可跑）",
    no_args_is_help=True,
)
app.add_typer(_eval_app, name="eval")


@_eval_app.command()
def run(
    golden: Annotated[
        Path,
        typer.Option("--golden", help="黄金集 JSON（缺省 evals/golden_set.json）"),
    ] = resource_root() / "evals" / "golden_set.json",
    profile: PROFILE_OPT = "api",
    config: CONFIG_OPT = None,
) -> None:
    """执行一次完整评测并落库 eval_runs（报告同时写入 data/eval-reports/）。

    前置：语料已导入 + 黄金集已冻结（tools/build_golden.py）。
    语料变动只跳过受影响的那几道题（报告里逐条写明）；全部可答题都对不上
    才会报错并提示重建题库。
    """
    from mikasa.eval.golden import load_golden
    from mikasa.eval.service import run_and_persist

    console = Console()
    settings = load_settings(profile, config)

    # ---- 载入黄金集（不存在/校验失败给可执行提示） ----
    try:
        golden_set = load_golden(golden)
    except EvalError as exc:
        console.print(f"[red]{exc}[/]")
        console.print(
            "[yellow]若还没有黄金集：先 mikasa ingest 导入语料，"
            "再运行 tools/build_golden.py 冻结题库。[/]"
        )
        raise typer.Exit(code=1) from exc
    console.print(
        f"[bold]开始评测[/] 黄金集=[cyan]{golden_set.name}[/]"
        f" 题量={len(golden_set.items)}（可答 {len(golden_set.answerable)}"
        f"/ 不可答 {len(golden_set.unanswerable)}） profile={settings.profile}"
    )

    # ---- 编排复用层：执行 → 落库 → 渲染回填 → 写报告 ----
    # （cli 与 Web 后台任务共用；题库全对不上/空库在此翻译为红字退出）
    try:
        persisted = run_and_persist(settings, golden_set)
    except ZhiwenError as exc:
        # 捕到基类：评测阶段 A/B 会真的调 LLM/嵌入，上游失败（密钥错/限流/
        # Ollama 未启动）抛 ProviderError——它不在 EvalError/StorageError 的
        # 兄弟分支里，原先会漏成裸 traceback（2026-09-11 修复）
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from exc
    result = persisted.result
    run_id = persisted.run_id
    report_path = persisted.report_path

    # ---- 控制台摘要（直接读聚合容器，与报告同口径） ----
    from mikasa.eval.metrics import summarize

    def fmt_mean(stats: dict[str, float | int] | None) -> str:
        """summarize 结果 → "均值（p50）"文本；空样本显示 —。"""
        if not stats or stats["n"] == 0:
            return "—"
        return f"{float(stats['mean']):.3f}（p50 {float(stats['p50']):.3f}）"

    gen = result.generation
    table = Table(title=f"评测完成（run #{run_id}，{result.latency_sec:.1f}s）")
    table.add_column("指标")
    table.add_column("值")
    table.add_row("recall@5 均值", fmt_mean(summarize(result.retrieval.recall[5].values)))
    table.add_row("recall@10 均值", fmt_mean(summarize(result.retrieval.recall[10].values)))
    table.add_row("MRR 均值", fmt_mean(summarize(result.retrieval.rr.values)))
    cg = summarize(gen.citation_gold.values) if gen.citation_gold.values else None
    table.add_row("citation gold ratio", fmt_mean(cg))
    accuracy = gen.refusal_accuracy()
    table.add_row(
        "拒答准确率",
        f"{gen.refusal_clean}/{gen.n_unanswerable}"
        + (f" = {accuracy:.1%}" if accuracy is not None else ""),
    )
    judge_text = (
        f"判 {result.judge.judged}，一致 {result.judge.consistent}"
        f"/ 不一致 {result.judge.inconsistent}"
        if result.judge.judge_model != "none"
        else "（未启用 → 仅协议层指标）"
    )
    table.add_row("语义裁判", f"{result.judge.judge_model} {judge_text}")
    table.add_row("完整报告", str(report_path))
    console.print(table)
    if gen.failed:
        console.print(
            f"[red]{gen.failed} 题生成链路失败[/]（见报告异常明细）；本次结果不完整，"
            "建议修复后重跑。"
        )
        raise typer.Exit(code=1)
    if gen.answered_unanswerable or gen.refused_answerable:
        console.print("[yellow]存在拒答纪律问题（见报告'异常明细'），建议人工复核。[/]")


@_eval_app.command(name="list")
def eval_list(
    profile: PROFILE_OPT = "api",
    config: CONFIG_OPT = None,
) -> None:
    """列出历史评测（eval_runs，最近优先）。"""
    from mikasa.storage import repo
    from mikasa.storage.db import open_db

    console = Console()
    settings = load_settings(profile, config)
    with open_db(settings.db_path) as conn:
        runs = repo.list_eval_runs(conn)
    if not runs:
        console.print("[yellow]还没有评测记录。运行：mikasa eval run --profile offline[/]")
        return
    table = Table(title=f"评测历史（共 {len(runs)} 次）")
    for col in ("ID", "黄金集", "语料指纹", "生成模型", "裁判", "时间"):
        table.add_column(col)
    for row in runs:
        try:
            config_data = json.loads(row["config_json"] or "{}")
            metrics_data = json.loads(row["metrics_json"] or "{}")
        except json.JSONDecodeError:
            config_data, metrics_data = {}, {}
        llm_model = str((config_data.get("llm") or {}).get("model", "?"))
        judge_model = str((metrics_data.get("judge") or {}).get("judge_model", "?"))
        table.add_row(
            str(row["id"]),
            Text(str(row["eval_set_name"])),  # 同文档列表：用户数据一律走 Text
            (row["corpus_sha256"] or "")[:12] + "…",
            Text(llm_model),
            Text(judge_model),
            (row["created_at"] or "")[:19],
        )
    console.print(table)


@_eval_app.command(name="compare")
def eval_compare(
    before: Annotated[int, typer.Argument(help="基准运行 id（`eval list` 里的 ID）")],
    after: Annotated[int, typer.Argument(help="对比运行 id")],
    profile: PROFILE_OPT = "api",
    config: CONFIG_OPT = None,
) -> None:
    """配对比较两次评测的检索层（同题库逐题配对 + bootstrap 置信区间）。

    **为什么必须配对**：两次独立跑出来的均值差会被题间方差淹没——63 题里
    几道难题的正常波动，比一次检索改动的真实效果还大。配对只看"同一道题
    变了多少"，62 道没变的题自动抵消，剩下的是改动本身。

    **怎么读**：区间不跨 0（`显著` 列）才算"这次改动有据可依"；跨 0 就是
    "题库这个样本量还看不出来"。区间只对**这一批题**成立，它不是物理定律。

    前置：两次运行都得是 2026-09-25 之后跑的（那之前的 metrics_json 没有
    逐题留痕，配不了对）——缺了会直接说明，不猜。
    """
    from mikasa.eval.metrics import paired_bootstrap
    from mikasa.storage import repo
    from mikasa.storage.db import open_db

    console = Console()
    settings = load_settings(profile, config)
    with open_db(settings.db_path) as conn:
        runs = {rid: repo.get_eval_run(conn, rid) for rid in (before, after)}
    found: dict[int, dict[str, Any]] = {}
    for rid, row in runs.items():
        if row is None:
            console.print(f"[red]没有 id={rid} 的评测记录。先跑 `mikasa eval list` 看有哪些。[/]")
            raise typer.Exit(code=1)
        found[rid] = row

    # 逐题留痕是配对的唯一依据（形状：题号 → {recall_at: {k: v}, ndcg_at: {...}, rr: v}）
    items: dict[int, dict[str, dict[str, Any]]] = {}
    for rid, row in found.items():
        try:
            metrics_data = json.loads(row["metrics_json"] or "{}")
        except json.JSONDecodeError:
            metrics_data = {}
        per_item = (metrics_data.get("retrieval") or {}).get("items") or {}
        if not per_item:
            console.print(
                f"[yellow]运行 {rid} 没有逐题检索留痕（2026-09-25 之前的运行没有），"
                "重新跑一次即可。[/]"
            )
            raise typer.Exit(code=1)
        items[rid] = per_item

    shared = sorted(set(items[before]) & set(items[after]))
    if len(shared) < 5:
        console.print(f"[red]两次运行共同的可答题只有 {len(shared)} 道，配不出有意义的结论。[/]")
        raise typer.Exit(code=1)

    ks = sorted(
        {int(k) for k in items[before][shared[0]]["recall_at"]}
        & {int(k) for k in items[after][shared[0]]["recall_at"]}
    )
    if ks != sorted({int(k) for k in items[after][shared[0]]["recall_at"]}):
        console.print("[yellow]警告：两次运行的 k 档不同，只比较共同档位。[/]")

    def series(run_id: int, pick: Callable[[dict[str, Any]], float]) -> list[float]:
        return [pick(items[run_id][qid]) for qid in shared]

    # (指标名, 取值的键路径)：三个指标方向一致（越大越好），所以渲染可以共用一套颜色
    def recall_of(record: dict[str, Any], k: int) -> float:
        return float(record["recall_at"][str(k)])

    metrics_spec: list[tuple[str, Callable[[dict[str, Any]], float]]] = [
        (f"recall@{k}", partial(recall_of, k=k)) for k in ks
    ]
    metrics_spec.append(("MRR", lambda record: float(record["rr"])))

    title = f"配对比较：#{before} → #{after}（{len(shared)} 道共同题）"
    table = Table(title=title)
    for col in ("指标", f"#{before} 均值", f"#{after} 均值", "差值", "95% CI（差值）", "显著"):
        table.add_column(col)
    for label, pick in metrics_spec:
        old, new = series(before, pick), series(after, pick)
        result = paired_bootstrap(old, new)
        if result is None:  # pragma: no cover - shared 已保证非空且等长
            continue
        lo, hi, delta = float(result["lo"]), float(result["hi"]), float(result["delta"])
        # 变好绿、变差红、看不出来黄——三个指标都是越大越好，所以一套配色够
        style = "yellow" if not result["significant"] else ("green" if delta > 0 else "red")
        table.add_row(
            label,
            f"{sum(old) / len(old):.3f}",
            f"{sum(new) / len(new):.3f}",
            Text(f"{delta:+.3f}", style=style),
            Text(f"[{lo:+.3f}, {hi:+.3f}]", style=style),
            Text("是" if result["significant"] else "否（跨 0）", style=style),
        )
    console.print(table)
    console.print(
        "[dim]口径：逐题配对 + bootstrap 2000 次重采样（种子固定，同数据同结果）；"
        "区间读作「若另抽一批同分布的题，差值均值大概落在哪」。[/]"
    )


# ---- index 子命令组（index stats / index rebuild 由 ingest --reindex 覆盖） ----
_auth_app = typer.Typer(
    help="访问口令（开给局域网/公网前必须设；见 ADR-0033）", no_args_is_help=True
)
app.add_typer(_auth_app, name="auth")


@_auth_app.command(name="set-password")
def auth_set_password(
    profile: PROFILE_OPT = "api",
    config: CONFIG_OPT = None,
) -> None:
    """设置（或更换）访问口令：换一次即让所有旧会话立刻失效。

    口令写在数据目录的 auth.json（PBKDF2-HMAC-SHA256 加盐哈希，不存明文）。
    **绑定了非回环地址（--host 0.0.0.0）时没设口令会拒绝启动**——局域网里
    任何人都能读你的资料、花你的额度，这道门是必须的。
    """
    import getpass

    from mikasa.web import auth

    settings = load_settings(profile, config)
    console = Console()
    console.print("[bold]设置 Mikasa 访问口令[/]（只存本机数据目录，不存明文）")
    first = getpass.getpass("输入口令：")
    if not first:
        console.print("[red]口令不能为空。[/]")
        raise typer.Exit(code=1)
    if len(first) < 6:
        console.print("[red]口令太短[/]：局域网里这是唯一一道门，至少 6 位。")
        raise typer.Exit(code=1)
    again = getpass.getpass("再输一次：")
    if first != again:
        console.print("[red]两次输入不一致，未改动。[/]")
        raise typer.Exit(code=1)
    auth.set_password(settings.data_dir, first)
    console.print(
        f"[green]已设置[/]（{auth.auth_file(settings.data_dir)}）\n"
        "现在可以： [bold]mikasa serve --host 0.0.0.0[/]，"
        "同网络的手机/电脑用浏览器打开服务地址，输入这道口令即可使用。"
    )


@_auth_app.command(name="clear-password")
def auth_clear_password(
    profile: PROFILE_OPT = "api",
    config: CONFIG_OPT = None,
) -> None:
    """删掉访问口令（之后非回环绑定会被拒绝启动；本机自用不受影响）。"""
    from mikasa.web import auth

    settings = load_settings(profile, config)
    console = Console()
    if auth.clear_password(settings.data_dir):
        console.print("[green]已删除访问口令。[/]本机自用不受影响。")
    else:
        console.print("本来就没有设过口令。")


_index_app = typer.Typer(
    help="索引状态与维护（stats 查看；重建用 ingest --reindex）", no_args_is_help=True
)
app.add_typer(_index_app, name="index")


@_index_app.command(name="stats")
def index_stats(
    profile: PROFILE_OPT = "api",
    config: CONFIG_OPT = None,
) -> None:
    """索引状态：文档/块数、分词器、双路索引、语料指纹。"""
    from mikasa.index.manager import IndexManager
    from mikasa.index.tokenizer import get_tokenizer_name
    from mikasa.storage import repo
    from mikasa.storage.db import open_db

    console = Console()
    settings = load_settings(profile, config)
    with open_db(settings.db_path) as conn:
        doc_count = repo.count_documents(conn)
    corpus = IndexManager(settings).corpus()

    table = Table(title="索引状态")
    table.add_column("项目")
    table.add_column("值")
    table.add_row("文档数", str(doc_count))
    table.add_row("chunk 数", str(len(corpus.chunks)))
    table.add_row("分词器", get_tokenizer_name())
    table.add_row("BM25 词表大小", f"{corpus.bm25.vocabulary_size:,}")
    table.add_row("向量模型", corpus.embedding_model or "（未启用）")
    if corpus.matrix is not None:
        table.add_row("向量规模", f"{corpus.matrix.shape[0]:,} × {corpus.matrix.shape[1]}")
    table.add_row("语料指纹", corpus.sha256[:16] + "…")
    table.add_row("数据目录", str(settings.data_dir))
    console.print(table)
    if corpus.matrix is None and settings.embedding.backend != "none":
        console.print(
            "[yellow]注意：dense 向量缺失（离线后端 / 中途失败）。重导：mikasa ingest --reindex[/]"
        )
    console.print(
        f"[dim]profile={settings.profile}｜chunk_size={settings.chunking.size}｜"
        f"top_k={settings.retrieval.fusion_top_k}｜reranker={settings.reranker.model}[/]"
    )


# ---- serve 子命令（M3：Web 界面，三页全功能） ----


@app.command()
def serve(
    # 默认 None（而非 "api"）只为区分"显式传了 --profile"：reload 子进程拿不到
    # 显式档位，必须拒绝而不是静默跑成另一档（见下方拦截）
    profile: Annotated[
        str | None,
        typer.Option("--profile", "-p", help=f"运行 profile（{' / '.join(VALID_PROFILES)}）"),
    ] = None,
    config: CONFIG_OPT = None,
    host: Annotated[
        str | None, typer.Option("--host", help="监听地址（默认取配置 web.host）")
    ] = None,
    port: Annotated[
        int | None, typer.Option("--port", help="监听端口（默认取配置 web.port）")
    ] = None,
    reload: Annotated[
        bool,
        typer.Option(
            "--reload",
            help="开发热重载：改动 src/ 自动重启（uvicorn reload 模式，限本机调试）",
        ),
    ] = False,
) -> None:
    """启动 Web 服务，浏览器打开 http://127.0.0.1:8000/ 使用四个页面
    （问答 / 知识库 / 找论文 / 评测；绑非回环地址时还会多一个登录页）。

    profile 语义与 CLI 其余命令一致：默认 api（真实 DeepSeek/SiliconFlow），
    零密钥演示用 --profile offline（MockLLM）。接口文档（Swagger）在 /docs。

    --reload 是开发选项，与 --config / --profile 均不兼容（热重载子进程只能
    按默认查找链加载配置），显式传入会被拒绝而不是静默跑错档。

    单进程约束：Web 进程内缓存（索引快照 / 评测任务槽）都是内存态，
    多 worker 会各自为政——serve 固定单进程，勿配 workers。
    """
    import uvicorn

    from mikasa.storage import repo
    from mikasa.storage.db import open_db
    from mikasa.web.app import create_app

    console = Console()

    # reload 与 --config 互斥：reload 走 import string（子进程重新 import
    # serve_app_factory），父进程内存里的显式 config 对象无法传给子进程，
    # 子进程只会按默认查找链加载——静默不一致不如显式拒绝（须先于
    # load_settings，否则无效 config 路径的报错会截断这条提示）
    if reload and config is not None:
        console.print(
            "[red]--reload 与 --config 不兼容[/]：热重载要求配置走默认查找链"
            "（config/config.yaml 或 profiles/<profile>.yaml）。"
        )
        raise typer.Exit(code=1)

    # 同理拦截显式 --profile：reload 子进程通过 import string 重新 import
    # serve_app_factory，那里只能按默认查找链加载（config.yaml 或 api 档），
    # 父进程内存里的显式档位传不过去——不拦就会"横幅写着 offline、实际按 api
    # 真实调用 DeepSeek/SiliconFlow"，offline 的零外部调用承诺被静默打破
    # （2026-09-11 排查发现，与 --config 属同一类问题）。
    if reload and profile is not None:
        console.print(
            "[red]--reload 与 --profile 不兼容[/]：热重载子进程只能按默认查找链"
            "加载配置（config/config.yaml 或默认 api 档），显式档位传不进子进程。"
            "需要指定档位请去掉 --reload。"
        )
        raise typer.Exit(code=1)

    settings = load_settings(profile or "api", config)
    listen_host = host or settings.web.host
    listen_port = port or settings.web.port
    # **把生效的监听地址写回 settings**：口令门（ADR-0033）与横幅都读
    # `settings.web.host`，而 --host 只在本地变量里——不写回就会出现
    # "命令行开了 0.0.0.0、门却以为还在本机"的空门（2026-09-21 实测踩到）。
    if listen_host != settings.web.host or listen_port != settings.web.port:
        settings = settings.model_copy(
            update={
                "web": settings.web.model_copy(update={"host": listen_host, "port": listen_port})
            }
        )

    # ---- 口令闸门（ADR-0033）：**没设口令就不许开给局域网** ----
    # 理由：这类绑定之后，同一网络里任何人都能读/删你的资料、花你的 API 额度
    # （密钥就在服务端配置里）。宁可拒绝启动、给一条能照做的命令，也不默认裸奔。
    from mikasa.web import auth as auth_module

    if not auth_module.is_loopback_host(listen_host) and not auth_module.is_configured(
        settings.data_dir
    ):
        console.print(
            "[red]绑定了非本机地址，但还没有设置访问口令[/]——\n"
            "  这样同一网络里的任何人都能读你的知识库、删你的文档，"
            "还能用你配置的模型额度。\n\n"
            "  先设口令再启动： [bold]mikasa auth set-password[/]\n"
            "  只想本机自用：去掉 --host（默认 127.0.0.1，不需要口令）。"
        )
        raise typer.Exit(code=1)

    # ---- 横幅：配置实况 + 访问地址（uvicorn 日志前的第一屏） ----
    with open_db(settings.db_path) as conn:
        doc_count = repo.count_documents(conn)
    url_host = "127.0.0.1" if listen_host in ("0.0.0.0", "::") else listen_host
    console.print(
        Panel(
            f"[bold]profile[/]  {settings.profile}\n"
            f"[bold]LLM[/]      {settings.llm.backend} / {settings.llm.model}\n"
            f"[bold]Embedding[/] {settings.embedding.backend} / {settings.embedding.model}\n"
            f"[bold]语料[/]     {doc_count} 篇文档已入库\n\n"
            f"[green]问答页[/] http://{url_host}:{listen_port}/\n"
            f"[green]接口文档[/] http://{url_host}:{listen_port}/docs",
            title="[bold]Mikasa · Web 界面[/]",
            border_style="green",
            subtitle="Ctrl+C 停止服务",
        )
    )

    # ws="none"：本应用没有 WebSocket 端点（流式走 SSE）。不关掉的话 uvicorn
    # 启动时要 import websockets 并加载协议实现，任何一份不完整的 websockets
    # 都会把服务直接打崩——v0.1.6 打包版实测（升级残留的空壳目录，见 limitations §四）。
    if reload:
        # reload 模式：import string + factory——uvicorn 的热重载子进程
        # 通过字符串重新 import，闭包/实例都会在子进程里失效
        uvicorn.run(
            "mikasa.web.app:serve_app_factory",
            factory=True,
            host=listen_host,
            port=listen_port,
            reload=True,
            ws="none",
        )
    else:
        uvicorn.run(create_app(settings), host=listen_host, port=listen_port, ws="none")


def _ensure_console_encoding() -> None:
    """控制台编码防御：非 UTF-8 终端下打印中文会直接把命令打断。

    英文 Windows 的控制台是 cp1252，rich 往里写中文或 ✔/✘ 会抛
    UnicodeEncodeError（2026-09-11 CI 实测：windows-latest 的 CLI 冒烟就是
    这么挂的 —— 真实用户在英文系统上跑 doctor/ask 同样会崩）。
    这里**不换编码**，只把错误策略改成 replace：中文 Windows（cp936）显示
    照旧，非中文终端退化成问号，但命令能跑完、退出码正确。
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(OSError, ValueError):  # 流已被替换/关闭
                stream.reconfigure(errors="replace")


def main() -> None:  # noqa: D103
    _ensure_console_encoding()
    # 文件日志是窗口形态（无控制台）唯一的排障入口，始终挂上。
    # 必须 force=True：本模块顶部的 `logger = get_logger("cli")` 在**导入时**
    # 就触发过一次无参 setup_logging()，把 _CONFIGURED 置了真——不 force 的话
    # 这里会被幂等守卫直接 return，FileHandler 永远挂不上（实测：data/logs 与
    # %LOCALAPPDATA%\Mikasa\logs 一直是空目录，而打包版弹窗还让用户去看它）。
    setup_logging(log_file=default_log_file(), force=True)
    load_dotenv_file()
    try:
        app()
    except ZhiwenError as exc:
        # 顶层兜底：business 错误（配置非法/找不到 profile/密钥缺失）给一句中文，
        # 不再吐二十行 typer 调用栈。命令内部已自行处理的不受影响（不会走到这）。
        Console().print(f"[red]{exc}[/]")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
