"""文档端点测试：上传→列表→删除全链路 + 安全三件套 + invalidate 生效。

MockLLM 离线链路不含真实嵌入，上传文档即入库分块（与 ingest 测试同
语料）；"删除后提问立即反映新语料"断言 invalidate 真正生效。
"""

from __future__ import annotations

from mikasa.storage.db import open_db
from tests.unit.web.conftest import NOTE


def _upload(c, name: str, content: str = NOTE):
    """multipart 上传助手（TestClient files 参数即 multipart 编码）。"""
    return c.post("/api/documents", files={"file": (name, content.encode("utf-8"))})


def test_upload_list_delete_roundtrip(client):
    c, settings = client
    assert c.get("/api/documents").json()["documents"] == []

    resp = _upload(c, "机器学习笔记.md")
    assert resp.status_code == 201
    body = resp.json()
    doc = body["document"]
    assert body["message"].startswith("入库成功")
    assert doc["title"] == "机器学习笔记"
    assert doc["chunk_count"] >= 1
    assert doc["ingest_status"] == "done"
    # 脱敏：本地路径与内容哈希不出现
    assert "file_path" not in doc and "file_sha256" not in doc
    assert "uploads" not in str(body)

    docs = c.get("/api/documents").json()["documents"]
    assert [d["id"] for d in docs] == [doc["id"]]

    deleted = c.delete(f"/api/documents/{doc['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] == doc["id"]
    assert c.get("/api/documents").json()["documents"] == []


def test_upload_same_content_skips_duplicate(client):
    """同内容再次上传：200 幂等跳过（HTTP 语义：资源已存在）。"""
    c, _ = client
    first = _upload(c, "甲笔记.md")
    second = _upload(c, "乙笔记.md")  # 不同名、同内容
    assert first.status_code == 201
    assert second.status_code == 200
    assert "已跳过重复导入" in second.json()["message"]
    assert second.json()["document"]["id"] == first.json()["document"]["id"]
    assert len(c.get("/api/documents").json()["documents"]) == 1


def test_upload_chinese_filename_preserved(client):
    """中文文件名保留（产品语义：用户中文资料名不被英文化）。

    注意 title 取自文件内容首标题而非文件名——文件名保留看 uploads
    副本名（sanitize 的输出）。
    """
    c, settings = client
    resp = _upload(c, "  深度学习 笔记.md  ")
    assert resp.status_code == 201
    assert (settings.uploads_dir / "深度学习 笔记.md").is_file()


def test_upload_path_traversal_sanitized(client):
    """路径穿越文件名被净化：../../evil.md → evil.md，且不在目录外落盘。"""
    c, settings = client
    resp = _upload(c, "../../evil.md")
    assert resp.status_code == 201
    assert not (settings.data_dir.parent / "evil.md").exists()
    assert (settings.uploads_dir / "evil.md").is_file()


def test_upload_unsupported_type_415(client):
    c, _ = client
    resp = _upload(c, "恶意.exe")
    assert resp.status_code == 415
    assert "不支持的文件类型" in resp.json()["detail"]


def test_upload_oversize_413(client, offline_settings):
    """超过上限：413 且不残留 web-tmp 临时文件。"""
    offline_settings = offline_settings.model_copy(
        update={"web": offline_settings.web.model_copy(update={"upload_max_mb": 1})}
    )
    from fastapi.testclient import TestClient

    from mikasa.web.app import create_app

    with TestClient(create_app(offline_settings)) as c:
        resp = _upload(c, "大文件.md", content="# x\n\n" + "字" * (2 * 1024 * 1024))
        assert resp.status_code == 413
        assert "大小上限" in resp.json()["detail"]
        tmp_dir = offline_settings.data_dir / "web-tmp"
        assert not tmp_dir.exists() or not any(tmp_dir.iterdir())


def test_upload_empty_file_400(client):
    c, _ = client
    resp = _upload(c, "空.md", content="")
    assert resp.status_code == 400


def test_delete_missing_document_404(client):
    c, _ = client
    resp = c.delete("/api/documents/9999")
    assert resp.status_code == 404
    assert "文档不存在" in resp.json()["detail"]


def test_delete_removes_uploads_copy(client):
    """删除后 uploads 副本同步移除（否则 reindex 会复活它）。"""
    c, settings = client
    doc_id = _upload(c, "待删除.md").json()["document"]["id"]
    uploads = list((settings.uploads_dir).glob("*.md"))
    assert any(f.name == "待删除.md" for f in uploads)
    c.delete(f"/api/documents/{doc_id}")
    assert not any(f.name == "待删除.md" for f in settings.uploads_dir.glob("*"))


def test_delete_then_ask_reflects_corpus_change(client):
    """删除文档后提问必须立即反映新语料（invalidate 生效的端到端断言）。

    未调 invalidate 时行数指纹仍认为语料未变、快照含旧块——提问会命中
    已被删除的文档内容。这里先问（命中），删后断言提问行为随语料变化。
    """
    c, _ = client
    doc_id = _upload(c, "待删笔记.md").json()["document"]["id"]
    resp = c.post("/api/ask", json={"question": "L2 正则化为什么能防止过拟合？"})
    assert resp.status_code == 200 and not resp.json()["answer"]["refused"]

    c.delete(f"/api/documents/{doc_id}")
    # 语料空了（唯一文档已删）：再次提问应报"知识库为空"而非命中旧快照
    resp = c.post("/api/ask", json={"question": "L2 正则化为什么能防止过拟合？"})
    assert resp.status_code == 400
    assert "知识库为空" in resp.json()["error"]["message"]


# ---------------------------------------------------------------------------
# v3：语料文件夹树（/api/kb-folders）+ 文档移夹/改名（PATCH /api/documents）
# ---------------------------------------------------------------------------


def _mk_folder(c, name: str, parent_id=None):
    """POST 新建语料文件夹，返回响应体 folder dict。"""
    body = {"name": name}
    if parent_id is not None:
        body["parent_id"] = parent_id
    resp = c.post("/api/kb-folders", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["folder"]


def test_kb_folder_tree_via_http(client):
    """HTTP 全链：建根/子夹 → 平铺列表 → 改名 → 移动（含移回根）→ 删空夹。"""
    c, _ = client
    assert c.get("/api/kb-folders").json()["folders"] == []

    root = _mk_folder(c, "论文")
    child = _mk_folder(c, "Transformer", parent_id=root["id"])
    rows = c.get("/api/kb-folders").json()["folders"]
    assert [r["id"] for r in rows] == [root["id"], child["id"]]
    assert rows[1]["parent_id"] == root["id"] and "created_at" in rows[0]

    # 改名
    resp = c.patch(f"/api/kb-folders/{child['id']}", json={"name": "Attention"})
    assert resp.status_code == 200
    assert resp.json()["folder"]["name"] == "Attention"
    # 移回根（显式 null）
    resp = c.patch(f"/api/kb-folders/{child['id']}", json={"parent_id": None})
    assert resp.status_code == 200
    assert resp.json()["folder"]["parent_id"] is None
    # 再移回 root 下（只带 parent_id、不带 name：字段互不干扰）
    resp = c.patch(f"/api/kb-folders/{child['id']}", json={"parent_id": root["id"]})
    assert resp.json()["folder"]["parent_id"] == root["id"]
    assert resp.json()["folder"]["name"] == "Attention"

    # 空夹可删；响应带 name
    resp = c.delete(f"/api/kb-folders/{child['id']}")
    assert resp.status_code == 200 and resp.json()["deleted"] == child["id"]
    assert c.get("/api/kb-folders").json()["folders"] == [rows[0]]  # 只剩 root


def test_kb_folder_name_blank_400_and_unknown_parent_404(client):
    c, _ = client
    assert c.post("/api/kb-folders", json={"name": "   "}).status_code == 400
    resp = c.post("/api/kb-folders", json={"name": "夹", "parent_id": 9999})
    assert resp.status_code == 404
    assert "父文件夹不存在" in resp.json()["detail"]
    assert c.get("/api/kb-folders").json()["folders"] == []  # 失败不留行


def test_kb_folder_move_into_own_subtree_409(client):
    """防环：把根夹移进自己子夹 → 409（判别集 = 自身 ∪ 全部后代）。"""
    c, _ = client
    root = _mk_folder(c, "根")
    child = _mk_folder(c, "子", parent_id=root["id"])
    resp = c.patch(f"/api/kb-folders/{root['id']}", json={"parent_id": child["id"]})
    assert resp.status_code == 409
    assert "移入自身或其子文件夹" in resp.json()["detail"]
    # 移到不存在的父 → 404
    resp = c.patch(f"/api/kb-folders/{child['id']}", json={"parent_id": 8888})
    assert resp.status_code == 404
    # 树未被破坏：child 还在 root 下
    rows = {r["id"]: r for r in c.get("/api/kb-folders").json()["folders"]}
    assert rows[child["id"]]["parent_id"] == root["id"]
    assert rows[root["id"]]["parent_id"] is None


def test_kb_folder_delete_nonempty_409_with_counts(client):
    """非空文件夹删除 → 409，文案带 子文件夹/文档 计数；清空后可删。"""
    c, _ = client
    root = _mk_folder(c, "根")
    _mk_folder(c, "子", parent_id=root["id"])
    doc_id = _upload(c, "夹内文档.md").json()["document"]["id"]
    c.patch(f"/api/documents/{doc_id}", json={"folder_id": root["id"]})

    resp = c.delete(f"/api/kb-folders/{root['id']}")
    assert resp.status_code == 409
    assert "1 个子文件夹、1 篇文档" in resp.json()["detail"]
    assert c.get("/api/documents").json()["documents"]  # 文档未被连坐

    # 移出子夹与文档后 → 空夹删除成功
    sub = [r for r in c.get("/api/kb-folders").json()["folders"] if r["name"] == "子"][0]
    c.patch(f"/api/kb-folders/{sub['id']}", json={"parent_id": None})
    assert c.delete(f"/api/kb-folders/{sub['id']}").status_code == 200
    c.patch(f"/api/documents/{doc_id}", json={"folder_id": None})
    assert c.delete(f"/api/kb-folders/{root['id']}").status_code == 200
    assert c.get("/api/kb-folders").json()["folders"] == []


def test_patch_document_title_and_folder(client):
    """PATCH /api/documents：改名与移夹同请求可叠加；列表与详情都带 folder_id。"""
    c, _ = client
    doc_id = _upload(c, "原标题.md").json()["document"]["id"]
    folder = _mk_folder(c, "参考资料")

    # 改名 + 移夹一起发
    resp = c.patch(f"/api/documents/{doc_id}", json={"title": "新标题", "folder_id": folder["id"]})
    assert resp.status_code == 200
    doc = resp.json()["document"]
    assert doc["title"] == "新标题"
    assert doc["folder_id"] == folder["id"]
    # 列表同步反映（前端树刷新数据源）
    listed = c.get("/api/documents").json()["documents"][0]
    assert listed["title"] == "新标题" and listed["folder_id"] == folder["id"]

    # 移回根（显式 null）
    resp = c.patch(f"/api/documents/{doc_id}", json={"folder_id": None})
    assert resp.json()["document"]["folder_id"] is None
    # 空 PATCH：200 空操作，行不变
    resp = c.patch(f"/api/documents/{doc_id}", json={})
    assert resp.status_code == 200 and resp.json()["document"]["title"] == "新标题"


def test_patch_document_errors(client):
    """文档 PATCH 错误面：不存在 404、空标题 400、未知文件夹 404。"""
    c, _ = client
    # 标题取自内容首标题（本语料为"机器学习笔记"），以回执为准做不落痕断言
    uploaded = _upload(c, "样本.md").json()["document"]
    doc_id, orig_title = uploaded["id"], uploaded["title"]
    assert c.patch("/api/documents/9999", json={"title": "改"}).status_code == 404

    resp = c.patch(f"/api/documents/{doc_id}", json={"title": "   "})
    assert resp.status_code == 400
    assert "标题不能为空" in resp.json()["detail"]

    resp = c.patch(f"/api/documents/{doc_id}", json={"folder_id": 7777})
    assert resp.status_code == 404
    assert "文件夹不存在" in resp.json()["detail"]
    # 失败的请求不落痕：标题与归属都没被前面的错误请求污染
    row = c.get("/api/documents").json()["documents"][0]
    assert row["title"] == orig_title and row["folder_id"] is None


# ---------------------------------------------------------------------------
# 阅读视图（2026-09-10）：/content 正文拼接 · /file 原文件 · /api/chunks 定位
# ---------------------------------------------------------------------------


def test_content_returns_stitched_text_and_spans(client):
    """正文端点：拼好的全文 + 各块偏移，且不含本地路径/哈希。"""
    c, _ = client
    doc = _upload(c, "机器学习笔记.md").json()["document"]

    body = c.get(f"/api/documents/{doc['id']}/content").json()
    assert body["document"]["id"] == doc["id"]
    assert "L2 正则化在损失中" in body["text"]
    assert body["chunks"], "至少一块"
    assert body["page_max"] is None  # md 无页码

    # 偏移不变量（前端按 start/end 切全文的唯一依据）
    spans = body["chunks"]
    assert spans[0]["start"] == 0
    assert spans[-1]["end"] == len(body["text"])
    for a, b in zip(spans, spans[1:], strict=False):
        assert a["end"] == b["start"]
    assert all(s["heading_path"] for s in spans)  # md 有标题路径

    # 脱敏纪律不因开口而松：本地路径与哈希永不进响应体
    assert "file_path" not in str(body)
    assert "file_sha256" not in str(body)


def test_content_missing_doc_404(client):
    c, _ = client
    resp = c.get("/api/documents/9999/content")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "文档不存在"


def test_content_pending_doc_returns_empty_chunks(client):
    """pending 文档 → 200 + 空 chunks（前端给引导，不新增错误分支）。"""
    c, settings = client
    with open_db(settings.db_path) as conn:
        cur = conn.execute(
            """INSERT INTO documents (title, file_path, file_type, file_sha256)
               VALUES ('未入库', 'x.md', 'md', 'sha-x')"""
        )
        doc_id = int(cur.lastrowid)
        conn.commit()

    resp = c.get(f"/api/documents/{doc_id}/content")
    assert resp.status_code == 200
    body = resp.json()
    assert body["chunks"] == [] and body["text"] == ""
    assert body["document"]["ingest_status"] == "pending"


def test_file_serves_inline_bytes_with_range_support(client):
    """原文件端点：inline（不是 attachment，否则 Chrome 不内嵌渲染）+ Range。"""
    c, _ = client
    doc = _upload(c, "机器学习笔记.md").json()["document"]

    resp = c.get(f"/api/documents/{doc['id']}/file")
    assert resp.status_code == 200
    assert resp.content.decode("utf-8") == NOTE  # 原文件内容
    assert resp.headers["content-type"].startswith("text/plain")
    assert "attachment" not in resp.headers.get("content-disposition", "")
    assert resp.headers.get("accept-ranges") == "bytes"
    assert resp.headers.get("x-content-type-options") == "nosniff"

    # Range：浏览器 PDF 阅读器能拖动进度条的前提
    part = c.get(f"/api/documents/{doc['id']}/file", headers={"Range": "bytes=0-9"})
    assert part.status_code == 206
    assert len(part.content) == 10
    assert part.headers["content-range"].startswith("bytes 0-9/")


def test_file_missing_doc_404(client):
    c, _ = client
    assert c.get("/api/documents/9999/file").status_code == 404


def test_file_missing_on_disk_404(client):
    """库里有行但 uploads 副本被删 → 可读 404（文本视图仍可用）。"""
    c, settings = client
    doc = _upload(c, "机器学习笔记.md").json()["document"]
    (settings.uploads_dir / "机器学习笔记.md").unlink()

    resp = c.get(f"/api/documents/{doc['id']}/file")
    assert resp.status_code == 404
    assert "原文件不可用" in resp.json()["detail"]


def test_file_path_traversal_is_neutralized(client):
    """file_path 是库里的不可信历史值：穿越路径必须被解析限死在 uploads 内。

    构造上把"合法文件"也拿掉（删 uploads 副本），这样若解析真被穿越带出
    目录就会返回 200 + secret 内容——测试才有区分度。
    """
    c, settings = client
    doc = _upload(c, "机器学习笔记.md").json()["document"]
    (settings.uploads_dir / "机器学习笔记.md").unlink()  # 合法文件不存在
    secret = settings.data_dir.parent / "secret.txt"
    secret.write_text("TOP-SECRET-CONTENT", encoding="utf-8")

    with open_db(settings.db_path) as conn:
        conn.execute(
            "UPDATE documents SET file_path = ? WHERE id = ?",
            (r"..\..\secret.txt", doc["id"]),
        )
        conn.commit()

    resp = c.get(f"/api/documents/{doc['id']}/file")
    assert resp.status_code == 404
    assert b"TOP-SECRET-CONTENT" not in resp.content


def test_file_title_fallback_when_path_is_stale(client):
    """复刻真实脏数据（实测 21/23 行指向旧项目路径）→ 按标题回退命中。

    doc 70 的真实形态：file_path 的 basename 对不上，磁盘文件按**文档标题**
    命名，只能靠第二跳找回。
    """
    c, settings = client
    doc = _upload(c, "原始名.md").json()["document"]
    title = doc["title"]
    # 磁盘文件按标题命名（doc 70 的形状），file_path 指向已废弃的旧项目
    (settings.uploads_dir / "原始名.md").rename(settings.uploads_dir / f"{title}.md")
    with open_db(settings.db_path) as conn:
        conn.execute(
            "UPDATE documents SET file_path = ? WHERE id = ?",
            (r"D:\Code\MyProject1\data\uploads\论文.pdf", doc["id"]),
        )
        conn.commit()

    resp = c.get(f"/api/documents/{doc['id']}/file")
    assert resp.status_code == 200
    assert resp.content.decode("utf-8") == NOTE


def test_file_rejects_content_mismatch(client):
    """候选文件内容与入库哈希不符 → 404 不一致（绝不把错文件发给用户）。"""
    c, settings = client
    doc = _upload(c, "机器学习笔记.md").json()["document"]
    title = doc["title"]
    path = settings.uploads_dir / "机器学习笔记.md"
    path.rename(settings.uploads_dir / f"{title}.md")  # 只剩标题命名的候选
    (settings.uploads_dir / f"{title}.md").write_text("被替换掉的内容", encoding="utf-8")
    with open_db(settings.db_path) as conn:
        conn.execute(
            "UPDATE documents SET file_path = ? WHERE id = ?",
            (r"D:\old\uploads\论文.pdf", doc["id"]),
        )
        conn.commit()

    resp = c.get(f"/api/documents/{doc['id']}/file")
    assert resp.status_code == 404
    assert "不一致" in resp.json()["detail"]


def test_locate_chunk_maps_to_document(client):
    """引用跳转那一跳：chunk_id → 所属文档（Citation 不带 document_id）。"""
    c, _ = client
    doc = _upload(c, "机器学习笔记.md").json()["document"]
    chunk_id = c.get(f"/api/documents/{doc['id']}/content").json()["chunks"][0]["chunk_id"]

    body = c.get(f"/api/chunks/{chunk_id}").json()
    assert body["document_id"] == doc["id"]
    assert body["document_title"] == doc["title"]
    assert body["seq"] == 0

    missing = c.get("/api/chunks/999999")
    assert missing.status_code == 404
    assert "原文片段不存在" in missing.json()["detail"]


def test_content_reports_real_pdf_page_count(client):
    """跳页上界 = **原件**页数而非正文页数（doc 71 实测 189 vs 301）。

    page_max 来自 chunks 的页码，参考文献区被剔除后会明显小于原文件；
    原件视图既然给的是原件，上界就该按原件算——故单独给 file_pages。
    """
    import pymupdf

    c, _ = client
    pdf = pymupdf.open()
    for i in range(3):
        page = pdf.new_page()
        page.insert_text((72, 96), f"Page {i + 1}: L2 regularization prevents overfitting.")
    resp = c.post(
        "/api/documents",
        files={"file": ("论文.pdf", pdf.tobytes(), "application/pdf")},
    )
    assert resp.status_code == 201, resp.text
    doc_id = resp.json()["document"]["id"]

    body = c.get(f"/api/documents/{doc_id}/content").json()
    assert body["file_pages"] == 3
    # 正文页码与原件页数是两个量，分别给出
    assert body["page_max"] is not None
    # 非 PDF 没有原件页数这回事
    md = _upload(c, "笔记.md").json()["document"]
    assert c.get(f"/api/documents/{md['id']}/content").json()["file_pages"] is None


def _make_pdf_bytes(pages: int = 3):
    """生成一个 n 页、每页一句可定位正文的 PDF（与既有页数测试同法）。"""
    import pymupdf

    pdf = pymupdf.open()
    for i in range(1, pages + 1):
        page = pdf.new_page()
        page.insert_text((72, 96), f"Page {i}: L2 regularization prevents overfitting in models.")
    return pdf.tobytes()


def _upload_pdf(c, name: str = "论文.pdf"):
    resp = c.post("/api/documents", files={"file": (name, _make_pdf_bytes(), "application/pdf")})
    assert resp.status_code == 201, resp.text
    return resp.json()["document"]


def test_page_image_renders_pdf_page(client):
    """页面图端点：把某一页渲染成 PNG，确定性结果给私有缓存。"""
    c, _ = client
    doc = _upload_pdf(c)

    resp = c.get(f"/api/documents/{doc['id']}/page/1.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic
    assert "max-age" in resp.headers.get("cache-control", "")
    assert resp.headers.get("x-content-type-options") == "nosniff"
    # 第二页是另一张图
    assert c.get(f"/api/documents/{doc['id']}/page/2.png").content != resp.content


def test_page_image_bounds_and_type(client):
    c, _ = client
    doc = _upload_pdf(c)
    assert c.get(f"/api/documents/{doc['id']}/page/0.png").status_code == 404
    assert c.get(f"/api/documents/{doc['id']}/page/999.png").status_code == 404
    assert c.get("/api/documents/9999/page/1.png").status_code == 404
    md = _upload(c, "笔记.md").json()["document"]
    assert c.get(f"/api/documents/{md['id']}/page/1.png").status_code == 400


def test_locate_chunk_returns_normalized_rects(client):
    """引用高亮的坐标来源：归一化到 0~1、按行分组。"""
    c, _ = client
    doc = _upload_pdf(c)
    chunks = c.get(f"/api/documents/{doc['id']}/content").json()["chunks"]
    chunk_id = chunks[0]["chunk_id"]

    body = c.get(f"/api/documents/{doc['id']}/locate/{chunk_id}").json()
    assert body["page"] == 1
    assert body["matched"] >= 6
    assert body["rects"], "应该定位到正文行"
    for x0, y0, x1, y1 in body["rects"]:
        assert 0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0


def test_locate_degrades_gracefully(client):
    """定位不到不是错误——块可能跨页或被清洗得差别太大，返回空 rects 即可。"""
    c, _ = client
    doc = _upload_pdf(c)
    md = _upload(c, "笔记.md").json()["document"]
    md_chunk = c.get(f"/api/documents/{md['id']}/content").json()["chunks"][0]["chunk_id"]
    # 非 PDF 没有页面可定位 → 空 rects 而非报错
    assert c.get(f"/api/documents/{md['id']}/locate/{md_chunk}").json()["rects"] == []

    # 块不属于该文档 → 404
    chunks = c.get(f"/api/documents/{doc['id']}/content").json()["chunks"]
    cid = chunks[0]["chunk_id"]
    assert c.get(f"/api/documents/{md['id']}/locate/{cid}").status_code == 404
    assert c.get(f"/api/documents/{doc['id']}/locate/999999").status_code == 404


def test_concurrent_same_name_uploads_are_isolated(client):
    """同名并发上传必须互不干扰（2026-09-11 排查发现）。

    前端多选/重复选文件是**并发** POST（documents.js 逐个 upload 不 await）。
    原先 web-tmp 直接用净化后的文件名做路径 → 两个请求共用同一个文件：
    后来者截断先来者的内容（落盘损坏却照常入库），先完成者的 finally unlink
    还会撞上后者仍持有的句柄（Windows 报 WinError 32，missing_ok 兜不住）。
    修复 = 临时文件名加 uuid 前缀。这里用真并发验证内容不被搅坏。
    """
    from concurrent.futures import ThreadPoolExecutor

    c, _settings = client
    text_a = NOTE  # 两个同名但内容不同的文件
    text_b = NOTE.replace("L2 正则化", "L1 正则化")

    def attempt():
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_a = pool.submit(_upload, c, "同名报告.md", text_a)
            fut_b = pool.submit(_upload, c, "同名报告.md", text_b)
            return fut_a.result(), fut_b.result()

    # 并发集成测试带时序噪声（TestClient 线程池 + Windows 文件系统 + SQLite
    # 写锁），允许重试；真正的缺陷（唯一约束冲突 / 内容损坏）是确定性的，
    # 重试也会稳定失败，不会因此放过 bug
    resp_a = resp_b = None
    for _ in range(3):
        resp_a, resp_b = attempt()
        if resp_a.status_code in (200, 201) and resp_b.status_code in (200, 201):
            break

    # 同名替换语义：两个请求都可成功（200 跳过 / 201 新建），但绝不能 500
    assert resp_a.status_code in (200, 201), resp_a.text
    assert resp_b.status_code in (200, 201), resp_b.text

    docs = c.get("/api/documents").json()["documents"]
    assert len(docs) == 1, "同名上传最终只应留一篇"
    content = c.get(f"/api/documents/{docs[0]['id']}/content").json()["text"]
    # 尾部句在 = 文件没被截断；L1/L2 恰好出现一个 = 两份内容没有交错
    assert "缩放点积注意力除以根号 dk" in content, "内容被截断（并发踩踏）"
    assert ("L1 正则化" in content) != ("L2 正则化" in content), "两份内容混在了一起"


def test_huge_int_path_is_404_not_500(client):
    """20+ 位的路径 id → 404（2026-09-11 修复）。

    裸 int 路径参数没有范围校验，超长数字直插 SQLite 绑定会抛 OverflowError
    → 500 + 整栈日志；语义上"这个 id 不存在"就该是 404。应用层统一兜住。
    """
    c, _ = client
    huge = "9" * 25
    # 注意用真实存在的端点（/api/documents/{id} 只有 PATCH/DELETE 没有 GET，
    # 方法不匹配会先返回 405，走不到 SQL 那一步）
    assert c.get(f"/api/documents/{huge}/content").status_code == 404
    assert c.get(f"/api/chunks/{huge}").status_code == 404
    assert c.delete(f"/api/documents/{huge}").status_code == 404
    assert c.patch(f"/api/documents/{huge}", json={"title": "x"}).status_code == 404
