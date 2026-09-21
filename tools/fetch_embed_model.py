#!/usr/bin/env python
"""把向量模型取到构建目录，供 PyInstaller 打进发布包（ADR-0030）。

用法：
    python tools/fetch_embed_model.py            # 取到 build/embed-model/
    python tools/fetch_embed_model.py --check    # 只检查在不在（构建前置门禁）
    python tools/fetch_embed_model.py --dest X   # 换目标目录

三条路径，按序尝试：
  1. 目标目录里已有该 revision 的完整快照 → 什么都不做（重复构建零成本）；
  2. 本机已有 fastembed 缓存（仓库 data/ 或 %LOCALAPPDATA%\\Mikasa），且
     refs/main 正是固定 revision → 直接复制（省 91MB 下载，离线也能构建）；
  3. 走网络 snapshot_download（huggingface.co 失败自动换 hf-mirror.com）。

为什么 revision 固定（而不是"跟着 main 走"）：**所有用户的向量必须出自同一份
权重**。上游 main 一动，新装的用户查询向量与老用户的索引就不同源（同一个库、
两代模型）——这类错误不报错、只是检索悄悄变差。要升级模型得改这个常量，
是一次有意的、连带重建索引的决策（ADR-0030）。

产物布局 = HuggingFace 缓存形状（models--Qdrant--bge-small-zh-v1.5/…）。
运行时 providers/embedding.py 会把它原样铺进用户数据目录的 fastembed 缓存，
fastembed 的 local_files_only 路径正好认这个形状——**首次入库不再联网**。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEST = REPO_ROOT / "build" / "embed-model"

# 配置里的模型名 → HuggingFace 仓库（fastembed 注册表：BAAI/bge-small-zh-v1.5
# 的 hf 源是 Qdrant/bge-small-zh-v1.5）
MODEL = "BAAI/bge-small-zh-v1.5"
HF_REPO = "Qdrant/bge-small-zh-v1.5"
REVISION = "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59"

# fastembed 运行时请求的文件（model_management.py 的 allow_patterns + model_file）
ALLOW_PATTERNS = [
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "preprocessor_config.json",
    "model_optimized.onnx",
]

# 判"这份快照完整"只看这几个——**allow_patterns 是过滤器，不是必需清单**：
# preprocessor_config.json 这个仓库里根本没有（2026-09-21 实测踩到：把它当
# 必需，本机缓存里明明有完整模型，却每次都判成"不完整"→ 白下一次 91MB）。
REQUIRED_FILES = ["config.json", "tokenizer.json", "model_optimized.onnx"]

# 本机可能已有的 fastembed 缓存（复制优先于下载）
_LOCAL_CACHES = (
    REPO_ROOT / "data" / "models" / "fastembed",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Mikasa" / "models" / "fastembed",
)


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def repo_dir(dest: Path) -> Path:
    """该模型在 HF 缓存形状里的目录名。"""
    return dest / ("models--" + HF_REPO.replace("/", "--"))


def snapshot_dir(dest: Path) -> Path:
    """固定 revision 的 snapshot 目录（不保证存在）。"""
    return repo_dir(dest) / "snapshots" / REVISION


def is_complete(dest: Path) -> bool:
    """目标目录里是否已有一份**完整**的固定 revision 快照。"""
    snap = snapshot_dir(dest)
    if not snap.is_dir():
        return False
    have = {p.name for p in snap.iterdir() if p.is_file()}
    return all(name in have for name in REQUIRED_FILES)


def _cache_at_revision(cache: Path) -> bool:
    """某个本机缓存里是否正好有固定 revision 的快照（内容完整）。"""
    ref = repo_dir(cache) / "refs" / "main"
    if not ref.is_file():
        return False
    try:
        got = ref.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return got == REVISION and is_complete(cache)


def _copy_from_local(cache: Path, dest: Path) -> None:
    """把本机缓存里的模型子树整份复制进目标目录。"""
    shutil.copytree(repo_dir(cache), repo_dir(dest), dirs_exist_ok=True)


def _download(dest: Path) -> None:
    """走网络取快照；直连失败换国内镜像（与运行时同一套镜像规矩）。"""
    from huggingface_hub import snapshot_download

    try:
        snapshot_download(
            repo_id=HF_REPO,
            revision=REVISION,
            cache_dir=str(dest),
            allow_patterns=ALLOW_PATTERNS,
        )
        return
    except Exception as exc:  # noqa: BLE001 - 换镜像前只关心"失败了"
        log(f"  直连 huggingface.co 失败（{type(exc).__name__}），改用镜像重试…")
    # 镜像硬要求：HF_ENDPOINT + HF_HUB_DISABLE_XET（镜像对 Xet 回 401）。
    # constants.ENDPOINT 在 import 时定值，所以两处都要改（同 embedding.py）。
    from huggingface_hub import constants

    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    constants.ENDPOINT = "https://hf-mirror.com"
    snapshot_download(
        repo_id=HF_REPO,
        revision=REVISION,
        cache_dir=str(dest),
        allow_patterns=ALLOW_PATTERNS,
    )


def _write_license_note(dest: Path) -> None:
    """在模型旁边留一份来源与许可说明（声明要跟着权重走）。

    `THIRD_PARTY_NOTICES.md` 在发布包根目录，而这里解决的是"有人单独把这个
    目录拎出去"的情况——MIT 要求随副本带上版权与许可声明。
    """
    (dest / "LICENSE.txt").write_text(
        "本目录内的模型权重（bge-small-zh-v1.5，ONNX 版）随 Mikasa 发布包分发。\n"
        "\n"
        "来源：HuggingFace Qdrant/bge-small-zh-v1.5（上游 BAAI/bge-small-zh-v1.5）\n"
        f"revision：{REVISION}\n"
        "许可证：MIT（两个仓库的模型卡均声明 license: mit；仓库内没有 LICENSE 文件）\n"
        "版权：北京智源人工智能研究院（BAAI）\n"
        "\n"
        "MIT 许可正文与全部第三方组件声明见发布包内的 THIRD_PARTY_NOTICES.md；\n"
        "Mikasa 自身以 AGPL-3.0 发布（见 LICENSE）。\n",
        encoding="utf-8",
    )


def _normalize_refs(dest: Path) -> None:
    """保证 refs/main 指向固定 revision。

    HF 在请求里给的是 commit sha 时只写 refs/<sha>，而 fastembed 运行时请求的
    是 "main"——少了 refs/main，铺到用户机器上的缓存会"解析不到模型"、退回
    联网（等于白带）。这一步是那处不对称的补丁。
    """
    refs = repo_dir(dest) / "refs"
    refs.mkdir(parents=True, exist_ok=True)
    (refs / "main").write_text(REVISION, encoding="utf-8")


def fetch(dest: Path) -> bool:
    """确保 dest 里有完整快照；返回是否是新取的。"""
    if is_complete(dest):
        log(f"已就绪：{snapshot_dir(dest)}")
        _write_license_note(dest)
        return False
    # 目标目录里若是别的 revision（历史遗留），整份清掉重建：两份快照会让
    # 载荷白白大一倍（91MB × 2），运行时也没人用得上旧的那份。
    if repo_dir(dest).exists():
        shutil.rmtree(repo_dir(dest), ignore_errors=True)

    for cache in _LOCAL_CACHES:
        if cache.is_dir() and _cache_at_revision(cache):
            log(f"从本机缓存复制（不联网）：{cache}")
            _copy_from_local(cache, dest)
            break
    else:
        log(f"网络获取 {HF_REPO}@{REVISION[:8]} …")
        _download(dest)

    _normalize_refs(dest)
    _write_license_note(dest)
    if not is_complete(dest):
        missing = [name for name in REQUIRED_FILES if not (snapshot_dir(dest) / name).is_file()]
        log(f"[x] 快照不完整，缺：{', '.join(missing) or '（整份都没有）'}")
        raise SystemExit(1)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="取向量模型到构建目录（供打包）")
    parser.add_argument("--dest", default=str(DEFAULT_DEST), help="目标目录")
    parser.add_argument("--check", action="store_true", help="只检查目标目录里有没有（不下载）")
    args = parser.parse_args()
    dest = Path(args.dest).resolve()

    if args.check:
        if is_complete(dest):
            log(f"向量模型在：{snapshot_dir(dest)}")
            return 0
        log(f"[x] 目标目录里没有完整的向量模型：{dest}\n    先跑 python tools/fetch_embed_model.py")
        return 1

    dest.mkdir(parents=True, exist_ok=True)
    fresh = fetch(dest)
    size_mb = sum(p.stat().st_size for p in snapshot_dir(dest).iterdir() if p.is_file()) / 1048576
    log(f"完成（{'新取' if fresh else '已有'} · {size_mb:.1f} MB · revision {REVISION[:8]}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
