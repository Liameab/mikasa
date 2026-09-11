# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（打包版 Mikasa）。

用法（在仓库根、已激活 .venv 的前提下）：

    pyinstaller packaging/Mikasa.spec --noconfirm

产物：dist/Mikasa/（onedir：启动比 onefile 快、且不会被解包到临时目录）。
把整个 dist/Mikasa 文件夹压缩发给别人，双击 Mikasa.exe 即用。

**资源与数据的分工**（对应 config/settings.py 的 resource_root / user_data_root）：
  - 打进去的（只读）：前端静态文件、profile 配置、示例语料、黄金集、.env 模板；
    运行时由 sys._MEIPASS 定位，用户改不了也不该改。
  - 不打进去的（可写）：数据库、uploads、索引快照、日志——运行时落到
    %LOCALAPPDATA%\\Mikasa，与安装位置无关。
"""

import sys
from pathlib import Path

SPEC_DIR = Path(SPECPATH).resolve()  # noqa: F821 - PyInstaller 注入
ROOT = SPEC_DIR.parent

# ---- 只读资源：目录整体带上，运行时按 <资源根>/<相对路径> 读取 ----
datas = [
    (str(ROOT / "src" / "mikasa" / "web" / "static"), "mikasa/web/static"),
    (str(ROOT / "config"), "config"),
    (str(ROOT / "sample-corpus"), "sample-corpus"),
    (str(ROOT / "evals"), "evals"),
    (str(ROOT / ".env.example"), "."),
]

# ---- 可选依赖：延迟导入 / 数据文件，静态分析看不见，显式声明 ----
hiddenimports = [
    "mikasa.providers.embedding",
    "mikasa.providers.reranker",
    "mikasa.providers.llm",
    "pymupdf",  # PDF 解析（延迟导入）
    "docx",  # python-docx（延迟导入）
    "multipart",  # python-multipart：上传端点运行时依赖
]

# 需要连数据文件一起收的包（jieba 的 5MB 词典、fastembed 的元数据等）
datas_packages = ["jieba"]

a = Analysis(  # noqa: F821 - PyInstaller 注入
    [str(SPEC_DIR / "entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # 明确排除：大而不用的科学计算栈（numpy 是必需的，不能排）
    excludes=["matplotlib", "tkinter", "pytest", "IPython", "pandas"],
    noarchive=False,
)

# jieba 的词典走 pkg_resources 读取，必须把包内数据一起收进来，
# 否则分词器会**静默降级**成 bigram（程序照跑、检索质量下滑，最难查的一类）
for pkg in datas_packages:
    try:
        from PyInstaller.utils.hooks import collect_data_files

        a.datas += collect_data_files(pkg)
    except Exception:  # noqa: BLE001 - 包缺失时不必阻断打包（有 doctor 兜底检查）
        pass

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Mikasa",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # 保留控制台：日志、错误提示、Ctrl+C 停止服务都靠它
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Mikasa",
)
