"""评测端点测试：同步预检 / 202 后台任务 / 轮询流转 / 409 / 历史与报告。

基调（与全项目一致）：真黄金集文件（仓库根 evals/golden_set.json）只
用于"载入"路径；成功流转类测试 monkeypatch 掉语料指纹预检与
run_and_persist——fake 不真跑 63 题，但真实走"落库占位行 + 进度回调 +
回填报告"三个编排点，让 202→轮询→报告 全链路的 DB 语义真实可信。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from mikasa.storage import repo
from mikasa.storage.db import open_db
from mikasa.web.app import create_app
from mikasa.web.routers import eval as eval_router


def _fake_run_and_persist(settings, golden, *, on_item=None):
    """假评测实现：不跑链路，但把编排的三件事做真——逐题回调推进进度、
    插占位行、回填报告——这样轮询/历史/详情端点拿到的是真 DB 语义。
    """
    if on_item is not None:
        for item in golden.items:
            on_item(item)
    metrics_json = json.dumps({"items": []}, ensure_ascii=False)
    with open_db(settings.db_path) as conn:
        run_id = repo.insert_eval_run(
            conn,
            eval_set_name=golden.name,
            corpus_sha256=golden.corpus_sha256,
            config_json="{}",
            metrics_json=metrics_json,
            report_md="",  # 占位行：真实编排先插行、报告后回填
        )
        repo.update_eval_run_report(conn, run_id, "# 假评测报告\n\n全部通过")
    return SimpleNamespace(
        run_id=run_id,
        report_path=settings.data_dir / "eval-reports" / f"{run_id:04d}-fake.md",
        result=SimpleNamespace(latency_sec=0.5),
    )


# ---------------------------------------------------------------------------
# 同步预检（真实逻辑；语料目录为空 / 语料非冻结指纹 → 4xx 即退）
# ---------------------------------------------------------------------------


def test_preflight_empty_corpus_400(client):
    """空知识库：即刻 400，不占任务槽。"""
    c, _ = client
    resp = c.post("/api/eval/runs")
    assert resp.status_code == 400
    assert "知识库为空" in resp.json()["detail"]


def test_preflight_all_gold_chunks_gone_400(seeded_client, monkeypatch):
    """标准答案分块全部对不上（语料被整体重灌）：即刻 400，别占任务槽白跑一场。

    这条原先是 `test_preflight_fingerprint_mismatch_400`：那时只要全库指纹不同
    就 400——包括"只是往库里加了一篇不相干文档"这种完全正常的使用。2026-09-19
    起改成逐题校验，只有**一道题都评不了**才是结构性失败。
    """
    c, _ = seeded_client
    # 把内置题库里的标准答案分块全部指向不存在的 id，模拟"语料整体重灌"
    import mikasa.web.routers.eval as eval_router
    from mikasa.eval.golden import load_golden

    original = load_golden(eval_router._GOLDEN_DEFAULT)
    stale = original.model_copy(
        update={
            "items": [
                item.model_copy(update={"gold_chunk_ids": [90001], "gold_hashes": ["0" * 64]})
                if item.kind == "answerable"
                else item
                for item in original.items
            ]
        }
    )
    monkeypatch.setattr(eval_router, "_preflight_golden", lambda settings, bank="builtin": stale)
    resp = c.post("/api/eval/runs")
    assert resp.status_code == 400
    assert "完全对不上" in resp.json()["detail"]


def test_preflight_golden_missing_404(client, monkeypatch, tmp_path):
    """黄金集文件缺失：404（先于语料检查）。"""
    monkeypatch.setattr(eval_router, "_GOLDEN_DEFAULT", tmp_path / "无此文件.json")
    c, _ = client
    resp = c.post("/api/eval/runs")
    assert resp.status_code == 404
    assert "黄金集不存在" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 成功流转：202 → 轮询到 done → 历史/详情（指纹预检让路，fake 落库）
# ---------------------------------------------------------------------------


def test_eval_run_flow(client, monkeypatch):
    """202 启动 + 轮询到 done（含 run_id）+ 历史列表 + 详情报告。"""
    monkeypatch.setattr(eval_router, "_preflight_corpus", lambda settings, golden: None)
    monkeypatch.setattr(eval_router, "run_and_persist", _fake_run_and_persist)
    c, _ = client

    resp = c.post("/api/eval/runs")
    assert resp.status_code == 202
    started = resp.json()
    assert started["job"]["status"] == "running"
    assert started["job"]["total"] > 0  # 题量 = 真实黄金集条目数（fake 全推进）
    assert started["job"]["done"] == 0

    # TestClient 下后台任务在 post 返回前已完成 → 轮询即 done
    job = c.get("/api/eval/jobs/current").json()["job"]
    assert job is not None
    assert job["status"] == "done"
    assert job["done"] == job["total"]
    run_id = job["run_id"]
    assert run_id is not None

    # 历史列表：1 条 done；详情带回报告正文与指标快照
    runs = c.get("/api/eval/runs").json()["runs"]
    assert len(runs) == 1
    assert runs[0]["id"] == run_id
    assert runs[0]["status"] == "done"

    detail = c.get(f"/api/eval/runs/{run_id}").json()["run"]
    assert detail["report_md"].startswith("# 假评测报告")
    assert detail["metrics"] == {"items": []}
    assert len(detail["corpus_sha256"]) == 12


def test_eval_job_slot_released_after_done(client, monkeypatch):
    """done 后槽位释放：可再次启动新评测（覆盖旧槽）。"""
    monkeypatch.setattr(eval_router, "_preflight_corpus", lambda settings, golden: None)
    monkeypatch.setattr(eval_router, "run_and_persist", _fake_run_and_persist)
    c, _ = client

    assert c.post("/api/eval/runs").status_code == 202
    second = c.post("/api/eval/runs")
    assert second.status_code == 202
    assert second.json()["job"]["status"] == "running"


def test_eval_jobs_current_empty(client):
    """从未启动过任务：job 为 null（前端据此隐藏进度条）。"""
    c, _ = client
    assert c.get("/api/eval/jobs/current").json()["job"] is None


def test_eval_run_history_detail_404(client):
    c, _ = client
    assert c.get("/api/eval/runs").json()["runs"] == []
    resp = c.get("/api/eval/runs/9999")
    assert resp.status_code == 404
    assert "评测记录不存在" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 409：已有 running 任务时再启动
# ---------------------------------------------------------------------------


def test_eval_run_conflict_409(client, offline_settings, monkeypatch):
    """占槽未释放时再 POST → 409（手工占槽模拟"另一场在跑"）。

    不能靠真后台任务保持 running：TestClient 在 post 返回前就同步跑完
    background。直接对 manager 占槽——等价于后台任务刚启动的瞬间。
    """
    monkeypatch.setattr(eval_router, "_preflight_corpus", lambda settings, golden: None)
    with TestClient(create_app(offline_settings)) as c:
        # 预检让路后手工占槽（跳过 run_and_persist：根本到不了启动那步）
        c.app.state.services.eval_jobs.start(total=63)
        resp = c.post("/api/eval/runs")
        assert resp.status_code == 409
        assert "已有评测任务运行中" in resp.json()["detail"]
        # 槽还在：轮询端如实反映 running
        job = c.get("/api/eval/jobs/current").json()["job"]
        assert job["status"] == "running"
        assert job["total"] == 63


# ---------------------------------------------------------------------------
# 题库选择与自动出题（2026-09-19：评测支持任意语料）
# ---------------------------------------------------------------------------


def test_goldens_lists_both_banks_even_when_auto_is_missing(client):
    """题库清单：内置那份永远在；自动那份没生成过也要列出来。

    不列的话用户根本不知道"评我自己的资料"这条路存在——而它正是这个接口
    存在的理由（人工题本的锚句是示例语料原文，换语料就失效）。
    """
    c, _ = client
    banks = {b["id"]: b for b in c.get("/api/eval/goldens").json()["banks"]}
    assert set(banks) == {"builtin", "auto"}
    assert banks["builtin"]["available"] is True
    assert banks["builtin"]["items"] > 0
    assert banks["builtin"]["source"] == "builtin"
    assert banks["auto"]["available"] is False
    assert banks["auto"]["label"] == "我的资料"


def test_synthesize_is_refused_on_the_mock_profile(client):
    """离线档的模拟模型不会出题：400 + 人话，别让用户跑到一半才发现全是废题。"""
    c, _ = client
    resp = c.post("/api/eval/synthesize")
    assert resp.status_code == 400
    assert "需要真实模型" in resp.json()["detail"]


def test_runs_with_auto_bank_404_before_it_exists(client):
    """还没生成过自动题库时选它 → 404，且告诉用户该点哪里。"""
    c, _ = client
    resp = c.post("/api/eval/runs?golden=auto")
    assert resp.status_code == 404
    assert "生成题库" in resp.json()["detail"]


def test_runs_rejects_unknown_bank(client):
    c, _ = client
    resp = c.post("/api/eval/runs?golden=nope")
    assert resp.status_code == 422
    assert "未知的题库" in resp.json()["detail"]


def test_synthesize_status_starts_empty(client):
    c, _ = client
    assert c.get("/api/eval/synthesize/status").json() == {"job": None}
