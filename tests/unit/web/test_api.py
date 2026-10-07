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


def test_page_routes_ignore_query_params(client):
    """页面路由不吃查询参数：`?page=` 不能改道到别的文件（2026-10-05 审查修复）。

    旧实现把 `lambda page=page:` 直接交给 FastAPI——带默认值的参数被当成
    查询参数声明，`GET /documents?page=../../../../pyproject.toml` 会原样进入
    FileResponse：未鉴权的任意文件读取（本机、以及 `--host 0.0.0.0` 下同网段
    都能打）。修法 = 闭包把 page 关进去，handler 签名零参数。
    """
    c, _ = client
    baseline = c.get("/documents").text
    # 探到仓库根的真实文件（static 目录向上四级）：修好前这条会 200 返回
    # pyproject.toml 的正文，修好后必须仍是 documents 页面本身
    resp = c.get("/documents", params={"page": "../../../../pyproject.toml"})
    assert resp.status_code == 200
    assert resp.text == baseline
    assert "[tool" not in resp.text
