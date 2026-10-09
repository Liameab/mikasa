"""跨站写闸门的主机/端口判据（2026-10-07 审查：端口原先不参与比较）。

端口不比的理由曾经是"同机不同端口没有安全含义"——不成立：本机任何一个别的
Web 服务（另一个 dev server、随便哪个 127.0.0.1:3000 的页面）都能对本应用发
**简单请求**，而它只是"不同端口"。缺省端口按 scheme 补，两侧同口径。
"""

from __future__ import annotations

import pytest

from mikasa.web.origin_guard import same_site


def test_same_host_and_port_is_allowed():
    assert same_site("http://127.0.0.1:8787", "127.0.0.1:8787") is True
    assert same_site("http://localhost:8000", "localhost:8000") is True


def test_default_port_is_normalized_on_both_sides():
    """省略默认端口的写法不能被误杀（Origin/Host 的端口写法本来就会不一致）。"""
    assert same_site("http://example.com", "example.com") is True
    assert same_site("http://example.com:80", "example.com") is True


def test_another_local_port_is_rejected():
    assert same_site("http://127.0.0.1:3000", "127.0.0.1:8787") is False
    assert same_site("http://localhost:9999", "localhost:8000") is False


def test_another_host_is_rejected():
    assert same_site("https://evil.example.com", "127.0.0.1:8787") is False


@pytest.mark.parametrize(
    ("origin", "host"),
    [
        ("http://[::1", "["),  # 畸形 IPv6 字面量
        ("http://a:99999", "a"),  # 端口越界：取 .port 时抛 ValueError
    ],
)
def test_malformed_values_are_rejected_not_raised(origin, host):
    assert same_site(origin, host) is False
