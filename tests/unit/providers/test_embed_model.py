"""随包向量模型（ADR-0030）：铺设、完整性判据、local_files_only 取舍。

三条不变量：
  1. 铺进缓存的是**内容**——大小不一致就重铺；用户已下好的同大小文件不动；
  2. "缓存里有模型"要看 refs/main → snapshots/<rev>/*.onnx 这条链，只查目录
     存在会把半截下载当成品（limitations §八 的 websockets 空壳就是同一类：
     **带一份坏的最糟**）；
  3. 缓存完整时构造 backend 必须带 local_files_only=True——不联网、也不会把
     远端更新过的权重混进同一个索引（向量必须同源）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import mikasa.config.settings as settings_module
from mikasa.providers.embedding import (
    LocalFastEmbed,
    _cached_snapshot,
    _hf_repo_of,
    _seed_bundled_model,
)

# 配置里写的是模型名（BAAI/…），缓存目录用的是 HF 仓库名（Qdrant/…）——
# 两个名字不一样，正是 _hf_repo_of 存在的理由
MODEL = "BAAI/bge-small-zh-v1.5"
HF_REPO = "Qdrant/bge-small-zh-v1.5"
REVISION = "f" * 40
ONNX = b"fake-onnx-weights" * 4


def _repo_dir(cache: Path) -> Path:
    return cache / f"models--{HF_REPO.replace('/', '--')}"


def _write_bundle(root: Path) -> Path:
    """在 root/models/embed 造一份假的"随包模型"（HF 缓存形状）。"""
    repo = _repo_dir(root / "models" / "embed")
    snap = repo / "snapshots" / REVISION
    snap.mkdir(parents=True, exist_ok=True)
    (repo / "refs").mkdir(exist_ok=True)
    (repo / "refs" / "main").write_text(REVISION, encoding="utf-8")
    (snap / "config.json").write_text("{}", encoding="utf-8")
    (snap / "tokenizer.json").write_text("{}", encoding="utf-8")
    (snap / "model_optimized.onnx").write_bytes(ONNX)
    return root


def _fake_factory(seen: dict, hf: str | None) -> type:
    """假 fastembed.TextEmbedding：记录构造参数，不联网、不加载 ONNX。"""
    import numpy as np

    class _Fake:
        @classmethod
        def list_supported_models(cls) -> list[dict]:
            return [{"model": MODEL, "sources": {"hf": hf}}]

        def __init__(self, model: str, **kwargs) -> None:
            seen["model"] = model
            seen["kwargs"] = kwargs

        def embed(self, texts):
            return iter([np.zeros(4, dtype=np.float32) for _ in texts])

    return _Fake


# ---------------------------------------------------------------------------
# 铺设（随包模型 → 用户缓存）
# ---------------------------------------------------------------------------


def test_seed_copies_bundle_and_is_idempotent(tmp_path, monkeypatch):
    """随包模型铺进缓存；第二次一个文件都不重复拷。"""
    root = _write_bundle(tmp_path / "res")
    monkeypatch.setattr(settings_module, "resource_root", lambda: root)
    cache = tmp_path / "cache"
    cache.mkdir()

    assert _seed_bundled_model(cache) == 4  # refs/main + config / tokenizer / onnx
    snap = _repo_dir(cache) / "snapshots" / REVISION
    assert (snap / "model_optimized.onnx").read_bytes() == ONNX
    assert (_repo_dir(cache) / "refs" / "main").read_text(encoding="utf-8") == REVISION
    assert _seed_bundled_model(cache) == 0  # 幂等


def test_seed_repairs_truncated_file_without_touching_user_cache(tmp_path, monkeypatch):
    """半截文件（大小不符）补齐；同大小的既有文件原样不动（不重下、不覆盖）。"""
    root = _write_bundle(tmp_path / "res")
    monkeypatch.setattr(settings_module, "resource_root", lambda: root)
    cache = tmp_path / "cache"
    snap = _repo_dir(cache) / "snapshots" / REVISION
    snap.mkdir(parents=True)
    (snap / "model_optimized.onnx").write_bytes(b"half")  # 半截下载的现场
    (snap / "config.json").write_text("[]", encoding="utf-8")  # 同大小、内容不同

    assert _seed_bundled_model(cache) == 3  # onnx 重铺 + tokenizer / refs 新增
    assert (snap / "model_optimized.onnx").read_bytes() == ONNX
    assert (snap / "config.json").read_text(encoding="utf-8") == "[]"


def test_seed_is_noop_without_bundled_model(tmp_path, monkeypatch):
    """开发环境（没有随包模型）静默跳过——行为与从前一致：第一次用才联网下。"""
    monkeypatch.setattr(settings_module, "resource_root", lambda: tmp_path / "nothing")
    assert _seed_bundled_model(tmp_path / "cache") == 0


# ---------------------------------------------------------------------------
# 完整性判据
# ---------------------------------------------------------------------------


def test_cached_snapshot_requires_refs_then_weights(tmp_path):
    """缺 refs/main、revision 为空、没有 .onnx —— 任一不满足都判"没有"。"""
    repo = _repo_dir(tmp_path)
    assert _cached_snapshot(tmp_path, HF_REPO) is None  # 什么都没有
    (repo / "snapshots" / REVISION).mkdir(parents=True)
    assert _cached_snapshot(tmp_path, HF_REPO) is None  # 只有空目录
    (repo / "refs").mkdir()
    (repo / "refs" / "main").write_text("", encoding="utf-8")
    assert _cached_snapshot(tmp_path, HF_REPO) is None  # refs 是空的
    (repo / "refs" / "main").write_text(REVISION, encoding="utf-8")
    assert _cached_snapshot(tmp_path, HF_REPO) is None  # 还没有权重
    (repo / "snapshots" / REVISION / "model_optimized.onnx").write_bytes(b"x")
    assert _cached_snapshot(tmp_path, HF_REPO) == repo / "snapshots" / REVISION


def test_hf_repo_comes_from_registry_not_model_name():
    """注册表里 BAAI/bge-small-zh-v1.5 的 hf 源是 Qdrant/…（拼名字会永远查不到）。"""
    fastembed = pytest.importorskip("fastembed")
    assert _hf_repo_of(fastembed.TextEmbedding, MODEL) == HF_REPO
    assert _hf_repo_of(fastembed.TextEmbedding, "某个不存在的模型") is None


# ---------------------------------------------------------------------------
# 构造 backend：有完整缓存 → 只认本地
# ---------------------------------------------------------------------------


def test_backend_uses_local_files_only_when_model_cached(local_settings, tmp_path, monkeypatch):
    """缓存完整 → 构造参数必须带 local_files_only（不联网、版本不漂移）。"""
    import fastembed

    cache = tmp_path / "cache"
    snap = _repo_dir(cache) / "snapshots" / REVISION
    snap.mkdir(parents=True)
    (snap / "model_optimized.onnx").write_bytes(b"x")
    (_repo_dir(cache) / "refs").mkdir()
    (_repo_dir(cache) / "refs" / "main").write_text(REVISION, encoding="utf-8")
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(cache))

    seen: dict = {}
    monkeypatch.setattr(fastembed, "TextEmbedding", _fake_factory(seen, HF_REPO))
    LocalFastEmbed(local_settings.embedding).embed_documents(["测试"])

    assert seen["model"] == MODEL
    assert seen["kwargs"] == {"local_files_only": True}


def test_backend_leaves_download_path_open_when_cache_empty(local_settings, tmp_path, monkeypatch):
    """缓存里什么都没有 → 不带 local_files_only，fastembed 自己先本地后网络。"""
    import fastembed

    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path / "empty-cache"))
    seen: dict = {}
    monkeypatch.setattr(fastembed, "TextEmbedding", _fake_factory(seen, HF_REPO))

    LocalFastEmbed(local_settings.embedding).embed_documents(["测试"])

    assert seen["kwargs"] == {}


def test_backend_stays_generic_for_unknown_model(local_settings, tmp_path, monkeypatch):
    """注册表查不到 hf 源（自定义模型）→ 退回旧行为，不猜缓存目录。"""
    import fastembed

    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path / "cache"))
    seen: dict = {}
    monkeypatch.setattr(fastembed, "TextEmbedding", _fake_factory(seen, None))

    LocalFastEmbed(local_settings.embedding).embed_documents(["测试"])

    assert seen["kwargs"] == {}
