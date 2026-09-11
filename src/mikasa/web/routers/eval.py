"""评测端点：启动后台评测（202 + 轮询）与历史/报告读取。

POST /api/eval/runs 的同步预检语义：黄金集载入、语料非空、语料指纹
匹配——三项都过才占槽启动；结构性失败（题库缺失/错配/空库）即刻
400/404，不占用任务槽、不等到后台任务里才失败。后台任务推进与落库
走 eval.service.run_and_persist（与 CLI 同一编排），on_item 回调驱动
JobManager 进度；GET /api/eval/jobs/current 每 ~1s 轮询直到 done，
再按 run_id 拉报告页数据。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from mikasa.config.settings import REPO_ROOT, Settings
from mikasa.errors import EvalError
from mikasa.eval.golden import GoldenSet, load_golden
from mikasa.eval.service import run_and_persist
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
_GOLDEN_DEFAULT = REPO_ROOT / "evals" / "golden_set.json"


def _preflight_golden(settings: Settings) -> GoldenSet:
    """同步预检①：黄金集存在且能载入（缺失/损坏 → 4xx 即退）。"""
    if not _GOLDEN_DEFAULT.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"黄金集不存在：{_GOLDEN_DEFAULT}（先 mikasa ingest 导入语料，"
            "再运行 tools/build_golden.py 冻结题库）",
        )
    try:
        return load_golden(_GOLDEN_DEFAULT)
    except EvalError as exc:
        raise HTTPException(status_code=400, detail=f"黄金集校验失败:{exc}") from None


def _preflight_corpus(settings: Settings, golden: GoldenSet) -> None:
    """同步预检②：语料非空 + 指纹匹配（错配进任务也是失败，不如立刻 400）。

    文案与 eval.runner 的 EvalError 同口径——预检放行后后台任务里的
    二次校验必然通过，只是防御。
    """
    corpus = IndexManager(settings).corpus()
    if corpus.empty:
        raise HTTPException(
            status_code=400, detail="知识库为空：评测前先导入语料（mikasa ingest <目录>）"
        )
    if corpus.sha256 != golden.corpus_sha256:
        raise HTTPException(
            status_code=400,
            detail="语料指纹不匹配：本黄金集冻结于语料 "
            f"{golden.corpus_sha256[:12]}…，当前语料为 {corpus.sha256[:12]}…。"
            "语料增删改后题库即失效——请先运行 tools/build_golden.py 重建。",
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
    services: AppServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
) -> dict:
    """启动一场后台评测。同步预检不过 → 4xx；已有运行中 → 409。

    黄金集与 settings 都是只读纯数据，可安全跨线程传给后台任务。
    """
    golden = _preflight_golden(settings)
    _preflight_corpus(settings, golden)
    manager = services.eval_jobs
    if not manager.start(total=len(golden.items)):
        raise HTTPException(status_code=409, detail="已有评测任务运行中，请稍候再试")
    background_tasks.add_task(_run_eval_job, settings, golden, manager)
    return {
        "message": "评测已开始，后台运行中",
        "job": manager.snapshot(),
    }


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
