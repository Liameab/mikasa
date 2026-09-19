#!/usr/bin/env python
"""把 dist/Mikasa 打成一个能直接发给别人的发布包（应用 + 安装程序）。

产物：dist/Mikasa-v<版本>-win64.zip，解压后的结构：

    Mikasa-v0.1.4-win64/
      Install.bat          ← 双击它安装（会问要不要建桌面快捷方式）
      install.ps1
      Uninstall.bat        ← 安装时也会被拷进安装目录
      uninstall.ps1
      安装说明.txt
      Mikasa/              ← 应用本体（onedir：Mikasa.exe + _internal）

为什么带两个入口：**绿色版与安装版都要能用**。直接双击 Mikasa/Mikasa.exe 就能跑
（onedir 不依赖注册表/安装位置），安装程序只是额外提供快捷方式与卸载入口——
不强迫只想试一下的人先走一遍安装。

前置：先跑 PyInstaller 出 dist/Mikasa（见 packaging/Mikasa.spec）。本脚本只做
"收集 + 压缩"，不触发构建——构建慢且失败原因多，分开跑更好定位。

用法：python tools/make_release.py [--skip-zip]
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DIST = REPO_ROOT / "dist"
APP_DIR = DIST / "Mikasa"
PACKAGING = REPO_ROOT / "packaging"

# 安装器这几个文件是"发布包的门面"，缺一个都不算能发（宁可这里失败，
# 也别发出一个双击 Install.bat 报错找不到 install.ps1 的包）
INSTALLER_FILES = ["Install.bat", "install.ps1", "Uninstall.bat", "uninstall.ps1"]

# 必须随包分发的法律文件：附第三方声明正是那些许可证的要求（MIT/BSD/Apache
# 都要求保留声明）——漏了它，发布包在合规上就是不完整的。缺文件直接失败。
LEGAL_FILES = ["LICENSE", "THIRD_PARTY_NOTICES.md"]


def app_version() -> str:
    """读版本号：优先问已安装的包，退回解析 pyproject（CI 上可能没 pip install -e）。"""
    try:
        sys.path.insert(0, str(REPO_ROOT / "src"))
        import mikasa

        return mikasa.__version__
    except Exception:  # noqa: BLE001
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if not match:
            raise SystemExit(
                "读不到版本号（既 import 不到 mikasa，pyproject 里也没有 version）"
            ) from None
        return match.group(1)


README = """Mikasa
================================

开始之前：先装 Ollama 和模型（重要）
------------------------------------
Mikasa 的问答默认跑**本机模型**，所以要先把 Ollama 装好，否则双击后
会弹「服务启动超时」——它连不上模型服务。

  1) 装 Ollama：到 https://ollama.com/download 下载 Windows 版，双击装完
     （装完它会自动常驻后台，任务栏能看到小羊驼图标）
  2) 拉默认模型：按 Win+R 输入 cmd 回车，敲下面这行（约 5 GB，只下一次）：
       ollama pull qwen3:8b
  3) 然后才打开 Mikasa。

不想装 Ollama 也可以：改用云端 API（见下面「模型从哪来」）。

另外**第一次用需要联网**：Mikasa 会自动下载中文向量模型（约 100 MB），
之后就完全离线了。

怎么用（两种，选一种就行）
--------------------------

【方式一：直接绿色运行】
  双击 Mikasa\\Mikasa.exe。独立窗口打开，不需要安装。

【方式二：安装（推荐，有快捷方式和卸载入口）】
  双击 Install.bat，按提示走：
    1) 回车用默认安装位置（也可自己输路径）
    2) 问你要不要建桌面快捷方式（y/n）
    3) 问你要不要在开始菜单建项（y/n）
  装完就能在桌面/开始菜单找到 Mikasa；卸载走「设置 → 应用和功能」，
  或进安装目录双击 Uninstall.bat。

第一次打开会看到引导面板
------------------------
新库是空的，面板会告诉你三步怎么上手（传文档 → 提问 → 点 [n] 看原文出处）。

我的资料存在哪
--------------
在 %LOCALAPPDATA%\\Mikasa（数据库、上传的原文、索引、日志）。
**卸载不会删它**——重装即恢复。要彻底清掉请手动删除该目录。

模型从哪来
----------
默认用本机 Ollama 的本地模型（免费、离线）。想换云端模型：打开 Mikasa，
点右上角 ⚙ →「模型」→ 选一个来源（DeepSeek / SiliconFlow / 自定义）→
粘贴 API 密钥 → 「保存并生效」，立即切换、不用重启也不用改任何文件。
（密钥只存本机 %LOCALAPPDATA%\\Mikasa\\.env，不会上传。）

