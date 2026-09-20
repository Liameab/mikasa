"""安装向导脚本（packaging/Mikasa.iss）的静态检查。

为什么需要：这条脚本**只在发版时被 Inno Setup 编译**，本地与 CI 的编译器
还不一样（本机 Inno 7 + 全套语言包，CI 上 choco 静默装的 6.7.1 没有任何
"非官方翻译"）。2026-09-20 首次跑 Release 流水线就栽在这里：

    Error on line 70 … Couldn't open include file
    "c:\\program files (x86)\\inno setup 6\\Languages\\ChineseSimplified.isl"

—— `[Languages]` 指着编译器自带的翻译目录，而那台机器上根本没装那份可选组件。
改用随仓库带的语言文件后，这两条断言就是那道闸：谁再把它指回 `compiler:`，
或把随包文件删掉，会在**单元测试**里当场变红，而不是等到发版。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGING = REPO_ROOT / "packaging"
ISS = PACKAGING / "Mikasa.iss"


def test_language_file_is_vendored_not_borrowed_from_the_compiler() -> None:
    """向导语言必须用仓库里那份：编译器自带的翻译是可选组件，CI 上没有。"""
    src = ISS.read_text(encoding="utf-8")
    match = re.search(r'MessagesFile:\s*"([^"]+)"', src)
    assert match, "[Languages] 里没有 MessagesFile"
    declared = match.group(1)
    # 只看取值：注释里写着"别再用 compiler:…"是允许的（本文件顶部也这么写）
    assert not declared.lower().startswith("compiler:"), (
        "MessagesFile 又指回编译器自带的翻译目录了——CI 的 Inno 没装那份可选组件，"
        "发版会直接失败（见本文件顶部的事故记录）"
    )
    path = PACKAGING / declared.replace("\\", "/")
    assert path.is_file(), f"随包的语言文件不存在：{path.relative_to(REPO_ROOT)}"
    text = path.read_text(encoding="utf-8")  # 必须是 UTF-8（文件头也这么声明）
    assert "[LangOptions]" in text and "LanguageName=" in text, "语言文件内容不像 .isl"


def test_referenced_repo_files_exist() -> None:
    """[Files] 里引用的**仓库内**文件必须真实存在（dist/ 是构建产物，跳过）。

    路径基准是 .iss 所在目录（Inno 的规矩），所以 "..\\LICENSE" 指仓库根的 LICENSE。
    """
    src = ISS.read_text(encoding="utf-8")
    checked = 0
    for raw in re.findall(r'Source:\s*"([^"]+)"', src):
        rel = raw.replace("\\", "/")
        if rel.startswith("../dist/"):
            continue  # 构建产物：CI 上 pytest 跑在 PyInstaller 之前，这里不该断言
        target = (PACKAGING / rel).resolve()
        assert target.exists(), f"Mikasa.iss 引用了不存在的文件：{raw}"
        checked += 1
    assert checked >= 2, "[Files] 至少该引用 LICENSE 与第三方声明，解析是不是坏了？"
