"""笔记原图端点测试（M6 ②）：增 / 列 / 读 / 删 + 三条不变量。

最重要的那条：**原图锚在笔记 key 上，不是 doc_id**——笔记每次保存都是
"同名替换换 id"，挂在 id 上的目录第一次编辑就成了孤儿。这里用"编辑一次、
拿新 id 再读"把它钉死（把锚点改回 doc_id，这条必红）。

另两条：同图重复上传幂等（不存两份）；删图之后再加不撞名（序号取
"现存最大 +1"，不是"文件个数 +1"）。原图存不存与视觉模型无关——识别是
另一条链路，这里全程不配置视觉模型。
"""

from __future__ import annotations

import hashlib

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
OTHER = b"\x89PNG\r\n\x1a\n" + b"1" * 64  # 内容不同 → 另一个 sha 前缀


def _create_note(c, title: str = "板书笔记") -> int:
    resp = c.post("/api/notes", json={"title": title, "body": "# 板书\n\nF = ma\n"})
    assert resp.status_code == 201, resp.text
    return resp.json()["document"]["id"]


def _upload(c, doc_id: int, data: bytes = PNG, name: str = "board.png"):
    return c.post(f"/api/notes/{doc_id}/media", files={"file": (name, data)})


def _names(c, doc_id: int) -> list[str]:
    resp = c.get(f"/api/notes/{doc_id}/media")
    assert resp.status_code == 200, resp.text
    return [item["name"] for item in resp.json()["items"]]


def _media_dir(settings, doc_id: int):
    """测试直接看盘：目录名必须等于**笔记 key**（不是 doc_id）。"""
    from mikasa.storage import repo
    from mikasa.storage.db import open_db

    with open_db(settings.db_path) as conn:
        doc = repo.get_document(conn, doc_id)
    key = (doc.source_ref or "").removeprefix("note:")
    return settings.note_media_dir / key


def test_upload_list_and_fetch(client):
    c, settings = client
    doc_id = _create_note(c)

    first = _upload(c, doc_id)
    assert first.status_code == 201, first.text
    digest = hashlib.sha256(PNG).hexdigest()[:8]
    assert first.json() == {"name": f"001-{digest}.png", "already": False}

    second = _upload(c, doc_id, data=OTHER)
    assert second.status_code == 201
    assert _names(c, doc_id) == [f"001-{digest}.png", second.json()["name"]]

    # 读字节：inline + nosniff（同源下图片是安全的，但类型与嗅探头照给）
    got = c.get(f"/api/notes/{doc_id}/media/{first.json()['name']}")
    assert got.status_code == 200
    assert got.content == PNG
    assert got.headers["content-type"].startswith("image/png")
    assert got.headers["x-content-type-options"] == "nosniff"


def test_same_image_is_idempotent(client):
    """同一张图重复上传返回既有那件（200 + already），盘上只有一份。"""
    c, settings = client
    doc_id = _create_note(c)
    first = _upload(c, doc_id)
    again = _upload(c, doc_id)
    assert again.status_code == 200
    assert again.json() == {"name": first.json()["name"], "already": True}
    assert len(list(_media_dir(settings, doc_id).iterdir())) == 1


def test_media_survives_edit_which_changes_doc_id(client):
    """★ 编辑笔记（id 会变）之后，原图仍按新 id 可达。

    标签页里"打开的是 id=7、保存完变成 id=9"是常态（同名替换）。目录锚在
    key 上，所以 id 换了照样找得到——锚回 doc_id 的话这里直接 0 项。
    """
    c, settings = client
    old_id = _create_note(c)
    name = _upload(c, old_id).json()["name"]

    resp = c.put(
        f"/api/notes/{old_id}",
        json={"title": "板书笔记", "body": "# 板书\n\nF = ma\n\n补充一行。"},
    )
    # 201：编辑走的是入库尾链（同名替换 = 删旧行插新行），不是"无改动短路"的 200
    assert resp.status_code in (200, 201), resp.text
    new_id = resp.json()["document"]["id"]
    assert new_id != old_id  # 同名替换 = 换行 id（设计如此）

    assert _names(c, new_id) == [name]
    assert c.get(f"/api/notes/{new_id}/media/{name}").status_code == 200
    # 目录名是 key，不是任何一版的 id
    assert _media_dir(settings, new_id).is_dir()


def test_traversal_and_bad_names_are_404(client):
    c, _ = client
    doc_id = _create_note(c)
    for evil in ("../evil.png", "..%2Fevil.png", "x.png", "001-zzzzzzzz.png", "001-abcd1234.svg"):
        assert c.get(f"/api/notes/{doc_id}/media/{evil}").status_code == 404
        assert c.delete(f"/api/notes/{doc_id}/media/{evil}").status_code == 404


def test_media_only_for_notes(client):
    """普通文档（上传件）不能挂原图：404 而不是静默存到别处。"""
    c, _ = client
    resp = c.post("/api/documents", files={"file": ("资料.md", "# 资料\n\n正文\n".encode())})
    assert resp.status_code == 201, resp.text
    doc_id = resp.json()["document"]["id"]
    assert _upload(c, doc_id).status_code == 404
    assert _names(c, doc_id) == []  # 列表对非笔记返回空（"没有原图"是事实）


def test_delete_media_and_renumber_does_not_collide(client):
    """删中间一张再加：新序号取"现存最大 +1"，不会与既有名字撞车。"""
    c, settings = client
    doc_id = _create_note(c)
    names = [_upload(c, doc_id, data=PNG + bytes([i])).json()["name"] for i in range(3)]
    assert [n[:3] for n in names] == ["001", "002", "003"]

    assert c.delete(f"/api/notes/{doc_id}/media/{names[1]}").status_code == 200
    assert _names(c, doc_id) == [names[0], names[2]]

    fresh = _upload(c, doc_id, data=PNG + b"fresh").json()["name"]
    assert fresh[:3] == "004"  # 不是 002（个数 +1 会撞 002）
    assert sorted(_names(c, doc_id)) == sorted([names[0], names[2], fresh])
    assert _upload(c, doc_id, data=PNG + b"fresh").status_code == 200  # 幂等照旧


def test_delete_document_removes_media_dir(client):
    """删文档连带收走原图目录（失败只记日志的政策不影响正常路径）。"""
    c, settings = client
    doc_id = _create_note(c)
    _upload(c, doc_id)
    media_dir = _media_dir(settings, doc_id)
    assert media_dir.is_dir()

    assert c.delete(f"/api/documents/{doc_id}").status_code == 200
    assert not media_dir.exists()


def test_media_upload_validates_image(client):
    """原图与识图共用同一套校验：后缀 415 / 魔数 415 / 空 400。"""
    c, _ = client
    doc_id = _create_note(c)
    assert _upload(c, doc_id, name="a.gif").status_code == 415
    assert _upload(c, doc_id, data=b"not an image", name="a.png").status_code == 415
    assert _upload(c, doc_id, data=b"").status_code == 400
