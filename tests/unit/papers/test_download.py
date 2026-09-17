"""下载防护单测：SSRF 地址校验 / 重定向逐跳复验 / 类型嗅探 / 体积上限。

全离线：`_check_host`/`_validate_url` 注入假 addrinfo（零真实 DNS）；
`download_pdf` 打桩 `_open_url` 网络缝返回假响应对象。
"""

from __future__ import annotations

import io
import urllib.error
import urllib.request

import pytest

import mikasa.papers.download as dl
from mikasa.papers.errors import PaperError


def _addrinfo(*ips: str):
    """假 getaddrinfo：ip → [(AF_INET, SOCK_STREAM, 6, "", (ip, 80))]。"""
    return [(2, 1, 6, "", (ip, 80)) for ip in ips]


# ---------------------------------------------------------------------------
# 地址校验（纯函数，注入假解析）
# ---------------------------------------------------------------------------


def test_https_public_allowed():
    dl._check_host("example.com", 443, resolve=lambda h, p, **k: _addrinfo("93.184.216.34"))


def test_https_private_rejected():
    for ip in ("10.0.0.1", "192.168.1.1", "172.16.0.1", "169.254.1.1", "127.0.0.1", "::1"):
        with pytest.raises(PaperError, match="安全策略"):
            dl._check_host("x", 443, resolve=lambda h, p, ip=ip, **k: _addrinfo(ip))


def test_https_mixed_public_private_rejected():
    """任一地址落到内网即拒（双栈解析全量检查）。"""
    with pytest.raises(PaperError, match="安全策略"):
        dl._check_host("x", 443, resolve=lambda h, p, **k: _addrinfo("93.184.216.34", "10.0.0.1"))


def test_http_public_and_loopback_allowed():
    """http：公网与回环都放行（2026-09-16 放宽，理由见 download.py 模块头）。

    实测中文开放获取论文约六成全文链接是明文 http（国内期刊/仓储普遍没上
    https），只收 https 等于"搜得到、导不进来"。链接来自上游记录而非用户
    输入、下的是公开论文、不带凭据，故放行公网 http；SSRF 防线（打不到内网）
    与协议无关，保持不变。
    """
    dl._validate_url("http://127.0.0.1:8080/pdf", resolve=lambda h, p, **k: _addrinfo("127.0.0.1"))
    dl._validate_url("http://example.com/x", resolve=lambda h, p, **k: _addrinfo("93.184.216.34"))


def test_http_private_still_rejected():
    """内网/保留地址在 http 下同样一律拒——这条是 SSRF 防线的本体。"""
    for ip in ("192.168.1.1", "10.0.0.1", "172.16.0.1", "169.254.1.1"):
        with pytest.raises(PaperError, match="安全策略"):
            dl._validate_url("http://intranet/x", resolve=lambda h, p, ip=ip, **k: _addrinfo(ip))
    # 双栈混合：任一地址落到内网即拒
    with pytest.raises(PaperError, match="安全策略"):
        dl._validate_url(
            "http://x/y", resolve=lambda h, p, **k: _addrinfo("93.184.216.34", "10.0.0.1")
        )


def test_validate_url_scheme_and_credentials():
    with pytest.raises(PaperError, match="http/https"):
        dl._validate_url("ftp://example.com/x")
    with pytest.raises(PaperError, match="凭据"):
        dl._validate_url("https://user:pass@example.com/x")
    with pytest.raises(PaperError, match="主机名"):
        dl._validate_url("https:///x")


# ---------------------------------------------------------------------------
# 重定向处理器（直测，不经网络）
# ---------------------------------------------------------------------------


def _req(url: str) -> urllib.request.Request:
    return urllib.request.Request(url)


def test_redirect_revalidates_each_hop():
    handler = dl._SafeRedirectHandler(resolve=lambda h, p, **k: _addrinfo("93.184.216.34"))
    # 公网 https：放行（返回 Request 即交给 stdlib 继续）
    out = handler.redirect_request(
        _req("https://a.com/x"), io.BytesIO(), 302, "Moved", {}, "https://b.com/y"
    )
    assert out is not None and out.full_url == "https://b.com/y"


