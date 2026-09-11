"""配置系统单元测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mikasa.config.settings import load_settings
from mikasa.errors import ConfigError


def test_offline_profile_defaults(offline_settings):
    """offline：mock LLM、无向量路、judge 关闭。"""
    s = offline_settings
    assert s.profile == "offline"
    assert s.llm.backend == "mock"
    assert s.embedding.backend == "none"
    assert s.retrieval.dense_enabled is False
    assert s.reranker.backend == "none"
    assert s.judge.enabled is False


def test_api_profile_fields(api_settings):
    """api：DeepSeek LLM + SiliconFlow embedding/reranker/judge。"""
    s = api_settings
    assert s.profile == "api"
    assert s.llm.backend == "api"
    assert s.llm.model.startswith("deepseek")
    assert s.llm.base_url == "https://api.deepseek.com"
    assert s.embedding.backend == "api"
    assert s.embedding.model == "BAAI/bge-m3"
    assert s.reranker.backend == "api"
    assert s.judge.enabled is True
    assert s.llm.api_key == "sk-test-deepseek-0000"
    assert s.embedding.api_key == "sk-test-silicon-0000"


def test_local_profile_env_expansion(tmp_path, monkeypatch):
    """local：${VAR:-default} 占位符展开（不设置时用默认值）。"""
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    s = load_settings("local", data_dir=tmp_path / "data")
    assert s.llm.backend == "local"
    assert s.llm.base_url == "http://localhost:11434/v1"
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9999/v1")
    s2 = load_settings("local", data_dir=tmp_path / "data")
    assert s2.llm.base_url == "http://127.0.0.1:9999/v1"


def test_local_profile_fields(local_settings):
    """local：Ollama 免密钥 + fastembed 本地嵌入 + 重排/judge 暂关。"""
    s = local_settings
    assert s.profile == "local"
    assert s.llm.backend == "local"
    assert s.llm.model == "qwen3:8b"
    assert s.llm.base_url == "http://localhost:11434/v1"
    assert s.llm.api_key is None  # api_key_env="" → 恒无密钥（免密钥形态，ADR-0014）
    assert s.embedding.backend == "local"
    assert s.embedding.model == "BAAI/bge-small-zh-v1.5"
    assert s.embedding.query_prefix  # bge 检索指令前缀非空（官方建议）
    assert s.retrieval.dense_enabled is True
    assert s.reranker.backend == "none"  # 本地重排收益未实证，暂关（ADR-0014）
    assert s.judge.enabled is False


def test_invalid_profile_raises():
    with pytest.raises(ConfigError, match="未知 profile"):
        load_settings("production")


def test_chunking_params_invalid():
    from mikasa.config.settings import ChunkingConfig

    with pytest.raises(ValidationError):
        ChunkingConfig(size=0)
    cfg = ChunkingConfig(size=10, overlap=10)
    with pytest.raises(ConfigError, match="非法的分块参数"):
        cfg.validate_self()


def test_derived_paths(offline_settings, tmp_path):
    """派生路径全部落在 data_dir 下。"""
    s = offline_settings
    base = tmp_path / "data"
    assert s.db_path == base / "mikasa.db"
    assert s.index_dir == base / "indexes"
    assert s.uploads_dir == base / "uploads"


def test_data_dir_env_isolated(offline_settings):
    """默认 data_dir 是仓库 data/（可用 --data-dir 覆盖，见 load_settings 参数）。"""
    from mikasa.config.settings import REPO_ROOT

    assert offline_settings.data_dir != REPO_ROOT / "data"


def test_crosslingual_flag_per_profile(offline_settings, api_settings, local_settings):
    """跨语言检索开关（2026-09-10）：代码默认关，local/api 档在 profile 开。

    默认关的理由：翻译依赖真实 LLM，offline/mock 保持零额外调用——
    任何走 offline_settings 的既有测试都不该触发翻译（回归护栏）。
    """
    from mikasa.config.settings import RetrievalConfig

    assert RetrievalConfig().crosslingual is False  # 代码默认
    assert offline_settings.retrieval.crosslingual is False  # offline yaml 不翻
    assert api_settings.retrieval.crosslingual is True  # 云端库中英混排：开
    assert local_settings.retrieval.crosslingual is True  # 本地库（qwen3 翻译）同样开


def test_bilingual_flag_per_profile(offline_settings, api_settings, local_settings):
    """双语对照开关（#8，2026-09-10）：代码默认关，local/api 档在 profile 开。

    默认关的理由：块翻译依赖真实 LLM，offline/mock 保持零额外调用。
    """
    from mikasa.config.settings import AnswerConfig

    assert AnswerConfig().bilingual is False  # 代码默认
    assert offline_settings.answer.bilingual is False  # offline yaml 不翻
    assert api_settings.answer.bilingual is True  # 云端库中英混排：开
    assert local_settings.answer.bilingual is True  # 本地库（qwen3 翻译）同样开
