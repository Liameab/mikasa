"""评测编排复用层测试：run_and_persist 的落库 / 回填 / 写文件三件事。

CLI（test_eval_cli）与 Web 后台任务（test_eval_api，monkeypatch 本函数）
都调它——这里直接调真实实现验证产物与副作用，CLI 视角与函数视角
各测一层，不重复堆场景。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mikasa.errors import StorageError
from mikasa.eval.golden import GoldenItem, GoldenSet, load_golden, save_golden
from mikasa.eval.service import run_and_persist
from mikasa.index.manager import IndexManager
from mikasa.ingest.service import IngestService
from mikasa.storage import repo
from mikasa.storage.db import open_db

NOTE = """# 深度学习笔记

## 正则化

L2 正则化在损失中加入权重的平方和惩罚项，鼓励小而分散的权重。
"""


def _seed(tmp_path: Path, settings) -> None:
    src = tmp_path / "notes"
    src.mkdir(exist_ok=True)
    (src / "dl.md").write_text(NOTE, encoding="utf-8")
    IngestService(settings).ingest_paths([src])


def _freeze(settings, tmp_path: Path) -> GoldenSet:
    """在隔离语料上冻结 2 题黄金集（1 可答 + 1 不可答），返回内存对象。"""
    corpus = IndexManager(settings).corpus()
    sentence = "L2 正则化在损失中加入权重的平方和惩罚项，鼓励小而分散的权重。"
    cids = [c.id for c in corpus.chunks if sentence in c.content]
    assert len(cids) == 1
    golden = GoldenSet(
        corpus_sha256=corpus.sha256,
        items=[
            GoldenItem(
                id="q1",
                kind="answerable",
                question=sentence,
                difficulty="easy",
                gold_chunk_ids=cids,
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
    save_golden(golden, tmp_path / "golden_set.json")
    return golden


def test_run_and_persist_writes_db_and_report_file(tmp_path, offline_settings):
    _seed(tmp_path, offline_settings)
    golden = _freeze(offline_settings, tmp_path)
    frozen_sha = load_golden(tmp_path / "golden_set.json").corpus_sha256

    persisted = run_and_persist(offline_settings, golden)
    assert persisted.run_id == 1
    assert persisted.result.latency_sec >= 0

    # 落库：一行记录，指纹与黄金集一致，report_md 已回填
    with open_db(offline_settings.db_path) as conn:
        rows = repo.list_eval_runs(conn)
    assert len(rows) == 1
    assert rows[0]["eval_set_name"] == "mikasa-golden"
    assert rows[0]["corpus_sha256"] == frozen_sha
    assert rows[0]["report_md"] == persisted.report_md  # 回填完成（≠ 占位空串）
    metrics = json.loads(rows[0]["metrics_json"])
    assert metrics["generation"]["refusal_accuracy"] == 1.0  # q2 拒答干净

    # 报告文件与内存产物一致（Web 报告页渲染的就是 report_md）
    assert persisted.report_path.is_file()
    assert persisted.report_path.read_text(encoding="utf-8") == persisted.report_md
    assert str(persisted.report_path).endswith("0001-mikasa-golden.md")


def test_run_and_persist_propagates_on_item(tmp_path, offline_settings):
    """on_item 透传给 runner：Web 进度轮询依赖它逐题推进。"""
    _seed(tmp_path, offline_settings)
    golden = _freeze(offline_settings, tmp_path)
    seen: list[str] = []
    run_and_persist(offline_settings, golden, on_item=lambda rec: seen.append(rec.id))
    assert seen == ["q1", "q2"]  # 阶段 B/C 每题一次，顺序与题本一致


def test_run_and_persist_empty_corpus_raises(tmp_path, offline_settings):
    empty_golden = GoldenSet(corpus_sha256="不会匹配任何语料", items=[])
    with pytest.raises(StorageError, match="知识库为空"):
        run_and_persist(offline_settings, empty_golden)
