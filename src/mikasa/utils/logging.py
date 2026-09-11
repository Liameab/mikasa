"""日志：统一入口 + 密钥脱敏。

密钥形如 sk-xxxxxxxx…，日志里任何位置出现都会被替换为 sk-****
（格式层脱敏，防止 .env 内容经由异常信息泄漏到日志/控制台）。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

_SECRET_PATTERN = re.compile(r"(sk-[A-Za-z0-9_-]{8,})")
_CONFIGURED = False


def redact(text: str) -> str:
    """将文本中的密钥形字符串替换为掩码（供报错信息复用）。"""
    return _SECRET_PATTERN.sub("sk-****", text)


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            record.args = tuple(redact(arg) if isinstance(arg, str) else arg for arg in record.args)
        return True


def setup_logging(
    level: int = logging.INFO,
    *,
    log_file: Path | None = None,
    force: bool = False,
) -> None:
    """初始化"mikasa"命名空间的日志（幂等）。

    - 控制台：rich 渲染，便于 CLI 阅读；
    - 可选文件：UTF-8 编码（Windows 控制台/文件编码陷阱的规避点）。
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    logger = logging.getLogger("mikasa")
    # 重建：先清空旧 handler，避免 force 调用时重复挂载
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(level)
    logger.propagate = False

    console_handler = RichHandler(
        console=Console(stderr=True), show_path=False, rich_tracebacks=True
    )
    console_handler.setLevel(level)
    console_handler.addFilter(_RedactFilter())
    logger.addHandler(console_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
        )
        file_handler.addFilter(_RedactFilter())
        logger.addHandler(file_handler)

    _CONFIGURED = True


def get_logger(name: str = "mikasa") -> logging.Logger:
    """获取带统一前缀的子 logger；首次调用时自动初始化。"""
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(f"mikasa.{name}" if name != "mikasa" else "mikasa")
