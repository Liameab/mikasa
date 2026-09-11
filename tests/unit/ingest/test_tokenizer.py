"""中文分词单测：bigram 确定性 + jieba→bigram 自动降级路径。

降级是 ADR-0008 的工程故事：jieba 在新环境可能 import 即崩
（pkg_resources 被移除），系统必须自动降级而非原地报错。
"""

from __future__ import annotations

import mikasa.index.tokenizer as tk


def test_bigram_cjk_single_chars_and_pairs():
    tokens = tk.bigram_tokenizer("机器学习")
    # 流式顺序：单字先出，紧接它与前字的二元组：
    # 机 | 器 机器 | 学 器学 | 习 学习
    assert tokens == ["机", "器", "机器", "学", "器学", "习", "学习"]
    assert len(tokens) == 2 * len("机器学习") - 1  # 单字 + 相邻对


def test_bigram_ascii_kept_as_token_and_lowercased():
    tokens = tk.bigram_tokenizer("Transformer模型")
    assert tokens[0] == "transformer"
    # ASCII 整体成词并小写，随后是 CJK 单字与相邻对
    assert tokens[1] == "模"
    assert tokens[2] == "型"
    assert tokens[3] == "模型"
    assert all(t.islower() or not t.isascii() for t in tokens)


def test_bigram_breaks_ascii_adjacency():
    """ASCII 词相邻不组 bigram（d2 不得变成 d2 的二元组）。"""
    tokens = tk.bigram_tokenizer("d2")
    assert tokens == ["d2"]
    assert "d2" in tokens  # 数字字母混排词保留


def test_bigram_strips_punctuation():
    tokens = tk.bigram_tokenizer("梯度下降，收敛快。")
    assert "，" not in tokens and "。" not in tokens
    assert "梯度" in tokens and "下降" in tokens  # 二元组在，标点不在


def test_bigram_is_deterministic():
    a = tk.bigram_tokenizer("反向传播与链式法则推导")
    b = tk.bigram_tokenizer("反向传播与链式法则推导")
    assert a == b


def test_jieba_tokenizer_falls_back_to_bigram(monkeypatch):
    """模拟 jieba 不可用：_probe_jieba 命中缓存 False → 走 bigram。"""
    monkeypatch.setattr(tk, "_jieba_status", False)
    assert tk.get_tokenizer_name() == "bigram"
    tokens = tk.jieba_tokenizer("支持向量机")
    assert tokens == tk.bigram_tokenizer("支持向量机")


def test_get_tokenizer_name_reports_jieba_when_available(monkeypatch):
    monkeypatch.setattr(tk, "_jieba_status", True)
    assert tk.get_tokenizer_name() == "jieba"
    assert tk.get_tokenizer() is tk.jieba_tokenizer
