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


@pytest.fixture(autouse=True)
def _isolate_user_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """全局隔离用户数据根：所有测试的用户级读写都落在 tmp。

    load_settings 会合并"用户配置覆盖层"（user_data_root()/config.yaml，
    Web 设置面板的写入目标）——开发机上这个文件一旦真实存在，就会给全库
    测试注入意外的 llm 覆盖（模型名/端点全变），且污染源不在测试目录里，
    排查时极难联想到。统一把 MIKASA_DATA_DIR 指到 tmp，测试世界与现实
    世界隔离（user_data_root() 及其派生的一切随之落到 tmp）。
    """
    monkeypatch.setenv("MIKASA_DATA_DIR", str(tmp_path / "userdata"))


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
