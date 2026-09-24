"""笔记互链（`[[标题]]` → doc_links）单测：解析口径 + 落边的替换语义。

两段分开测：
  - `extract_targets` 是纯函数，把"什么算链接"钉死（代码块/别名/空目标/去重）；
  - `sync_doc_links` 走真库（tmp 隔离），验的是**替换语义**：删掉一个链接，
    对应的边必须消失——那是它与"追加"写法唯一的分水岭。
"""

from __future__ import annotations

import pytest

from mikasa.config.settings import load_settings
from mikasa.ingest.wikilinks import extract_targets, sync_doc_links
from mikasa.models.document import Document
from mikasa.storage import repo
from mikasa.storage.db import open_db


@pytest.fixture()
def settings(tmp_path):
    s = load_settings("offline", data_dir=tmp_path / "data")
    s.ensure_dirs()
    return s


def _add_doc(settings, title: str) -> int:
    """直接插一行 documents（不跑入库：互链只认标题，不需要正文与向量）。"""
    with open_db(settings.db_path) as conn:
        doc_id = repo.insert_document(
            conn,
            Document(
                title=title,
                file_path=f"/x/{title}.md",
                file_type="md",
                file_sha256=f"sha-{title}",
                char_count=10,
            ),
        )
        conn.commit()
    return doc_id


# ---------------------------------------------------------------------------
# 解析：什么算一条链接
# ---------------------------------------------------------------------------


def test_extract_basic_alias_and_dedup():
    """基本形态、别名（`[[目标|显示文字]]`）、重复目标只算一次。"""
    text = "见 [[线性回归]]，以及 [[逻辑回归|那篇分类的]]，再回头看 [[线性回归]]。"
    assert extract_targets(text) == ["线性回归", "逻辑回归"]


def test_extract_ignores_code():
    """行内代码与围栏代码块里的 [[ ]] 是字面量（同 KaTeX 那套闸门）。"""
    text = (
        "正文 [[真链接]]\n"
        "`[[行内代码里的]]`\n"
        "```python\n# [[代码块里的]]\n```\n"
        "收尾 [[另一个真链接]]"
    )
    assert extract_targets(text) == ["真链接", "另一个真链接"]


def test_extract_normalizes_and_skips_empty():
    """标题折空白；空目标（`[[]]`、`[[  ]]`）不算链接。"""
    assert extract_targets("[[  多  空格 ]]") == ["多 空格"]
    assert extract_targets("[[]] [[|只有别名]] [[  ]]") == []
    assert extract_targets("没有链接的一段话") == []


# ---------------------------------------------------------------------------
# 落边：替换语义与解析规则
# ---------------------------------------------------------------------------


def test_sync_writes_edges_to_matching_titles(settings):
    """命中的标题落成边（两个方向都算邻居的那套读由 repo 负责，这里只管写）。"""
    a = _add_doc(settings, "甲篇")
    b = _add_doc(settings, "乙篇")
    c = _add_doc(settings, "丙篇")

    added = sync_doc_links(settings, a, "接 [[乙篇]] 与 [[丙篇]]")
    assert added == 2

    with open_db(settings.db_path) as conn:
        links = repo.links_for_document(conn, a)
    assert len(links) == 2
    assert {row["other_id"] for row in links} == {b, c}
    assert all(row["source"] == "wikilink" and row["relation"] == "mentions" for row in links)


def test_sync_is_a_replace_not_an_append(settings):
    """删掉链接再保存 → 边随之消失（这是"替换而非追加"的分水岭）。"""
    a = _add_doc(settings, "甲篇")
    b = _add_doc(settings, "乙篇")
    _add_doc(settings, "丙篇")

    sync_doc_links(settings, a, "接 [[乙篇]] 与 [[丙篇]]")
    sync_doc_links(settings, a, "只接 [[乙篇]] 了")

    with open_db(settings.db_path) as conn:
        links = repo.links_for_document(conn, a)
    assert [row["other_id"] for row in links] == [b]


def test_sync_clears_edges_when_all_links_removed(settings):
    """链接全部删光时也得跑一次替换，否则旧边永远留着。"""
    a = _add_doc(settings, "甲篇")
    _add_doc(settings, "乙篇")
    sync_doc_links(settings, a, "接 [[乙篇]]")
    sync_doc_links(settings, a, "不接了")
    with open_db(settings.db_path) as conn:
        assert repo.links_for_document(conn, a) == []


def test_sync_skips_unknown_titles_and_self_links(settings):
    """写了一半的 `[[还没写的笔记]]` 是常态，不是错误；自引用也不落边。"""
    a = _add_doc(settings, "甲篇")
    added = sync_doc_links(settings, a, "接 [[还没写的笔记]] 与 [[甲篇]]")
    assert added == 0


def test_sync_keeps_manual_edges(settings):
    """人工确认过的边（source='manual'）不被笔记重存顺手抹掉。"""
    a = _add_doc(settings, "甲篇")
    b = _add_doc(settings, "乙篇")
    with open_db(settings.db_path) as conn:
        repo.replace_doc_links(
            conn, a, [{"dst_doc_id": b, "relation": "manual", "source": "manual"}]
        )
        conn.commit()

    sync_doc_links(settings, a, "这次没写任何链接")

    with open_db(settings.db_path) as conn:
        links = repo.links_for_document(conn, a)
    assert [row["other_id"] for row in links] == [b]


def test_sync_on_missing_document_is_a_noop(settings):
    """文档刚被并发删掉：没有可挂的边，安静返回 0（不是抛）。"""
    assert sync_doc_links(settings, 99999, "接 [[随便]]") == 0
