"""CLI 冒烟测试：八个命令的参数解析、输出契约与失败路径。

CLI 是薄壳（typer → service），核心逻辑已被下层单测覆盖，
这里锁住"用户能跑通"的部分：命令注册、退出码、关键词输出、
空库/无密钥等失败路径。数据目录通过 monkeypatch load_settings
注入隔离目录，不触碰仓库 data/。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import mikasa.cli as cli
from mikasa.cli import app
from mikasa.config.settings import load_settings

runner = CliRunner()

NOTE = """# 机器学习笔记

## 正则化

L2 正则化在损失中加入权重的平方和惩罚项，鼓励小而分散的权重。
"""


def _seed(tmp_path: Path) -> Path:
    src = tmp_path / "notes"
    src.mkdir()
    (src / "n.md").write_text(NOTE, encoding="utf-8")
    return src


def _isolate(cli: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI 模块内 load_settings → 隔离数据目录的 offline settings。"""
    settings = load_settings("offline", data_dir=tmp_path / "data")
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: settings)


def test_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("init", "doctor", "ingest", "list", "ask", "chat", "index"):
        assert cmd in result.output


def test_init_creates_dirs_and_prints_profile(tmp_path):
    result = runner.invoke(app, ["init", "--profile", "offline", "--data-dir", str(tmp_path / "d")])
    assert result.exit_code == 0
    assert "数据目录就绪" in result.output
    assert (tmp_path / "d").is_dir()  # ensure_dirs 已执行


def test_doctor_offline_all_green(tmp_path, monkeypatch):
    _isolate(cli, tmp_path, monkeypatch)
    result = runner.invoke(app, ["doctor", "--profile", "offline"])
    assert result.exit_code == 0
    assert "全部通过" in result.output
    assert "mock 模式无需密钥" in result.output


def test_doctor_missing_key_exits_1(tmp_path, monkeypatch):
    """api profile 无密钥 → 检查失败且退出码 1（体检的失败契约）。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    api_settings = load_settings("api", data_dir=tmp_path / "data")
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: api_settings)
    result = runner.invoke(app, ["doctor", "--profile", "api"])
    assert result.exit_code == 1
    assert "未通过" in result.output and "密钥" in result.output


# ---------------------------------------------------------------------------
# doctor local：Ollama 连通 / 模型已拉取 / fastembed（探测函数被
# monkeypatch 替换，不真连本机 Ollama——单测契约：分支判定 + 文案指引）
# ---------------------------------------------------------------------------


def _isolate_local(cli: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI 模块内 load_settings → 隔离数据目录的 local settings。"""
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    settings = load_settings("local", data_dir=tmp_path / "data")
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: settings)


