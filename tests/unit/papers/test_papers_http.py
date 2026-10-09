"""papers/http.py：统一 User-Agent 与网络级重试（2026-09-19 新增）。

这层存在的原因写在模块 docstring 里：三源各自 15 秒超时 + 匿名 UA，导致
arXiv/OpenAlex 在本机链路上周期性整个来源消失（用户报"搜出来没几条"）。
这里的测试锁住两件事：**只重试网络级失败**（HTTP 状态错误重试会把限流
打得更死），以及 **UA 带上项目主页**（实测决定了 0.8 秒 vs 15 秒超时）。
"""

from __future__ import annotations

import urllib.error

import pytest

from mikasa.papers.http import USER_AGENT, with_retry


def test_user_agent_identifies_the_project():
    """UA 必须带得了主页：arXiv 对匿名 UA 会间歇性 406 / 挂住。"""
    assert "Mikasa" in USER_AGENT
    assert "github.com/Liameab/mikasa" in USER_AGENT


def test_returns_first_success_without_retrying():
    calls = []

    def attempt():
        calls.append(1)
        return b"ok"

    assert with_retry(attempt, delay=0) == b"ok"
    assert len(calls) == 1


def test_retries_once_on_network_error():
    calls = []

    def attempt():
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.URLError("connection reset")
        return b"ok"

    assert with_retry(attempt, delay=0) == b"ok"
    assert len(calls) == 2


def test_retries_once_on_timeout():
    calls = []

    def attempt():
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("timed out")
        return b"ok"

    assert with_retry(attempt, delay=0) == b"ok"
    assert len(calls) == 2


def test_gives_up_after_one_retry_and_raises_last_error():
    calls = []

    def attempt():
        calls.append(1)
        raise urllib.error.URLError("still down")

    with pytest.raises(urllib.error.URLError):
        with_retry(attempt, delay=0)
    assert len(calls) == 2  # 首发 + 一次重试，不做无底洞重试


def test_http_status_errors_are_never_retried():
    """429/5xx 是上游在明确说话：立刻重打只会把限流打得更死（2026-09-16 教训）。"""
    calls = []

    def attempt():
        calls.append(1)
        raise urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)

    with pytest.raises(urllib.error.HTTPError):
        with_retry(attempt, delay=0)
    assert len(calls) == 1


def test_business_errors_pass_through_untouched():
    """非网络异常（解析失败等）不该被吞进重试循环。"""

    def attempt():
        raise ValueError("解析失败")

    with pytest.raises(ValueError):
        with_retry(attempt, delay=0)


def test_truncated_response_is_a_paper_error_not_a_crash(monkeypatch):
    """读到一半连接断了（IncompleteRead）→ PaperError，而不是 500（2026-10-07 审查）。

    `http.client.IncompleteRead` 是 HTTPException 的子类，**不是** OSError/URLError
    ——不接住就会从 with_retry 穿出去，一个源响应被截断就让整页搜索失败。
    """
    import http.client

    import mikasa.papers.http as http_mod
    from mikasa.papers.errors import PaperError

    class _Boom:
        def __enter__(self):
            raise http.client.IncompleteRead(b"partial")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(http_mod.urllib.request, "urlopen", lambda *a, **k: _Boom())
    client = http_mod.ThrottledClient("测试源", 0.0)
    with pytest.raises(PaperError, match="截断"):
        client.get("https://example.invalid/x", timeout=1.0)
