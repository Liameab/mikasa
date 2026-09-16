"""DESIGN.md 与 style.css 的一致性护栏（零浏览器、零新依赖）。

为什么存在：设计文档最容易烂的方式是"代码改了、文档没改"——一份过期的
色值比没有文档更坏（下一个人会照它写错）。这里不做审美判断，也不检查
正文（规则、Do/Don't 是给人读的），只核对三类**事实**：

  1. front-matter 能解析，且 `colors:` 里每个 token 的名字与值都和
     `style.css` `:root` 里的同名 CSS 变量逐字一致；
  2. 「Components / 组件」段里点名的每个 CSS 类（或 #id）都真实存在于
     样式表，带 `-*` 的族名按前缀核对；
  3. 圆角五档与 z-index 阶梯里的每个数值都在样式表里出现过。

**中英两份都查**（`DESIGN.md` 是事实源，`docs/zh-CN/DESIGN.md` 是镜像）：
镜像不得各自漂移，中文版漏改一个色值同样会红。

上游改了 token 却没动文档，这一份会红——把文档拉回与代码一致，或者把
新值写进文档，两条路都行，但别让两边各自漂着。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
STYLE_CSS = REPO_ROOT / "src" / "mikasa" / "web" / "static" / "css" / "style.css"

# (文档路径, Components 段的中英标题)——两份文档结构同源，仅标题词不同
DESIGN_FILES = [
    (REPO_ROOT / "DESIGN.md", "Components"),
    (REPO_ROOT / "docs" / "zh-CN" / "DESIGN.md", "组件"),
]
DESIGN_IDS = ["en", "zh-CN"]

# front-matter：文件开头的 --- 块（DESIGN.md 的 token 段）
_FRONT_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
# `:root { ... }` 里的 CSS 变量（值取到分号为止，行尾注释不算值）
_ROOT_RE = re.compile(r":root\s*\{(.*?)\}", re.DOTALL)
_VAR_RE = re.compile(r"--([\w-]+)\s*:\s*([^;]+);")
# 选择器 token（.class / #id，可带 `*` 表示族名；`◌` 这类内容不会被匹配）
_SELECTOR_RE = re.compile(r"[.#][A-Za-z][\w-]*\*?")
_WILDCARD_SUFFIX = "*"

pytestmark = pytest.mark.skipif(
    not STYLE_CSS.is_file() or not all(p.is_file() for p, _ in DESIGN_FILES),
    reason="design doc 或 style.css 不存在",
)


def front_matter(design_md: Path) -> dict:
    text = design_md.read_text(encoding="utf-8")
    match = _FRONT_RE.match(text)
    assert match, f"{design_md.name} 开头必须是 --- 包裹的 YAML front-matter"
    return yaml.safe_load(match.group(1))


def css_vars() -> dict[str, str]:
    css = STYLE_CSS.read_text(encoding="utf-8")
    root = _ROOT_RE.search(css)
    assert root, "style.css 找不到 :root 变量块"
    return {m.group(1): m.group(2).strip() for m in _VAR_RE.finditer(root.group(1))}


def components_section(design_md: Path, title: str) -> str:
    """Components 段的正文（到下一个 ## 标题为止）。"""
    text = design_md.read_text(encoding="utf-8")
    start = text.index(f"\n## {title}")
    end = text.index("\n## ", start + 1)
    return text[start:end]


@pytest.mark.parametrize(("design_md", "title"), DESIGN_FILES, ids=DESIGN_IDS)
def test_front_matter_parses(design_md: Path, title: str):
    data = front_matter(design_md)
    for section in ("colors", "typography", "rounded", "spacing", "z-index", "components"):
        assert section in data, f"front-matter 缺 {section} 段"


@pytest.mark.parametrize(("design_md", "title"), DESIGN_FILES, ids=DESIGN_IDS)
def test_colors_match_root_variables(design_md: Path, title: str):
    """colors 段的每个 token：样式表里必须有同名变量、值逐字一致。"""
    declared = front_matter(design_md)["colors"]
    actual = css_vars()
    problems = []
    for name, value in declared.items():
        if name not in actual:
            problems.append(f"--{name} 在 style.css :root 里不存在")
        elif actual[name] != str(value):
            problems.append(f"--{name}: 文档 {value!r} ≠ 样式表 {actual[name]!r}")
    assert not problems, f"{design_md.name} 的 colors 与 :root 不一致：\n" + "\n".join(problems)


@pytest.mark.parametrize(("design_md", "title"), DESIGN_FILES, ids=DESIGN_IDS)
def test_documented_classes_exist(design_md: Path, title: str):
    """Components 段点名的每个选择器都真实存在（`-*` 族名按前缀核对）。"""
    css = STYLE_CSS.read_text(encoding="utf-8")
    tokens = sorted(set(_SELECTOR_RE.findall(components_section(design_md, title))))
    assert len(tokens) > 50, (
        f"{design_md.name} 的组件段只解析出 {len(tokens)} 个选择器，疑似格式被改坏"
    )
    missing = []
    for token in tokens:
        if token.endswith(_WILDCARD_SUFFIX):
            pattern = re.escape(token[: -len(_WILDCARD_SUFFIX)])  # 族名：只要求前缀存在
        else:
            # 类名后面的字符不能是词字符或连字符（避免 .card 命中 .card-title）
            pattern = rf"{re.escape(token)}(?![\w-])"
        if not re.search(pattern, css):
            missing.append(token)
    assert not missing, f"{design_md.name} 点名了样式表里不存在的选择器：" + "、".join(missing)


@pytest.mark.parametrize(("design_md", "title"), DESIGN_FILES, ids=DESIGN_IDS)
def test_radius_and_zindex_values_exist(design_md: Path, title: str):
    """圆角五档与 z-index 阶梯的数值都必须能在样式表里找到。"""
    data = front_matter(design_md)
    css = STYLE_CSS.read_text(encoding="utf-8")
    problems = []
    for name, value in data["rounded"].items():
        if f"border-radius: {value}" not in css:
            problems.append(f"rounded.{name} = {value} 不在样式表里")
    for name, value in data["z-index"].items():
        if f"z-index: {value}" not in css:
            problems.append(f"z-index.{name} = {value} 不在样式表里")
    assert not problems, f"{design_md.name} 的圆角/z-index 与样式表不一致：\n" + "\n".join(problems)
