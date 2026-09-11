"""日志脱敏测试。"""

from __future__ import annotations

import logging
from pathlib import Path

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


def test_default_log_file_lives_under_data_root(tmp_path, monkeypatch):
    """默认日志文件跟着可写数据目录走（打包后 = %LOCALAPPDATA%\\Mikasa）。

    两个入口（CLI 与窗口）共用这一处定义——写死路径就会再次跑偏成"弹窗让用户
    去看一个空目录"。
    """
    from mikasa.utils.logging import default_log_file

    monkeypatch.setenv("MIKASA_DATA_DIR", str(tmp_path))

    assert default_log_file() == tmp_path / "logs" / "mikasa.log"
    assert isinstance(default_log_file(), Path)


def test_strip_paths_keeps_only_the_file_name():
    r"""错误消息回显绝对路径会让 LAN 客户端摸清服务器目录结构。

    段内允许空格（`C:\Program Files\...` 是常态），所以不能按空白切。
    """
    from mikasa.errors import strip_paths

    bs = chr(92)
    win = f"C:{bs}Users{bs}张三{bs}AppData{bs}Local{bs}Mikasa{bs}uploads{bs}论文.pdf"
    assert strip_paths(f"入库失败：{win} 被占用") == "入库失败：论文.pdf 被占用"
    spaced = f"C:{bs}Program Files{bs}Mikasa{bs}_internal{bs}evals{bs}golden_set.json"
    assert strip_paths(f"黄金集不存在：{spaced}") == "黄金集不存在：golden_set.json"
    assert strip_paths("FileNotFoundError: /var/log/mikasa.log") == "FileNotFoundError: mikasa.log"


def test_strip_paths_leaves_ordinary_text_alone():
    """别把普通文本里的斜杠当路径剥掉——脱敏不能改语义。"""
    from mikasa.errors import strip_paths

    assert strip_paths("普通文本 a/b 不是路径") == "普通文本 a/b 不是路径"
    assert strip_paths("没有路径") == "没有路径"
