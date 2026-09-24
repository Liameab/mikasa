"""文档两棵树的护栏（2026-09-20 起中文为主，英文镜像在 docs/en/）。

为什么存在：README 与 docs 各有中英两份，靠人记着同步必然漂移——漏一篇、
改坏一条相对链接、英文镜像落在后面，都是"打开才发现是坏的/过期的"那种坏法，
而它们**恰好是访客最先看到的东西**。这里只钉两件**事实**（不做翻译质量判断）：

  1. **两树一一对应**：`docs/*.md` 与 `docs/en/*.md` 的文件名集合相同
     （`ideas-and-backlog.md` 例外：私人总账，不进仓库、没有英文镜像）；
  2. **相对链接都能落到真实文件**：仓库根三份 + 两棵树里每个 `](…)` 目标
     （去掉 #锚点、跳过 http/https/mailto）都必须存在——仓库做过一次
     "docs 两棵树对调"的搬家（中文升主干、英文进 en/、DESIGN.md 换位），
     最容易漏的就是这一类链接。

同一篇的中英文**内容**是否同步，本文件不管：那是翻译的活，机器判不了。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS = REPO_ROOT / "docs"
DOCS_EN = DOCS / "en"

# 私人总账与内部交接文档：被 .gitignore 排除、不进仓库、没有英文镜像，
# 天然不参与对应关系（它们自己的链接坏了只影响本机阅读，不该让 CI 红）
_PRIVATE = {
    "ideas-and-backlog.md",
    "codex-handover.md",
    # 考研复试准备材料（2026-09-24 加入）：个人使用、.gitignore 排除、无英文镜像
    "interview-handbook.md",
    "advisor-readme.md",
    # 技术路线提案（2026-09-25 加入）：Claude ↔ Codex 的协商稿，同上
    "tech-roadmap-proposal.md",
    # 技术路线回复（2026-09-25 加入）：Codex 的逐条判断，同上
    "tech-roadmap-response.md",
    # 技术路线回复的回复（2026-09-25 加入）：Claude 的采纳/更正/补充，同上
    "tech-roadmap-reply.md",
}

# 检查链接的范围：仓库根的门面 + 两棵文档树
_PAGES = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "README.en.md",
    REPO_ROOT / "DESIGN.md",
    *sorted(p for p in DOCS.glob("*.md") if p.name not in _PRIVATE),
    *sorted(DOCS_EN.glob("*.md")),
]

# 只认链接，**不认图片**：`![alt](url)` 的目标是同源资源地址（生成图那条），
# 不是仓库里的文件；把图片当链接会让每篇写了图片语法的文档误报。
# 判据是"左方括号不能紧跟感叹号"——`!` 在**开**括号前，不在闭括号前。
_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")


def _md_names(root: Path) -> set[str]:
    return {p.name for p in root.glob("*.md")} - _PRIVATE


def test_trees_correspond_file_by_file() -> None:
    """两棵树同名文件一一对应：缺一个就是"这篇没翻译/没原文"。"""
    zh, en = _md_names(DOCS), _md_names(DOCS_EN)
    assert zh == en, (
        "docs/ 与 docs/en/ 的文件名集合不一致："
        f"仅中文有 {sorted(zh - en)}；仅英文有 {sorted(en - zh)}"
    )
    # 设计系统走"根目录成对"（与 README 同款）：DESIGN.md 中文事实源 + DESIGN.en.md 英文镜像
    assert (REPO_ROOT / "DESIGN.md").is_file()
    assert (REPO_ROOT / "DESIGN.en.md").is_file()


def test_language_switch_lines_exist() -> None:
    """首页与文档页都要有互相切换的入口（用户找不到英文版 = 白翻）。"""
    root_zh = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    root_en = (REPO_ROOT / "README.en.md").read_text(encoding="utf-8")
    assert "README.en.md" in root_zh, "中文首页没有指向英文版的链接"
    assert "](README.md)" in root_en, "英文首页没有指回中文版的链接"


def test_relative_links_resolve() -> None:
    """所有相对链接都要落到真实文件（搬家、改名、删文件都会在这里现形）。"""
    broken: list[str] = []
    for page in _PAGES:
        for raw in _LINK_RE.findall(page.read_text(encoding="utf-8")):
            target = raw.split("#", 1)[0].strip()
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (page.parent / target).exists():
                broken.append(f"{page.relative_to(REPO_ROOT)} → {raw}")
    assert not broken, "文档里有解析不到的相对链接：\n" + "\n".join(broken)
