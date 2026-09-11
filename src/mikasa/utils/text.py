"""文本工具：原文保真的轻度清洗、编码探测、重复行清理。

Windows 环境的中文文档常见坑（GBK 编码、PDF 页眉页脚、粘连重复行），
统一在这里收敛。

清洗哲学（面试常问）：清洗只做“不改变语义”的格式收敛——空白折叠、
控制字符过滤、空行清理；**不做全半角标点转换**。中文标点（“”，。…）
必须随原文保存：引用溯源展示、分块断句都依赖原文排版；检索侧由分词器
天然忽略标点，转换对召回毫无收益却会破坏展示质量
（决策记录：ADR-0007，docs/design-decisions.md；初版曾做全角归一，后被推翻）。
"""

from __future__ import annotations

import re
import unicodedata


def normalize_text(text: str) -> str:
    """轻度清洗：去首尾空白、折叠空白、过滤控制字符（保留原文标点）。

    换行信息保留；段内连续空白（含全角空格 \\u3000）折叠为单个空格。
    """
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    text = "\n".join(lines)
    text = re.sub(r"[ \t　]+", " ", text)

    # 过滤控制字符（保留换行与制表符之外的全部）
    def _keep(ch: str) -> bool:
        return ch in ("\n", "\t") or not unicodedata.category(ch).startswith("C")

    return "".join(ch for ch in text if _keep(ch)).strip()


def guess_encoding(raw: bytes) -> str:
    """编码探测：优先 UTF-8，其次 GBK（Windows 中文文档高频编码）。

    fallback 一律 errors='replace' 保证不抛异常、不中断导入。
    """
    for encoding in ("utf-8", "gbk"):
        try:
            raw.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    return "utf-8"  # 探测失败时按 UTF-8 容错解码


def decode_text(raw: bytes) -> str:
    """按探测编码解码为文本（容错）。"""
    encoding = guess_encoding(raw)
    return raw.decode(encoding, errors="replace")


def strip_repeated_lines(text: str, min_count: int = 3) -> str:
    """去除全局重复的噪声行（PDF 转文本时每页相同的页眉/页脚）。

    规则（真实 bug 的回归，见 tests/unit/utils/）：
      - 空行是段落分隔符，一律保留（此前空行被全局去重，
        导致 md/txt 的段落结构在清洗阶段就被摧毁）；
      - 非空行全文出现 ≥min_count 次才视为噪声：只保留首次出现；
        出现 1-2 次很可能是合法内容（列表、强调、重复的结论句），不误伤。
    """
    if min_count < 2:
        min_count = 2
    from collections import Counter

    totals = Counter(line.strip() for line in text.splitlines() if line.strip())
    seen: set[str] = set()
    kept: list[str] = []
    for line in text.splitlines():
        key = line.strip()
        if not key:
            kept.append(line)  # 空行保留：分隔符不是噪声
            continue
        if totals[key] >= min_count and key in seen:
            continue  # 高频重复行：只留首次出现
        seen.add(key)
        kept.append(line)
    return "\n".join(kept)


# 语言主导性判定（#8，2026-09-10）：与 pipeline/ask.py 的 _CJK_RE 同范围，
# 保持全仓"含不含汉字"口径一致
_CJK_CHAR_RE = re.compile(r"[一-鿿]")
_ASCII_LETTER_RE = re.compile(r"[A-Za-z]")


def is_english_dominant(text: str) -> bool:
    """启发式判定"这段块是否以英文为主"（是否值得附原文+译文对照）。

    规则（真实教训见 docs/limitations-and-failures.md）：
      - ASCII 字母数 ≥ 20：短块、数字/公式纯块直接拦下（无翻译价值）；
      - CJK 字符占比 < 20%：英文论文偶夹汉字（作者名/术语）不算中文块。
    这是"值得翻译呈现"的启发式，不是严格语种分类。
    """
    if not text or len(_ASCII_LETTER_RE.findall(text)) < 20:
        return False
    return len(_CJK_CHAR_RE.findall(text)) / len(text) < 0.2


def fold_title(text: str, limit: int = 20) -> str:
    """把任意文本折叠成单行会话标题：空白（含换行/全角空格）压单空格并截断。

    会话标题只需要"这轮讲了什么"的锚，换行/缩进/段内空白对单行标题
    无意义（Python str.split 按 Unicode 空白切分，全角空格 \\u3000 一并
    收敛）；超长硬截断、不补省略号——补号会把真实字符挤出 limit 之外，
    观感由展示层 CSS ellipsis 负责。只动空白不动标点：标题可能正以
    全角问号收尾，全角标点不是排版噪声（见模块清洗哲学）。

    自动标题的两条路径（LLM 失败后的截断兜底、v1→v2 迁移的存量回填）
    共用本函数，保证兜底形态处处一致。
    """
    folded = " ".join(text.split())
    return folded if len(folded) <= limit else folded[:limit]
