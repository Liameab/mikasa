"""更新链路的业务错误。

网络/上游一类错误走 ProviderError（app 层处理器统一翻 502，与"上游
API 失败"同一语义）；本类收的是逻辑错误：找不到安装包资产、校验和不符、
还没下载完就想安装——这些是 400，重试也解决不了，得看文案。
"""

from __future__ import annotations

from mikasa.errors import ZhiwenError


class UpdateError(ZhiwenError):
    """更新流程可预期的业务错误（中文文案，直接回显给用户）。"""
