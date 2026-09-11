"""日志脱敏测试。"""

from __future__ import annotations

import logging

from mikasa.utils.logging import redact


def test_redact_sk_key():
    assert redact("key=sk-abc12345xyz999") == "key=sk-****"


def test_short_string_unchanged():
    # 不足 8 位不脱敏，避免误伤普通文本
    assert redact("sk-ab") == "sk-ab"


def test_redact_leaves_normal_text():
    assert redact("hello world 你好") == "hello world 你好"


def test_file_log_contains_masked_key(tmp_path):
    """经完整 logging 链（filter 挂载在 handler 上）后文件里只有掩码。"""
    from mikasa.utils.logging import setup_logging

    log_file = tmp_path / "mikasa.log"
    setup_logging(level=logging.INFO, log_file=log_file, force=True)
    logging.getLogger("mikasa").warning("已加载密钥 sk-abcdef1234567890 到客户端")
    # 还原默认（移除文件 handler，避免影响其他用例）
    setup_logging(force=True)

    text = log_file.read_text(encoding="utf-8")
    assert "sk-abcdef1234567890" not in text
    assert "sk-****" in text
