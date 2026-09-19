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
    "webview",  # 独立窗口套壳（系统 WebView2 渲染，双击不再是浏览器标签页）
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    # pythonnet / CLR：pywebview 在 Windows 上靠它托管 WinForms + WebView2。
    # 单文件模式（onefile）下静态分析看不见这些动态导入，缺了会**静默降级**成
    # Edge --app 窗口（2026-09-11 实测：窗口开出来是 msedge 而不是独立进程）。
    "clr",
    "clr_loader",
    "pythonnet",
]

# pythonnet 的原生运行时（Python.Runtime.dll + clr_loader 的 hostfxr 逻辑）
# 不随包自动走：collect_all 把它们捞进来，否则单文件模式下 CLR 初始化失败
_py_net_datas, _py_net_bins, _py_net_hidden = [], [], []
for _pkg in ("pythonnet", "clr_loader"):
    try:
        from PyInstaller.utils.hooks import collect_all

        _d, _b, _h = collect_all(_pkg)
        _py_net_datas += _d
        _py_net_bins += _b
        _py_net_hidden += _h
    except Exception:  # noqa: BLE001 - 包缺失不阻断（entry.py 有降级路径）
        pass

a = Analysis(  # noqa: F821 - PyInstaller 注入
    [str(SPEC_DIR / "entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=_py_net_bins,
    datas=datas + _py_net_datas,
    hiddenimports=hiddenimports + _py_net_hidden,
    hookspath=[],
    runtime_hooks=[],
    # 明确排除：大而不用的科学计算栈（numpy 是必需的，不能排）
    # mypy/ast_serialize/librt 是**开发工具**，用户侧一行都用不到——它们是顺着
    # `pydantic.mypy`（pydantic 给 mypy 用的插件模块）被牵连进来的：源码树里
    # 存在这个文件，PyInstaller 就会把它 import 的整个 mypy 收进包（2026-09-11
    # 实测 _internal 里躺着 mypy 904K + ast_serialize 2.5M + librt 40K）。
    # 那是给**类型检查**用的，运行期永远走不到，排掉纯赚。
    excludes=[
        "matplotlib",
        "tkinter",
        "pytest",
        "IPython",
        "pandas",
        "mypy",
        "ast_serialize",
        "librt",
    ],
    noarchive=False,
)

# jieba 的词典走 pkg_resources 读取，必须把包内数据一起收进来，否则分词器会
# **静默降级**成 bigram（程序照跑、检索质量下滑，最难查的一类）。
# 不在这里手动 `a.datas +=`：Analysis 之后的 a.datas 已是规范化格式（三元组），
# 追加 collect_data_files 的二元组结果会撞 "not enough values to unpack"
# （2026-09-11 首次打包实测）。pyinstaller-hooks-contrib 自带 hook-jieba，
# 会在 Analysis 阶段自动收集——doctor 的"分词器 == jieba"检查是最后一道保险。

pyz = PYZ(a.pure)  # noqa: F821

# ---- 版本信息资源：让 exe 的「属性 → 详细信息」有内容 ----
# 不写的话 ProductName / FileVersion / CompanyName 全是空白——这是"不像正经软件"
# 最直观的一处（用户排障时让你看版本号，你只能说"我不知道"）。
# 版本号从 src/mikasa/__init__.py 读（**单一来源**），不在这里写死第二份。
# 生成到 build/ 下：那是 gitignore 的构建中间产物区，不必往仓库里塞生成物。
import re as _re  # noqa: E402

_ver_src = (ROOT / "src" / "mikasa" / "__init__.py").read_text(encoding="utf-8")
_ver_match = _re.search(r'__version__\s*=\s*"([^"]+)"', _ver_src)
if _ver_match is None:
    raise SystemExit("读不到 __version__（src/mikasa/__init__.py 改了写法？）")
_version = _ver_match.group(1)
# FixedFileInfo 要 4 段整数："0.1.4" → (0, 1, 4, 0)
_quad = tuple(int(p) for p in _version.split(".")) + (0, 0, 0, 0)

_vi_dir = ROOT / "build"
_vi_dir.mkdir(exist_ok=True)
VERSION_FILE = _vi_dir / "version_info.txt"
VERSION_FILE.write_text(
    f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={_quad[:4]}, prodvers={_quad[:4]},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404B0', [
        StringStruct('CompanyName', 'Liameab'),
        StringStruct('FileDescription', 'Mikasa'),
        StringStruct('FileVersion', '{_version}'),
        StringStruct('InternalName', 'Mikasa'),
        StringStruct('LegalCopyright', 'Copyright (c) 2026 Liameab'),
        StringStruct('OriginalFilename', 'Mikasa.exe'),
        StringStruct('ProductName', 'Mikasa'),
        StringStruct('ProductVersion', '{_version}'),
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [0x0804, 1200])])
  ]
)
""",
    encoding="utf-8",
)

# ---- 文件夹模式（onedir）----
# 为什么最终选它而不是单文件：单文件每次启动都要把 105MB 解压到临时目录，
# 实测 7-10 秒毫无响应，用户直接判定为"卡死"（2026-09-11 实测反馈）。
# 文件夹模式启动 2 秒。"用户找不到 exe" 的问题交给**安装程序**解决——
# 装完自动创建桌面与开始菜单快捷方式，用户根本不需要知道文件在哪。
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Mikasa",
    # 应用图标（tools/make_icon.py 生成；2026-09-11 起改为"图片 + 圆角"，见该脚本）
    # ⚠ **换图标后必须清掉 EXE 级中间产物再打包**：
    #     rm build/Mikasa/{EXE-00.toc,Mikasa.exe,Mikasa.pkg,PKG-00.toc}
    #   图标只影响 EXE 这一级的输入，而 PyInstaller 的增量判断抓不到它——实测
    #   换了 ico 重打包，Analysis/COLLECT 都跑了新的、EXE 却复用了上一轮的缓存，
    #   COLLECT 只是把旧 exe 拷进 dist，于是**新时间戳 + 旧图标**，看着像"没生效"。
    icon=str(SPEC_DIR / "Mikasa.ico"),
    version=str(VERSION_FILE),  # 上一步生成：属性页里的产品名/版本/版权
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # 无控制台：双击时不该弹黑窗口（正经软件的样子）。代价是出错时用户看不见，
    # 由 entry.py 的系统弹窗兜底，日志照常落 %LOCALAPPDATA%\Mikasa\logs
    console=False,
)


coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Mikasa",
)
