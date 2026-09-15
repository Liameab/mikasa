"""用户配置覆盖层与密钥写回（Web 设置面板的落盘层，ADR-0018）。

覆盖三件事：① 覆盖层只在"基底是 profile 文件"时叠加；② 密钥写入/清除的
文件行为；③ .env 查找链**全部加载**（面板写的数据目录 .env 不能把仓库
根 .env 的其它密钥屏蔽掉——这是实现当天修掉的真 bug）。
"""

from __future__ import annotations

import os

import pytest

import mikasa.config.settings as settings_module
from mikasa.config.settings import (
    clear_api_key,
    load_dotenv_file,
    load_settings,
    user_config_path,
    user_env_path,
    write_api_key,
    write_llm_overlay,
)
from mikasa.errors import ConfigError

# ---------------------------------------------------------------------------
# 覆盖层合并
# ---------------------------------------------------------------------------


def test_overlay_merges_into_profile(offline_settings):
    """覆盖层写 llm 段 → 生效；其它段（embedding/retrieval）保持档位原值。"""
    write_llm_overlay(
        {
            "backend": "api",
            "base_url": "https://example.com/v1",
            "api_key_env": "MY_OVERLAY_KEY",
            "model": "overlay-model",
        }
    )
    s = load_settings("offline", data_dir=offline_settings.data_dir)
    assert s.llm.backend == "api"
    assert s.llm.base_url == "https://example.com/v1"
    assert s.llm.model == "overlay-model"
    assert s.profile == "offline"  # 档位不变
    assert s.embedding.backend == "none"  # 未被波及（改 embedding 要重索引）
    assert s.retrieval.fusion_top_k == 10  # offline 档的检索参数原样
    assert s.llm.max_tokens == 1024  # 没写的字段继续取 profile 值


def test_overlay_missing_is_noop(offline_settings):
    """覆盖层文件不存在时行为与从前完全一致（零爆炸半径）。"""
    assert not user_config_path().exists()
    s = load_settings("offline", data_dir=offline_settings.data_dir)
    assert s.llm.backend == "mock"


def test_overlay_skipped_with_explicit_config(tmp_path, offline_settings):
    """--config 是"完整替换"语义：显式配置文件在场时覆盖层不叠加。"""
    write_llm_overlay({"model": "overlay-model"})
    cfg = tmp_path / "custom.yaml"
    cfg.write_text("profile: offline\nllm:\n  model: explicit-model\n", encoding="utf-8")
    s = load_settings("offline", config_path=cfg, data_dir=offline_settings.data_dir)
    assert s.llm.model == "explicit-model"


def test_overlay_skipped_when_repo_config_yaml_present(tmp_path, monkeypatch, offline_settings):
    """仓库根 config.yaml 同样是完整配置：它在场时覆盖层不生效。"""
    write_llm_overlay({"model": "overlay-model"})
    fake_root = tmp_path / "repo"
    (fake_root / "config").mkdir(parents=True)
    (fake_root / "config" / "config.yaml").write_text(
        "profile: offline\nllm:\n  model: repo-config-model\n", encoding="utf-8"
    )
    monkeypatch.setattr(settings_module, "REPO_ROOT", fake_root)
    s = load_settings("offline", data_dir=offline_settings.data_dir)
    assert s.llm.model == "repo-config-model"


def test_overlay_profile_key_ignored(offline_settings):
    """覆盖层里的 profile 键一律忽略：面板不能悄悄改档（embedding 会对不上）。"""
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("profile: api\nllm:\n  model: from-overlay\n", encoding="utf-8")
    s = load_settings("offline", data_dir=offline_settings.data_dir)
    assert s.profile == "offline"
    assert s.embedding.backend == "none"
    assert s.llm.model == "from-overlay"


def test_overlay_expands_env_vars(monkeypatch, offline_settings):
    """覆盖层同样支持 ${VAR:-default} 占位符（与 profile 文件一致）。"""
    monkeypatch.setenv("OVERLAY_TEST_URL", "http://from-env/v1")
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "llm:\n  base_url: ${OVERLAY_TEST_URL:-http://fallback/v1}\n",
        encoding="utf-8",
    )
    s = load_settings("offline", data_dir=offline_settings.data_dir)
    assert s.llm.base_url == "http://from-env/v1"