def test_redirect_to_private_rejected():
    handler = dl._SafeRedirectHandler(resolve=lambda h, p, **k: _addrinfo("10.0.0.8"))
    with pytest.raises(PaperError, match="安全策略"):
        handler.redirect_request(
            _req("https://a.com/x"), io.BytesIO(), 302, "Moved", {}, "https://10.0.0.8/y"
        )


def test_redirect_over_max_hops():
    handler = dl._SafeRedirectHandler(resolve=lambda h, p, **k: _addrinfo("93.184.216.34"))
    for _ in range(dl._MAX_REDIRECTS):
        handler.redirect_request(
            _req("https://a.com/x"), io.BytesIO(), 302, "Moved", {}, "https://b.com/y"
        )
    with pytest.raises(urllib.error.HTTPError, match="重定向次数超过上限"):
        handler.redirect_request(
            _req("https://b.com/y"), io.BytesIO(), 302, "Moved", {}, "https://c.com/z"
        )


# ---------------------------------------------------------------------------
# download_pdf（打桩 _open_url；同时放行 http 回环以模拟 E2E 假源）
# ---------------------------------------------------------------------------


class _FakeResp:
    """最小文件对象：headers + 分块 read。"""

    def __init__(self, data: bytes, content_type: str = "application/pdf") -> None:
        self._buf = io.BytesIO(data)
        self.headers = {"Content-Type": content_type}

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub_open(data: bytes, content_type: str = "application/pdf"):
    calls: dict = {}

    def fake(url: str, timeout: float):
        calls["url"] = url
        calls["timeout"] = timeout
        return _FakeResp(data, content_type)

    return fake, calls


def test_download_happy_path(tmp_path, monkeypatch):
    body = b"%PDF-1.7 fake content " + b"x" * 100
    fake, calls = _stub_open(body)
    monkeypatch.setattr(dl, "_open_url", fake)
    dest = tmp_path / "out.pdf"
    total = dl.download_pdf("http://127.0.0.1:9/pdf/x", dest)
    assert total == len(body)
    assert dest.read_bytes() == body
    assert calls["timeout"] == dl._DOWNLOAD_TIMEOUT


def test_download_bad_content_type(tmp_path, monkeypatch):
    fake, _ = _stub_open(b"%PDF-1.7", content_type="text/html")
    monkeypatch.setattr(dl, "_open_url", fake)
    with pytest.raises(PaperError, match="不是 PDF"):
        dl.download_pdf("http://127.0.0.1:9/x", tmp_path / "out.pdf")
    assert not (tmp_path / "out.pdf").exists()  # 没写任何字节


def test_download_sniffs_pdf_magic(tmp_path, monkeypatch):
    """伪装成 PDF 的 HTML 错误页：首块嗅探拦下。"""
    fake, _ = _stub_open(b"<html>error page</html>", content_type="application/pdf")
    monkeypatch.setattr(dl, "_open_url", fake)
    with pytest.raises(PaperError, match="文件头嗅探"):
        dl.download_pdf("http://127.0.0.1:9/x", tmp_path / "out.pdf")


def test_download_oversize_deletes_partial(tmp_path, monkeypatch):
    big = b"%PDF-1.7" + b"y" * (dl._PDF_MAX_BYTES + 1)
    fake, _ = _stub_open(big)
    monkeypatch.setattr(dl, "_open_url", fake)
    dest = tmp_path / "out.pdf"
    with pytest.raises(PaperError, match="50 MB 上限"):
        dl.download_pdf("http://127.0.0.1:9/x", dest)
    assert not dest.exists()  # 半成品已删


def test_download_http_error_translated(tmp_path, monkeypatch):
    def boom(url: str, timeout: float):
        raise urllib.error.HTTPError(url, 404, "not found", {}, None)

    monkeypatch.setattr(dl, "_open_url", boom)
    with pytest.raises(PaperError, match="HTTP 404"):
        dl.download_pdf("http://127.0.0.1:9/x", tmp_path / "out.pdf")
