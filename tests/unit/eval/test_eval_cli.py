"""eval CLI 测试：run/list 命令的注册、成功路径与两类失败路径。

沿用 test_cli.py 的隔离手法：monkeypatch cli.load_settings 注入
隔离数据目录的 offline settings（不触碰仓库 data/），黄金集 JSON
由"语料入库 → 锚句解析 → save"小助手在 tmp 下现造。
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

import mikasa.cli as cli
from mikasa.cli import app
from mikasa.config.settings import load_settings
from mikasa.eval.golden import GoldenItem, GoldenSet, save_golden
from mikasa.index.manager import IndexManager
from mikasa.ingest.service import IngestService
from mikasa.storage import repo
from mikasa.storage.db import open_db

runner = CliRunner()

NOTE = """# 深度学习笔记

## 正则化

L2 正则化在损失中加入权重的平方和惩罚项，鼓励小而分散的权重。

## 早停

验证集损失不再下降时停止训练，这是成本最低的防过拟合手段。
"""


def _isolate(tmp_path: Path, monkeypatch) -> object:
    """CLI 模块内 load_settings → 隔离数据目录的 offline settings。"""
    settings = load_settings("offline", data_dir=tmp_path / "data")
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: settings)
    return settings


def _seed(settings, tmp_path: Path) -> None:
    src = tmp_path / "notes"
    src.mkdir(exist_ok=True)
    (src / "dl.md").write_text(NOTE, encoding="utf-8")
    IngestService(settings).ingest_paths([src])


def _resolve_one(anchor: str, chunks) -> int:
    hits = [c.id for c in chunks if anchor in c.content]
    assert len(hits) == 1, f"锚句命中 {len(hits)} 个块"
    return hits[0]


def _freeze(settings, tmp_path: Path) -> Path:
    """在隔离语料上冻结小型黄金集 JSON，返回文件路径。"""
    corpus = IndexManager(settings).corpus()
    chunks = corpus.chunks
    sentence = "L2 正则化在损失中加入权重的平方和惩罚项，鼓励小而分散的权重。"
    golden = GoldenSet(
        corpus_sha256=corpus.sha256,
        items=[
            GoldenItem(
                id="q1",
                kind="answerable",
                question=sentence,
                difficulty="easy",
                gold_chunk_ids=[_resolve_one(sentence, chunks)],
                notes="L2→权重收缩。",
            ),
            GoldenItem(
                id="q2",
                kind="unanswerable",
                question="推荐一部动画片。",
                reason="unrelated",
                notes="语料无关。",
            ),
        ],
    )
    golden.validate_items()
    path = tmp_path / "golden_set.json"
    save_golden(golden, path)
    return path


def test_eval_help_lists_run_and_list():
    result = runner.invoke(app, ["eval", "--help"])
    assert result.exit_code == 0
    assert "run" in result.output and "list" in result.output


def test_eval_run_full_flow_writes_db_and_report(tmp_path, monkeypatch):
    settings = _isolate(tmp_path, monkeypatch)
    _seed(settings, tmp_path)
    golden_path = _freeze(settings, tmp_path)

    result = runner.invoke(app, ["eval", "run", "--golden", str(golden_path)])
    assert result.exit_code == 0
    assert "开始评测" in result.output
    assert "run #1" in result.output

    # 落库：eval_runs 一行，report_md 落库且写文件到 data/eval-reports/
    with open_db(settings.db_path) as conn:
        runs = repo.list_eval_runs(conn)
    assert len(runs) == 1
    assert runs[0]["eval_set_name"] == "mikasa-golden"
    frozen = json.loads(golden_path.read_text(encoding="utf-8"))
    assert runs[0]["corpus_sha256"] == frozen["corpus_sha256"]  # 落库指纹与黄金集一致
    assert "## 阶段A" in (runs[0]["report_md"] or "")
    metrics = json.loads(runs[0]["metrics_json"])
    assert metrics["generation"]["refusal_accuracy"] == 1.0  # q2 拒答干净

    report_file = settings.data_dir / "eval-reports" / "0001-mikasa-golden.md"
    assert report_file.is_file()
    assert report_file.read_text(encoding="utf-8").startswith("# Mikasa评测报告")


def test_eval_run_missing_golden_exits_1_with_hint(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    result = runner.invoke(app, ["eval", "run", "--golden", str(tmp_path / "nope.json")])
    assert result.exit_code == 1
    assert "黄金集不存在" in result.output
    assert "build_golden" in result.output  # 给出可执行修复指引


def test_eval_run_survives_unrelated_corpus_change(tmp_path, monkeypatch):
    """库里多了不相干的文档，评测照跑（2026-09-19 起逐题校验取代全库指纹）。

    这条原先是 `test_eval_run_fingerprint_mismatch_exits_1`：那时往库里加一篇
    笔记就让整份题库作废、CLI 红字退出——而真正的风险只涉及题目引用的那几个
    分块，守卫范围远大于风险范围。
    """
    settings = _isolate(tmp_path, monkeypatch)
    _seed(settings, tmp_path)
    golden_path = _freeze(settings, tmp_path)
    extra = tmp_path / "notes" / "extra.md"
    extra.write_text("# 新文档\n\n新增一段与题库无关的内容。\n", encoding="utf-8")
    IngestService(settings).ingest_paths([extra.parent])

    result = runner.invoke(app, ["eval", "run", "--golden", str(golden_path)])
    assert result.exit_code == 0, result.output
    assert "recall@5" in result.output


def test_eval_list_empty_then_after_run(tmp_path, monkeypatch):
    settings = _isolate(tmp_path, monkeypatch)
    _seed(settings, tmp_path)
    golden_path = _freeze(settings, tmp_path)

    empty = runner.invoke(app, ["eval", "list"])
    assert empty.exit_code == 0
    assert "还没有评测记录" in empty.output

    runner.invoke(app, ["eval", "run", "--golden", str(golden_path)])
    listing = runner.invoke(app, ["eval", "list"])
    assert listing.exit_code == 0
    assert "评测历史（共 1 次）" in listing.output
    assert "mikasa-golden" in listing.output
