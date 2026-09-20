#!/usr/bin/env python
"""生成 THIRD_PARTY_NOTICES.md：随包分发的第三方组件与其许可证。

**为什么要这份文件**：分发二进制时随附第三方许可证与版权声明是通行要求
（MIT/BSD/Apache 都要求保留声明，AGPL 还要求提供对应源码）。缺了它不是
"没人管"，而是真被追究时说不清。

**为什么不能只读 pyproject**：pyproject 只列直接依赖，而**随 exe 分发**的是
PyInstaller 实际收进去的那一大票（含全部传递依赖）。

**为什么也不能只读 dist-info**：PyInstaller 只为**一部分**包拷元数据
（2026-09-11 实测：实际分发 23 个发行版，`_internal/*.dist-info` 只有 10 个）。
只看它会漏报一半，而**漏报的合规清单比没有更危险**——它看着像权威。所以这里
以"打包产物里的顶层包"为准，再用 `packages_distributions()` 映射回发行版，
dist-info 只作补充。

用法：
  python tools/make_third_party_notices.py            # 读 dist/Mikasa/_internal
  python tools/make_third_party_notices.py --out X.md
退出码：0 = 生成成功；1 = 找不到打包产物
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCAN = REPO_ROOT / "dist" / "Mikasa" / "_internal"
DEFAULT_OUT = REPO_ROOT / "THIRD_PARTY_NOTICES.md"

# 带 copyleft 条款的许可证：分发它们不是"附个声明"就能了事，要单独点名
COPYLEFT_HINTS = ("AGPL", "GPL", "LGPL", "MPL", "EUPL", "SSPL")

# 随包内置的**非 Python 资源**（扫描 Python 包看不到，故在此手工维护）。
# 新增 vendor 资源/第三方随包文件必须补一行——清单漏报第三方许可，是分发层面的合规问题。
BUNDLED_ASSETS = [
    {
        "name": "KaTeX",
        "version": "0.18.7",
        "license": "MIT",
        "dir": REPO_ROOT / "src" / "mikasa" / "web" / "static" / "vendor" / "katex",
        "purpose": "数学公式渲染（renderToString 纯字符串渲染，离线，无运行时依赖）",
    },
    {
        "name": "Inno Setup 简体中文翻译（ChineseSimplified.isl）",
        "version": "6.5.0+",
        "license": "Inno Setup License（社区翻译，随 Inno Setup 分发）",
        "dir": REPO_ROOT / "packaging" / "languages",
        "purpose": "安装向导的中文界面：Inno 不自带非官方翻译，CI 上那份 6.7.1 就没有",
        "license_note": "文件头部注明来源与维护者（jrsoftware.org/files/istrans，"
        "Zhenghan Yang），随 packaging/languages/ 一起分发。",
    },
]


def is_copyleft(package: dict) -> bool:
    haystack = package["license"] + " " + " ".join(package["classifiers"])
    upper = haystack.upper()
    # "LGPL" 里含 "GPL"，用同一条判据即可（都属于要提醒的范畴）
    return any(hint in upper for hint in COPYLEFT_HINTS)


def bundled_distributions(scan_dir: Path) -> tuple[set[str], list[str]]:
    """找出打包产物里**真正分发了哪些发行版**，返回 (发行版名, 无法归类的顶层名)。

    两个来源缺一不可，理由见模块注释。
    """
    from importlib.metadata import packages_distributions

    mapping = packages_distributions()
    names: set[str] = set()
    unmapped: list[str] = []
    for entry in scan_dir.iterdir():
        top = entry.name
        if entry.is_dir():
            if top.endswith((".dist-info", ".data")) or top == "__pycache__":
                continue
        elif entry.suffix in (".py", ".pyd", ".so"):
            top = entry.stem
        else:
            continue
        dists = mapping.get(top)
        if dists:
            names.update(dists)
        else:
            unmapped.append(top)
    names |= {d.name.rsplit("-", 1)[0] for d in scan_dir.glob("*.dist-info")}
    return names, sorted(unmapped)


def license_texts(dist) -> list[str]:
    """从已安装发行版的文件清单里找许可证正文并读出来。"""
    texts: list[str] = []
    seen: set[str] = set()
    for file in dist.files or []:
        name = Path(str(file)).name
        if name in seen or not re.match(r"(LICENSE|COPYING|NOTICE)", name, re.IGNORECASE):
            continue
        seen.add(name)
        try:
            body = Path(dist.locate_file(file)).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if body.strip():
            texts.append(f"#### {name}\n\n```\n{body.strip()}\n```")
    return texts


def collect(scan_dir: Path) -> list[dict]:
    """收集全部随包分发的第三方组件（元数据从**当前环境**读）。"""
    from importlib.metadata import PackageNotFoundError, distribution

    names, unmapped = bundled_distributions(scan_dir)
    packages: list[dict] = []
    for name in sorted(names):
        try:
            dist = distribution(name)
        except PackageNotFoundError:
            continue
        meta = dist.metadata
        classifiers = meta.get_all("Classifier") or []
        # License 字段可能整个是空白（有些包只给 Classifier）——直接 splitlines()[0]
        # 会 IndexError，必须先判空（2026-09-11 实测踩中）
        license_field = (meta.get("License") or "").strip()
        license_first = license_field.splitlines()[0].strip()[:120] if license_field else ""
        license_label = (
            meta.get("License-Expression")
            or license_first
            or next((c.split("::")[-1].strip() for c in classifiers if "License" in c), "")
            or "(未标注)"
        )
        packages.append(
            {
                "name": meta.get("Name", name),
                "version": meta.get("Version", "?"),
                "license": license_label,
                "classifiers": [c.split("::")[-1].strip() for c in classifiers if "License" in c],
                "texts": license_texts(dist),
            }
        )
    if unmapped:
        packages.append(
            {
                "name": "(未归属发行版)",
                "version": "-",
                "license": "见说明",
                "classifiers": [],
                "texts": [
                    "以下顶层模块存在于打包产物中，但无法映射到具体发行版："
                    + "、".join(unmapped)
                    + "。它们多为 Python 内置扩展或本项目自身代码；"
                    "若其中混有第三方模块，说明本清单有漏报，需人工核对。"
                ],
            }
        )
    return packages


def render(packages: list[dict]) -> str:
    copyleft = [p for p in packages if is_copyleft(p)]
    lines = [
        "# Third-Party Notices",
        "",
        "Mikasa 的发布包内含下列第三方组件。本文件由 "
        "`tools/make_third_party_notices.py` 从**实际打进包的模块**反查生成，"
        "不是照 pyproject 抄的（pyproject 只列直接依赖，会漏掉全部传递依赖）。",
        "",
        f"组件总数：**{len(packages)}**"
        + (f"（另有 {len(BUNDLED_ASSETS)} 个随包非 Python 资源）" if BUNDLED_ASSETS else ""),
        "",
    ]
    if copyleft:
        lines += [
            "## ⚠ 带 copyleft 条款的组件（分发前请确认义务）",
            "",
            "下列组件的许可证带 copyleft 条款，**不是「附上声明」就够了**："
            "AGPL/GPL 类通常要求以同许可证提供完整对应源码（或购买商业许可）。",
            "Mikasa 自身以 MIT 发布，与之存在冲突。",
            "",
            "| 组件 | 版本 | 许可证 |",
            "| --- | --- | --- |",
        ]
        lines += [f"| {p['name']} | {p['version']} | {p['license']} |" for p in copyleft]
        lines.append("")

    lines += ["## 全部组件", "", "| 组件 | 版本 | 许可证 |", "| --- | --- | --- |"]
    lines += [
        f"| {p['name']} | {p['version']} | {p['license']} |"
        for p in sorted(packages, key=lambda x: x["name"].lower())
    ]
    lines += ["", "## 许可证正文", "", "以下正文来自各组件随包附带的许可证文件。", ""]
    for package in sorted(packages, key=lambda x: x["name"].lower()):
        if not package["texts"]:
            continue
        lines.append(f"### {package['name']} {package['version']} — {package['license']}")
        lines.append("")
        lines += package["texts"]
        lines.append("")

    if BUNDLED_ASSETS:
        lines += [
            "## 随包内置的非 Python 资源",
            "",
            "这些不是 Python 包（扫描反查看不到），因此**手工维护**：新增 vendor 资源或"
            "随包第三方文件必须在此补一行，否则清单会漏报。",
            "",
            "| 组件 | 版本 | 许可证 | 位置 | 用途 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for asset in BUNDLED_ASSETS:
            rel = asset["dir"].relative_to(REPO_ROOT).as_posix()
            lines.append(
                f"| {asset['name']} | {asset['version']} | {asset['license']} "
                f"| `{rel}/` | {asset['purpose']} |"
            )
        lines += ["", "### 这些资源的许可证正文", ""]
        for asset in BUNDLED_ASSETS:
            license_file = asset["dir"] / "LICENSE"
            body = (
                license_file.read_text(encoding="utf-8").strip()
                if license_file.is_file()
                else asset.get("license_note", "(见该组件随包文件内的说明)")
            )
            lines += [
                f"#### {asset['name']} {asset['version']} — {asset['license']}",
                "",
                body,
                "",
            ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="生成第三方许可证清单")
    parser.add_argument("--scan", default=str(DEFAULT_SCAN), help="打包产物目录")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()

    scan_dir = Path(args.scan)
    if not scan_dir.is_dir():
        print(f"找不到打包产物目录：{scan_dir}（先跑 PyInstaller 打包）")
        return 1

    packages = collect(scan_dir)
    real = [p for p in packages if p["name"] != "(未归属发行版)"]
    if len(real) < 10:
        # 少于 10 个几乎肯定是打包产物不完整或映射表坏了——宁可失败也别发出漏报的清单
        print(f"只识别出 {len(real)} 个第三方组件，明显偏少，先查清再生成（避免漏报）。")
        return 1

    out = Path(args.out)
    out.write_text(render(packages), encoding="utf-8")
    with_text = sum(1 for p in packages if p["texts"])
    copyleft = [p["name"] for p in packages if is_copyleft(p)]
    print(
        f"已生成 {out.relative_to(REPO_ROOT)}：{len(packages)} 个组件"
        f"（{with_text} 个含许可证正文；copyleft 组件 {len(copyleft)} 个：{copyleft or '无'}）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
