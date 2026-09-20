"""发布校验和清单：两个发布脚本的**执行顺序**不能决定清单的完整性。

为什么单独钉这一条（2026-09-19 发布前复查）：应用内更新会拿发布页上的
`SHA256SUMS.txt` 去查安装包的哈希（`src/mikasa/update/install.py` 的
`expected_sha256`），而这份清单由**两个**脚本分别写——`make_release.py` 写
zip 与内层 exe，`build_installer.py` 追加 Setup.exe。

原先一个整份覆盖、一个只在清单已存在时追加，于是"先编译安装包、后打包"的顺序
会让最终清单里**没有 Setup 那一行**：所有用户的一键更新都失败在"校验和文件里
没有该记录"，而本地双击安装包一切正常——发布时看不出来，只有用户会中招。
ADR-0022 把"每次发布必须带 SHA256SUMS"写成了硬规则，但在此之前没有任何东西
保证清单**含安装包那一行**，也没有任何测试覆盖这两个脚本。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import build_installer, make_release  # noqa: E402


def _write(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    return path


def _listed_names(sums: Path) -> set[str]:
    return {
        line.split("  ", 1)[-1].strip().lstrip("*")
        for line in sums.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


@pytest.mark.parametrize("installer_first", [False, True])
def test_manifest_is_complete_in_either_order(tmp_path: Path, installer_first: bool) -> None:
    """先打包后编译、先编译后打包，两种顺序都必须列出全部三个产物。"""
    zip_path = _write(tmp_path / "Mikasa-v9.9.9-win64.zip", b"zip-bytes")
    inner_exe = _write(tmp_path / "Mikasa.exe", b"inner-exe-bytes")
    setup = _write(tmp_path / "Mikasa-Setup-9.9.9-win64.exe", b"setup-bytes")

    if installer_first:
        build_installer.append_checksum(setup)
        sums = make_release.write_checksums(zip_path, inner_exe)
    else:
        make_release.write_checksums(zip_path, inner_exe)
        sums = build_installer.append_checksum(setup)

    assert sums == tmp_path / "SHA256SUMS.txt"
    assert _listed_names(sums) == {zip_path.name, inner_exe.name, setup.name}


def test_rewriting_is_idempotent_and_keeps_lf(tmp_path: Path) -> None:
    """重复跑不留重复行；行尾必须是 LF（CRLF 会让 Linux 上的 sha256sum -c 整份失败）。"""
    zip_path = _write(tmp_path / "Mikasa-v9.9.9-win64.zip", b"zip-bytes")
    inner_exe = _write(tmp_path / "Mikasa.exe", b"inner-exe-bytes")
    setup = _write(tmp_path / "Mikasa-Setup-9.9.9-win64.exe", b"setup-bytes")

    for _ in range(2):
        make_release.write_checksums(zip_path, inner_exe)
        sums = build_installer.append_checksum(setup)

    raw = sums.read_bytes()
    assert b"\r" not in raw
    lines = [line for line in raw.decode("utf-8").splitlines() if line.strip()]
    assert len(lines) == 3


def test_old_version_line_is_pruned(tmp_path: Path) -> None:
    """换版本号后，指向已不存在文件的旧行不该留在清单里。"""
    old_setup = tmp_path / "Mikasa-Setup-0.1.1-win64.exe"
    sums = tmp_path / "SHA256SUMS.txt"
    sums.write_text(f"{'a' * 64}  {old_setup.name}\n", encoding="utf-8", newline="\n")

    zip_path = _write(tmp_path / "Mikasa-v9.9.9-win64.zip", b"zip-bytes")
    inner_exe = _write(tmp_path / "Mikasa.exe", b"inner-exe-bytes")
    setup = _write(tmp_path / "Mikasa-Setup-9.9.9-win64.exe", b"setup-bytes")

    make_release.write_checksums(zip_path, inner_exe)
    build_installer.append_checksum(setup)

    assert old_setup.name not in _listed_names(sums)
    assert _listed_names(sums) == {zip_path.name, inner_exe.name, setup.name}
