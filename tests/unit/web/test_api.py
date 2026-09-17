"""Web API 冒烟：health、三页 HTML、错误响应壳（500 兜底待 qa 端点落地）。"""

from __future__ import annotations


def test_health_reports_empty_corpus(client):
    c, _settings = client
    resp = c.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Mikasa"
    assert body["profile"] == "offline"
    assert body["version"]
    assert body["llm_model"] and body["embedding_model"]
    assert body["documents"] == 0 and body["chunks"] == 0


def test_health_counts_corpus_after_seed(seeded_client):
    c, _settings = seeded_client
    body = c.get("/api/health").json()
    assert body["documents"] == 1
    assert body["chunks"] >= 1


def test_pages_served(client):
    """四页 + 首页：浏览器直开的入口（找论文页见 ADR-0020）。"""
    c, _ = client
    for path in ("/", "/documents", "/papers", "/eval"):
        resp = c.get(path)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")


def test_static_assets_served(client):
    c, _ = client
    for path in ("/static/css/style.css", "/static/js/common.js"):
        assert c.get(path).status_code == 200
    # 未注册的静态路径 → 404（防前端路径写错时悄悄 200 空文件）
    assert c.get("/static/js/nope.js").status_code == 404


def test_unknown_api_returns_404(client):
    c, _ = client
    assert c.get("/api/不存在").status_code == 404
