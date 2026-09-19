"""文本清洗单测：strip_repeated_lines 的噪声判定回归。

回归锚点（真实 bug，见 utils/text.py）：
  1. 空行曾是"全局去重"的牺牲品 → md/txt 的段落结构在清洗阶段被摧毁，
     标题下所有段落粘成一段（load_markdown 段落数因此错误）；
  2. min_count 参数此前形同虚设（任何重复都删）→ 合法重复行（列表项、
     重复的结论句）被误删。
"""

from __future__ import annotations

from mikasa.utils.text import decode_text, is_english_dominant, normalize_text, strip_repeated_lines


def test_blank_lines_preserved_as_separators():
    text = "段落甲。\n\n段落乙。\n\n段落丙。"
    assert strip_repeated_lines(text) == text  # 空行一个不少


def test_repeated_headers_kept_only_once():
    # 每页相同的页眉出现 3 次（≥min_count）→ 只保留首次
    lines = "\n".join(["学习笔记 · 页眉", "正文一", "学习笔记 · 页眉", "正文二", "学习笔记 · 页眉"])
    cleaned = strip_repeated_lines(lines)
    assert cleaned.count("学习笔记 · 页眉") == 1
    assert "正文一" in cleaned and "正文二" in cleaned


def test_low_frequency_repetition_is_legitimate():
    """出现 2 次的行 < min_count：真实内容（列表/强调），全部保留。"""
    text = "- 重点：先复习线性代数\n- 重点：先复习线性代数\n- 然后做真题"
    assert strip_repeated_lines(text, min_count=3) == text
    # 显式收紧阈值后才会去重
    assert strip_repeated_lines(text, min_count=2).count("- 重点：先复习线性代数") == 1


def test_distinct_page_number_lines_kept():
    """每页不同的页码（第 1 页/第 2 页…）互不重复 → 不是噪声，全保留。"""
    text = "第 1 页\n内容一\n第 2 页\n内容二\n第 3 页\n内容三"
    cleaned = strip_repeated_lines(text)
    assert cleaned.count("第") == 3


def test_normalize_text_keeps_punctuation_and_folds_whitespace():
    assert normalize_text("学习“注意力机制”，很关键。  ") == "学习“注意力机制”，很关键。"
    assert normalize_text("a　b  c") == "a b c"  # 全角/半角空格统一折叠


def test_decode_text_gbk_roundtrip():
    raw = "机器学习方向复习提纲".encode("gbk")
    assert decode_text(raw) == "机器学习方向复习提纲"


# ---------------------------------------------------------------------------
# is_english_dominant（#8，2026-09-10）：英文块的启发式判定
# ---------------------------------------------------------------------------


def test_is_english_dominant_true_for_long_english_block():
    assert is_english_dominant(
        "Under cyclic loading, the capacity of helical anchors degrades with load cycles."
    )


def test_is_english_dominant_false_for_chinese():
    assert not is_english_dominant("循环荷载下螺旋锚的承载力随加载循环次数逐渐退化。")


def test_is_english_dominant_false_for_short_or_numeric():
    assert not is_english_dominant("short english")  # ASCII 字母 <20：短块无翻译价值
    assert not is_english_dominant("1234 5678 3.14 x = y + 2")  # 数字/符号纯块
    assert not is_english_dominant("")


def test_is_english_dominant_cjk_ratio_boundary():
    # 英长文混 ≥20% 汉字 → 不算英文块（中英混排按中文处理）
    mixed = "Under cyclic loading the capacity degrades with cycles " + "中文术语" * 15
    assert not is_english_dominant(mixed)
    # 少量汉字（<20%）→ 仍算英文块（英文论文偶夹作者名/术语）
    few = (
        "Under cyclic loading the capacity of helical anchors in sand degrades "
        "with the number of load cycles and the applied amplitude 中文"
    )
    assert is_english_dominant(few)


def test_guess_encoding_handles_non_gbk_non_utf8():
    """UTF-16 不该被当成 GBK 解成乱码。

    原来的顺序是"UTF-8 解不开就试 GBK"——而 **GBK 几乎不会解码失败**
    （双字节覆盖率高），于是这条路等价于"非 UTF-8 一律当 GBK"：Big5 /
    Shift-JIS / 混合编码的文件会整篇解成乱码且不报错（2026-09-11 审查实测）。
    现在先过一遍统计判定（候选集受限，见 _ENCODING_CANDIDATES）。
    """
    from mikasa.utils.text import decode_text, guess_encoding

    assert decode_text("中文笔记 abc".encode("gbk")) == "中文笔记 abc"
    assert decode_text("中文".encode("utf-16")) == "中文"
    assert decode_text("中文笔记".encode()) == "中文笔记"
    assert guess_encoding(b"") == "utf-8"


def test_guess_encoding_candidates_are_restricted():
    """候选集必须受限：不限制时 charset-normalizer 会把 GBK 短样本判成
    utf_16_le（解出来是乱码，比不做统计判定还糟）。"""
    from mikasa.utils.text import guess_encoding

    assert guess_encoding("中文笔记 abc".encode("gbk")) in ("gbk", "gb18030")


def test_strip_markup_removes_tags_and_restores_entities():
    """标记剥除：删标签、还原实体、折叠空白——**不渲染富文本**。"""
    from mikasa.utils.text import strip_markup

    assert strip_markup("a <sub>1</sub> b") == "a 1 b"
    assert strip_markup("A &amp; B &lt;5 mg/L") == "A & B <5 mg/L"
    assert strip_markup("  多  <i>空</i>  白  ") == "多 空 白"
    assert strip_markup("") == ""
