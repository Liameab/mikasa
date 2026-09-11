"""在 PDF 页面上定位一段文本的矩形（阅读视图的引用高亮用）。

**为什么要单独一层**：合并视图要把"引用块"高亮到**原样渲染的页面图**上，
所以需要知道每个块在页内的位置。而库里只存文本、不存坐标。

**为什么不用 `page.search_for`**（2026-09-11 实测）：它要求逐字匹配，而
chunk 文本在入库时被清洗过（空白折叠、页眉页脚剔除、表格按几何重排），
与 PDF 原文并非逐字一致——实测命中率只有 **55%**（22/40）。

**本模块的做法**：把两边都归一化成"词元序列"再比对（NFKC 归一、转小写、
剥掉所有非字母数字汉字），空白与标点的差异就此消弭。实测 **98%**（59/60）。

纯函数、零 IO：输入是 PyMuPDF 的 `page.get_text("words")` 结果与一段文本，
输出是**归一化到 0~1 的矩形列表**（前端按任意 DPI 缩放，不必知道页面尺寸）。
矩形**按行分组**而不是合成一个大包围盒——一个跨十几行的块若只给外框，
高亮会糊住整页。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

# 词元归一：只留字母数字与汉字（含扩展区），其余（空白/标点/连字符/特殊符号）全部剥掉
_TOKEN_STRIP = re.compile(r"[^0-9a-z一-鿿㐀-䶿]+")

# 最少要连续匹配多少个词元才算"找到了"——太短的匹配到处都是，会把高亮打到别处
MIN_RUN = 6

# 同一个词元在页面上出现多次时，只取前若干个匹配候选做尝试（防退化）
_MAX_ANCHORS = 200


@dataclass(frozen=True)
class Located:
    """定位结果：按行分组的矩形（已归一化到 0~1）+ 命中的词元数。"""

    rects: list[tuple[float, float, float, float]]
    matched: int

    @property
    def ok(self) -> bool:
        return bool(self.rects)


def normalize_token(raw: str) -> str:
    """词元归一：NFKC（全角→半角、连字→分解）→ 小写 → 剥掉非字母数字汉字。"""
    return _TOKEN_STRIP.sub("", unicodedata.normalize("NFKC", raw).lower())


def _tokenize(text: str) -> list[str]:
    """把一段文本切成非空词元序列（按空白切分后逐个归一）。"""
    return [t for t in (normalize_token(x) for x in text.split()) if t]


def _group_by_line(
    rects: Sequence[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float]]:
    """把词矩形按纵向重叠分组成行，每行取并集。

    判定用"纵向有交集"而非固定阈值：词元来自同一行时 y 区间必然重叠，
    换行则不重叠（表头基线偏移那种 7.5pt 的情况也仍然重叠，故安全）。
    """
    if not rects:
        return []
    ordered = sorted(rects, key=lambda r: (r[1], r[0]))
    lines: list[list[float]] = []
    for x0, y0, x1, y1 in ordered:
        if lines and y0 < lines[-1][3] and y1 > lines[-1][1]:  # 与当前行纵向相交
            cur = lines[-1]
            cur[0], cur[1] = min(cur[0], x0), min(cur[1], y0)
            cur[2], cur[3] = max(cur[2], x1), max(cur[3], y1)
        else:
            lines.append([x0, y0, x1, y1])
    return [(a, b, c, d) for a, b, c, d in lines]


def locate_in_page(
    words: Sequence[tuple[float, float, float, float, str]],
    text: str,
    *,
    page_width: float,
    page_height: float,
    min_run: int = MIN_RUN,
) -> Located:
    """在 `words` 里定位 `text`，返回归一化矩形（按行分组）。

    `words` 是 PyMuPDF `page.get_text("words")` 的元素序列，每项前四列是
    `(x0, y0, x1, y1)`、第五列是词文本（多余的 block/line/word 下标忽略）。

    算法：把两边都归一化成词元序列后，用**滑动的连续词元段**做锚点匹配，
    命中后向后尽量延伸（取最长的那次匹配），再把命中词的矩形按行分组。
    页面上词元重复出现时取最长匹配，避免锚在最前面的偶然重合上。

    定位不到（< `min_run` 个连续词元命中）→ 返回空 rects，调用方优雅降级。
    """
    if not words or not text.strip() or page_width <= 0 or page_height <= 0:
        return Located(rects=[], matched=0)

    page_tokens = [normalize_token(w[4]) for w in words]
    wanted = _tokenize(text)
    if len(wanted) < min_run:
        return Located(rects=[], matched=0)

    best_start = best_end = -1
    anchors = 0
    # 从块的头部开始试锚点；头部匹配不上就往后挪（清洗可能削掉了开头一小段）
    for head in range(len(wanted) - min_run + 1):
        probe = wanted[head : head + min_run]
        for i in range(len(page_tokens) - min_run + 1):
            if page_tokens[i : i + min_run] != probe:
                continue
            anchors += 1
            if anchors > _MAX_ANCHORS:
                break
            # 命中后向后延伸：页面上词元顺序与块内一致（表格重排也按 x 序，
            # 与 get_text("words") 的内容流序在这一段里一致）
            j, k = i + min_run, head + min_run
            while j < len(page_tokens) and k < len(wanted) and page_tokens[j] == wanted[k]:
                j += 1
                k += 1
            if (k - head) > (best_end - best_start):
                best_start, best_end = i, j
        if anchors > _MAX_ANCHORS:
            break

    if best_start < 0:
        return Located(rects=[], matched=0)

    rects = [(w[0], w[1], w[2], w[3]) for w in words[best_start:best_end]]
    lines = _group_by_line(rects)
    norm = [
        (
            max(0.0, x0 / page_width),
            max(0.0, y0 / page_height),
            min(1.0, x1 / page_width),
            min(1.0, y1 / page_height),
        )
        for x0, y0, x1, y1 in lines
    ]
    return Located(rects=norm, matched=best_end - best_start)
