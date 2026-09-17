"""笔记端点测试（M6 ①）：新建 / 编辑 / 读原文 + 三个"静默出错"坑的回归锁。

笔记 = `source_ref = "note:<key>"` 的普通文档（ADR-0021），因此删除/改名/移夹
复用 documents 端点，本文件只测笔记特有的行为。三条重点：
  1) force=True 绕开 sha 内容去重——同内容的两条笔记必须共存；
  2) 标题在分块前生效——只改标题也要让索引词空间跟着变（chunks.tokens）；
  3) 保存整段持有 ingest 锁——"保存 vs 删除"不许让删掉的笔记无标记复活。
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from mikasa.errors import ProviderError
from mikasa.ingest.service import IngestService
from mikasa.storage import repo
from mikasa.storage.db import open_db

BODY = """# 快速排序

分治思想：选基准、划分、递归。平均复杂度 O(n log n)。

```python
def quick_sort(a):
    return a if len(a) < 2 else quick_sort([x for x in a[1:] if x <= a[0]])
```

| 场景 | 复杂度 |
| --- | --- |
| 最好 | O(n log n) |
"""


def _create(c, title: str = "快排笔记", body: str = BODY, **extra):
    """新建笔记助手。"""
    return c.post("/api/notes", json={"title": title, "body": body, **extra})


def _rows(settings) -> list:
    """库里的文档行（断言"只应有一行"这类不变量的直接证据）。"""
    with open_db(settings.db_path) as conn:
        return repo.list_documents(conn)


def _uploads_files(settings) -> list[str]:
    uploads = settings.uploads_dir
    return sorted(p.name for p in uploads.iterdir() if p.is_file()) if uploads.is_dir() else []


def _web_tmp_leftovers(settings) -> list[str]:
    """web-tmp 下残留的独占子目录（入库尾链必须把它们收干净）。"""
    tmp = settings.data_dir / "web-tmp"
    return [p.name for p in tmp.iterdir()] if tmp.is_dir() else []


def _chunk_tokens(settings, doc_id: int) -> str:
    """该文档全部 chunk 的预分词拼成一个串（索引词空间的观测窗口）。

    断言的标记词要用**纯 ASCII**：jieba 会把中文词组切开（"桶排序" →
    "桶"+"排序"），拿中文串做子串断言会假失败。正文断言请用 _chunk_text。
    """
    with open_db(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT tokens FROM chunks WHERE document_id = ? ORDER BY seq", (doc_id,)
        ).fetchall()
    return " ".join(row["tokens"] for row in rows)


def _chunk_text(settings, doc_id: int) -> str:
    """该文档全部 chunk 的正文拼接（检索命中的实际内容）。"""
    with open_db(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT content FROM chunks WHERE document_id = ? ORDER BY seq", (doc_id,)
        ).fetchall()
    return "\n".join(row["content"] for row in rows)


# ---------------------------------------------------------------------------
# 新建
# ---------------------------------------------------------------------------


def test_create_note_roundtrip(client):
    """新建 → 标题是用户填的（不是"标题-<key>"）、带标记、副本按名可查。"""
    c, settings = client
    resp = _create(c, title="线性代数笔记")
    assert resp.status_code == 201, resp.text
    doc = resp.json()["document"]
    # 标题：文件主名是"标题 (note key)"这种内部名，绝不能被当成显示标题
    assert doc["title"] == "线性代数笔记"
    assert doc["source_ref"].startswith("note:")
    assert doc["chunk_count"] >= 1
    assert doc["ingest_status"] == "done"
    assert doc["folder_id"] is None
    # 脱敏口径与上传一致
    assert "file_path" not in doc and "file_sha256" not in doc

    with open_db(settings.db_path) as conn:
        row = repo.get_document(conn, doc["id"])
    copy_name = Path(row.file_path).name
    assert copy_name.startswith("线性代数笔记 (note ")
    assert copy_name.endswith(".md")
    assert copy_name in _uploads_files(settings)
    assert _web_tmp_leftovers(settings) == [], "入库尾链应把 web-tmp 独占子目录收干净"


def test_create_two_notes_with_identical_body_coexist(client):
    """同内容的两条笔记必须共存（force=True 绕开 sha 去重）。

    不 force 的话第二篇走 `_ingest_one` 的"同内容已在库 → skipped"分支：
    接口照样返回成功，但库里只有一行、树里少一篇——典型的静默丢数据。
    """
    c, settings = client
    first = _create(c, title="甲笔记")
    second = _create(c, title="乙笔记")
    assert first.status_code == 201
    assert second.status_code == 201, second.text
    assert first.json()["document"]["id"] != second.json()["document"]["id"]
    assert len(_rows(settings)) == 2
    assert len(_uploads_files(settings)) == 2


def test_create_note_title_sanitized_for_filename_only(client):
    """标题净化只作用于**文件名**：显示标题原样保留，文件名回退到"无标题"。"""
    c, settings = client
    resp = _create(c, title="???")
    assert resp.status_code == 201, resp.text
    doc = resp.json()["document"]
    assert doc["title"] == "???"  # 显示标题不因文件名净化而改写
    with open_db(settings.db_path) as conn:
        row = repo.get_document(conn, doc["id"])
    assert Path(row.file_path).name.startswith("无标题 (note ")


def test_create_note_blank_title_400(client):
    c, _ = client
    resp = _create(c, title="   ")
    assert resp.status_code == 400
    # 断到文案：只断状态码的话，任何别的 400 都能冒充这条用例通过
    assert "标题" in resp.json()["detail"]


def test_create_note_empty_body_400(client):
    """空 / 纯空白正文在**路由层**就被挡回（连临时文件都不写）。"""
    c, settings = client
    for body in ("", "   \n\n  ", "　　"):  # 含全角空格：bytes.strip() 认不出它
        resp = _create(c, body=body)
        assert resp.status_code == 400, f"{body!r} 应被拒: {resp.text}"
        assert "正文不能为空" in resp.json()["detail"]
    assert _rows(settings) == []
    assert _uploads_files(settings) == []


def test_create_note_code_only_body_400_with_note_flavoured_hint(client):
    """整篇围栏代码块 → 400，文案要说明"代码块不进检索"，且不留残渣。

    这条路与上面那条**不是同一条**：路由层的空白判定放行（正文字符非空），
    写盘 → ingest 解析出 0 段落才抛"文档为空"，全靠 `finally` 清干净临时目录。
    所以"无残渣"的断言挂在这里才有意义（空正文那条根本走不到写盘）。
    """
    c, settings = client
    resp = _create(c, body="```python\nprint(1)\n```\n")
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "代码块" in detail, detail
    assert _rows(settings) == []
    assert _uploads_files(settings) == []
    assert _web_tmp_leftovers(settings) == [], "入库失败也必须把 web-tmp 独占子目录收干净"


def test_create_note_into_folder(client):
    """保存到指定文件夹；文件夹不存在 → 404 且不留下半成品。"""
    c, settings = client
    folder = c.post("/api/kb-folders", json={"name": "需要的"}).json()["folder"]

    resp = _create(c, folder_id=folder["id"])
    assert resp.status_code == 201
    assert resp.json()["document"]["folder_id"] == folder["id"]

    bad = _create(c, title="另一篇", folder_id=folder["id"] + 999)
    assert bad.status_code == 404
    assert len(_rows(settings)) == 1, "文件夹校验失败不该留下已入库的笔记"


def test_create_note_title_length_boundary(client):
    c, _ = client
    assert _create(c, title="甲" * 120).status_code == 201
    assert _create(c, title="乙" * 121).status_code == 422  # 与 DocumentPatchIn 对齐


# ---------------------------------------------------------------------------
# 编辑
# ---------------------------------------------------------------------------


def test_update_note_body_in_place(client):
    """改正文：uploads 副本仍是同一个文件、库里仍只有一行、索引内容更新。"""
    c, settings = client
    doc = _create(c).json()["document"]
    copy_before = _uploads_files(settings)

    resp = c.put(
        f"/api/notes/{doc['id']}", json={"title": "快排笔记", "body": "新的正文：桶排序思路。"}
    )
    assert resp.status_code == 201, resp.text
    updated = resp.json()["document"]
    assert updated["source_ref"].startswith("note:")
    assert _uploads_files(settings) == copy_before, "编辑不该产生第二个副本文件"
    assert len(_rows(settings)) == 1, "编辑必须是原地更新，不能多出一篇"
    assert "桶排序" in _chunk_text(settings, updated["id"])


def test_update_note_title_only_rebuilds_index_text(client):
    """只改标题也要让索引词空间跟着变（分块前生效，不是入库后补写）。

    这条锁的是一个静默缺陷：分块用 doc_title 构造《标题》前缀，若标题在
    入库后才 UPDATE，BM25/向量里留的仍是旧名字——改完名搜不到新名字。
    用纯 ASCII 标记词，避免 jieba 把标题切碎导致断言不可靠。
    """
    c, settings = client
    doc = _create(c, title="旧名字").json()["document"]

    resp = c.put(f"/api/notes/{doc['id']}", json={"title": "ZetaMarker9", "body": BODY})
    assert resp.status_code == 201, resp.text
    updated = resp.json()["document"]
    assert updated["title"] == "ZetaMarker9"
    assert "ZetaMarker9" in _chunk_tokens(settings, updated["id"])


def test_update_note_unchanged_is_noop(client):
    """内容没变 → 200 短路，**不动库**（id 不变是最直接的证据）。

    没有这条短路，"打开编辑器随手点保存"= 删旧行插新行：id 变、重新嵌入、
    树里的选中态与已落库回答的引用一起失效。
    """
    c, settings = client
    doc = _create(c).json()["document"]
    resp = c.put(f"/api/notes/{doc['id']}", json={"title": "快排笔记", "body": BODY})
    assert resp.status_code == 200
    assert "没有变化" in resp.json()["message"]
    assert resp.json()["document"]["id"] == doc["id"]
    assert len(_rows(settings)) == 1


def test_update_note_crlf_normalized_so_save_is_stable(client):
    """CRLF 正文归一成 LF 后落盘：再提交同一份内容仍然命中午改动短路。

    不归一的话，同一份内容会因为换行风格不同算出不同 sha：每次"打开→保存"
    都白重嵌一次，还会把 id 换掉。
    """
    c, _ = client
    doc_id = _create(c).json()["document"]["id"]
    crlf = "第一行\r\n第二行\r\n"
    first = c.put(f"/api/notes/{doc_id}", json={"title": "快排笔记", "body": crlf})
    assert first.status_code == 201
    # **保存会重建行 → id 变了**，第二次必须用响应回传的新 id（前端同理）
    new_id = first.json()["document"]["id"]
    second = c.put(f"/api/notes/{new_id}", json={"title": "快排笔记", "body": crlf})
    assert second.status_code == 200
    assert "没有变化" in second.json()["message"]


def test_update_note_reverting_body_leaves_one_row_and_one_file(client):
    """正文改出去又改回来：仍然一行一文件（同名替换不产生孤儿副本）。"""
    c, settings = client
    doc_id = _create(c).json()["document"]["id"]
    mid = c.put(f"/api/notes/{doc_id}", json={"title": "快排笔记", "body": "临时内容"})
    assert mid.status_code == 201
    back = c.put(
        f"/api/notes/{mid.json()['document']['id']}",
        json={"title": "快排笔记", "body": BODY},
    )
    assert back.status_code == 201
    assert len(_rows(settings)) == 1
    assert len(_uploads_files(settings)) == 1
    # 用正文里的句子断言：`# 快速排序` 那行被 markdown loader 当结构性标题
    # 吃掉（只进 heading_path，不进 chunk 正文），属既有 loader 行为
    assert "分治思想" in _chunk_text(settings, back.json()["document"]["id"])


def test_update_and_get_reject_non_note_documents(client):
    """越权防线：普通上传件、论文导入件、不存在的 id 一律 404 且不改内容。"""
    c, settings = client
    uploaded = c.post(
        "/api/documents", files={"file": ("普通.md", "上传件正文，不该被笔记端点改写".encode())}
    ).json()["document"]
    paper = c.post(
        "/api/documents", files={"file": ("论文.md", "论文正文，同样不该被改写".encode())}
    ).json()["document"]
    with open_db(settings.db_path) as conn:
        repo.set_document_source_ref(conn, paper["id"], "arxiv:2401.12345")
        conn.commit()

    for doc_id in (uploaded["id"], paper["id"], 10**9):
        got = c.get(f"/api/notes/{doc_id}")
        assert got.status_code == 404, f"id={doc_id} 不该可读"
        put = c.put(f"/api/notes/{doc_id}", json={"title": "改名", "body": "改写"})
        assert put.status_code == 404, f"id={doc_id} 不该可写"

    titles = {d["title"] for d in c.get("/api/documents").json()["documents"]}
    assert "普通" in titles and "论文" in titles, "被拒的编辑不该留下任何改动"
    assert len(_rows(settings)) == 2


def test_update_note_long_title_422(client):
    c, _ = client
    doc = _create(c).json()["document"]
    resp = c.put(f"/api/notes/{doc['id']}", json={"title": "甲" * 121, "body": BODY})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 读原文
# ---------------------------------------------------------------------------


def test_get_note_body_roundtrip(client):
    """回填必须逐字符一致：Markdown 语法、emoji、末尾无换行都要原样回来。

    走 /content（拼 chunk）会丢掉 `#` 标题行与代码围栏——那样"打开→保存"
    就会静默重写用户的笔记。
    """
    c, _ = client
    body = (
        "# 标题行要保留\n\n段落带 emoji 🎯 与行内 `code`。\n\n"
        "```\n围栏也要在\n```\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n末尾没有换行"
    )
    doc = _create(c, title="语法保真", body=body).json()["document"]

    got = c.get(f"/api/notes/{doc['id']}")
    assert got.status_code == 200
    assert got.json()["body"] == body
    assert got.json()["document"]["id"] == doc["id"]

    # 对照：/content 是拼 chunk 的阅读正文，必然丢语法（证明两者不是一回事）
    content = c.get(f"/api/documents/{doc['id']}/content").json()["text"]
    assert "```" not in content


def test_get_note_missing_copy_404_not_500(client):
    """副本被手工删掉 → 404 且文案可解，不是 500。"""
    c, settings = client
    doc = _create(c).json()["document"]
    for name in _uploads_files(settings):
        (settings.uploads_dir / name).unlink()

    got = c.get(f"/api/notes/{doc['id']}")
    assert got.status_code == 404
    assert "副本" in got.json()["detail"]


# ---------------------------------------------------------------------------
# 与既有链路的交互
# ---------------------------------------------------------------------------


def test_delete_note_clears_row_copy_and_marker(client):
    c, settings = client
    doc = _create(c).json()["document"]
    assert c.delete(f"/api/documents/{doc['id']}").status_code == 200
    assert _rows(settings) == []
    assert _uploads_files(settings) == []
    with open_db(settings.db_path) as conn:
        assert repo.list_source_refs(conn) == set()


def test_patch_rename_and_move_keep_copy_name_and_marker(client):
    """改名/移夹只动组织属性：副本名与笔记标记都不受影响。"""
    c, settings = client
    doc = _create(c).json()["document"]
    folder = c.post("/api/kb-folders", json={"name": "随记"}).json()["folder"]
    copy_before = _uploads_files(settings)

    resp = c.patch(
        f"/api/documents/{doc['id']}", json={"title": "改名了", "folder_id": folder["id"]}
    )
    assert resp.status_code == 200
    assert c.get(f"/api/notes/{doc['id']}").status_code == 200, "改名后仍是笔记"
    assert _uploads_files(settings) == copy_before


def test_reindex_keeps_note_marker_title_and_folder(client):
    """reindex 全量重建：标记、改名、归属都要活下来（_KeptProps 的既有承诺）。"""
    c, settings = client
    doc = _create(c).json()["document"]
    folder = c.post("/api/kb-folders", json={"name": "随记"}).json()["folder"]
    c.patch(
        f"/api/documents/{doc['id']}", json={"title": "重导后仍叫这名", "folder_id": folder["id"]}
    )

    IngestService(settings).reindex()

    docs = c.get("/api/documents").json()["documents"]
    assert len(docs) == 1
    assert docs[0]["title"] == "重导后仍叫这名"
    assert docs[0]["folder_id"] == folder["id"]
    assert docs[0]["source_ref"].startswith("note:"), "reindex 后必须仍是笔记"


def test_note_is_immediately_searchable_and_update_reflects(client):
    """入库即被检索：新建后问得到，改完正文后旧关键词失效、新关键词命中。"""
    c, _ = client
    doc = _create(c, title="长颈鹿笔记", body="长颈鹿的血压调节机制主要靠颈部血管网缓冲。")
    doc_id = doc.json()["document"]["id"]

    ask = c.post("/api/ask", json={"question": "长颈鹿的血压调节机制是什么"})
    assert ask.status_code == 200
    answer = ask.json()["answer"]
    assert not answer["refused"]
    assert any(cite["document_title"] == "长颈鹿笔记" for cite in answer["citations"])

    updated = c.put(
        f"/api/notes/{doc_id}", json={"title": "河马笔记", "body": "河马的皮肤会分泌红色黏液。"}
    )
    assert updated.status_code == 201
    after = c.post("/api/ask", json={"question": "长颈鹿的血压调节机制是什么"}).json()["answer"]
    assert after["refused"], "旧内容已被替换，旧问题不该再命中"


def test_upload_identical_to_note_is_skipped_by_sha(client):
    """既有 sha 去重语义的现状锁定：上传与笔记同内容的文件 → 200 指向笔记。

    不是笔记端点引入的行为——跨文档内容去重是 `_ingest_one` 的老语义。
    """
    c, settings = client
    doc = _create(c, body="同一份内容会被内容哈希认出来。").json()["document"]
    resp = c.post(
        "/api/documents",
        files={"file": ("复制品.md", "同一份内容会被内容哈希认出来。".encode())},
    )
    assert resp.status_code == 200
    assert "已跳过重复导入" in resp.json()["message"]
    assert resp.json()["document"]["id"] == doc["id"]
    assert len(_rows(settings)) == 1


def test_concurrent_save_and_delete_never_revives_markerless(client):
    """保存与删除并发：不许 500，且**绝不允许出现无标记的孤儿行**。

    两者都持 ingest 锁 → 串行，两种顺序的终态不同（这正是断言不能写死"零行"的
    原因）：删除先到 → PUT 拿不到行而 404，剩 0 行；保存先到 → 同名替换换了行
    id，删除拿的是**旧 id** → 404，剩 1 行（仍带标记）。真正的不变量只有一条：
    不许出现 `source_ref` 为 None 的行——那正是保存跑在锁外时会发生的
    （ingest 照 tmp 重建副本并插一行普通文档，删掉的笔记以陌生文档身份回来）。
    并发缺陷是概率性的，所以只锁不变量、不锁顺序。
    """
    from concurrent.futures import ThreadPoolExecutor

    c, settings = client
    for _ in range(3):
        doc = _create(c).json()["document"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_save = pool.submit(
                c.put, f"/api/notes/{doc['id']}", json={"title": "快排笔记", "body": "并发改写"}
            )
            fut_del = pool.submit(c.delete, f"/api/documents/{doc['id']}")
            save, delete = fut_save.result(), fut_del.result()
        assert save.status_code in (200, 201, 404), save.text
        assert delete.status_code in (200, 404), delete.text

        docs = _rows(settings)
        assert all((d.source_ref or "").startswith("note:") for d in docs), (
            f"出现无标记的孤儿行（保存跑在锁外就会这样）：{[(d.id, d.source_ref) for d in docs]}"
        )


def test_note_key_is_unique_per_note(client):
    """同标题的两条笔记必须各自成篇（key 让副本名互不相同）。"""
    c, settings = client
    a = _create(c, title="同名笔记").json()["document"]
    b = _create(c, title="同名笔记").json()["document"]
    assert a["id"] != b["id"]
    assert a["source_ref"] != b["source_ref"]
    assert len(_uploads_files(settings)) == 2


def test_note_web_tmp_isolated_per_save(client):
    """**同标题**并发建笔记：既不互相踩临时文件，也不互相顶行。

    同标题才会真的竞争同一段代码（副本名只差 key、web-tmp 目录只差 uuid）：
    不同标题的话两条路径天然互不相干，测不出任何东西。
    """
    from concurrent.futures import ThreadPoolExecutor

    c, settings = client
    bodies = [f"并发正文 {uuid4().hex} 段落内容。" for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(_create, c, "同名笔记", bodies[i]) for i in range(2)]
        results = [f.result() for f in futs]
    for resp in results:
        assert resp.status_code == 201, resp.text
    ids = {r.json()["document"]["id"] for r in results}
    refs = {r.json()["document"]["source_ref"] for r in results}
    assert len(ids) == 2 and len(refs) == 2, f"同标题并发必须各自成篇：{ids} / {refs}"
    assert len(_rows(settings)) == 2
    assert _web_tmp_leftovers(settings) == []


# ---------------------------------------------------------------------------
# 审查回合补的缺口（2026-09-16：多路 agent 审查点名"零覆盖"的那些分支）
# ---------------------------------------------------------------------------


def test_create_note_key_collision_retries_and_spares_existing(client, monkeypatch):
    """key 撞车必须换一个重试——碰撞的后果是**顶掉别人的行**，不是报错。

    48 bit 随机撞车概率可忽略，但守卫本身必须真的有效（此前零覆盖）。
    这里把 uuid4 换成"第一次就给既有的 key"，逼它走重试分支。
    """
    import mikasa.web.routers.documents as docs_router

    c, settings = client
    first = _create(c, title="甲笔记").json()["document"]
    taken_key = first["source_ref"].removeprefix("note:")

    class _FakeUuid:
        def __init__(self, value: str) -> None:
            self.hex = value

    calls = {"n": 0}

    def fake_uuid4():
        calls["n"] += 1
        # 第 1 次：故意与既有笔记同 key（触发 continue）；之后给唯一值
        hex_str = taken_key if calls["n"] == 1 else f"{calls['n']:012x}"
        return _FakeUuid(hex_str)

    monkeypatch.setattr(docs_router, "uuid4", fake_uuid4)
    second = _create(c, title="乙笔记")
    assert second.status_code == 201, second.text

    with open_db(settings.db_path) as conn:
        original = repo.get_document(conn, first["id"])
    assert original is not None and original.source_ref == first["source_ref"], "既有笔记被顶掉了"
    assert len(_rows(settings)) == 2
    assert second.json()["document"]["source_ref"] != first["source_ref"]


def test_update_note_empty_body_400(client):
    """PUT 的空正文也走 400（与 POST 是两条不同的代码路径，各自都要有锁）。"""
    c, _ = client
    doc_id = _create(c).json()["document"]["id"]
    resp = c.put(f"/api/notes/{doc_id}", json={"title": "快排笔记", "body": "   \n "})
    assert resp.status_code == 400
    assert "正文不能为空" in resp.json()["detail"]


def test_note_body_over_limit_422_in_chinese(client):
    """正文超上限：422 且文案是中文（Pydantic 默认英文会原样弹到界面上）。"""
    from mikasa.web.schemas import NOTE_BODY_MAX

    c, _ = client
    resp = _create(c, body="甲" * (NOTE_BODY_MAX + 1))
    assert resp.status_code == 422
    text = resp.text
    assert "笔记正文过长" in text
    assert "String should have at most" not in text


def test_update_note_repairs_stale_file_path_in_place(client):
    """历史脏 file_path：编辑要**修好它并原地更新**，不能插出第二行。

    库里的 file_path 可能是废弃的旧项目路径（本项目真发生过：23 行里 21 行
    指向改名前的目录）。而 ingest 的"同名替换"判定用**全路径**比对——脏路径
    会被判成"这是个新文件"，于是插出第二行：多一篇、丢掉笔记标记、索引里还是
    旧内容，用户还会看到文件夹归属"掉回根级"（2026-09-16 三路审查各自复现）。
    """
    c, settings = client
    folder = c.post("/api/kb-folders", json={"name": "随记"}).json()["folder"]
    doc = _create(c, folder_id=folder["id"]).json()["document"]

    with open_db(settings.db_path) as conn:
        row = repo.get_document(conn, doc["id"])
        stale = f"D:\\Code\\MyProject1\\data\\uploads\\{Path(row.file_path).name}"
        repo.set_document_file_path(conn, doc["id"], stale)
        conn.commit()

    resp = c.put(f"/api/notes/{doc['id']}", json={"title": "快排笔记", "body": "改过的正文。"})
    assert resp.status_code == 201, resp.text
    updated = resp.json()["document"]
    assert updated["source_ref"].startswith("note:"), "编辑后必须仍是笔记"
    assert updated["folder_id"] == folder["id"], "文件夹归属不能掉"

    rows = _rows(settings)
    assert len(rows) == 1, f"脏路径下编辑插出了第二行：{[(r.id, r.source_ref) for r in rows]}"
    assert Path(rows[0].file_path).parent == settings.uploads_dir, "脏路径必须被修回 uploads"


def test_update_note_self_heals_when_copy_was_deleted(client):
    """副本被手工删掉：编辑应当**自愈**（重建副本、仍是一行、标记保留）。"""
    c, settings = client
    doc_id = _create(c).json()["document"]["id"]
    for name in _uploads_files(settings):
        (settings.uploads_dir / name).unlink()

    resp = c.put(f"/api/notes/{doc_id}", json={"title": "快排笔记", "body": "自愈后的正文。"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["document"]["source_ref"].startswith("note:")
    assert len(_rows(settings)) == 1
    assert len(_uploads_files(settings)) == 1


def test_update_note_keeps_folder_across_edits(client):
    """ "移进文件夹再编辑"的组合：归属必须留住（重灌不拆家最容易坏的地方）。"""
    c, _ = client
    folder = c.post("/api/kb-folders", json={"name": "论文"}).json()["folder"]
    doc_id = _create(c).json()["document"]["id"]
    assert c.patch(f"/api/documents/{doc_id}", json={"folder_id": folder["id"]}).status_code == 200

    resp = c.put(f"/api/notes/{doc_id}", json={"title": "快排笔记", "body": "编辑一次。"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["document"]["folder_id"] == folder["id"]


def test_get_note_unreadable_copy_404_not_500(client, monkeypatch):
    """副本读不出来（被别的程序占着）→ 404 而不是 500。"""
    c, _ = client
    doc_id = _create(c).json()["document"]["id"]

    def boom(self):
        raise PermissionError("模拟文件被占用")

    monkeypatch.setattr(Path, "read_bytes", boom)
    resp = c.get(f"/api/notes/{doc_id}")
    assert resp.status_code == 404
    assert "读不到" in resp.json()["detail"]


def test_note_filename_truncated_and_readable(client):
    """满上限的标题：文件名主名截到 _NOTE_STEM_MAX、后缀仍是 .md（loader 靠它分发）。"""
    c, settings = client
    doc = _create(c, title="长" * 120).json()["document"]  # 120 = 标题上限
    with open_db(settings.db_path) as conn:
        row = repo.get_document(conn, doc["id"])
    name = Path(row.file_path).name
    assert name.endswith(".md")
    assert len(name) <= 90, name  # 60 字主名 + " (note <key12>).md"
    assert doc["title"] == "长" * 120  # 显示标题不截断


def test_update_note_survives_readonly_copy(client):
    """旧副本只读/被占用时保存：**必须报成功**——内容其实已经入库了。

    原先"旧副本退役"（`displaced.unlink()`）裸奔在 try 之外：只读或 Windows
    文件锁会让 unlink 抛错，整条请求被翻成 400「入库失败」，而新内容早已落库
    ——用户看到失败就会重试，于是多出一篇（2026-09-16 对抗性实测）。
    """
    import os
    import stat as stat_mod

    c, settings = client
    doc_id = _create(c).json()["document"]["id"]
    copy = settings.uploads_dir / _uploads_files(settings)[0]
    os.chmod(copy, stat_mod.S_IREAD)  # 只读：Windows 上 unlink 会失败
    try:
        resp = c.put(f"/api/notes/{doc_id}", json={"title": "快排笔记", "body": "只读下保存。"})
        assert resp.status_code == 201, resp.text
        assert resp.json()["document"]["source_ref"].startswith("note:")
        assert len(_rows(settings)) == 1
        assert "只读下保存" in _chunk_text(settings, resp.json()["document"]["id"])
        # 删不掉的旧副本会以 .replacing 留在 uploads：是垃圾，但不会被 reindex 扫到
        if os.name == "nt":  # POSIX 下删只读文件不失败（看目录权限）
            assert any(n.endswith(".replacing") for n in _uploads_files(settings))
    finally:
        os.chmod(copy, stat_mod.S_IWRITE)


def test_update_note_keeps_marker_when_rows_vanish_midflight(client, monkeypatch):
    """别的进程 reindex 在保存中途清空全表：标记必须活下来。

    进程内锁管不到别的进程——`mikasa serve` 跑着时另一个终端执行
    `mikasa ingest --reindex`（换嵌入模型后的标准动作）正好卡在"读行 → 入库"
    之间，入库就会走"新文件"分支。原先 update_note 不传 source_ref、只指望
    同名替换从旧行继承，那一刻旧行已经没了 → 新行落成普通文档，笔记**永久**
    降级（2026-09-16 对抗性实测命中）。这里在写临时文件那一步清空全表，
    确定性地复现那个窗口。
    """
    import mikasa.web.routers.documents as docs_router

    c, settings = client
    doc_id = _create(c, title="被抢的笔记").json()["document"]["id"]
    real_write = docs_router._write_note_tmp

    def wipe_then_write(settings_, safe_name, payload):
        with open_db(settings_.db_path) as conn:
            conn.execute("DELETE FROM documents")  # 模拟另一进程 reindex 清表
            conn.commit()
        return real_write(settings_, safe_name, payload)

    monkeypatch.setattr(docs_router, "_write_note_tmp", wipe_then_write)
    resp = c.put(f"/api/notes/{doc_id}", json={"title": "被抢的笔记", "body": "改写后的正文。"})
    assert resp.status_code == 201, resp.text
    new_id = resp.json()["document"]["id"]
    assert resp.json()["document"]["source_ref"].startswith("note:"), "笔记被降级成普通文档了"
    assert c.get(f"/api/notes/{new_id}").status_code == 200, "标记丢了就再也编辑不了"


def test_delete_note_with_readonly_copy_still_200(client):
    """副本只读时删除笔记：行已经删了就该回 200，不能让 unlink 失败翻成 500。

    500 会误导（用户以为没删掉），而盘上留下的副本会被下一次 reindex 当新文档
    扫回来——所以服务端日志里那条警告要写清怎么收拾（这里只锁状态码）。
    """
    import os
    import stat as stat_mod

    c, settings = client
    doc_id = _create(c).json()["document"]["id"]
    copy = settings.uploads_dir / _uploads_files(settings)[0]
    os.chmod(copy, stat_mod.S_IREAD)
    try:
        assert c.delete(f"/api/documents/{doc_id}").status_code == 200
        assert _rows(settings) == []
    finally:
        os.chmod(copy, stat_mod.S_IWRITE)


def test_create_note_title_with_lone_surrogate_422_not_500(client):
    """标题含孤立代理项：422 而不是 500。

    422 的响应体会**回显出错字段的原值**，而原值里的孤立代理项在编码成 UTF-8
    时会炸（`'\\ud800'.encode('utf-8')` 抛 UnicodeEncodeError）——FastAPI 默认
    处理器于是把"客户端送了个坏字符串"变成了"服务器内部错误"。已在 app 层
    洗掉代理项（`_drop_surrogates`）。
    """
    import json as json_mod

    c, _ = client
    raw = json_mod.dumps({"title": "\ud800", "body": "正文"}).encode("utf-8")
    resp = c.post("/api/notes", content=raw, headers={"content-type": "application/json"})
    assert resp.status_code == 422, resp.text
    assert isinstance(resp.json().get("detail"), list)  # 仍是标准 422 形状


def test_note_stem_truncation_keeps_visible_characters(client):
    """全是不可见字符 + 尾部可见字符的标题：截断后不允许变成"看不见的文件名"。

    `_note_stem` 早期版本先判可见性再截断，于是 60 个零宽字符 + "abc" 会被
    "abc" 救活、截断又把 "abc" 切掉 → 产出 60 个看不见的字符，恰好是兜底常量
    要防的那种名字（2026-09-16 审查实测）。
    """
    c, settings = client
    title = "​" * 65 + "abc"
    doc = _create(c, title=title).json()["document"]
    with open_db(settings.db_path) as conn:
        row = repo.get_document(conn, doc["id"])
    name = Path(row.file_path).name
    assert name.startswith("无标题"), f"截断后应回退兜底名：{name!r}"


def test_note_embedding_provider_failure_502(client, monkeypatch):
    """笔记入库撞上嵌入服务失败 → 502（与上传/论文导入同一条尾链，2026-09-17）。"""
    c, settings = client

    def boom(self, chunks, doc_title):
        raise ProviderError("模拟嵌入服务 429")

    monkeypatch.setattr(IngestService, "_embed_chunks", boom)
    resp = _create(c)
    assert resp.status_code == 502, resp.text
    assert "入库失败" in resp.json()["detail"]
    assert _rows(settings) == [], "失败不留半成品行"
    assert _uploads_files(settings) == [], "失败不留 uploads 副本"
    assert _web_tmp_leftovers(settings) == [], "失败也要把 web-tmp 独占子目录收干净"
