"""中文分词：Protocol + 双实现 + 自动降级（本项目最核心的工程故事之一）。

背景（详见 docs/design-decisions.md ADR-0008）：
- 2026 年起新版 setuptools 移除 pkg_resources，`import jieba` 在新环境必崩
  （jieba#1043，官方认可的临时方案是锁 setuptools<82）；
- 本项目不依赖锁版本单点：Tokenizer 抽象为函数协议，jieba 失败时
  自动降级到纯 Python 的字符 bigram 分词（零依赖、确定性），
  并在评测中实测两种分词对检索指标的影响（docs/evaluation.md）。
"""

from __future__ import annotations

import logging
import re
import warnings
from collections.abc import Callable
from typing import Any

from mikasa.utils.logging import get_logger

logger = get_logger("index.tokenizer")

# 分词函数协议：文本 -> 词条列表
Tokenizer = Callable[[str], list[str]]

# jieba 是否可用的缓存结果（None=未探测）
_jieba_status: bool | None = None


def _import_jieba() -> Any:
    """导入 jieba 并静音其 import 期弃用告警。

    jieba 0.42.1 内部依赖已被移除的 pkg_resources，每次 import 触发
    UserWarning（过渡噪音，非故障）；真实故障（ImportError）不屏蔽，
    仍交由上层探针走降级路径。
    """
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message=r"pkg_resources is deprecated.*", category=UserWarning
        )
        import jieba

    # jieba 自带 logger 每次启动打印"前缀词典构建/加载"INFO 到 stderr，
    # 与 mikasa 的日志体系无关，直接静音（词典缓存路径等细节无用）
    logging.getLogger("jieba").setLevel(logging.WARNING)
    return jieba


def _probe_jieba(*, verbose: bool) -> bool:
    """探测 jieba 可导入性并缓存（verbose 控制失败日志详略，幂等）。"""
    global _jieba_status
    if _jieba_status is None:
        try:
            _import_jieba()
            _jieba_status = True
            if verbose:
                logger.info("jieba 可用：启用中文分词主实现")
        except Exception as exc:  # pkg_resources 缺失等
            _jieba_status = False
            if verbose:
                logger.warning(
                    "jieba 导入失败（%s: %s），降级到纯 Python bigram 分词"
                    "（离线/CI 可复现基线，评测对比见 docs/evaluation.md）",
                    type(exc).__name__,
                    exc,
                )
    return _jieba_status


def bigram_tokenizer(text: str) -> list[str]:
    """纯 Python 中文 bigram 分词（零依赖、确定性）。

    规则：连续 ASCII 词整体切出并小写；每个 CJK 字符单独成词，
    相邻 CJK 组成二元组（bigram）也成词——bigram 能缓解单字
    分词的歧义与召回不足，是离线/CI 的可复现基线。
    """
    tokens: list[str] = []
    last_cjk: str | None = None

    for match in re.finditer(r"[A-Za-z0-9]+|[一-鿿]", text):
        piece = match.group(0)
        if piece.isascii():
            tokens.append(piece.lower())
            last_cjk = None
        else:  # 单个 CJK 字符
            tokens.append(piece)
            if last_cjk is not None:
                tokens.append(last_cjk + piece)
            last_cjk = piece
    return tokens


def jieba_tokenizer(text: str) -> list[str]:
    """jieba 搜索引擎模式分词（召回更好）；不可用时自动降级 bigram。"""
    if _probe_jieba(verbose=True):
        return list(_import_jieba().cut_for_search(text))
    return bigram_tokenizer(text)


def get_tokenizer() -> Tokenizer:
    """获取当前生效的分词器（自动探测降级）。"""
    return jieba_tokenizer


def get_tokenizer_name() -> str:
    """当前生效分词器名称（记录进评测配置快照，保证可复现）。"""
    return "jieba" if _probe_jieba(verbose=False) else "bigram"
