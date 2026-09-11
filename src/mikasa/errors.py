"""统一异常层级：所有可预期的业务错误都继承 ZhiwenError。

CLI 与 Web 层据此给出可读的中文错误信息，避免裸堆栈面向用户。
"""


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
