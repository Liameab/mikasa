"""统一异常层级：所有可预期的业务错误都继承 ZhiwenError。

CLI 与 Web 层据此给出可读的中文错误信息，避免裸堆栈面向用户。
"""

from __future__ import annotations

import re

# 绝对路径：Windows 盘符路径 / UNC 共享 / POSIX 路径。
# 段内**允许空格**（`C:\Program Files\...` 是常态，按空白切会把路径截成两半，
# 留下 "Program Files\Mikasa\..." 这种既泄漏又看不懂的残渣）；段边界用 Windows
# 文件名的非法字符集划，不用空白划。
_WIN_SEG = r"[^\\/:*?\"<>|\r\n]+"
_ABS_PATH = re.compile(
    rf"[A-Za-z]:\\(?:{_WIN_SEG}\\)*{_WIN_SEG}"
    rf"|\\\\{_WIN_SEG}\\{_WIN_SEG}(?:\\{_WIN_SEG})*"
    r"|/(?:[^\s'\"<>|:]+/)+[^\s'\"<>|:]*"
)


def strip_paths(text: str) -> str:
    """把错误文本里的**绝对路径**替换成文件名，防止回显服务器目录结构。

    为什么需要：Web 层的 ZhiwenError 处理器原样回显 `str(exc)`（本模块的 docstring
    早就写着"errors 层负责 redact"，但那个函数其实一直不存在），而 OSError 与
    pymupdf 的异常消息常带完整路径。`--host 0.0.0.0`（无鉴权）下任何同网段的人
    都能据此摸清数据目录布局（2026-09-11 审查发现）。

    只留最后一段：报错仍然指得出是哪个文件，但看不出服务器装在哪儿。
    """
    return _ABS_PATH.sub(lambda m: re.split(r"[\\/]", m.group(0))[-1] or m.group(0), text)


class ZhiwenError(Exception):
    """项目内所有可预期业务错误的基类。"""


class ConfigError(ZhiwenError):
    """配置缺失/非法（找不到 profile、密钥未配置等）。"""


class ProviderError(ZhiwenError):
    """模型提供方调用失败（鉴权失败、限流、超时、网络等）。"""


class IngestError(ZhiwenError):
    """文档导入失败（格式不支持、解析错误、空文件等）。"""


class StorageError(ZhiwenError):
    """本地存储/索引损坏或不一致。"""


class EvalError(ZhiwenError):
    """评测体系错误（黄金集错配、指标计算输入非法等）。"""
