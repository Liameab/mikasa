"""版本更新：查 GitHub Release 有没有新版、下载安装包并启动安装器。

设计存档见 ADR-0022（防线与流程）与 ADR-0024（续传与接续）。四个纪律：

1. **客户端碰不到 URL**——下载地址由服务端从 GitHub API 响应里挑
   （release.py），前端只会说"下载最新版"，不给任何地址。
2. **下载完必须校验**——安装包与同批发布的 SHA256SUMS.txt 一起下，
   逐字节核对 sha256 才允许启动安装器（install.py）。
3. **检查失败一律静默**——离线用户不该被一个网络错误打扰；错误只
   进日志与状态接口（前端自己决定要不要显示）。
4. **下载可续、任务可接**——半成品在 `.part`、传输断了带 Range 接着下；
   任务槽自带存活登记与接管（install.py），界面只是观察窗。
"""

from mikasa.update.checker import UpdateChecker
from mikasa.update.errors import UpdateError
from mikasa.update.install import UpdateManager, run_download
from mikasa.update.release import (
    ReleaseInfo,
    fetch_latest_release,
    is_newer,
    parse_version,
)

__all__ = [
    "ReleaseInfo",
    "UpdateChecker",
    "UpdateError",
    "UpdateManager",
    "fetch_latest_release",
    "is_newer",
    "parse_version",
    "run_download",
]
