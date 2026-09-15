"""论文来源层业务错误。

消息恒为**可直接回显给用户的中文**（不再包一层翻译）：由路由层决定
映射成哪个 HTTP 状态码——搜索端点逐源捕获进 errors 字典，导入端点
按场景 409/502。与 mikasa.errors.ZhiwenError 的差异：那是全应用通用
错误基类，这是论文来源专门化的一层，避免 providers 的错误语义混进来。
"""

from __future__ import annotations


class PaperError(Exception):
    """论文源（arXiv/OpenAlex/下载器）的失败：str(exc) 即用户可读文案。"""
