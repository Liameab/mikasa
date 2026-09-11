"""Web 测试夹具：TestClient + 隔离 data_dir 的 offline app。

基调与全项目一致：offline profile（MockLLM）零密钥零网络；
create_app 显式注入 settings → 每个测试进程独立 app/data 目录。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mikasa.config.settings import load_settings
from mikasa.ingest.service import IngestService
from mikasa.web.app import create_app

# 与 pipeline/test_ask.py 同款语料：锚句即问题原文，MockLLM 行为可预测
NOTE = """# 机器学习笔记

## 正则化

L2 正则化在损失中加入权重的平方和惩罚项，鼓励小而分散的权重。

## 注意力机制

缩放点积注意力除以根号 dk，防止点积随维度增大而方差过大。
"""


@pytest.fixture()
def offline_settings(tmp_path: Path):
    """offline profile + 隔离数据目录（不污染仓库 data/）。"""
    return load_settings("offline", data_dir=tmp_path / "data")


@pytest.fixture()
def client(offline_settings):
    """Web 测试入口：app 常驻同一进程，TestClient 直连。"""
    with TestClient(create_app(offline_settings)) as c:
        yield c, offline_settings


def seed_corpus(settings) -> None:
    """往 app 的数据目录导入 NOTE 语料（upload 副本语义与 ingest 一致）。

    NOTE：语料目录取 data_dir 的父级（tmp_path/data）——测试隔离目录
    由 pytest tmp_path 提供，父级即 tmp_path 本身。
    """
    src = settings.data_dir.parent / "notes"
    src.mkdir(exist_ok=True)
    (src / "note.md").write_text(NOTE, encoding="utf-8")
    IngestService(settings).ingest_paths([src])


class _WebFreeLLM:
    """free 端到端替身：确定性回答、流式两段产出。

    与 pipeline/test_ask.py 的 _FreeLLM 同构；独立存放是避免跨模块
    import 依赖 pytest 布局（无 __init__.py 的 tests/ 目录）。
    """

    model = "fake-free"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)

    def stream(self, messages, *, temperature, max_tokens):
        if not self.replies:
            raise AssertionError("流式回复已用尽——调用次数与脚本不符")
        yield from self.replies


@pytest.fixture()
def free_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """api profile + FreeLLM 替身的 app：free 模式端到端（空库、零网络）。

    monkeypatch 目标 mikasa.pipeline.ask.get_llm：AskService 构造期的
    注入点（web AppServices 不暴露 llm 参数，靠换工厂顶掉 OpenAICompatLLM）；
    embedding 客户端惰性构造（_get_client 才建连接），api 配置零请求。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek-0000")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-test-silicon-0000")
    settings = load_settings("api", data_dir=tmp_path / "data")
    settings.ensure_dirs()
    llm = _WebFreeLLM(replies=["第一段", "第二段"])
    monkeypatch.setattr("mikasa.pipeline.ask.get_llm", lambda config: llm)
    with TestClient(create_app(settings)) as c:
        yield c, settings, llm


@pytest.fixture()
def seeded_client(client):
    """client + 语料已导入：问答/评测页测试的起点（重复 seed 幂等跳过）。"""
    c, settings = client
    seed_corpus(settings)
    return c, settings
