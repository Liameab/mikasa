"""评测端点：启动后台评测（202 + 轮询）、出题、历史/报告读取。

两套题库（2026-09-19 起）：
  builtin —— 仓库内置的人工题库（evals/golden_set.json，题目面向示例语料）；
  auto    —— **用户自己语料**自动生成的题库（数据目录 eval/golden-auto.json，
             由 POST /api/eval/synthesize 生成）。人工题本的锚句是示例语料的
             原文，所以"评我自己的资料"只能靠自动生成。

POST /api/eval/runs 的同步预检语义：题库载入、语料非空、**逐题校验后仍有题
可评**——三项都过才占槽启动；结构性失败即刻 400/404，不占用任务槽。注意预检
**不再比全库指纹**：那是旧设计，往库里加一篇文档就让整份题库作废（范围远大于
风险）。现在只有"标准答案分块确实变了"的那几道题被跳过（报告里写明）。

后台任务推进与落库走 eval.service.run_and_persist（与 CLI 同一编排），
on_item 回调驱动 JobManager 进度；GET /api/eval/jobs/current 每 ~1s 轮询直到
done，再按 run_id 拉报告页数据。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from mikasa.config.settings import Settings, resource_root
from mikasa.errors import EvalError, strip_paths
from mikasa.eval.golden import GoldenSet, check_against_corpus, load_golden
from mikasa.eval.service import run_and_persist
from mikasa.eval.synth import golden_path as synth_golden_path
from mikasa.eval.synth import synthesize_to_disk
from mikasa.index.manager import IndexManager
from mikasa.storage import repo
from mikasa.storage.db import open_db
from mikasa.utils.logging import get_logger
from mikasa.web.deps import get_services, get_settings
from mikasa.web.services import AppServices, EvalJobManager

router = APIRouter(tags=["eval"])

logger = get_logger("web.routers.eval")

# 黄金集默认路径与 CLI eval run 一致（evals/ 与 data/ 都在仓库根，
# 运行时前置：tools/build_golden.py 已冻结题库）
_GOLDEN_DEFAULT = resource_root() / "evals" / "golden_set.json"

# 题库选择器：前端下拉框的取值
_BANKS = ("builtin", "auto")


def _bank_path(settings: Settings, bank: str) -> Path:
    if bank == "auto":
        return synth_golden_path(settings)
    return _GOLDEN_DEFAULT


def _preflight_golden(settings: Settings, bank: str = "builtin") -> GoldenSet:
    """同步预检①：题库存在且能载入（缺失/损坏 → 4xx 即退）。"""
    if bank not in _BANKS:
        choices = "、".join(_BANKS)
        raise HTTPException(status_code=422, detail=f"未知的题库：{bank}（可选 {choices}）")
    path = _bank_path(settings, bank)
    if not path.is_file():
        if bank == "auto":
            raise HTTPException(
                status_code=404,
                detail="还没有为你的资料生成题库：在评测页点「为我的资料生成题库」"
                "（需要 api 或 local 档的真实模型）。",
            )
        raise HTTPException(
            status_code=404,
            detail=f"黄金集不存在：{path}（先 mikasa ingest 导入语料，"
            "再运行 tools/build_golden.py 冻结题库）",
        )
    try:
        return load_golden(path)
    except EvalError as exc:
        raise HTTPException(status_code=400, detail=f"黄金集校验失败:{exc}") from None


def _preflight_corpus(settings: Settings, golden: GoldenSet) -> None:
    """同步预检②：语料非空 + 逐题校验后仍有可评的题。

    **不再比全库指纹**（2026-09-19）：加一篇不相干的文档不该让评测失败。
    只有"所有题的标准答案分块都没了"（语料整体重灌）才立刻 400——那种情况
    跑下去只是在浪费一次 LLM 调用。
    """
    corpus = IndexManager(settings).corpus()
    if corpus.empty:
        raise HTTPException(
            status_code=400, detail="知识库为空：评测前先导入语料（mikasa ingest <目录>）"
        )
    check = check_against_corpus(golden, corpus.content_hashes())
    if not check.golden.answerable:
        raise HTTPException(
            status_code=400,
            detail=f"题库与当前语料完全对不上：{len(check.skipped)} 道可答题的标准答案"
            "分块都不在了。语料被整体重灌过的话，题库需要重建——"
            "内置题库用 tools/build_golden.py，自己的资料用「为我的资料生成题库」。",
        )


def _run_eval_job(settings: Settings, golden: GoldenSet, manager: EvalJobManager) -> None:
    """后台任务体：跑完全程（run_and_persist，与 CLI 同编排）并收尾槽位。

    任何异常（含结构性二次校验失败）都进 manager.fail——错误消息随
    状态给轮询端展示，CLI 语义一致（红字信息不回吞）。失败任务不改
    eval_runs 表：占位行由 run_and_persist 内部插入，未及回填即失败时
    遗留的"无报告"行是历史留痕，列表以 status=incomplete 标注。
    """
    try:
        persisted = run_and_persist(settings, golden, on_item=manager.on_item)
    except Exception as exc:  # noqa: BLE001 - 任务失败也要状态可见
        logger.exception("后台评测任务失败")
        manager.fail(str(exc))
    else:
        manager.finish(persisted.run_id)
        logger.info(
            "后台评测完成：run #%d（%.1fs）", persisted.run_id, persisted.result.latency_sec
        )


@router.post("/api/eval/runs", status_code=202)
def start_eval_run(
    background_tasks: BackgroundTasks,
    golden: str = "builtin",
    services: AppServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
) -> dict:
    """启动一场后台评测。同步预检不过 → 4xx；已有运行中 → 409。

    黄金集与 settings 都是只读纯数据，可安全跨线程传给后台任务。
    `golden` 选题库：builtin（内置示例语料）/ auto（自己资料生成的）。
    """
    bank = _preflight_golden(settings, golden)
    _preflight_corpus(settings, bank)
    manager = services.eval_jobs
    if not manager.start(total=len(bank.items)):
        raise HTTPException(status_code=409, detail="已有评测任务运行中，请稍候再试")
    background_tasks.add_task(_run_eval_job, settings, bank, manager)
    return {
        "message": "评测已开始，后台运行中",
        "golden": golden,
        "job": manager.snapshot(),
    }


@router.get("/api/eval/goldens")
def list_goldens(settings: Settings = Depends(get_settings)) -> dict:
    """可用题库清单（评测页的下拉框）：内置一份 + 自己生成的那份（可能还没有）。"""
    out = []
    for bank in _BANKS:
        path = _bank_path(settings, bank)
        entry: dict = {
            "id": bank,
            "label": "内置示例语料" if bank == "builtin" else "我的资料",
            "available": path.is_file(),
            "items": 0,
            "answerable": 0,
            "unanswerable": 0,
            "source": "",
            "model": "",
            "created": "",
        }
        if path.is_file():
            try:
                loaded = load_golden(path)
            except EvalError as exc:
                entry["error"] = str(exc)
            else:
                entry.update(
                    items=len(loaded.items),
                    answerable=len(loaded.answerable),
                    unanswerable=len(loaded.unanswerable),
                    source=loaded.source,
                    model=loaded.model,
                    created=loaded.created,
                )
        out.append(entry)
    return {"banks": out}


def _run_synth_job(
    settings: Settings, questions: int, unanswerable: int, services: AppServices
) -> None:
    """后台出题任务体：生成 → 落盘 → 收尾状态（异常全部落在状态里）。"""
    manager = services.synth_jobs
    try:
        golden, path = synthesize_to_disk(
            settings,
            questions=questions,
            unanswerable=unanswerable,
            on_progress=manager.on_progress,
        )
    except Exception as exc:  # noqa: BLE001 - 任务失败也要状态可见
        logger.exception("后台出题任务失败")
        manager.fail(strip_paths(str(exc)))
    else:
        manager.finish(
            answerable=len(golden.answerable),
            unanswerable=len(golden.unanswerable),
            gold_path=str(path),
        )
        logger.info(
            "自动题库已生成：%s（可答 %d / 不可答 %d）",
            path.name,
            len(golden.answerable),
            len(golden.unanswerable),
        )


@router.post("/api/eval/synthesize", status_code=202)
def start_synthesize(
    background_tasks: BackgroundTasks,
    questions: int = 24,
    unanswerable: int = 8,
    services: AppServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
) -> dict:
    """为当前语料自动生成题库（202 + 轮询进度）。

    需要真实模型（api / local 档）：离线档的模拟模型不会出题，预检直接 400
    说清楚，不让用户在"跑到一半全是废题"里摸索。
    """
    if settings.llm.backend == "mock":
        raise HTTPException(
            status_code=400,
            detail="自动出题需要真实模型，当前是离线档的模拟模型。"
            "请在「设置 → 模型」里接入 api 或 local 档后再试。",
        )
    if not 1 <= questions <= 200 or not 0 <= unanswerable <= 100:
        raise HTTPException(status_code=422, detail="题量超出范围（可答 1-200，不可答 0-100）")
    manager = services.synth_jobs
    if not manager.start(total=questions + unanswerable):
        raise HTTPException(status_code=409, detail="已有出题任务运行中，请稍候再试")
    background_tasks.add_task(_run_synth_job, settings, questions, unanswerable, services)
    return {"message": "出题已开始，后台运行中", "job": manager.snapshot()}


@router.get("/api/eval/synthesize/status")
def synthesize_status(services: AppServices = Depends(get_services)) -> dict:
    """出题进度（前端 ~1s 轮询）；无任务 → job: null。"""
    return {"job": services.synth_jobs.snapshot()}


@router.get("/api/eval/jobs/current")
def current_job(
    services: AppServices = Depends(get_services),
) -> dict:
    """当前任务状态（前端 1s 轮询）；无任务（含已结束被覆盖）→ job: null。"""
    return {"job": services.eval_jobs.snapshot()}


@router.get("/api/eval/runs")
def list_eval_runs(
    settings: Settings = Depends(get_settings),
) -> dict:
    """历史评测列表（最近优先，轻字段不携带报告正文）。"""
    with open_db(settings.db_path) as conn:
        rows = repo.list_eval_runs(conn, limit=50)
    return {"runs": [_run_summary(r) for r in rows]}


@router.get("/api/eval/runs/{run_id}")
def get_eval_run(
    run_id: int,
    settings: Settings = Depends(get_settings),
) -> dict:
    """单次评测详情：报告 markdown + 解码后的指标快照（报告页数据源）。"""
    with open_db(settings.db_path) as conn:
        row = repo.get_eval_run(conn, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="评测记录不存在")
    run = _run_summary(row)
    # metrics_json 双引号字符串 → 解码为对象；config_json 含配置快照
    # 不返回（报告首部已含 profile/模型信息，避免响应肥大）
    run["metrics"] = json.loads(row["metrics_json"] or "{}")
    run["report_md"] = row["report_md"]
    return {"run": run}


def _run_summary(row: dict) -> dict:
    """eval_runs 行 → 轻量摘要 + 状态推断。

    状态语义：report_md 非空 = 报告已渲染回填 = 整场完成（done）；
    为空 = 占位行（运行中，或崩溃遗留的不完整行）→ incomplete。
    """
    return {
        "id": row["id"],
        "eval_set_name": row["eval_set_name"],
        "created_at": row["created_at"],
        "corpus_sha256": (row["corpus_sha256"] or "")[:12],
        "status": "done" if row["report_md"] else "incomplete",
    }