def test_doctor_local_all_green(tmp_path, monkeypatch):
    """Ollama 在位 + 模型已拉取 + fastembed 可用 → 全绿 exit 0。"""
    _isolate_local(cli, tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_fetch_ollama_tags", lambda base_url: ["qwen3:8b"])
    monkeypatch.setattr(cli, "_check_fastembed_import", lambda: None)
    result = runner.invoke(app, ["doctor", "--profile", "local"])
    assert result.exit_code == 0
    assert "全部通过" in result.output
    assert "免密钥" in result.output  # 密钥行：local 免密钥绿行
    assert "Ollama 服务连通" in result.output
    assert "Ollama 模型已拉取" in result.output
    assert "fastembed（本地嵌入）" in result.output


def test_doctor_local_ollama_down_exits_1(tmp_path, monkeypatch):
    """服务不可达 → 服务连通红行（含逃生口指引）+ exit 1。"""
    _isolate_local(cli, tmp_path, monkeypatch)

    def _down(base_url):
        raise RuntimeError(
            "Ollama 服务不可达（http://localhost:11434/api/tags）："
            "连接被拒绝。可设 OLLAMA_BASE_URL=http://127.0.0.1:11434/v1 后重跑"
        )

    monkeypatch.setattr(cli, "_fetch_ollama_tags", _down)
    result = runner.invoke(app, ["doctor", "--profile", "local"])
    assert result.exit_code == 1
    assert "未通过" in result.output
    assert "Ollama 服务连通" in result.output
    assert "127.0.0.1" in result.output


def test_doctor_local_model_missing_exits_1(tmp_path, monkeypatch):
    """服务在位但模型未拉取 → 红行带 ollama pull 指引 + exit 1。"""
    _isolate_local(cli, tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_fetch_ollama_tags", lambda base_url: ["qwen3:4b"])
    monkeypatch.setattr(cli, "_check_fastembed_import", lambda: None)
    result = runner.invoke(app, ["doctor", "--profile", "local"])
    assert result.exit_code == 1
    assert "Ollama 模型已拉取" in result.output
    # 红行详情在 rich 表格里会按列宽软换行（CJK 可断行点不确定），
    # 只断言 ASCII 指引（不会被换行拆散）与整行语义
    assert "ollama pull qwen3:8b" in result.output


def test_doctor_local_fastembed_missing_exits_1(tmp_path, monkeypatch):
    """嵌入依赖缺失 → fastembed 红行（安装指引）+ exit 1（模型行仍绿）。"""
    _isolate_local(cli, tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_fetch_ollama_tags", lambda base_url: ["qwen3:8b"])

    def _missing():
        raise RuntimeError('缺少 fastembed（本地嵌入）：pip install -e ".[local]"')

    monkeypatch.setattr(cli, "_check_fastembed_import", _missing)
    result = runner.invoke(app, ["doctor", "--profile", "local"])
    assert result.exit_code == 1
    assert "fastembed（本地嵌入）" in result.output
    assert "pip install -e" in result.output
    assert "Ollama 模型已拉取" in result.output  # 前置行不受牵连


def test_ollama_api_root_derivation():
    """base_url → API 根推导：去 /v1、容忍尾斜杠、非 /v1 直接追加。"""
    from mikasa.cli import _ollama_api_root

    assert _ollama_api_root("http://localhost:11434/v1") == "http://localhost:11434/api"
    assert _ollama_api_root("http://h:11434/v1/") == "http://h:11434/api"
    assert _ollama_api_root("http://h:11434") == "http://h:11434/api"


def test_fetch_ollama_tags_parses_models(monkeypatch):
    """成功路径：urlopen 到 /api/tags，解析出模型名列表。"""
    import json
    import urllib.request

    from mikasa.cli import _fetch_ollama_tags

    captured: dict[str, object] = {}
    payload = json.dumps({"models": [{"name": "qwen3:8b"}, {"name": "qwen3:4b"}]})

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return payload.encode("utf-8")

    def _fake_urlopen(url, timeout):
        captured["url"] = url
        assert timeout == 3  # 本地探测超时须短
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    assert _fetch_ollama_tags("http://localhost:11434/v1") == ["qwen3:8b", "qwen3:4b"]
    assert captured["url"] == "http://localhost:11434/api/tags"


def test_fetch_ollama_tags_connection_failure_translated(monkeypatch):
    """连接失败 → RuntimeError：含 URL 与 Windows 逃生口指引（非裸异常）。"""
    import urllib.error
    import urllib.request

    from mikasa.cli import _fetch_ollama_tags

    def _boom(url, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    with pytest.raises(RuntimeError, match="Ollama 服务不可达"):
        _fetch_ollama_tags("http://localhost:11434/v1")
    with pytest.raises(RuntimeError, match="127.0.0.1"):
        _fetch_ollama_tags("http://localhost:11434/v1")


def test_check_fastembed_import_missing(monkeypatch):
    """fastembed 不可导入 → RuntimeError（安装指引），不看本机安装状态。"""
    import sys

    from mikasa.cli import _check_fastembed_import

    monkeypatch.setitem(sys.modules, "fastembed", None)
    with pytest.raises(RuntimeError, match="pip install"):
        _check_fastembed_import()


# ---------------------------------------------------------------------------
# doctor 索引一致性：探测本体直调（无 rich 断行干扰）+ E2E 退出码契约
# ---------------------------------------------------------------------------


def _isolate_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """CLI 模块内 load_settings → 隔离数据目录的 api settings（假密钥）。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek-0000")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-test-silicon-0000")
    settings = load_settings("api", data_dir=tmp_path / "data")
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: settings)
    return settings


def _seed_indexed_db(
    settings,
    *,
    chunk_n=3,
    counts=None,
    dim=1024,
    meta_model="BAAI/bge-m3",
    meta_dim=None,
):
    """手工造"已建库"状态：schema 上的 documents/chunks/embeddings + 快照。

    counts 形如 {"BAAI/bge-m3": 2}（model → 行数）：不足 chunk_n 时自动补
    chunk（embeddings 挂真实 chunk，受 FK 约束）。向量行数/模型分布/
    快照模型/维度均可注入，覆盖 doctor 索引一致性的各错配形态。
    """
    import numpy as np

    from mikasa.models.document import Chunk, Document
    from mikasa.storage import repo
    from mikasa.storage.db import open_db
    from mikasa.storage.index_files import save_index_meta

    if counts is None:
        counts = {"BAAI/bge-m3": chunk_n}
    total = sum(counts.values())
    need = max(total, chunk_n)
    with open_db(settings.db_path) as conn:
        doc_id = repo.insert_document(
            conn,
            Document(
                title="样本",
                file_path="样本.md",
                file_type="md",
                file_sha256="sha",
                char_count=1,
            ),
        )
        repo.insert_chunks(
            conn,
            [
                Chunk(document_id=doc_id, seq=i, content=f"块{i}", content_sha256=f"s{i}")
                for i in range(need)
            ],
        )
        ids = repo.chunk_ids_of_document(conn, doc_id)
        cursor = 0
        for model, n in counts.items():
            repo.save_embeddings(
                conn, model, ids[cursor : cursor + n], np.zeros((n, dim), dtype=np.float32)
            )
            cursor += n
    save_index_meta(
        settings,
        chunk_count=need,
        embedding_model=meta_model,
        embedding_dim=dim if meta_dim is None else meta_dim,
        corpus_sha256="deadbeef",
    )


def test_index_consistency_green_when_aligned(api_settings):
    """库/快照/配置三方对齐 → 不抛（doctor 索引行绿）。"""
    from mikasa.cli import _check_index_consistency

    _seed_indexed_db(api_settings)
    _check_index_consistency(api_settings)


def test_index_consistency_no_vectors_raises(api_settings):
    from mikasa.cli import _check_index_consistency

    _seed_indexed_db(api_settings, counts={})
    with pytest.raises(RuntimeError, match="没有任何向量"):
        _check_index_consistency(api_settings)


def test_index_consistency_meta_model_mismatch_raises(api_settings):
    """快照模型 ≠ 配置模型：切了 embedding 模型未 reindex 的形态。"""
    from mikasa.cli import _check_index_consistency

    _seed_indexed_db(api_settings, meta_model="BAAI/bge-small-zh-v1.5")
    with pytest.raises(RuntimeError, match="≠ 当前配置") as exc:
        _check_index_consistency(api_settings)
    assert "reindex" in str(exc.value)


def test_index_consistency_mixed_residue_raises(api_settings):
    """库内混嵌（部分迁移残留）：两种模型向量并存。"""
    from mikasa.cli import _check_index_consistency

    _seed_indexed_db(api_settings, counts={"BAAI/bge-m3": 2, "BAAI/bge-small-zh-v1.5": 1})
    with pytest.raises(RuntimeError, match="残留向量"):
        _check_index_consistency(api_settings)


def test_index_consistency_missing_vectors_raises(api_settings):
    """向量行数 < chunk 数（增量导入后未重嵌的形态）。"""
    from mikasa.cli import _check_index_consistency

    _seed_indexed_db(api_settings, counts={"BAAI/bge-m3": 2})  # chunk_n=3
    with pytest.raises(RuntimeError, match="chunk 数"):
        _check_index_consistency(api_settings)


def test_index_consistency_mixed_dims_raise(api_settings):
    """同模型下维度不单一（手工损坏/迁移事故残留）。"""
    from mikasa.cli import _check_index_consistency
    from mikasa.storage.db import open_db

    _seed_indexed_db(api_settings)
    with open_db(api_settings.db_path) as conn:
        conn.execute(
            "UPDATE embeddings SET dim = 512 WHERE chunk_id = "
            "(SELECT MIN(chunk_id) FROM embeddings)"
        )
    with pytest.raises(RuntimeError, match="维度不单一"):
        _check_index_consistency(api_settings)


def test_index_consistency_meta_dim_mismatch_raises(api_settings):
    """快照维度与库内维度不一致（meta 陈旧）。"""
    from mikasa.cli import _check_index_consistency

    _seed_indexed_db(api_settings, meta_dim=512)
    with pytest.raises(RuntimeError, match="快照维度"):
        _check_index_consistency(api_settings)


def test_doctor_index_consistent_exits_0(tmp_path, monkeypatch):
    """E2E：索引一致时 doctor api 全绿 exit 0。"""
    settings = _isolate_api(tmp_path, monkeypatch)
    _seed_indexed_db(settings)
    result = runner.invoke(app, ["doctor", "--profile", "api"])
    assert result.exit_code == 0
    assert "索引一致性" in result.output


def test_doctor_index_inconsistent_exits_1(tmp_path, monkeypatch):
    """E2E：错配形态经 rich 表格红行呈现，仍遵守"全部失败汇总 + exit 1"。"""
    settings = _isolate_api(tmp_path, monkeypatch)
    _seed_indexed_db(settings, counts={"BAAI/bge-m3": 2})  # 缺 1 块向量
    result = runner.invoke(app, ["doctor", "--profile", "api"])
    assert result.exit_code == 1
    assert "索引一致性" in result.output
    assert "reindex" in result.output


def test_list_empty_library_hints(tmp_path, monkeypatch):
    _isolate(cli, tmp_path, monkeypatch)
    result = runner.invoke(app, ["list", "--profile", "offline"])
    assert result.exit_code == 0
    assert "知识库为空" in result.output


def test_ingest_list_ask_full_flow(tmp_path, monkeypatch):
    """ingest → list → ask（有据/无据）真实跑通 CLI 链路。"""
    _isolate(cli, tmp_path, monkeypatch)
    src = _seed(tmp_path)

    ingest_result = runner.invoke(app, ["ingest", str(src), "--profile", "offline"])
    assert ingest_result.exit_code == 0
    assert "导入结果" in ingest_result.output
    assert "新导入/更新" in ingest_result.output

    list_result = runner.invoke(app, ["list", "--profile", "offline"])
    assert list_result.exit_code == 0
    assert "文档列表" in list_result.output
    assert "机器学习笔记" in list_result.output

    ask_result = runner.invoke(
        app, ["ask", "L2 正则化为什么能防止过拟合？", "--profile", "offline"]
    )
    assert ask_result.exit_code == 0
    assert "根据资料" in ask_result.output
    assert "[1]" in ask_result.output  # 引用标记渲染
    assert "延迟" in ask_result.output

    refuse_result = runner.invoke(
        app, ["ask", "如何在一周内学会做菠萝包？", "--show-sources", "--profile", "offline"]
    )
    assert refuse_result.exit_code == 0
    assert "已拒答" in refuse_result.output


def test_ask_empty_corpus_exits_1(tmp_path, monkeypatch):
    _isolate(cli, tmp_path, monkeypatch)
    result = runner.invoke(app, ["ask", "什么是线性代数？", "--profile", "offline"])
    assert result.exit_code == 1
    assert "知识库为空" in result.output


def test_chat_one_round_then_quit(tmp_path, monkeypatch):
    """chat 交互循环：输入一行 + quit 结束，答案正常渲染。"""
    _isolate(cli, tmp_path, monkeypatch)
    src = _seed(tmp_path)
    runner.invoke(app, ["ingest", str(src), "--profile", "offline"])
    result = runner.invoke(
        app,
        ["chat", "--profile", "offline"],
        input="L2 正则化为什么能防止过拟合？\nquit\n",
    )
    assert result.exit_code == 0
    assert "Mikasa" in result.output and "根据资料" in result.output


# ---------------------------------------------------------------------------
# --mode free（自由问答）：offline 守卫 / 非法值 / api 实跑
# ---------------------------------------------------------------------------


class _CliFreeLLM:
    """CLI free 替身：非流式原样回应（ask 实跑只需 complete）。"""

    model = "fake-free"

    def complete(self, messages, *, temperature, max_tokens):
        from mikasa.providers.llm import Completion

        return Completion(
            text=f"（自由作答）{messages[-1]['content']}",
            prompt_tokens=1,
            completion_tokens=1,
        )


def test_ask_free_mode_offline_exits_1(tmp_path, monkeypatch):
    """offline（mock 无语义）--mode free：红字提示 + exit 1。"""
    _isolate(cli, tmp_path, monkeypatch)
    result = runner.invoke(app, ["ask", "讲讲太阳系", "--mode", "free", "--profile", "offline"])
    assert result.exit_code == 1
    assert "api / local" in result.output


def test_chat_free_mode_offline_exits_1(tmp_path, monkeypatch):
    """chat --mode free 同守：首屏（建会话前）即拦，不落审计轨迹。"""
    _isolate(cli, tmp_path, monkeypatch)
    result = runner.invoke(app, ["chat", "--mode", "free", "--profile", "offline"], input="quit\n")
    assert result.exit_code == 1
    assert "api / local" in result.output
    assert "Mikasa对话模式" not in result.output  # 横幅未出：早失败


def test_ask_invalid_mode_exits_2(tmp_path, monkeypatch):
    """非法 mode：typer 字面量校验失败（usage 错误 exit 2）。"""
    _isolate(cli, tmp_path, monkeypatch)
    result = runner.invoke(app, ["ask", "随便问问", "--mode", "banana", "--profile", "offline"])
    assert result.exit_code == 2
    assert "banana" in result.output  # usage 行回显非法值


def test_ask_free_mode_answers_without_kb_warnings(tmp_path, monkeypatch):
    """free 实跑（api settings + FreeLLM 替身）：直答且无 kb 专属文案。

    锁三类输出契约：无黄色"未检出引用标记"（free 无引用是常态）、
    无"检索/重排"延迟段（free 只有生成段）。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek-0000")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-test-silicon-0000")
    settings = load_settings("api", data_dir=tmp_path / "data")
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: settings)
    monkeypatch.setattr("mikasa.pipeline.ask.get_llm", lambda config: _CliFreeLLM())

    result = runner.invoke(app, ["ask", "讲个笑话", "--mode", "free", "--profile", "api"])
    assert result.exit_code == 0
    assert "（自由作答）讲个笑话" in result.output
    assert "未检出引用标记" not in result.output
    assert "检索" not in result.output and "重排" not in result.output
    assert "延迟：生成" in result.output


def test_index_stats_table(tmp_path, monkeypatch):
    _isolate(cli, tmp_path, monkeypatch)
    src = _seed(tmp_path)
    runner.invoke(app, ["ingest", str(src), "--profile", "offline"])
    result = runner.invoke(app, ["index", "stats", "--profile", "offline"])
    assert result.exit_code == 0
    assert "索引状态" in result.output
    assert "chunk 数" in result.output and "分词器" in result.output


# ---------------------------------------------------------------------------
# serve（Web 服务）：monkeypatch uvicorn.run 捕获参数，不真起服
# ---------------------------------------------------------------------------


def _serve_args_capture(monkeypatch, tmp_path):
    """serve 测试基座：隔离 settings + 捕获 uvicorn.run 的调用参数。"""
    _isolate(cli, tmp_path, monkeypatch)
    import uvicorn

    calls = {}
    monkeypatch.setattr(
        uvicorn,
        "run",
        lambda *args, **kwargs: calls.update(args=args, kwargs=kwargs),
    )
    return calls


def test_serve_product_mode_passes_app_instance(tmp_path, monkeypatch):
    """默认模式：uvicorn.run 收到的是 FastAPI 实例 + 配置端口。"""
    calls = _serve_args_capture(monkeypatch, tmp_path)
    result = runner.invoke(app, ["serve", "--profile", "offline"])
    assert result.exit_code == 0
    assert "Web 界面" in result.output
    assert "http://127.0.0.1:8000/" in result.output  # offline 默认端口横幅
    (app_obj,) = calls["args"]
    assert app_obj is not None and hasattr(app_obj, "include_router")
    assert calls["kwargs"]["host"] == "127.0.0.1"
    assert calls["kwargs"]["port"] == 8000
    assert not calls["kwargs"].get("reload")


def test_serve_reload_uses_import_string_factory(tmp_path, monkeypatch):
    """reload 模式：uvicorn.run 收到 import string + factory=True。

    合法形态是不带 --profile（走默认查找链）——显式档位传不进子进程，
    见下一条测试。
    """
    calls = _serve_args_capture(monkeypatch, tmp_path)
    result = runner.invoke(app, ["serve", "--reload"])
    assert result.exit_code == 0
    (app_ref,) = calls["args"]
    assert app_ref == "mikasa.web.app:serve_app_factory"
    assert calls["kwargs"]["factory"] is True
    assert calls["kwargs"]["reload"] is True


def test_serve_reload_rejects_explicit_profile(tmp_path, monkeypatch):
    """--reload + 显式 --profile 必须直接拒绝（2026-09-11 修复）。

    热重载子进程按 import string 重导 serve_app_factory，档位只能走默认
    查找链——不拦就会"横幅写着 offline、实际按 api 真实调用外部 API"，
    offline 的零外部调用承诺被静默打破。
    """
    calls = _serve_args_capture(monkeypatch, tmp_path)
    result = runner.invoke(app, ["serve", "--profile", "offline", "--reload"])
    assert result.exit_code == 1
    # rich 会按终端宽度折行，比对前先去掉所有空白
    flat = "".join(result.output.split())
    assert "--reload与--profile不兼容" in flat
    assert calls == {}  # 压根没启动 uvicorn（calls 只在 run 被调用时填充）


def test_serve_host_port_override(tmp_path, monkeypatch):
    """--host/--port 覆盖配置默认值。"""
    calls = _serve_args_capture(monkeypatch, tmp_path)
    result = runner.invoke(
        app, ["serve", "--profile", "offline", "--host", "0.0.0.0", "--port", "9000"]
    )
    assert result.exit_code == 0
    assert calls["kwargs"]["host"] == "0.0.0.0"
    assert calls["kwargs"]["port"] == 9000


def test_serve_reload_config_conflict_exits_1(tmp_path, monkeypatch):
    """--reload 与 --config 互斥：退出 1 且不启动。"""
    calls = _serve_args_capture(monkeypatch, tmp_path)
    result = runner.invoke(app, ["serve", "--profile", "offline", "--reload", "--config", "x.yaml"])
    assert result.exit_code == 1
    assert "--reload 与 --config 不兼容" in result.output
    assert calls == {}  # uvicorn.run 未被调用


def test_main_entry_module_routes_through_main(monkeypatch):
    """python -m mikasa 必须走 cli.main()（它负责 .env 装载与日志初始化）。

    真实 bug：__main__.py 曾直调 app()，使 python -m mikasa 与 mikasa 命令
    不等价——.env 不加载，api profile 假报"密钥缺失"（§四档案）。
    """
    import runpy

    import mikasa.cli as cli_module

    calls: list[str] = []
    monkeypatch.setattr(cli_module, "main", lambda: calls.append("main"))
    runpy.run_module("mikasa.__main__", run_name="__main__")
    assert calls == ["main"]


def test_ask_provider_error_prints_readable_message(tmp_path, monkeypatch):
    """上游失败（密钥错/限流/Ollama 未启动）给红字提示，不是裸堆栈。

    2026-09-11 修复：ProviderError 与 ConfigError/StorageError 是兄弟类，
    原先的 except 元组捕不到它 —— 用户直接吃一屏 traceback。
    """
    _isolate(cli, tmp_path, monkeypatch)
    import mikasa.pipeline.ask as ask_mod
    from mikasa.errors import ProviderError

    class _BoomAsk:
        def __init__(self, *_a, **_k): ...

        def ask(self, *_a, **_k):
            raise ProviderError("LLM 调用失败（qwen3:8b）：连接被拒绝")

    monkeypatch.setattr(ask_mod, "AskService", _BoomAsk)
    result = runner.invoke(app, ["ask", "--profile", "offline", "测试问题"])

    assert result.exit_code == 1
    assert "LLM 调用失败" in result.output
    assert "Traceback" not in result.output


def test_print_answer_keeps_bracket_text():
    """答案正文与引用里的 arr[i]/[x] 不能被 rich markup 吞掉（2026-09-11 修复）。

    rich 默认按标记解析且**静默吞字**（"用 arr[i] 表示" 渲染成 "用 arr 表示"），
    论文/代码类回答常带方括号，落库正确但屏幕失真。
    """
    from rich.console import Console

    from mikasa.cli import _print_answer
    from mikasa.models.answer import Answer, Citation

    console = Console(width=200, record=True)
    answer = Answer(
        question="q",
        text="答案：用 arr[i] 表示第 i 个元素，见 [x] 处。",
        citations=[
            Citation(
                marker=1,
                chunk_id=1,
                document_title="paper[2024]notes.md",
                snippet="原文见 arr[i] 与 [x]",
            )
        ],
        model="fake",
        latency_ms={"generate": 12.0},
    )
    _print_answer(console, answer, show_sources=False, mode="kb")
    out = console.export_text()

    assert "arr[i]" in out, "正文里的方括号被 markup 吞掉了"
    assert "[x]" in out
    assert "paper[2024]notes.md" in out, "引用标题里的方括号被吞掉了"
