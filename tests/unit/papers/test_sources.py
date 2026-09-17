"""论文来源解析单测：arXiv Atom / OpenAlex JSON 的归一化与错误翻译。

全离线：打桩各模块的 `_http_get` 网络缝（或覆盖 base URL），不真联网。
"""

from __future__ import annotations

import urllib.error
import urllib.parse

import pytest

import mikasa.papers.arxiv as arxiv
import mikasa.papers.openalex as openalex
from mikasa.papers.errors import PaperError
from mikasa.papers.sources import PaperFilters, PaperResult


class _FakeUrlopen:
    """替身 urlopen：记录 Request，可选抛错或返回指定字节。

    用于打桩各模块的 `urllib.request.urlopen`（`_http_get` 内部的网络缝
    再往里一层）——这样错误翻译/节流/参数拼接这些**实现**才真正被测到。
    """

    def __init__(self, data: bytes = b"", exc: Exception | None = None) -> None:
        self._data = data
        self._exc = exc
        self.req = None

    def __call__(self, req, timeout=None):
        self.req = req
        if self._exc is not None:
            raise self._exc
        return self

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# arXiv：Atom 解析
# ---------------------------------------------------------------------------

ARXIV_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <title>ArXiv Query Results</title>
  <opensearch:totalResults xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">42</opensearch:totalResults>
  <entry>
    <id>http://arxiv.org/abs/2401.12345v2</id>
    <updated>2024-01-20T00:00:00Z</updated>
    <published>2024-01-15T00:00:00Z</published>
    <title>Attention Is All You Need:
      A Survey</title>
    <summary>  We survey the attention
      mechanism.  </summary>
    <author><name>Alice Zhang</name></author>
    <author><name>Bob Li</name></author>
    <arxiv:doi>10.1145/1234567.891011</arxiv:doi>
    <link href="http://arxiv.org/abs/2401.12345v2" rel="alternate" type="text/html"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/cs/0011001</id>
    <published>2000-11-01T00:00:00Z</published>
    <title>Old Style Paper</title>
    <author><name>Carol Wang</name></author>
    <link href="http://arxiv.org/abs/cs/0011001" rel="alternate" type="text/html"/>
  </entry>
