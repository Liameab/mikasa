"""前端静态资源的一致性检查（零浏览器、零依赖）。

为什么存在：2026-09-11 事故 —— qa.js 在同一作用域重复声明 `const card`，
ESM 求值期抛 SyntaxError，**整页 JS 静默失效**（表现为"正在连接服务…"、
会话树空白），而后端 415 个测试全绿也拦不住它。前端没有构建链、没有单元
测试，这类"语法/链接层面"的错误一旦漏到浏览器就是整页死，故在此兜底：

  1. node --check：真语法解析（重复声明、括号不配对……）；
  2. 导入图检查：每个相对 import 的目标文件存在，且**具名导入确实在目标
     模块里被导出**（模块链接期错误——node --check 查不出，同样打死整页）；
  3. HTML 引用的静态资源存在（改文件名的低级事故）。

模块顶层的运行时错误（如漏 import 导致的 ReferenceError）静态查不出来，
仍由无头 Chrome 探针覆盖运行时（tools/chrome_probe.py 已能捕获未捕获异常，
见 tools 里 2026-09-11 的注释）。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
STATIC_DIR = REPO_ROOT / "src" / "mikasa" / "web" / "static"
JS_DIR = STATIC_DIR / "js"

# 具名导入：`import { a, b as c } from "./x.js"`（`[^}]*` 可跨行）
_IMPORT_RE = re.compile(
    r"""import\s*(?:\{([^}]*)\})?\s*(?:\*\s*as\s*[A-Za-z_$][\w$]*\s*)?(?:[A-Za-z_$][\w$]*\s*)?from\s*["']([^"']+)["']"""
)
# `import "./x.js"`（纯副作用导入，无 from）
_IMPORT_BARE_RE = re.compile(r"""import\s*["']([^"']+)["']""")


def js_files() -> list[Path]:
    return sorted(JS_DIR.glob("*.js"))


def collect_exports(src: str) -> set[str]:
    """粗粒度提取一个模块的导出名（够用于"导入了不存在的名字"这类检查）。"""
    names: set[str] = set()
    names |= set(
        re.findall(
            r"export\s+(?:async\s+)?(?:function\*?|const|let|var|class)\s+([A-Za-z_$][\w$]*)",
            src,
        )
    )
    for block in re.findall(r"export\s*\{([^}]*)\}", src):  # 支持 "a as b"
        for part in block.split(","):
            part = part.strip()
            if part:
                names.add(part.split(" as ")[-1].strip())
    if re.search(r"export\s+default\b", src):
        names.add("default")
    return names


def test_js_esm_syntax(tmp_path: Path) -> None:
    """node --check 逐个解析（复制成 .mjs 让 node 按 ESM 解析）。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 node，跳过前端语法检查")
    failures = []
    for f in js_files():
        tmp = tmp_path / (f.stem + ".mjs")
        tmp.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
        proc = subprocess.run([node, "--check", str(tmp)], capture_output=True, text=True)
        if proc.returncode != 0:
            failures.append(f"{f.name}:\n{proc.stderr.strip()}")
    assert not failures, "前端 ESM 语法错误（会导致整页 JS 失效）：\n" + "\n".join(failures)


def test_js_relative_imports_resolve() -> None:
    """相对导入：目标文件存在 + 具名导入确实被导出。"""
    exports = {f.name: collect_exports(f.read_text(encoding="utf-8")) for f in js_files()}
    for f in js_files():
        src = f.read_text(encoding="utf-8")
        specs = [m.group(2) for m in _IMPORT_RE.finditer(src)]
        specs += [m.group(1) for m in _IMPORT_BARE_RE.finditer(src)]
        for spec in specs:
            if not spec.startswith("."):
                continue  # 只查相对导入（项目无第三方 ESM 依赖）
            target = (f.parent / spec).resolve()
            assert target.exists(), f"{f.name} 导入了不存在的模块：{spec}"
            for m in _IMPORT_RE.finditer(src):
                if m.group(2) != spec or not m.group(1):
                    continue
                for part in m.group(1).split(","):
                    local = part.strip().split(" as ")[0].strip()
                    if local:
                        assert local in exports.get(target.name, set()), (
                            f"{f.name} 从 {spec} 导入了未导出的名字：{local}"
                            "（模块链接期错误，会让整页 JS 失效）"
                        )


def test_html_static_assets_exist() -> None:
    """三页 HTML 里 /static/... 的 script/link 资源必须真实存在。"""
    for html in sorted(STATIC_DIR.glob("*.html")):
        src = html.read_text(encoding="utf-8")
        for m in re.finditer(r"""(?:src|href)=["'](/static/[^"'?#]+)["']""", src):
            rel = m.group(1).removeprefix("/static/")
            assert (STATIC_DIR / rel).exists(), f"{html.name} 引用了不存在的资源：{m.group(1)}"


def test_katex_is_bundled_and_wired_into_every_page() -> None:
    """公式渲染靠随包内置的 KaTeX（离线）。文件缺一个 / 有页面漏引，都是静默退化。

    KaTeX 缺席时 common.js 会**原样显示 LaTeX**（不吞内容），所以这种坏法在
    后端测试里完全看不见——只能在这里钉住"文件在 + 四页都引了 + 样式顺序对"。
    """
    vendor = STATIC_DIR / "vendor" / "katex"
    for rel in (
        "katex.min.js",
        "katex.min.css",
        "LICENSE",
        "fonts/KaTeX_Main-Regular.woff2",  # 主字体（中文正文里的字母/数字全靠它）
        "fonts/KaTeX_Math-Italic.woff2",  # 变量斜体
    ):
        assert (vendor / rel).is_file(), f"KaTeX 内置文件缺失：vendor/katex/{rel}"

    for html in sorted(STATIC_DIR.glob("*.html")):
        src = html.read_text(encoding="utf-8")
        assert "/static/vendor/katex/katex.min.js" in src, (
            f"{html.name} 没引 KaTeX 脚本：公式会裸奔"
        )
        # 样式必须排在 style.css **之前**：KaTeX 自带 `font:` 简写，同优先级下
        # 只有后加载的项目样式才能覆盖它的字号
        katex_css = src.find("/static/vendor/katex/katex.min.css")
        own_css = src.find("/static/css/style.css")
        assert katex_css != -1, f"{html.name} 没引 KaTeX 样式表"
        assert 0 <= katex_css < own_css, f"{html.name} 的 KaTeX 样式应排在 style.css 之前"


def test_font_mime_types_are_registered(offline_settings) -> None:
    """静态服务必须认字体类型：Windows 注册表查不到 .woff2 → 会回 octet-stream。

    公式的字体全在 vendor/katex/fonts/ 下，靠 `@font-face` 加载。MIME 不对未必
    立刻炸（浏览器对字体不强制校验 MIME），但一旦退化成兜底字形，表现是"公式
    看着怪"这种最难归因的问题——所以在建 app 时就显式注册。
    """
    import mimetypes

    from mikasa.web.app import create_app

    create_app(offline_settings)
    assert mimetypes.guess_type("katex.min.woff2")[0] == "font/woff2"
    assert mimetypes.guess_type("x.woff")[0] == "font/woff"
    assert mimetypes.guess_type("x.ttf")[0] == "font/ttf"