出错了怎么办
------------
日志在 %LOCALAPPDATA%\\Mikasa\\logs\\mikasa.log。
双击报错时弹的提示框里也会给出这个路径。
"""


def build_release_dir(version: str) -> Path:
    if not (APP_DIR / "Mikasa.exe").is_file():
        raise SystemExit(
            f"没找到 {APP_DIR / 'Mikasa.exe'}——先跑 PyInstaller 打包（见 packaging/Mikasa.spec）"
        )

    missing = [name for name in INSTALLER_FILES if not (PACKAGING / name).is_file()]
    if missing:
        raise SystemExit(f"packaging/ 下缺少安装器文件：{missing}")
    missing_legal = [name for name in LEGAL_FILES if not (REPO_ROOT / name).is_file()]
    if missing_legal:
        raise SystemExit(
            f"仓库根缺少法律文件：{missing_legal}"
            "（THIRD_PARTY_NOTICES.md 用 tools/make_third_party_notices.py 生成）"
        )

    out = DIST / f"Mikasa-v{version}-win64"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    for name in INSTALLER_FILES:
        shutil.copy2(PACKAGING / name, out / name)
    for name in LEGAL_FILES:
        shutil.copy2(REPO_ROOT / name, out / name)
    # 安装说明用 UTF-8 with BOM：中文 Windows 的记事本读无 BOM 的 UTF-8 会乱码
    (out / "安装说明.txt").write_text(README, encoding="utf-8-sig")
    shutil.copytree(APP_DIR, out / "Mikasa")
    return out


def zip_dir(folder: Path) -> Path:
    """压缩。写入时把顶层目录一起带上——解压出来是一个文件夹而不是一堆散件。"""
    # 不能用 with_suffix(".zip")：它替换**最后一个后缀**，而 "Mikasa-v0.1.4-win64"
    # 的 ".0-win64" 会被当成后缀 → 得到 "Mikasa-v0.1.zip"（2026-09-11 实测踩中）。
    zip_path = folder.parent / (folder.name + ".zip")
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(folder.parent))
    return zip_path


def write_checksums(zip_path: Path, exe_path: Path) -> Path:
    """写 SHA256SUMS.txt —— 下载者据此验证包没被篡改/传坏。

    企业发布的惯例是把校验和与包一起给出；没有它，用户遇到"下载不完整导致
    装不上"时无从判断，只能反复重下。

    **只覆盖自己负责的两行，其余行原样保留**（2026-09-19 发布前复查）：本函数
    原先整份覆盖，而 Setup.exe 的哈希由 build_installer.py 追加。两个脚本的
    **执行顺序**于是决定了清单完不完整——先编译安装包、后打包，最终清单里就没有
    Setup 那一行，而应用内更新恰恰在清单里查它的哈希（install.py 的
    expected_sha256），**所有用户的一键更新都会失败在"校验和文件里没有该记录"**，
    且本地双击安装包一切正常、发布时看不出来。保留外来行之后任意顺序都对，同时
    顺手清掉指向已不存在文件的旧行（换了版本号之后上一次的 Setup 行）。

    **行尾必须是 LF**（`newline="\n"`）：默认在 Windows 上会写成 CRLF，而
    `sha256sum -c` 在 Linux/macOS 上读 CRLF 清单会**整份失败**
    （`'name'$'\r': No such file or directory`）——2026-09-19 独立复算时实测
    踩中。校验和文件是给跨平台下载者用的，行尾不能跟着构建机走。
    """
    lines = []
    names = set()
    for path in (zip_path, exe_path):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.name}")
        names.add(path.name)
    out = zip_path.parent / "SHA256SUMS.txt"
    # 外来行（安装包那一行归 build_installer.py 管）保留；指向已删文件的旧行丢掉
    kept: list[str] = []
    if out.is_file():
        for line in out.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            name = line.split("  ", 1)[-1].strip().lstrip("*")
            if name not in names and (out.parent / name).is_file():
                kept.append(line)
    out.write_text("\n".join(lines + kept) + "\n", encoding="utf-8", newline="\n")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="打包发布 zip（应用 + 安装程序）")
    parser.add_argument("--skip-zip", action="store_true", help="只收集目录，不压缩")
    args = parser.parse_args()

    version = app_version()
    folder = build_release_dir(version)
    app_mb = sum(f.stat().st_size for f in (folder / "Mikasa").rglob("*") if f.is_file()) / 1e6
    print(f"发布目录已就绪：{folder.relative_to(REPO_ROOT)}（应用本体 {app_mb:.0f} MB）")

    if args.skip_zip:
        return 0
    zip_path = zip_dir(folder)
    checksums = write_checksums(zip_path, folder / "Mikasa" / "Mikasa.exe")
    print(f"发布包：{zip_path.relative_to(REPO_ROOT)}（{zip_path.stat().st_size / 1e6:.0f} MB）")
    print(f"校验和：{checksums.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