def test_overlay_bad_yaml_raises(offline_settings):
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("llm: [未闭合\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="用户配置读取失败"):
        load_settings("offline", data_dir=offline_settings.data_dir)


def test_overlay_non_mapping_raises(offline_settings):
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("- 列表\n- 不是键值\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="键值结构"):
        load_settings("offline", data_dir=offline_settings.data_dir)


def test_overlay_empty_file_is_noop(offline_settings):
    """只有头注释的空覆盖层（写入后未填字段）不改变任何行为。"""
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# 只有注释\n", encoding="utf-8")
    s = load_settings("offline", data_dir=offline_settings.data_dir)
    assert s.llm.backend == "mock"


# ---------------------------------------------------------------------------
# 覆盖层写入
# ---------------------------------------------------------------------------


def test_write_llm_overlay_writes_only_llm_section():
    path = write_llm_overlay({"backend": "api", "model": "m1"})
    assert path == user_config_path()
    text = path.read_text(encoding="utf-8")
    assert "Mikasa 用户配置覆盖层" in text  # 头注释在
    assert "llm:" in text
    assert "backend" in text and "m1" in text
    assert not (path.parent / (path.name + ".tmp")).exists()  # 无残留临时文件


# ---------------------------------------------------------------------------
# 密钥写回（.env）
# ---------------------------------------------------------------------------


def test_user_env_path_defaults_to_data_dir():
    assert user_env_path() == settings_module.user_data_root() / ".env"


def test_user_env_path_follows_env_file_override(tmp_path, monkeypatch):
    """MIKASA_ENV_FILE 显式指定时，写入目标与读取链第一候选保持一致。"""
    target = tmp_path / "custom.env"
    monkeypatch.setenv("MIKASA_ENV_FILE", str(target))
    assert user_env_path() == target
    write_api_key("OVERRIDE_PATH_KEY", "v")
    assert target.is_file()


def test_write_api_key_creates_file_with_header():
    path = write_api_key("NEW_KEY", "sk-abc")
    text = path.read_text(encoding="utf-8")
    assert "Mikasa 本地密钥文件" in text  # 头注释（新建时）
    assert "NEW_KEY" in text


def test_write_api_key_updates_in_place_and_quotes():
    """同名键覆盖而不追加；含特殊字符的值被引用包住；其它键保留。"""
    write_api_key("K1", "first")
    write_api_key("K1", "has space and #hash")
    write_api_key("K2", "other")
    text = user_env_path().read_text(encoding="utf-8")
    assert text.count("K1=") == 1  # 只有一行 K1
    assert "has space and #hash" in text
    assert "K2=" in text


def test_clear_api_key_removes_line_and_is_idempotent():
    write_api_key("K1", "v1")
    write_api_key("K2", "v2")
    clear_api_key("K1")
    text = user_env_path().read_text(encoding="utf-8")
    assert "K1" not in text
    assert "K2" in text
    clear_api_key("NEVER_SET")  # 清除不存在的键：静默返回，不是错误


def test_written_key_loads_through_chain(tmp_path, monkeypatch):
    """写进数据目录 .env 的密钥，load_dotenv_file 能读回（闭环）。"""
    monkeypatch.setattr(settings_module, "resource_root", lambda: tmp_path / "res")
    monkeypatch.setattr(os, "environ", dict(os.environ))
    write_api_key("ROUNDTRIP_KEY", "sk-roundtrip")
    load_dotenv_file()
    assert os.environ["ROUNDTRIP_KEY"] == "sk-roundtrip"


# ---------------------------------------------------------------------------
# .env 链：全部加载（回归：不能"命中第一个就停"）
# ---------------------------------------------------------------------------


def test_load_dotenv_loads_every_existing_file(tmp_path, monkeypatch):
    """数据目录 .env 存在时，资源根 .env 的其它密钥仍然加载。

    回归背景（实现当天修掉的真 bug）：旧实现"第一个存在的文件加载后就
    return"。设置面板把 DeepSeek 密钥写进数据目录 .env 后，开发模式下
    仓库根 .env 里的 SILICONFLOW_API_KEY 会被静默屏蔽 → api 档的
    embedding/reranker/judge 全报"未配置 API 密钥"。
    """
    monkeypatch.setattr(settings_module, "resource_root", lambda: tmp_path / "res")
    monkeypatch.delenv("MIKASA_ENV_FILE", raising=False)
    monkeypatch.setattr(os, "environ", dict(os.environ))

    data_env = settings_module.user_data_root() / ".env"  # 链序 #1（数据目录）
    res_env = tmp_path / "res" / ".env"  # 链序 #2（资源根）
    data_env.parent.mkdir(parents=True, exist_ok=True)
    res_env.parent.mkdir(parents=True, exist_ok=True)
    data_env.write_text('DEEPSEEK_API_KEY="from-data"\n', encoding="utf-8")
    res_env.write_text(
        'SILICONFLOW_API_KEY="from-res"\nDEEPSEEK_API_KEY="stale"\n', encoding="utf-8"
    )

    load_dotenv_file()
    assert os.environ["SILICONFLOW_API_KEY"] == "from-res"  # 没被屏蔽
    assert os.environ["DEEPSEEK_API_KEY"] == "from-data"  # 同名键：链序在前者胜
