"""构建期取件工具（tools/fetch_embed_model.py）的回归测试。

这里钉住的是 2026-09-21 那次真实事故的形状：**同一份 91MB 权重在发布包里
出现两次**（CI 直连走 Xet → 缓存里 `blobs/<etag>` 与 `snapshots/<rev>/文件`
各一份真数据；PyInstaller 两份都收 → zip 263.6MB 而不是 165MB）。
两处防线各一条测试：
  1. `_drop_blob_duplicates` 删掉第二副本、但**先把符号链接落成真文件**；
  2. `--check` 发现权重不止一份时报错（release.yml 里那道载荷断言用的就是它）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[3] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import fetch_embed_model as fem  # noqa: E402

REV = fem.REVISION
ONNX = b"fake-onnx" * 16


def _repo(cache: Path) -> Path:
    return cache / f"models--{fem.HF_REPO.replace('/', '--')}"


def _complete_tree(cache: Path, blobs_at: str = "none") -> Path:
    """造一份形状正确的快照。

    blobs_at 指定第二副本放在哪——**两种落点都是真实存在的**（这就是第一版
    只清仓库级、结果 CI 里一个都没删掉的原因）：
      "none" 普通 HTTP / 本机缓存（没有第二副本）
      "repo" 仓库级 `models--ORG--NAME/blobs/`
      "root" **缓存根级** `blobs/`（Xet 传输；CI 现场就是这个形状）
    """
    snap = _repo(cache) / "snapshots" / REV
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "config.json").write_text("{}", encoding="utf-8")
    (snap / "tokenizer.json").write_text("{}", encoding="utf-8")
    (snap / "model_optimized.onnx").write_bytes(ONNX)
    refs = _repo(cache) / "refs"
    refs.mkdir(parents=True, exist_ok=True)
    (refs / "main").write_text(REV, encoding="utf-8")
    if blobs_at != "none":
        blobs = (_repo(cache) if blobs_at == "repo" else cache) / "blobs"
        blobs.mkdir(exist_ok=True)
        # Xet 写的那份真数据：HF 用 etag 命名，**没有 .onnx 后缀**
        # （所以"数 .onnx 个数"抓不到它，判据得看总字节）
        (blobs / "deadbeef0123456789").write_bytes(ONNX * 4096)
    return cache


def test_drops_blob_duplicate(tmp_path, capsys):
    """有 blobs 时删掉它（数据留在 snapshots），并报告释放的字节数。"""
    cache = _complete_tree(tmp_path / "cache", blobs_at="repo")
    freed = fem._drop_blob_duplicates(cache)
    assert freed == len(ONNX) * 4096
    assert not (_repo(cache) / "blobs").exists()
    assert (_repo(cache) / "snapshots" / REV / "model_optimized.onnx").read_bytes() == ONNX
    assert fem.is_complete(cache)


def test_drops_root_level_blob_duplicate(tmp_path):
    """**缓存根级**的 blobs/ 也要清——CI 走 Xet 时就是写在这里的。

    第一版只认仓库级 `models--ORG--NAME/blobs/`（本机镜像下载的形状），
    于是 CI 里一个都没删掉：载荷仍是 181.2MB，发版作业被 `--check` 拦下
    （2026-09-21 实测）。这条测试就是那次的现场。
    """
    cache = _complete_tree(tmp_path / "cache", blobs_at="root")
    before = fem.payload_bytes(cache)
    freed = fem._drop_blob_duplicates(cache)
    assert freed == len(ONNX) * 4096
    assert not (cache / "blobs").exists()
    assert fem.payload_bytes(cache) == before - freed
    assert fem.is_complete(cache)
    # 快照本体必须完好（删的是副本，不是数据）
    assert (_repo(cache) / "snapshots" / REV / "model_optimized.onnx").read_bytes() == ONNX


def test_drops_blob_duplicate_materializes_symlinks(tmp_path):
    """snapshots 里是指向 blobs 的符号链接时，先落成真文件再删 blobs。

    （Windows 建符号链接要开发者模式/管理员；没权限就跳过这一条——
    真实 CI 是 Linux 语义的链接，本机测不到也不影响结论。）
    """
    cache = _complete_tree(tmp_path / "cache", blobs_at="repo")
    snap_file = _repo(cache) / "snapshots" / REV / "model_optimized.onnx"
    blob_file = _repo(cache) / "blobs" / "deadbeef0123456789"
    snap_file.unlink()
    try:
        snap_file.symlink_to(blob_file)
    except (OSError, NotImplementedError):
        pytest.skip("本机不允许建符号链接")

    fem._drop_blob_duplicates(cache)
    assert not snap_file.is_symlink()
    assert snap_file.read_bytes() == ONNX * 4096
    assert not (_repo(cache) / "blobs").exists()


def test_no_blobs_is_noop(tmp_path):
    """普通 HTTP 下载/本机缓存复制出来的树（blobs 为空或不存在）不动它。"""
    cache = _complete_tree(tmp_path / "cache")
    before = sorted(p.relative_to(cache).as_posix() for p in cache.rglob("*"))
    assert fem._drop_blob_duplicates(cache) == 0
    after = sorted(p.relative_to(cache).as_posix() for p in cache.rglob("*"))
    assert before == after


def test_check_fails_on_doubled_payload(tmp_path, monkeypatch, capsys):
    """--check 在载荷超预算时报错（release.yml 的载荷断言靠它）。

    真事故里的第二副本是 91MB（假数据凑不出来），所以把预算卡在"干净"与
    "翻倍"之间当探针——测的是判据本身，不是那个具体数字。
    """
    payload = _complete_tree(tmp_path / "payload", blobs_at="root")
    doubled = fem.payload_bytes(payload)
    fem._drop_blob_duplicates(payload)
    clean = fem.payload_bytes(payload)
    assert doubled > clean
    monkeypatch.setattr(sys, "argv", ["fetch_embed_model.py", "--check", "--dest", str(payload)])
    monkeypatch.setattr(fem, "MAX_PAYLOAD_MB", (clean + doubled) / 2 / 1048576)

    assert fem.main() == 0  # 只有一份：放行

    _complete_tree(payload, blobs_at="root")  # 第二副本回来了
    assert fem.main() == 1
    assert "MB 以内" in capsys.readouterr().err
