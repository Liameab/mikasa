"""在线论文检索端点（/api/papers/*）单元测试。

全离线：整表 monkeypatch `mikasa.papers.service.SOURCES` 为假源、
`mikasa.web.routers.papers.download_pdf` 为假下载器（写真 PDF 字节——
pymupdf 生成、有文字层，与 test_documents_api 的 _make_pdf_bytes 同款）。
密钥写回落在 autouse MIKASA_DATA_DIR 隔离的临时目录，不碰真 .env。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import mikasa.papers.service as papers_service
from mikasa.papers.errors import PaperError
from mikasa.papers.sources import PaperFilters, PaperResult, SourceCaps
from mikasa.web.routers import papers as papers_router
from tests.unit.web.test_documents_api import _mk_folder


def _pdf_bytes(text: str) -> bytes:
    """生成含指定文本的单页 PDF（中文需 pymupdf 内置 china-s 字体）。

    与 test_documents_api._make_pdf_bytes 同源不同参：那边按页数生成固定
    英文正文（页数渲染测试用），这边要的是"正文里有什么"。
    """
    import pymupdf

    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 96), text, fontname="china-s", fontsize=12)
    return pdf.tobytes()


def _paper(
    source: str,
    paper_id: str,
    title: str = "测试论文",
    pdf_url: str | None = "http://127.0.0.1:9/pdf/x",
    venue: str = "某期刊",
    oa: bool = True,
    doi: str = "",
) -> PaperResult:
    return PaperResult(
        source=source,
        id=paper_id,
        title=title,
        authors=("作者甲", "作者乙"),
        year=2024,
        venue=venue,
        abstract="摘要正文",
        doi=doi,
        pdf_url=pdf_url,
        landing_url=f"https://example.org/{paper_id}",
        oa=oa,
    )


class _FakeSource:
    """可编排假源：search 返回固定结果，fetch 按 id 返回。"""

    def __init__(
        self, name: str, results: list[PaperResult] | None = None, caps: SourceCaps | None = None
    ) -> None:
        self.name = name
        self.label = name
        self.caps = caps or SourceCaps()
        self.results = results or []
        self.fetch_map: dict[str, PaperResult] = {}
        self.fetch_error: PaperError | None = None
        self.last_filters: PaperFilters | None = None

    def search(
        self, q: str, start: int, count: int, *, filters: PaperFilters | None = None
    ) -> tuple[list[PaperResult], bool]:
        self.last_filters = filters
        return list(self.results[start : start + count]), False

    def fetch(self, paper_id: str) -> PaperResult:
        if self.fetch_error:
            raise self.fetch_error
        return self.fetch_map[paper_id]


@pytest.fixture()
def fake_sources(monkeypatch):
    """把 SOURCES 整表换成假源（autouse 后复原）。"""
    monkeypatch.setattr(papers_service, "SOURCES", {})

    def register(name: str, source) -> None:
        papers_service.SOURCES[name] = source

    return register


@pytest.fixture()
def fake_downloader(monkeypatch):
    """假下载器：往 dest 写真 PDF 字节（可切换为抛错）。

    字节按 URL 缓存：真下载器下同一篇论文每次拿到的字节相同（sha256 去重
    路径才走得到），不同论文 URL 不同、内容也不同（同名不同 id 的双导入才
    不会互踩）。不能让每次调用现生成——pymupdf 的 tobytes() 带创建时间与
    文档 ID，两次生成字节不同，会把"重复导入跳过"整条路径打穿。
    """
    state = {"error": None, "writes": []}
    cache: dict[str, bytes] = {}

    def bytes_for(url: str) -> bytes:
        """该 URL 的固定字节（同 URL 恒定）。**测试若需要"手工入库一份同内容
        文件"，必须走这里拿字节**——自己调 `_pdf_bytes(...)` 会得到另一份
        （pymupdf 的 tobytes 带创建时间/document id），sha 对不上。"""
        return cache.setdefault(url, _pdf_bytes(f"论文正文（{url}）。"))

    def fake(url: str, dest: Path) -> int:
        if state["error"]:
            raise state["error"]
        data = bytes_for(url)
        dest.write_bytes(data)
        state["writes"].append((url, dest.name))
        return len(data)

    state["bytes_for"] = bytes_for
    monkeypatch.setattr(papers_router, "download_pdf", fake)
    return state


# ---------------------------------------------------------------------------
# POST /api/papers/search
# ---------------------------------------------------------------------------


def test_search_returns_normalized_shape(client, fake_sources):
    c, _settings = client
    src = _FakeSource("arxiv", [_paper("arxiv", "2401.12345")])
    fake_sources("arxiv", src)
    fake_sources("openalex", _FakeSource("openalex"))

    resp = c.post("/api/papers/search", json={"q": "retrieval", "sources": ["arxiv"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["errors"] == {} and body["has_more"] is False and body["notes"] == {}
    assert len(body["results"]) == 1
    r = body["results"][0]
    assert r["source"] == "arxiv" and r["id"] == "2401.12345"
    assert r["title"] == "测试论文"
    assert r["authors"] == ["作者甲", "作者乙"]  # tuple 序列化后是数组
    assert r["pdf_url"] == "http://127.0.0.1:9/pdf/x"
    assert r["in_library"] is False  # 未导入过 → 不在库
    assert "api_key" not in r  # 无密钥字段可泄


class _Boom:
    """必失败的假源（能力声明齐全，否则编排层读 caps 会 AttributeError）。"""

    def __init__(self, name: str, message: str) -> None:
        self.name = name
        self.label = name
        self.caps = SourceCaps()
        self.message = message

    def search(self, q, start, count, *, filters=None):
        raise PaperError(self.message)

    def fetch(self, paper_id):
        raise AssertionError


def test_search_partial_failure_degrades(client, fake_sources):
    """单源失败 → 200 + errors 里带中文消息，另一源结果照常。"""
    c, _settings = client
    fake_sources("arxiv", _Boom("arxiv", "无法连接 arXiv（网络不可达或超时），请稍后重试"))
    fake_sources("openalex", _FakeSource("openalex", [_paper("openalex", "W3160856016")]))

    resp = c.post("/api/papers/search", json={"q": "水库坝"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 1 and body["results"][0]["source"] == "openalex"
    assert "arxiv" in body["errors"]
    assert "无法连接 arXiv" in body["errors"]["arxiv"]


def test_search_all_sources_fail_502(client, fake_sources):
    c, _settings = client
    fake_sources("arxiv", _Boom("arxiv", "挂了"))
    fake_sources("openalex", _Boom("openalex", "也挂了"))
    resp = c.post("/api/papers/search", json={"q": "x"})
    assert resp.status_code == 502
    assert "论文检索暂时不可用" in resp.json()["detail"]


def test_search_filters_translated_and_notes(client, fake_sources):
    """筛选透传到来源；不支持的能力在响应 notes 里如实说明（不假装生效）。"""
    c, _settings = client
    src = _FakeSource("arxiv", [_paper("arxiv", "2401.12345")], caps=SourceCaps(year=True))
    fake_sources("arxiv", src)
    fake_sources("openalex", _FakeSource("openalex"))

    resp = c.post(
        "/api/papers/search",
        json={
            "q": "x",
            "sources": ["arxiv"],
            "filters": {"date_from": "2020-01-01", "sort": "cited", "oa_only": True},
        },
    )
    assert resp.status_code == 200
    assert isinstance(src.last_filters, PaperFilters)
    assert src.last_filters.date_from == "2020-01-01" and src.last_filters.sort == "cited"
    assert src.last_filters.oa_only is True
    assert "被引" in resp.json()["notes"]["arxiv"]  # arxiv 无被引数据 → 降级说明


def test_search_marks_in_library(client, fake_sources, fake_downloader):
    """导入过的论文在后续检索里标 in_library=True（source_ref 反查）。"""
    c, _settings = client
    src = _FakeSource("arxiv")
    src.fetch_map["2401.12345"] = _paper("arxiv", "2401.12345")
    fake_sources("arxiv", src)
    fake_sources("openalex", _FakeSource("openalex"))
    assert (
        c.post("/api/papers/import", json={"source": "arxiv", "id": "2401.12345"}).status_code
        == 201
    )

    hits = _FakeSource("arxiv", [_paper("arxiv", "2401.12345"), _paper("arxiv", "2401.99999")])
    fake_sources("arxiv", hits)
    body = c.post("/api/papers/search", json={"q": "x", "sources": ["arxiv"]}).json()
    assert [r["in_library"] for r in body["results"]] == [True, False]


def test_sources_catalog(client, fake_sources):
    """来源目录：名字/展示名/能力齐备（前端据此渲染筛选器）。"""
    c, _settings = client
    fake_sources("arxiv", _FakeSource("arxiv", caps=SourceCaps(year=True, oa="always")))
    resp = c.get("/api/papers/sources")
    assert resp.status_code == 200
    item = resp.json()["sources"][0]
    assert item["name"] == "arxiv" and item["label"] == "arxiv"
    assert item["caps"]["year"] is True and item["caps"]["oa"] == "always"


@pytest.mark.parametrize(
    "payload",
    [
        {"q": ""},
        {"q": "x" * 201},
        {"q": "x", "limit": 51},
        {"q": "x", "limit": 0},
        {"q": "x", "offset": -1},
        {"q": "x", "sources": ["google"]},
        {"q": "x", "sources": []},  # 空选择集：不能是"没有来源"的静默合法态
        {"q": "x", "filters": {"sort": "whatever"}},
        {"q": "x", "filters": {"date_from": "2024-01-01", "date_to": "2020-01-01"}},  # 倒置
        {"q": "x", "filters": {"date_from": "2020-13-45"}},  # 非法日期
    ],
)
def test_search_422_matrix(client, payload):
    c, _settings = client
    assert c.post("/api/papers/search", json=payload).status_code == 422


# ---------------------------------------------------------------------------
# POST /api/papers/import
# ---------------------------------------------------------------------------


def test_import_happy_path(client, fake_sources, fake_downloader):
    c, settings = client
    src = _FakeSource("arxiv")
    src.fetch_map["2401.12345"] = _paper("arxiv", "2401.12345", title="注意力机制综述")
    fake_sources("arxiv", src)
    fake_sources("openalex", _FakeSource("openalex"))

    resp = c.post("/api/papers/import", json={"source": "arxiv", "id": "2401.12345"})
    assert resp.status_code == 201
    body = resp.json()
    doc = body["document"]
    assert doc["folder_id"] is None  # 落根级
    assert doc["file_type"] == "pdf"
    assert "入库成功" in body["message"]
    # uploads 副本名带去重后缀；标题 = 文件名 stem（ingest 语义，无 .pdf）
    assert doc["title"] == "注意力机制综述 (arxiv 2401.12345)"
    assert (settings.uploads_dir / f"{doc['title']}.pdf").is_file()
    # web-tmp 已清理（fake_downloader 的 dest 目录已随 ingest_web_file 收走）
    assert list((settings.data_dir / "web-tmp").iterdir()) == []


def test_import_same_title_different_ids_no_collision(client, fake_sources, fake_downloader):
    """两篇同名论文（不同 id）双入库不互踩；重导同一篇走 sha 跳过。"""
    c, settings = client
    src = _FakeSource("arxiv")
    src.fetch_map["2401.11111"] = _paper(
        "arxiv", "2401.11111", title="重名论文", pdf_url="http://127.0.0.1:9/a.pdf"
    )
    src.fetch_map["2401.22222"] = _paper(
        "arxiv", "2401.22222", title="重名论文", pdf_url="http://127.0.0.1:9/b.pdf"
    )
    fake_sources("arxiv", src)
    fake_sources("openalex", _FakeSource("openalex"))

    r1 = c.post("/api/papers/import", json={"source": "arxiv", "id": "2401.11111"})
    r2 = c.post("/api/papers/import", json={"source": "arxiv", "id": "2401.22222"})
    assert r1.status_code == 201 and r2.status_code == 201
    assert "2401.11111" in r1.json()["document"]["title"]
    assert "2401.22222" in r2.json()["document"]["title"]

    # 内容相同的第二次导入 → 200 跳过（sha256 幂等，不是同名替换）
    r3 = c.post("/api/papers/import", json={"source": "arxiv", "id": "2401.11111"})
    assert r3.status_code == 200
    assert "已跳过重复导入" in r3.json()["message"]
    docs = c.get("/api/documents").json()["documents"]
    assert len(docs) == 2  # 仍只有两篇


def test_import_backfills_source_ref_on_sha_skip(client, fake_sources, fake_downloader):
    """手拖入库的同内容论文被导入时：200 跳过 + **回填来源标记**。

    这是「已在库中」标记的兜底路径（此前零覆盖，2026-09-16 审查点名）：
    用户先把 PDF 拖进知识库（source_ref=NULL），之后又在「找论文」页导入同一篇
    —— sha256 会直接跳过，不回填的话这行永远显示"未在库中"，而字节明明在库里。
    只在原本没有来源时写：有来源的不覆盖（先来的更权威）。
    """
    c, settings = client
    src = _FakeSource("arxiv")
    paper = _paper("arxiv", "2401.99999", pdf_url="http://127.0.0.1:9/手工.pdf")
    src.fetch_map["2401.99999"] = paper
    fake_sources("arxiv", src)
    fake_sources("openalex", _FakeSource("openalex"))

    # 先"手工"入库：字节必须取自假下载器的缓存，**逐字节相同**才会走 sha 跳过
    same_bytes = fake_downloader["bytes_for"]("http://127.0.0.1:9/手工.pdf")
    uploaded = c.post("/api/documents", files={"file": ("手工下载的论文.pdf", same_bytes)}).json()[
        "document"
    ]
    assert uploaded["source_ref"] is None

    resp = c.post("/api/papers/import", json={"source": "arxiv", "id": "2401.99999"})
    assert resp.status_code == 200, resp.text
    assert "已跳过重复导入" in resp.json()["message"]
    assert resp.json()["document"]["id"] == uploaded["id"], "不该新建第二篇"
    assert resp.json()["document"]["source_ref"] == "arxiv:2401.99999"

    docs = c.get("/api/documents").json()["documents"]
    assert len(docs) == 1
    assert docs[0]["source_ref"] == "arxiv:2401.99999"


def test_import_no_open_access_409(client, fake_sources):
    c, _settings = client
    src = _FakeSource("openalex")
    src.fetch_map["W3160856016"] = _paper("openalex", "W3160856016", pdf_url=None, oa=False)
    fake_sources("arxiv", _FakeSource("arxiv"))
    fake_sources("openalex", src)

    resp = c.post("/api/papers/import", json={"source": "openalex", "id": "W3160856016"})
    assert resp.status_code == 409
    assert "开放获取" in resp.json()["detail"]
    assert "打开" in resp.json()["detail"]


def test_import_download_failure_502_and_cleans_tmp(client, fake_sources, fake_downloader):
    c, settings = client
    src = _FakeSource("arxiv")
    src.fetch_map["2401.12345"] = _paper("arxiv", "2401.12345")
    fake_sources("arxiv", src)
    fake_sources("openalex", _FakeSource("openalex"))
    fake_downloader["error"] = PaperError("论文下载失败（HTTP 404）")

    resp = c.post("/api/papers/import", json={"source": "arxiv", "id": "2401.12345"})
    assert resp.status_code == 502
    assert "HTTP 404" in resp.json()["detail"]
    assert list((settings.data_dir / "web-tmp").iterdir()) == []  # 半成品已清


def test_import_ingest_failure_400(client, fake_sources, monkeypatch):
    """下载成功但内容不是可解析的 PDF → ingest 报 400（假下载器写垃圾字节）。"""
    c, _settings = client

    def fake_download(url: str, dest: Path) -> int:
        garbage = b"%PDF-1.7 this is not a parseable pdf document body"
        dest.write_bytes(garbage)
        return len(garbage)

    monkeypatch.setattr(papers_router, "download_pdf", fake_download)
    src = _FakeSource("arxiv")
    src.fetch_map["2401.12345"] = _paper("arxiv", "2401.12345")
    fake_sources("arxiv", src)
    fake_sources("openalex", _FakeSource("openalex"))

    resp = c.post("/api/papers/import", json={"source": "arxiv", "id": "2401.12345"})
    assert resp.status_code == 400
    assert "入库失败" in resp.json()["detail"]


def test_import_invalid_id_422(client, fake_sources):
    c, _settings = client
    resp = c.post("/api/papers/import", json={"source": "arxiv", "id": "../etc/passwd"})
    assert resp.status_code == 422
    assert "非法的论文编号" in resp.json()["detail"]
    resp = c.post("/api/papers/import", json={"source": "openalex", "id": "W1"})
    assert resp.status_code == 422


def test_import_fetch_failure_502(client, fake_sources):
    c, _settings = client
    src = _FakeSource("arxiv")
    src.fetch_error = PaperError("arXiv 未找到该论文（2401.99999）")
    fake_sources("arxiv", src)
    fake_sources("openalex", _FakeSource("openalex"))

    resp = c.post("/api/papers/import", json={"source": "arxiv", "id": "2401.99999"})
    assert resp.status_code == 502
    assert "未找到" in resp.json()["detail"]


def test_import_invalidate_index_called(client, fake_sources, fake_downloader, monkeypatch):
    """入库后索引快照失效（spy 断言 201 与 200 跳过路径都触发）。"""
    c, _settings = client
    src = _FakeSource("arxiv")
    src.fetch_map["2401.12345"] = _paper("arxiv", "2401.12345")
    fake_sources("arxiv", src)
    fake_sources("openalex", _FakeSource("openalex"))

    calls: list[int] = []
    monkeypatch.setattr(
        c.app.state.services.ask, "invalidate_index", lambda: calls.append(1) or None
    )
    assert (
        c.post("/api/papers/import", json={"source": "arxiv", "id": "2401.12345"}).status_code
        == 201
    )
    assert (
        c.post("/api/papers/import", json={"source": "arxiv", "id": "2401.12345"}).status_code
        == 200
    )
    assert calls == [1, 1]


def test_import_then_move_into_folder(client, fake_sources, fake_downloader):
    """导入后 PATCH 移夹照常（共享尾链不该破坏 folder 语义）。"""
    c, _settings = client
    src = _FakeSource("arxiv")
    src.fetch_map["2401.12345"] = _paper("arxiv", "2401.12345", title="待移夹论文")
    fake_sources("arxiv", src)
    fake_sources("openalex", _FakeSource("openalex"))

    doc_id = c.post("/api/papers/import", json={"source": "arxiv", "id": "2401.12345"}).json()[
        "document"
    ]["id"]
    folder_id = _mk_folder(c, "论文夹")["id"]
    resp = c.patch(f"/api/documents/{doc_id}", json={"folder_id": folder_id})
    assert resp.status_code == 200
    assert resp.json()["document"]["folder_id"] == folder_id


# ---------------------------------------------------------------------------
# OpenAlex 密钥端点
# ---------------------------------------------------------------------------


def test_papers_settings_roundtrip(client):
    """PUT 写入 → GET 回 true；GET 永不下发密钥值；空串清除。"""
    c, _settings = client
    assert c.get("/api/papers/settings").json() == {"has_api_key": False}

    resp = c.put("/api/papers/settings", json={"api_key": "sk-papers-test"})
    assert resp.json() == {"has_api_key": True}
    assert os.environ.get("OPENALEX_API_KEY") == "sk-papers-test"  # 热生效

    body = c.get("/api/papers/settings").json()
    assert body == {"has_api_key": True}  # 只有布尔，没有值

    from mikasa.config.settings import user_env_path

    assert "sk-papers-test" in user_env_path().read_text(encoding="utf-8")

    assert c.put("/api/papers/settings", json={"api_key": ""}).json() == {"has_api_key": False}
    assert "OPENALEX_API_KEY" not in os.environ
    assert "OPENALEX_API_KEY" not in user_env_path().read_text(encoding="utf-8")


def test_papers_settings_none_untouched(client):
    """api_key=None（缺省）：不动已有密钥（同 ModelSettingsIn 三段语义）。"""
    c, _settings = client
    c.put("/api/papers/settings", json={"api_key": "sk-keep"})
    resp = c.put("/api/papers/settings", json={})
    assert resp.json() == {"has_api_key": True}
    assert os.environ.get("OPENALEX_API_KEY") == "sk-keep"


def test_search_schema_accepts_every_registered_source():
    """守卫：schema 的来源名单必须覆盖注册表里的每一个来源。

    2026-09-19 加 DOAJ 时实测踩中——注册表加了、schema 的 Literal 没加，
    前端勾上它就是一个 422（"服务端不认识这个来源"），而且只在真点的时候
    才暴露。两张表各写一份是必要的（pydantic 要 Literal 才能给 422 文案），
    所以要有一条测试把它们钉在一起。
    """
    from typing import get_args

    from mikasa.papers import source_catalog
    from mikasa.web.schemas import PaperImportIn, PaperSearchIn

    registered = {item["name"] for item in source_catalog()}
    # 检索请求体：list[Literal[...]] | None
    annotation = PaperSearchIn.model_fields["sources"].annotation
    inner = next(a for a in get_args(annotation) if a is not type(None))
    search_allowed = set(get_args(get_args(inner)[0]))
    # 导入请求体：裸 Literal[...]（导入只能指一个来源）
    import_allowed = set(get_args(PaperImportIn.model_fields["source"].annotation))
    for label, allowed in (("检索", search_allowed), ("导入", import_allowed)):
        assert registered <= allowed, f"{label} schema 缺少这些来源：{sorted(registered - allowed)}"


# ---------------------------------------------------------------------------
# 引证关系（v0.1.4：相关论文 / 引用了它）
# ---------------------------------------------------------------------------


def _stub_relations(
    monkeypatch,
    *,
    work_id="W1234567890",
    search_results=None,
    cited=None,
    cited_total=None,
    references=None,
):
    """相关论文走**本地检索**（桩 papers_search）、引证关系走 OpenAlex（桩它自己的函数）。"""
    import mikasa.papers.openalex as openalex_mod
    import mikasa.web.routers.papers as papers_router
    from mikasa.papers.service import PaperPage

    monkeypatch.setattr(openalex_mod, "resolve_work_id", lambda **kw: work_id)
    monkeypatch.setattr(
        papers_router,
        "papers_search",
        lambda q, sources, offset, limit, filters: PaperPage(
            results=tuple(search_results or []), errors={}, has_more=False
        ),
    )
    monkeypatch.setattr(openalex_mod, "cited_by", lambda wid, limit: (cited or [], cited_total))
    monkeypatch.setattr(openalex_mod, "references_of", lambda wid, limit: references or [])
    monkeypatch.setattr(papers_router, "fetch_paper", lambda source, pid: _PaperStub())


class _PaperStub:
    """fetch_paper 的替身：related 只用标题；cited/references 用 DOI。"""

    title = "南海北部天然气水合物富集特征及定量评价"
    doi = "10.3799/dqkx.2020.321"


def _openalex_paper(idx=1):
    from mikasa.papers.sources import PaperResult

    return PaperResult(
        source="openalex",
        id=f"W2{idx:09d}",
        title=f"南海北部天然气水合物相关论文 {idx}",  # 与桩标题共享词面（否则被守卫挡掉）
        authors=("作者甲",),
        year=2024,
        venue="某某学报",
        abstract="摘要",
        doi="",
        pdf_url=None,
        landing_url="https://example.org/x",
        oa=False,
    )


def test_related_uses_a_title_search(client, monkeypatch):
    """相关论文 = 拿标题再检索一次（不再用上游的 related_works，见路由注释）。"""
    _stub_relations(monkeypatch, search_results=[_openalex_paper(1), _openalex_paper(2)])
    c, _ = client
    body = c.get("/api/papers/related", params={"source": "openalex", "id": "W2123456789"}).json()
    assert [r["title"] for r in body["results"]] == [
        "南海北部天然气水合物相关论文 1",
        "南海北部天然气水合物相关论文 2",
    ]
    assert "按标题关键词检索" in body["note"]
    assert body["total"] is None


def test_related_drops_the_paper_itself(client, monkeypatch):
    """标题检索必然把它自己搜出来——要剔掉（否则第一条就是它）。"""
    from mikasa.papers.sources import PaperResult

    itself = PaperResult(
        source="openalex",
        id="W2123456789",
        title="南海北部天然气水合物（它自己）",
        authors=(),
        year=None,
        venue="",
        abstract="",
        doi="",
        pdf_url=None,
        landing_url="",
        oa=False,
    )
    _stub_relations(monkeypatch, search_results=[itself, _openalex_paper(1)])
    c, _ = client
    body = c.get("/api/papers/related", params={"source": "openalex", "id": "W2123456789"}).json()
    assert [r["title"] for r in body["results"]] == ["南海北部天然气水合物相关论文 1"]


def test_cited_reports_total(client, monkeypatch):
    _stub_relations(monkeypatch, cited=[_openalex_paper(3)], cited_total=66)
    c, _ = client
    body = c.get(
        "/api/papers/related",
        params={"source": "openalex", "id": "W2123456789", "kind": "cited"},
    ).json()
    assert body["total"] == 66
    assert len(body["results"]) == 1


def test_cited_bridges_other_sources_by_doi(client, monkeypatch):
    """非 OpenAlex 来源的"被引/参考文献"：按 DOI 桥接（中文刊也能看引证）。"""
    import mikasa.papers.openalex as openalex_mod

    seen = {}

    def fake_resolve(*, doi="", openalex_id=""):
        seen["doi"] = doi
        return "W2999999999"

    _stub_relations(monkeypatch, cited=[_openalex_paper(1)], cited_total=5)
    monkeypatch.setattr(openalex_mod, "resolve_work_id", fake_resolve)

    c, _ = client
    body = c.get(
        "/api/papers/related",
        params={"source": "doaj", "id": "0" * 32, "kind": "cited"},
    ).json()
    assert seen["doi"] == "10.3799/dqkx.2020.321"
    assert len(body["results"]) == 1 and body["total"] == 5


def test_references_kind(client, monkeypatch):
    _stub_relations(monkeypatch, references=[_openalex_paper(9)])
    c, _ = client
    body = c.get(
        "/api/papers/related",
        params={"source": "openalex", "id": "W2123456789", "kind": "references"},
    ).json()
    assert [r["title"] for r in body["results"]] == ["南海北部天然气水合物相关论文 9"]


def test_cited_without_doi_says_so_instead_of_inventing(client, monkeypatch):
    import mikasa.web.routers.papers as papers_router

    class _NoDoi:
        title = "无 DOI 的预印本"
        doi = ""

    monkeypatch.setattr(papers_router, "fetch_paper", lambda s, i: _NoDoi())
    c, _ = client
    body = c.get(
        "/api/papers/related", params={"source": "arxiv", "id": "2401.12345", "kind": "cited"}
    ).json()
    assert body["results"] == []
    assert "没有 DOI" in body["note"]
    assert body["total"] is None


def test_cited_when_openalex_has_no_record(client, monkeypatch):
    _stub_relations(monkeypatch, work_id=None)
    c, _ = client
    body = c.get(
        "/api/papers/related", params={"source": "openalex", "id": "W2123456789", "kind": "cited"}
    ).json()
    assert body["results"] == []
    assert "OpenAlex 里没有这篇论文的记录" in body["note"]


def test_related_rejects_bad_kind_and_id(client):
    c, _ = client
    assert (
        c.get(
            "/api/papers/related", params={"source": "openalex", "id": "W2123456789", "kind": "x"}
        ).status_code
        == 422
    )
    assert (
        c.get("/api/papers/related", params={"source": "openalex", "id": "bad"}).status_code == 422
    )


def test_related_query_truncates_cjk_titles():
    """相关论文的查询串：中文取前 8 字、英文取前 10 词（实测见函数 docstring）。"""
    import mikasa.web.routers.papers as papers_router

    q = papers_router._related_query("南海北部天然气水合物富集特征及定量评价")
    assert q == "南海北部天然气水"  # 8 字（实测这个长度在 OpenAlex 上召回最好）
    assert papers_router._related_query("Attention Is All You Need: A Survey of Transformers") == (
        "Attention Is All You Need: A Survey of Transformers"
    )  # 少于 10 词 → 原样
    long_en = " ".join(f"w{i}" for i in range(20))
    assert len(papers_router._related_query(long_en).split()) == 10


def test_related_passes_the_truncated_query_to_search(client, monkeypatch):
    """端到端：交给检索的确实是截断后的查询串（不是整串标题）。"""
    import mikasa.papers.openalex as openalex_mod
    import mikasa.web.routers.papers as papers_router
    from mikasa.papers.service import PaperPage

    seen = {}

    def fake_search(q, sources, offset, limit, filters):
        seen["q"] = q
        return PaperPage(results=(), errors={}, has_more=False)

    class _Stub:
        title = "南海北部天然气水合物富集特征及定量评价"
        doi = ""

    monkeypatch.setattr(papers_router, "papers_search", fake_search)
    monkeypatch.setattr(papers_router, "fetch_paper", lambda s, i: _Stub())
    monkeypatch.setattr(openalex_mod, "resolve_work_id", lambda **kw: "W1")
    c, _ = client
    c.get("/api/papers/related", params={"source": "openalex", "id": "W2123456789"})
    assert seen["q"] == "南海北部天然气水"


def test_related_filters_candidates_that_share_no_topic_words():
    """词面守卫：候选要与标题共享 ≥2 个汉字二元组（实测挡掉"蛇类生态"那类噪声）。"""
    import mikasa.web.routers.papers as papers_router

    title = "南海北部天然气水合物富集特征及定量评价"
    assert papers_router._shares_topic(
        title, "南海北部神狐海域沉积物颗粒对天然气水合物聚集的主要影响"
    )
    assert not papers_router._shares_topic(title, "白石砬子地区蛇类生态习性资源调查与管理")
    # 非中文标题不套这条（英文二元组没意义）
    assert papers_router._shares_topic("Attention Is All You Need", "Completely Unrelated Title")


def test_related_applies_the_topic_guard(client, monkeypatch):
    """端到端：噪声候选被挡掉，同主题的留下。"""
    import mikasa.papers.openalex as openalex_mod
    import mikasa.web.routers.papers as papers_router
    from mikasa.papers.service import PaperPage
    from mikasa.papers.sources import PaperResult

    def paper(t):
        return PaperResult(
            source="openalex",
            id=f"W{abs(hash(t)) % 10**9:09d}",
            title=t,
            authors=(),
            year=None,
            venue="",
            abstract="",
            doi="",
            pdf_url=None,
            landing_url="",
            oa=False,
        )

    class _Stub:
        title = "南海北部天然气水合物富集特征及定量评价"
        doi = ""

    monkeypatch.setattr(
        papers_router,
        "papers_search",
        lambda q, sources, offset, limit, filters: PaperPage(
            results=(
                paper("南海北部神狐海域沉积物颗粒对天然气水合物聚集的主要影响"),
                paper("白石砬子地区蛇类生态习性资源调查与管理"),
            ),
            errors={},
            has_more=False,
        ),
    )
    monkeypatch.setattr(papers_router, "fetch_paper", lambda s, i: _Stub())
    monkeypatch.setattr(openalex_mod, "resolve_work_id", lambda **kw: "W1")
    c, _ = client
    body = c.get("/api/papers/related", params={"source": "openalex", "id": "W2123456789"}).json()
    assert [r["title"][:6] for r in body["results"]] == ["南海北部神狐"]
