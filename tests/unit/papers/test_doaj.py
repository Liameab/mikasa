"""DOAJ 来源单测：URL 形状、字段归一化、年份语法、id 反查与错误翻译。

全离线：打桩 `mikasa.papers.doaj._http_get`（唯一网络缝）；错误翻译那一例
打到更深一层的 `urlopen`（桩在 _http_get 上会把翻译代码整段跳过，与
test_core.py 同一手法）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse

import pytest

import mikasa.papers.doaj as doaj
from mikasa.papers.errors import PaperError
from mikasa.papers.sources import PaperFilters

_ITEM_ID = "04f84a13de1b4cd8a19e9af019205cbf"


def _row(**over) -> dict:
    bib = {
        "title": "DWTR-PAC强化混凝去除水中Cr（Ⅵ）的研究及参数优化",
        "year": "2022",
        "abstract": "给水污泥（DWTR）是自来水厂产生的废弃物……",
        "author": [{"name": "陈子涵"}, {"name": "张笑语"}],
        "journal": {"title": "工业水处理", "publisher": "Editorial Office", "language": ["ZH"]},
        "identifier": [{"id": "1005-829X", "type": "pissn"}],
        "link": [{"type": "fulltext", "url": "http://www.iwt.cn/thesisDetails#10.19965/x"}],
    }
    bib.update(over)
    return {"id": _ITEM_ID, "bibjson": bib}


def _stub_get(monkeypatch, payload: dict, captured: dict | None = None):
    def fake(url: str, timeout: float) -> bytes:
        if captured is not None:
            captured["url"] = url
        return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(doaj, "_http_get", fake)


# ---------------- 检索与归一化 ----------------


def test_search_url_shape_and_row_normalization(monkeypatch):
    captured: dict[str, str] = {}
    _stub_get(monkeypatch, {"total": 185, "results": [_row()]}, captured)

    results, total = doaj.DoajSource().search("充填体", 0, 10)

    url = captured["url"]
    assert url.startswith("https://doaj.org/api/search/articles/")
    assert "充填体" in urllib.parse.unquote(url)  # 查询串在路径里（DOAJ 的形状）
    assert "pageSize=10" in url and "page=1" in url
    assert total == 185

    paper = results[0]
    assert paper.source == "doaj" and paper.id == _ITEM_ID
    assert paper.title.startswith("DWTR-PAC")
    assert paper.authors == ("陈子涵", "张笑语")
    assert paper.year == 2022  # 字符串 "2022" → int
    assert paper.venue == "工业水处理"
    assert paper.oa is True
    assert paper.cited_by is None  # 目录不提供被引数据
    assert paper.language == "zh"  # 来自 journal.language 的 ["ZH"]
    # 落地页 = link[] 的第一条；**不是** pdf_url——导入要的是 PDF 字节
    assert paper.pdf_url is None
    assert paper.landing_url.startswith("http://www.iwt.cn/")


def test_search_slices_window_from_single_page(monkeypatch):
    """窗口对齐：一次取够 start+count 再切片（与 OpenAlex 同一条纪律）。"""
    captured: dict[str, str] = {}
    rows = []
    for i in range(40):
        r = _row()
        r["bibjson"]["title"] = f"论文{i}"
        rows.append(r)
    _stub_get(monkeypatch, {"total": 40, "results": rows}, captured)

    results, total = doaj.DoajSource().search("x", 7, 6)

    assert "pageSize=13" in captured["url"]  # start+count，不是 count
    assert [r.title for r in results] == [f"论文{i}" for i in range(7, 13)]
    assert total == 40


def test_year_filter_goes_into_the_query(monkeypatch):
    """DOAJ 没有 filter 参数：年份走查询语法（实测 185 → 115 条）。"""
    captured: dict[str, str] = {}
    _stub_get(monkeypatch, {"total": 0, "results": []}, captured)

    doaj.DoajSource().search(
        "充填体", 0, 5, filters=PaperFilters(date_from="2015-03-01", date_to="2026-09-19")
    )

    query = urllib.parse.unquote(captured["url"])
    assert "bibjson.year:[2015 TO 2026]" in query


@pytest.mark.parametrize(
    ("date_from", "date_to", "expected"),
    [
        # 只填一边时，另一边走默认——**按含义取界，不按"填了几个"**（2026-09-24 修）。
        # 修前只填 date_to 会被当下界使（`[2020 TO 2100]`），"检索 2020 年以前"
        # 返回的正好是补集，且 caps.year=True 不产生降级提示，静默错。
        ("2015-03-01", None, "bibjson.year:[2015 TO 2100]"),
        (None, "2020-12-31", "bibjson.year:[1900 TO 2020]"),
        ("2015-03-01", "2026-09-19", "bibjson.year:[2015 TO 2026]"),
    ],
)
def test_year_bounds_are_per_side(monkeypatch, date_from, date_to, expected):
    captured: dict[str, str] = {}
    _stub_get(monkeypatch, {"total": 0, "results": []}, captured)

    doaj.DoajSource().search("充填体", 0, 5, filters=PaperFilters(date_from, date_to))

    assert expected in urllib.parse.unquote(captured["url"])


def test_no_year_filter_means_no_clause(monkeypatch):
    """两边都不填时**不拼子句**（不是拼一个 `[1900 TO 2100]`）。"""
    captured: dict[str, str] = {}
    _stub_get(monkeypatch, {"total": 0, "results": []}, captured)

    doaj.DoajSource().search("充填体", 0, 5, filters=None)

    assert "bibjson.year" not in urllib.parse.unquote(captured["url"])


def test_pdf_link_is_detected_only_when_it_really_ends_with_pdf(monkeypatch):
    row = _row(
        link=[
            {"type": "fulltext", "url": "https://example.org/article/1"},
            {"type": "fulltext", "url": "https://example.org/article/1.PDF"},
        ]
    )
    _stub_get(monkeypatch, {"total": 1, "results": [row]})
    results, _ = doaj.DoajSource().search("x", 0, 1)
    assert results[0].pdf_url == "https://example.org/article/1.PDF"


def test_doi_taken_from_identifier_type_doi(monkeypatch):
    row = _row(
        identifier=[
            {"id": "1005-829X", "type": "pissn"},
            {"id": "10.19965/j.cnki.iwt.2021-0579", "type": "doi"},
        ]
    )
    _stub_get(monkeypatch, {"total": 1, "results": [row]})
    results, _ = doaj.DoajSource().search("x", 0, 1)
    assert results[0].doi == "10.19965/j.cnki.iwt.2021-0579"


def test_landing_falls_back_to_doi_then_doaj_page(monkeypatch):
    """没有 link[] 时：优先 DOI 跳转，再退到 DOAJ 自己的条目页。"""
    _stub_get(
        monkeypatch,
        {"total": 1, "results": [_row(link=[], identifier=[{"id": "10.1/x", "type": "doi"}])]},
    )
    results, _ = doaj.DoajSource().search("x", 0, 1)
    assert results[0].landing_url == "https://doi.org/10.1/x"

    _stub_get(monkeypatch, {"total": 1, "results": [_row(link=[], identifier=[])]})
    results, _ = doaj.DoajSource().search("x", 0, 1)
    assert results[0].landing_url == f"https://doaj.org/article/{_ITEM_ID}"


@pytest.mark.parametrize("year", ["2022x", "202022", "710300", "", None, "abcd"])
def test_dirty_year_becomes_none(monkeypatch, year):
    _stub_get(monkeypatch, {"total": 1, "results": [_row(year=year)]})
    results, _ = doaj.DoajSource().search("x", 0, 1)
    assert results[0].year is None


def test_missing_total_is_none(monkeypatch):
    _stub_get(monkeypatch, {"results": [_row()]})
    _, total = doaj.DoajSource().search("x", 0, 1)
    assert total is None


def test_unknown_id_shape_raises(monkeypatch):
    _stub_get(monkeypatch, {"total": 1, "results": [{"id": "不是十六进制", "bibjson": {}}]})
    with pytest.raises(PaperError, match="无法识别的条目编号"):
        doaj.DoajSource().search("x", 0, 1)


def test_missing_results_key_raises(monkeypatch):
    _stub_get(monkeypatch, {"total": 1})
    with pytest.raises(PaperError, match="缺少 results"):
        doaj.DoajSource().search("x", 0, 1)


# ---------------- 反查与 id 校验 ----------------


def test_fetch_by_id_uses_id_query(monkeypatch):
    captured: dict[str, str] = {}
    _stub_get(monkeypatch, {"total": 1, "results": [_row()]}, captured)

    paper = doaj.DoajSource().fetch(_ITEM_ID)

    # 实测 id:"x" 与 id:x 都能反查到（total=1）；实现用不带引号的那种
    assert f"id:{_ITEM_ID}" in urllib.parse.unquote(captured["url"])
    assert paper.id == _ITEM_ID


def test_fetch_missing_raises(monkeypatch):
    _stub_get(monkeypatch, {"total": 0, "results": []})
    with pytest.raises(PaperError, match="未找到该条目"):
        doaj.DoajSource().fetch(_ITEM_ID)


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        (_ITEM_ID, True),
        ("04F84A13DE1B4CD8A19E9AF019205CBF", False),
        ("x" * 32, False),
        ("", False),
        ("04f84a13", False),
    ],
)
def test_validate_id(value, ok):
    assert doaj.validate_id(value) is ok


# ---------------- 错误翻译（桩打到 urlopen 那一层） ----------------


@pytest.mark.parametrize(
    ("exc", "fragment"),
    [
        (urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None), "过于频繁"),
        (urllib.error.HTTPError("u", 500, "Server Error", {}, None), "HTTP 500"),
        (urllib.error.URLError("reset"), "无法连接 DOAJ"),
    ],
)
def test_error_translation(monkeypatch, exc, fragment):
    def boom(req, timeout=None):
        raise exc

    monkeypatch.setattr(doaj.urllib.request, "urlopen", boom)
    with pytest.raises(PaperError, match=fragment):
        doaj.DoajSource().search("x", 0, 1)


def test_markup_is_stripped_from_title_and_abstract(monkeypatch):
    """DOAJ 把论文 HTML 原样塞进元数据：界面只该看到纯文本。

    实测症状（2026-09-19 截图里看到的）：摘要显示成
    `1<sup>#</sup> - 2<sup>#</sup>`——标签没删，读者看到的是源码。
    顺手也锁住实体还原（`&amp;` → `&`）。
    """
    row = _row(
        title="改性纳米零价铁<i>去除</i>硝酸盐 &amp; 硫酸盐",
        abstract="分为 1<sup>#</sup> 与 2<sup>#</sup> 两级，浓度 &lt;5 mg/L",
    )
    _stub_get(monkeypatch, {"total": 1, "results": [row]})
    results, _ = doaj.DoajSource().search("x", 0, 1)
    assert results[0].title == "改性纳米零价铁 去除 硝酸盐 & 硫酸盐"
    assert results[0].abstract == "分为 1 # 与 2 # 两级，浓度 <5 mg/L"


def _paged_doaj_fake(total: int, calls: list[tuple[int, int]]):
    """按 page/pageSize 真实切片的假 DOAJ（深翻页只能从这里断言"取的是哪一页"）。"""

    def fake_get(url: str, timeout: float) -> bytes:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        page = int(query.get("page", ["1"])[0])
        size = int(query.get("pageSize", ["10"])[0])
        calls.append((page, size))
        rows = []
        for n in range((page - 1) * size, min(page * size, total)):
            r = _row()
            r["bibjson"]["title"] = f"论文{n}"
            rows.append(r)
        return json.dumps({"total": total, "results": rows}).encode("utf-8")

    return fake_get


def test_deep_window_asks_for_the_page_containing_it(monkeypatch):
    """深翻页（2026-09-20）：只取包含窗口的那一页，而不是从第 1 页取到 start。

    旧实现受单页上限 100 拖累，第 8 页之后这个源就悄悄不出结果了。
    """
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(doaj, "_http_get", _paged_doaj_fake(5_000, calls))

    results, total = doaj.DoajSource().search("x", 450, 13)

    assert calls == [(5, 100)]  # 450 // 100 + 1，一次请求
    assert [r.title for r in results] == [f"论文{n}" for n in range(450, 463)]
    assert total == 5_000


def test_window_straddling_two_pages_fetches_both(monkeypatch):
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(doaj, "_http_get", _paged_doaj_fake(1_000, calls))

    results, _ = doaj.DoajSource().search("x", 95, 13)

    assert calls == [(1, 100), (2, 100)]
    assert [r.title for r in results] == [f"论文{n}" for n in range(95, 108)]


def test_stops_at_the_depth_limit_without_a_request(monkeypatch):
    """超过深度上限：如实返回空，且**一个请求都不发**。"""
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(doaj, "_http_get", _paged_doaj_fake(50_000, calls))

    assert doaj.DoajSource().search("x", 10_000, 13) == ([], None)
    assert calls == []
