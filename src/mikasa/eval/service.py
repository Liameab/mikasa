"""评测编排复用层：CLI 与 Web 后台任务共用的"跑完并落库"。

原来这段编排只活在 cli eval run 命令里（runner.run → insert_eval_run →
render_report → 回填 → 写报告文件）。M3 起 Web 的"开始评测"后台任务
也要同一套流程，于是下沉到这里：CLI 负责打印摘要，Web 负责进度轮询，
两者都调 run_and_persist()。

golden 由调用方载入再传入（不在本层 load）：CLI 要先打"开始评测"
横幅、Web 要在 POST 时同步校验（指纹失配立刻 400 而不是任务里失败）——
各自 load 一次即免重复。GoldenSet 是纯数据对象，跨线程传递安全。

失败语义：结构性失败（指纹失配、空库）抛 EvalError / StorageError，
由调用方决定呈现方式；单题失败是评测的正常组成部分，由 EvalRunner
逐条留痕（record.error），不影响整场收尾。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from mikasa.config.settings import Settings
from mikasa.eval.golden import GoldenSet
from mikasa.eval.report import render_report
from mikasa.eval.runner import EvalResult, EvalRunner, ItemRecord
from mikasa.storage import repo
from mikasa.storage.db import open_db


@dataclass(frozen=True)
class PersistedEvalRun:
    """run_and_persist 的产物：CLI 摘要打印与 Web 响应的公共数据。"""

    run_id: int
    created_at: str
    result: EvalResult
    report_md: str
    report_path: Path


def run_and_persist(
    settings: Settings,
    golden: GoldenSet,
    *,
    on_item: Callable[[ItemRecord], None] | None = None,
) -> PersistedEvalRun:
    """执行一次完整评测：落库 eval_runs + 写报告文件，返回产物。

    on_item：阶段 B/C 每完成一题回调一次（逐条留痕）——Web 后台任务的
    进度推进点；CLI 不传。

    **失败不留行**：`run()` 全程跑完才插 eval_runs（插行时才拿得到
    run_id，报告文件名要用它），所以结构性失败（题库全对不上、空库、
    上游密钥错）一行都不会写进历史——失败现场在**作业状态**里如实回显，
    事后追溯靠日志。占位行（`report_md=""`）只存在于"已插行、报告还没
    渲染完"的那个窗口，由 Web 轮询渲染成"评测中"。
    """
    settings.ensure_dirs()

    result = EvalRunner(settings, golden).run(on_item=on_item)

    created_at = datetime.now().isoformat(timespec="seconds")
    metrics_json = json.dumps(result.to_metrics_json(), ensure_ascii=False)
    config_json = json.dumps(settings.model_dump(mode="json"), ensure_ascii=False)
    with open_db(settings.db_path) as conn:
        run_id = repo.insert_eval_run(
            conn,
            eval_set_name=golden.name,
            corpus_sha256=golden.corpus_sha256,
            config_json=config_json,
            metrics_json=metrics_json,
            report_md="",  # 占位：报告拿到 run_id 后渲染再回填
        )

    report_md = render_report(settings, golden, result, run_id=run_id, created_at=created_at)
    with open_db(settings.db_path) as conn:
        repo.update_eval_run_report(conn, run_id, report_md)

    reports_dir = settings.data_dir / "eval-reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"{run_id:04d}-{golden.name}.md"
    report_path.write_text(report_md, encoding="utf-8")

    return PersistedEvalRun(
        run_id=run_id,
        created_at=created_at,
        result=result,
        report_md=report_md,
        report_path=report_path,
    )
