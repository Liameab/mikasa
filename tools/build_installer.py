#!/usr/bin/env python
"""编译 Mikasa 的单文件安装向导（Inno Setup → dist/Mikasa-Setup-<版本>-win64.exe）。

与 `tools/make_release.py` 的分工：
  - 本脚本产 **单文件 Setup.exe**（双击 → 下一步 → 勾桌面快捷方式 → 装完即用）。
  - make_release.py 产 **zip 绿色包**（解压即用 + install.ps1 兜底安装）。

两者用**同一份载荷**（`dist/Mikasa`，PyInstaller 的 onedir 产物），所以先跑
PyInstaller，再跑这两个。

**版本号单一来源**：从 `src/mikasa/__init__.py` 读，用 `/DAppVersion=` 传给 ISCC
（.iss 里只写 `#ifndef` 默认值）。不在 .iss 里再写一份——那迟早和 exe 属性页对不上。

用法：
  python tools/build_installer.py
  python tools/build_installer.py --iscc "D:\\Inno Setup 7\\Inno Setup 7\\ISCC.exe"
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ISS = REPO_ROOT / "packaging" / "Mikasa.iss"
APP_DIR = REPO_ROOT / "dist" / "Mikasa"

# ISCC 的常见安装位置：Inno Setup 7 的安装器默认装到 D 盘也是常见的（本机就是）
_ISCC_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Inno Setup 7\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 7\ISCC.exe"),
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    Path(r"D:\Inno Setup 7\Inno Setup 7\ISCC.exe"),
]


def find_iscc(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise SystemExit(f"指定的 ISCC 不存在：{path}")
        return path
    found = shutil.which("ISCC") or shutil.which("iscc")
    if found:
        return Path(found)
    for candidate in _ISCC_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise SystemExit(
        "找不到 ISCC.exe（Inno Setup 的编译器）。安装 Inno Setup 后重试，或用 --iscc 指定路径。"
    )


def app_version() -> str:
    text = (REPO_ROOT / "src" / "mikasa" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if match is None:
        raise SystemExit("读不到 __version__（src/mikasa/__init__.py 改了写法？）")
    return match.group(1)


def append_checksum(path: Path) -> Path | None:
    """把刚编译出的 Setup.exe 追加进 dist/SHA256SUMS.txt。

    为什么写在这里而不是 make_release.py：那个脚本跑在编译**之前**，它写校验和
    时 Setup.exe 还不存在——于是清单里只有 zip 与内层 exe，而绝大多数用户下载的
    偏偏是 Setup.exe，成了唯一没有校验和的产物（2026-09-11 发布前复查发现）。

    幂等：同名行先删再追加，重编译不会留两行。清单不存在则返回 None——那份清单
    归 make_release.py 管，这里不凭空造一份只含 Setup 的残缺版。
    """
    sums = path.parent / "SHA256SUMS.txt"
    if not sums.is_file():
        return None
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    kept = [
        line
        for line in sums.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.endswith("  " + path.name)
    ]
    kept.append(f"{digest}  {path.name}")
    # newline="\n"：与 make_release.write_checksums 同一纪律（CRLF 会让
    # Linux/macOS 上的 sha256sum -c 整份读不了），见那边的注释
    sums.write_text("\n".join(kept) + "\n", encoding="utf-8", newline="\n")
    return sums


def main() -> int:
    parser = argparse.ArgumentParser(description="编译单文件安装向导")
    parser.add_argument("--iscc", default=None, help="ISCC.exe 路径（缺省自动查找）")
    args = parser.parse_args()

    if not (APP_DIR / "Mikasa.exe").is_file():
        raise SystemExit(
            f"没找到 {APP_DIR / 'Mikasa.exe'}——先跑 PyInstaller 打包"
            "（pyinstaller packaging/Mikasa.spec --noconfirm）"
        )

    version = app_version()
    iscc = find_iscc(args.iscc)
    print(f"用 {iscc} 编译，应用版本 {version}")

    result = subprocess.run(
        [str(iscc), str(ISS), f"/DAppVersion={version}"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        errors="replace",
    )
    tail = (result.stdout or "").strip().splitlines()[-6:]
    for line in tail:
        print("  " + line)
    if result.returncode != 0:
        print((result.stderr or "").strip()[-800:])
        raise SystemExit(f"ISCC 编译失败（退出码 {result.returncode}）")

    out = REPO_ROOT / "dist" / f"Mikasa-Setup-{version}-win64.exe"
    if not out.is_file():
        raise SystemExit(f"ISCC 报成功但没找到产物：{out}")
    print(f"安装向导已生成：{out.relative_to(REPO_ROOT)}（{out.stat().st_size / 1e6:.0f} MB）")
    sums = append_checksum(out)
    if sums is not None:
        print(f"校验和已更新：{sums.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
