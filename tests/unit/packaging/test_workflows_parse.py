"""工作流文件必须是合法 YAML（2026-10-09 踩坑后加的护栏）。

为什么值得一条测试：`if:` 的值若写成裸标量、里面又含 ": "（比如
`'chore(release): publish'`），YAML 解析会直接失败，而 GitHub 的表现是
**一个 job 都没有的启动失败**——不点进去看日志几乎发现不了。2026-10-09 的
v0.1.16 转正连挂三次就是这么来的；本地解析一遍只要几毫秒，能把这类错误挡在
推送之前。
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"


def test_every_workflow_is_parseable_yaml() -> None:
    files = sorted(WORKFLOWS.glob("*.yml"))
    assert files, "没有找到任何工作流文件（路径变了？）"
    for path in files:
        try:
            yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise AssertionError(f"{path.name} 不是合法 YAML：{exc}") from exc