</feed>
"""


def test_arxiv_parse_entry_fields(monkeypatch):
    """逐字段：版本后缀剥离、标题折叠空白、作者顺序、doi、pdf/landing URL。"""
    captured: dict[str, str] = {}

    def fake_get(url: str, timeout: float) -> bytes:
        captured["url"] = url
        return ARXIV_FEED

    monkeypatch.setattr(arxiv, "_http_get", fake_get)
    results, full = arxiv.ArxivSource().search('attention "survey"', 0, 10)

    assert captured["url"].startswith("https://export.arxiv.org/api/query?")
    assert "search_query=all%3A%22attention+survey%22" in captured["url"]  # 引号被剥
    assert "start=0" in captured["url"] and "max_results=10" in captured["url"]
    assert "sortBy=relevance" in captured["url"]
    assert full is False  # 2 条 < 10，未取满

    first, second = results
    assert first == PaperResult(
        source="arxiv",
        id="2401.12345",  # v2 已剥离
        title="Attention Is All You Need: A Survey",  # 换行折叠成空格
        authors=("Alice Zhang", "Bob Li"),
        year=2024,
        venue="arXiv",
        abstract="We survey the attention mechanism.",
        doi="10.1145/1234567.891011",
        pdf_url="https://arxiv.org/pdf/2401.12345",
        landing_url="http://arxiv.org/abs/2401.12345v2",
        oa=True,
    )
    assert second.id == "cs/0011001"  # 老式 id：无版本后缀，正则不误伤
    assert second.abstract == ""  # 无 summary → 空串
    assert second.doi == ""
    assert second.authors == ("Carol Wang",)


def test_arxiv_full_flag_and_env_base(monkeypatch):
    """取满 count → full=True；MIKASA_PAPERS_ARXIV_BASE 在调用时生效。"""
    calls: list[str] = []

    def fake_get(url: str, timeout: float) -> bytes:
        calls.append(url)
        return ARXIV_FEED

    monkeypatch.setattr(arxiv, "_http_get", fake_get)
    monkeypatch.setenv("MIKASA_PAPERS_ARXIV_BASE", "http://127.0.0.1:9/api/query")
    _, full = arxiv.ArxivSource().search("x", 0, 2)
    assert full is True
    assert calls[0].startswith("http://127.0.0.1:9/api/query?")


def test_arxiv_fetch_by_id_list(monkeypatch):
    captured: dict[str, str] = {}

    def fake_get(url: str, timeout: float) -> bytes:
        captured["url"] = url
        return ARXIV_FEED

    monkeypatch.setattr(arxiv, "_http_get", fake_get)
    result = arxiv.ArxivSource().fetch("2401.12345")
    assert "id_list=2401.12345" in captured["url"]
    assert result.id == "2401.12345"
    assert result.pdf_url.endswith("/2401.12345")


def test_arxiv_error_translation(monkeypatch):
    """URLError → 中文可回显文案（打桩 urlopen，让翻译逻辑真实跑一遍）。"""
    fake = _FakeUrlopen(exc=urllib.error.URLError("连接被拒"))
    monkeypatch.setattr(arxiv.urllib.request, "urlopen", fake)
    with pytest.raises(PaperError, match="无法连接 arXiv"):
        arxiv.ArxivSource().search("x", 0, 5)


def test_arxiv_garbage_response(monkeypatch):
    monkeypatch.setattr(arxiv.urllib.request, "urlopen", _FakeUrlopen(b"<html>"))
    with pytest.raises(PaperError, match="无法解析"):
        arxiv.ArxivSource().search("x", 0, 5)


def test_arxiv_throttle_sleeps_only_within_window(monkeypatch):
    """成功后补睡到 ≥3s 间隔；间隔已过则零等待（打桩 time，不真睡）。"""
    fake_time = {"now": 100.0, "slept": []}
    monkeypatch.setattr(arxiv.urllib.request, "urlopen", _FakeUrlopen(ARXIV_FEED))
    monkeypatch.setattr(arxiv.time, "monotonic", lambda: fake_time["now"])
    monkeypatch.setattr(arxiv.time, "sleep", lambda s: fake_time["slept"].append(s))

    arxiv._last_ok = 0.0  # noqa: SLF001 - 测试复位模块节流状态
    arxiv.ArxivSource().search("x", 0, 1)
    assert fake_time["slept"] == []  # 首次请求不睡
    fake_time["now"] += 1.0
    arxiv.ArxivSource().search("x", 0, 1)
    assert fake_time["slept"] == [2.0]  # 距上次成功 1s → 补睡 2s
    fake_time["now"] += 10.0
    arxiv.ArxivSource().search("x", 0, 1)
    assert fake_time["slept"] == [2.0]  # 间隔已过 → 不再睡


@pytest.mark.parametrize("bad", ["../etc/passwd", "a b", "", "x?y", "%00"])
def test_arxiv_id_rejects(bad):
    assert not arxiv.validate_id(bad)


@pytest.mark.parametrize("good", ["2401.12345", "cs/0011001", "math-ph/0505001"])
def test_arxiv_id_accepts(good):
    assert arxiv.validate_id(good)


# ---------------------------------------------------------------------------
# OpenAlex：JSON 归一化与倒排摘要重建
# ---------------------------------------------------------------------------

OPENALEX_WORK = {
    "id": "https://openalex.org/W3160856016",
    "doi": "https://doi.org/10.1038/s41586-021-04016-z",
    "display_name": "Highly accurate protein structure prediction",
    "publication_year": 2021,
    "authorships": [
        {"author": {"display_name": "John Jumper"}},
        {"author": {"display_name": "Richard Evans"}},
    ],
    "primary_location": {"source": {"display_name": "Nature"}},
    "abstract_inverted_index": {
        "proteins": [0],
        "are": [1],
        "essential": [2],
        "we": [3],
        "show": [4],
        "a": [5],
        "method": [6],
    },
    "open_access": {"is_oa": True, "oa_url": "https://www.nature.com/s41586-021-04016-z.pdf"},
    "language": "en",
}


def _fake_openalex_get(payload: dict | list):
    import json

    def fake_get(url: str, timeout: float) -> bytes:
        return json.dumps(payload).encode("utf-8")

    return fake_get


def test_openalex_parse_work(monkeypatch):
    monkeypatch.setattr(openalex, "_http_get", _fake_openalex_get(OPENALEX_WORK))
    result = openalex.OpenAlexSource().fetch("W3160856016")
    assert result.id == "W3160856016"
    assert result.title == "Highly accurate protein structure prediction"
    assert result.authors == ("John Jumper", "Richard Evans")
    assert result.year == 2021
    assert result.venue == "Nature"
    assert result.doi == "10.1038/s41586-021-04016-z"  # https://doi.org/ 前缀已剥
    assert result.oa is True
    assert result.pdf_url == "https://www.nature.com/s41586-021-04016-z.pdf"
    assert result.landing_url == "https://doi.org/10.1038/s41586-021-04016-z"


def test_reconstruct_abstract_exact():
    """倒排索引 → 按位置排序的正文（小写无标点，格式本身如此）。"""
    index = {"b": [1], "a": [0], "c": [2, 4], "d": [3]}
    assert openalex.reconstruct_abstract(index) == "a b c d c"
    assert openalex.reconstruct_abstract(None) == ""
    assert openalex.reconstruct_abstract({}) == ""


def _paged_openalex_fake(total: int):
    """按 page/per-page 真实切片的假 OpenAlex（id 里编上全局序号）。

    窗口对齐的正确性只能这样验：假源必须真的按 page/per-page 切片，
    否则"取错页"在上游看起来和"取对页"一样。
    """
    import json
    import urllib.parse

    def fake_get(url: str, timeout: float) -> bytes:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        page = int(query.get("page", ["1"])[0])
        per_page = int(query.get("per-page", ["10"])[0])
        start = (page - 1) * per_page
        works = [
            {"id": f"https://openalex.org/W{n:09d}", "display_name": f"w{n}"}
            for n in range(start, min(start + per_page, total))
        ]
        return json.dumps({"meta": {"count": total}, "results": works}).encode("utf-8")

    return fake_get


def test_openalex_search_params_and_select(monkeypatch):
    """请求参数与 select 瘦身列表（窗口切片的正确性由对齐测试覆盖）。"""
    captured: dict[str, str] = {}

    def fake_get(url: str, timeout: float) -> bytes:
        captured["url"] = url
        return _fake_openalex_get({"meta": {"count": 1}, "results": [OPENALEX_WORK]})(url, timeout)

    monkeypatch.setattr(openalex, "_http_get", fake_get)
    results, full = openalex.OpenAlexSource().search("水库坝", 0, 10)

    assert captured["url"].startswith("https://api.openalex.org/works?")
    assert "search=%E6%B0%B4%E5%BA%93%E5%9D%9D" in captured["url"]
    assert "sort=relevance_score%3Adesc" in captured["url"]
    assert "abstract_inverted_index" in captured["url"]  # select 瘦身列表
    assert "is_oa" not in captured["url"]  # 没要求"只看 OA"就不发该 filter
    assert len(results) == 1 and full is False


def test_openalex_filter_translation(monkeypatch):
    """年份 / 只看 OA / 语言合并进一个 filter 参数（逗号分隔的 OpenAlex 语法）。"""
    captured: dict[str, str] = {}

    def fake_get(url: str, timeout: float) -> bytes:
        captured["url"] = url
        return b'{"results": []}'

    monkeypatch.setattr(openalex, "_http_get", fake_get)
    openalex.OpenAlexSource().search(
        "x",
        0,
        5,
        filters=PaperFilters(
            date_from="2020-01-01", date_to="2024-12-31", oa_only=True, language="zh", sort="cited"
        ),
    )
    url = urllib.parse.unquote(captured["url"])
    assert (
        "filter=from_publication_date:2020-01-01,to_publication_date:2024-12-31,is_oa:true,language:zh"
        in url
    )
    assert "sort=cited_by_count:desc" in url


@pytest.mark.parametrize(("start", "count"), [(0, 7), (7, 6), (13, 7), (20, 6), (33, 10)])
def test_openalex_window_alignment(monkeypatch, start, count):
    """窗口对齐：请求的每页值与切片偏移必须同源于 per-page。

    回归锁（2026-09-16 真踩过）：第一版按 count 对齐页号、却按 per-page
    取页，两者不是同一倍数 → 翻页时结果重复（E2E 实测 40 条里 5 条重复）。
    N 源轮转下 start 一般不是 count 的整数倍，只有这里能拦住。
    """
    monkeypatch.setattr(openalex, "_http_get", _paged_openalex_fake(100))
    results, full = openalex.OpenAlexSource().search("x", start, count)
    ids = [r.id for r in results]
    expected = [f"W{n:09d}" for n in range(start, start + count)]
    assert ids == expected
    assert full is True  # 100 条足够，取满


def test_openalex_window_alignment_no_duplicate_across_pages(monkeypatch):
    """逐页取数零重复：模拟前端按窗口大小推进 offset 的真实翻页。"""
    monkeypatch.setattr(openalex, "_http_get", _paged_openalex_fake(100))
    src = openalex.OpenAlexSource()
    seen: list[str] = []
    for page_start in (0, 7, 14, 21, 28):  # count=7 的三源轮转窗口
        results, _ = src.search("x", page_start, 7)
        seen.extend(r.id for r in results)
    assert len(seen) == len(set(seen)), f"翻页出现重复：{seen}"


def test_openalex_missing_fields_defaults(monkeypatch):
    work = {"id": "https://openalex.org/W1234567"}
    monkeypatch.setattr(openalex, "_http_get", _fake_openalex_get(work))
    result = openalex.OpenAlexSource().fetch("W1234567")
    assert result.title == "(无标题)"
    assert result.authors == () and result.venue == "" and result.abstract == ""
    assert result.year is None and result.doi == "" and result.pdf_url is None
    assert result.oa is False
    assert result.landing_url.startswith("https://openalex.org/W1234567")


def test_openalex_429_without_key_hints_registration(monkeypatch):
    """429 且无 key → 文案指向免费注册 + 面板粘贴入口。"""
    fake = _FakeUrlopen(exc=urllib.error.HTTPError("url", 429, "budget", {}, None))
    monkeypatch.setattr(openalex.urllib.request, "urlopen", fake)
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    with pytest.raises(PaperError, match="免费 API 密钥"):
        openalex.OpenAlexSource().search("x", 0, 5)


def test_openalex_429_with_key_hints_retry(monkeypatch):
    fake = _FakeUrlopen(exc=urllib.error.HTTPError("url", 429, "budget", {}, None))
    monkeypatch.setattr(openalex.urllib.request, "urlopen", fake)
    monkeypatch.setenv("OPENALEX_API_KEY", "sk-fake")
    with pytest.raises(PaperError, match="限流"):
        openalex.OpenAlexSource().search("x", 0, 5)


def test_openalex_key_read_at_call_time(monkeypatch):
    """key 在调用时读取（.env 热生效）：先设后调 → URL 带 api_key。"""
    fake = _FakeUrlopen(b'{"meta": {"count": 0}, "results": []}')
    monkeypatch.setattr(openalex.urllib.request, "urlopen", fake)
    monkeypatch.setenv("OPENALEX_API_KEY", "sk-call-time")
    openalex.OpenAlexSource().search("x", 0, 5)
    assert "api_key=sk-call-time" in fake.req.full_url


def test_openalex_timeout_translation(monkeypatch):

    fake = _FakeUrlopen(exc=TimeoutError("读超时"))
    monkeypatch.setattr(openalex.urllib.request, "urlopen", fake)
    with pytest.raises(PaperError, match="无法连接 OpenAlex"):
        openalex.OpenAlexSource().search("x", 0, 5)


@pytest.mark.parametrize("bad", ["W1", "w1234567", "W12 34", "W", "abc"])
def test_openalex_id_rejects(bad):
    assert not openalex.validate_id(bad)


def test_openalex_id_accepts():
    assert openalex.validate_id("W3160856016")
