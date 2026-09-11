"""pytest 共享夹具：全部测试默认离线、零网络、零密钥。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 保证 `import mikasa` 优先命中仓库源码（未 pip install -e 时兜底）
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from mikasa.config.settings import load_settings  # noqa: E402


@pytest.fixture()
def offline_settings(tmp_path: Path):
    """offline profile + 隔离数据目录（不污染仓库 data/）。"""
    return load_settings("offline", data_dir=tmp_path / "data")


@pytest.fixture()
def api_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """api profile + 假密钥 + 隔离数据目录（不实际联网）。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek-0000")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-test-silicon-0000")
    return load_settings("api", data_dir=tmp_path / "data")


@pytest.fixture()
def local_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """local profile + 隔离数据目录（不实际联网、不触发模型下载）。

    api_key_env="" → llm.api_key 恒 None（免密钥形态，见 ADR-0014）；
    OLLAMA_BASE_URL 删除保证 base_url 取配置默认值（本机环境无关）。
    """
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    return load_settings("local", data_dir=tmp_path / "data")
