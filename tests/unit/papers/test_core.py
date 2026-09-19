"""CORE 来源单测：JSON 解析 / 脏字段防御 / URL 翻译 / 错误文案。

全离线：打桩 `mikasa.papers.core._http_get`（唯一网络缝），永不真联网。
上游字段形状以 2026-09-16 的实测为准（见 core.py 模块头）。
"""

from __future__ import annotations

import json
import urllib.parse

import pytest

import mikasa.papers.core as core
from mikasa.papers.errors import PaperError
from mikasa.papers.sources import PaperFilters


@pytest.fixture()
def stub_http(monkeypatch):
    """假网络缝：记录请求 URL，返回编排好的响应。"""
    state: dict = {"urls": [], "body": b"{}", "error": None}

    def fake(url: str, timeout: float) -> bytes:
        state["urls"].append(url)
        if state["error"]:
            raise state["error"]
        return state["body"]

    monkeypatch.setattr(core, "_http_get", fake)
    return state


def _work(**overrides) -> dict:
    """一条实测形状的 work 记录（字段名逐字照抄）。"""
    work = {
        "id": 72543,
        "title": "Benefit of Grand Ethiopian Renaissance Dam Project",
        "authors": [{"name": "Tesfa, Belachew"}, {"name": "另一位作者"}],
        "yearPublished": 2021,
        "citationCount": 12,
        "abstract": "This article quantifies the major benefits …",
        "doi": "https://doi.org/10.1000/core-1",
        "downloadUrl": "https://core.ac.uk/download/20363594.pdf",
        "journals": [],
        "publisher": "EIPSA Communicating Article",
        "language": {"code": "en", "name": "English"},
        "links": [
            {"type": "download", "url": "https://core.ac.uk/download/20363594.pdf"},
            {"type": "reader", "url": "https://core.ac.uk/reader/20363594"},
        ],
    }
    work.update(overrides)
    return work


def _results_body(works: list[dict]) -> bytes:
    return json.dumps({"totalHits": len(works), "results": works}).encode("utf-8")


# ---------------------------------------------------------------------------
# 解析与字段映射
# ---------------------------------------------------------------------------


def test_normalize_maps_all_fields():
    paper = core._normalize(_work())
    assert paper.source == "core" and paper.id == "72543"
    assert paper.title.startswith("Benefit of Grand Ethiopian")
    assert paper.authors == ("Tesfa, Belachew", "另一位作者")
    assert paper.year == 2021
    assert paper.venue == "EIPSA Communicating Article"  # journals 空 → 退 publisher
    assert paper.doi == "10.1000/core-1"  # https://doi.org/ 前缀剥掉
    assert paper.pdf_url == "https://core.ac.uk/download/20363594.pdf"
    assert paper.landing_url == "https://doi.org/10.1000/core-1"  # 有 DOI 走 DOI
    assert paper.cited_by == 12
    assert paper.language == "en"
    assert paper.oa is True


def test_normalize_prefers_journal_title_over_publisher():
    work = _work(journals=[{"title": "某某学报"}])
    assert core._normalize(work).venue == "某某学报"


def test_normalize_year_defends_against_dirty_values():
    """`yearPublished` 实测见过 710300 / 202022 这类脏值。

    规则：取前四位当 YYYYMM 解释，落在 1900~2100 之外判 None——否则界面上
    会出现"710300 年"。`202022` 按此规则得到 2020（年份本身可信）。
    """
    for raw, expected in [
        (2021, 2021),
        ("2021", 2021),
        (201703, 2017),  # YYYYMM 形态：取前四位
        (202022, 2020),  # 同上（月份非法但年份可信）
        (710300, None),  # 前四位越界 → 判 None
        ("", None),
        (None, None),
        ("abcd", None),
        (1800, None),
    ]:
        assert core._parse_year(raw) == expected, raw


def test_normalize_missing_download_url_means_no_fulltext():
    """无 downloadUrl = 这条记录没有可抓的全文（**不能**因为"CORE 全 OA"
    就写死一个 pdf_url），导入端点据此走 409 提示路径。"""
    paper = core._normalize(_work(downloadUrl="", doi=None, links=[]))
    assert paper.pdf_url is None
    assert paper.landing_url == "https://core.ac.uk/works/72543"  # 兜底详情页


def test_normalize_landing_falls_back_to_reader_link():
    paper = core._normalize(_work(doi=None))
    assert paper.landing_url == "https://core.ac.uk/reader/20363594"


def test_normalize_language_accepts_dict_and_string():
    assert core._normalize(_work(language={"code": "zh", "name": "Chinese"})).language == "zh"
    assert core._normalize(_work(language="en")).language == "en"
    assert core._normalize(_work(language=None)).language == ""


