"""评测子系统（M2）：黄金集 → 两阶段指标 → 语义裁判 → 报告落库。

对外主入口：
  EvalRunner.run()        —— 执行一次完整评测（纯计算，不写库）
  render_report()         —— 渲染中文 markdown 报告（CLI 落库用）
  load_questions_yaml()   —— 读人工题本（evals/questions.yaml）
  load_golden()/save_golden() —— 冻结黄金集（evals/golden_set.json）读写

模块分工（各自可独立测试）：
  golden.py   题本与黄金集模型（生命周期见其模块 docstring）
  metrics.py  检索指标公式（手写，不引评测库）
  judge.py    LLM-as-Judge 裁判（双轮位置交换 + 双量尺 + 跨厂商）
  runner.py   编排：指纹校验 → 检索层 → 生成层 → 裁判
  report.py   报告渲染（人读 markdown，与机读 metrics_json 同源）
"""

from __future__ import annotations

from mikasa.eval.golden import (
    GoldenItem,
    GoldenSet,
    QuestionDraft,
    QuestionsYaml,
    load_golden,
    load_questions_yaml,
    save_golden,
)
from mikasa.eval.judge import JudgeVerdict, LLMJudge, NoJudge
from mikasa.eval.metrics import RetrievalMetrics, make_retrieval_metrics, summarize
from mikasa.eval.report import render_report
from mikasa.eval.runner import EvalResult, EvalRunner, ItemRecord, build_judge

__all__ = [
    "EvalResult",
    "EvalRunner",
    "GoldenItem",
    "GoldenSet",
    "ItemRecord",
    "JudgeVerdict",
    "LLMJudge",
    "NoJudge",
    "QuestionDraft",
    "QuestionsYaml",
    "RetrievalMetrics",
    "build_judge",
    "load_golden",
    "load_questions_yaml",
    "make_retrieval_metrics",
    "render_report",
    "save_golden",
    "summarize",
]