def test_normalize_rejects_bad_id():
    with pytest.raises(PaperError, match="无法识别的论文编号"):
        core._normalize(_work(id="not-a-number"))


def test_validate_id():
    assert core.validate_id("72543") and core.validate_id("20363594")
    for bad in ("", "W123", "12/34", "../etc", "12 34", "a72543"):
        assert not core.validate_id(bad), bad


# ---------------------------------------------------------------------------
# search / fetch 的 URL 翻译
# ---------------------------------------------------------------------------


def test_search_url_shape(stub_http):
    stub_http["body"] = _results_body([_work()])
    results, total = core.CoreSource().search("水库坝", 20, 10)
    assert len(results) == 1 and total == 1  # totalHits（假响应里是 len(results)）
    url = stub_http["urls"][0]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert query["q"] == ["水库坝"]
    assert query["limit"] == ["10"]  # 必须显式传（CORE 默认 10 / 上限 100）
    assert query["offset"] == ["20"]  # 原生 offset，与窗口公式直连
    assert query["sort"] == ["relevance"]
    assert urlsplit_path(url) == "/v3/search/works/"


def urlsplit_path(url: str) -> str:
    return urllib.parse.urlsplit(url).path


def test_search_translates_year_into_query_syntax(stub_http):
    """年份过滤**只能**走查询语法：yearFrom/yearTo 参数实测被静默忽略。"""
    stub_http["body"] = _results_body([])
    core.CoreSource().search(
        "x", 0, 5, filters=PaperFilters(date_from="2020-01-01", date_to="2024-12-31")
    )
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(stub_http["urls"][0]).query)["q"][0]
    assert "yearPublished>=2020" in q and "yearPublished<=2024" in q


def test_search_sort_translation(stub_http):
    stub_http["body"] = _results_body([])
    src = core.CoreSource()
    src.search("x", 0, 5, filters=PaperFilters(sort="recent"))
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(stub_http["urls"][0]).query)["sort"] == [
        "recency"
    ]
    # cited：CORE 不支持（sort=citationCount 会 500）→ 回落 relevance
    src.search("x", 0, 5, filters=PaperFilters(sort="cited"))
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(stub_http["urls"][1]).query)["sort"] == [
        "relevance"
    ]


def test_search_total(stub_http):
    """上游 totalHits → 命中总数（界面用它显示"命中 N 条"）。"""
    stub_http["body"] = _results_body([_work(), _work(id=72544)])
    _, total = core.CoreSource().search("x", 0, 2)
    assert total == 2


def test_fetch_by_id(stub_http):
    stub_http["body"] = json.dumps(_work()).encode("utf-8")
    paper = core.CoreSource().fetch("72543")
    assert paper.id == "72543"
    assert urlsplit_path(stub_http["urls"][0]) == "/v3/works/72543"


def test_fetch_rejects_bad_id(stub_http):
    with pytest.raises(PaperError, match="非法的 CORE 论文编号"):
        core.CoreSource().fetch("../../etc/passwd")
    assert stub_http["urls"] == []  # 校验不过不联网


# ---------------------------------------------------------------------------
# 错误路径
# ---------------------------------------------------------------------------


def test_rate_limit_message(monkeypatch):
    """429 要给可操作的中文提示（该源限流很紧，实测连打 5~6 次即中）。

    这一例打桩的是 `urlopen`（比 `_http_get` 更深一层）：要验的正是
    **错误翻译**那段代码，桩在 `_http_get` 上会把它整段跳过。
    """
    import urllib.error

    def boom(req, timeout=None):  # noqa: ANN001 - 对齐 urlopen 签名
        raise urllib.error.HTTPError("u", 429, "Too Many", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(core.urllib.request, "urlopen", boom)
    with pytest.raises(PaperError, match="过于频繁"):
        core.CoreSource().search("x", 0, 5)


def test_bad_json_and_bad_shape(stub_http):
    stub_http["body"] = b"<html>not json</html>"
    with pytest.raises(PaperError, match="无法解析"):
        core.CoreSource().search("x", 0, 5)
    stub_http["body"] = json.dumps({"results": "不是列表"}).encode("utf-8")
    with pytest.raises(PaperError, match="缺少 results 列表"):
        core.CoreSource().search("x", 0, 5)


def test_caps_declared_from_measurements():
    """能力声明只以实测为准（未验证的一律 False）。"""
    caps = core.CoreSource().caps
    assert caps.year is True  # 走 q 里的 yearPublished 语法
    assert caps.recent_sort is True  # sort=recency 实测有效
    assert caps.cited_sort is False  # sort=citationCount 实测 HTTP 500
    assert caps.language is False
    assert caps.oa == "always"  # CORE 本身是开放获取聚合库
