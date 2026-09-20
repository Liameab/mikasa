"""笔记识图端点测试（M6 ②）：/api/notes/ocr 的拒绝路径与成功路径。

替身走 `services.vision`（真识别要外部模型，单测不打网络）。四类拒绝各有
各的状态码，不能混成一个"识别失败"：
  - 未接入（offline 档默认）→ 400，文案要能直接照做；
  - 上游失败（ProviderError）→ 502（走 app 层处理器，端点自己不 catch）；
  - 模型空回复 → 502（如实说"没有返回内容"，不能返回空文本让前端存成空笔记）；
  - 后缀 / 魔数 / 大小三关各自 415 / 415 / 413。
"""

from __future__ import annotations

import pytest

from mikasa.providers.llm import Completion
from mikasa.providers.vision import NoVision

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


class _FakeVision:
    """识别替身：记录收到的图片与 mime，可按需抛错或返回空文本。"""

    model = "fake-vl"

    def __init__(self, text: str = "## 板书\n\n牛顿第二定律 F = ma", error=None) -> None:
        self.text = text
        self.error = error
        self.seen: list[dict] = []

    def describe(self, image: bytes, *, mime: str, prompt=None) -> Completion:
        self.seen.append({"image": image, "mime": mime})
        if self.error is not None:
            raise self.error
        return Completion(text=self.text, prompt_tokens=10, completion_tokens=5)


def _post_image(c, name: str = "board.png", data: bytes = PNG):
    return c.post("/api/notes/ocr", files={"file": (name, data)})


def test_ocr_returns_text_and_metadata(client):
    """成功路径：文本 + 模型名 + 用量；发出去的 mime 由魔数决定。"""
    c, _ = client
    fake = _FakeVision()
    c.app.state.services.vision = fake

    resp = _post_image(c)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["text"].startswith("## 板书")
    assert body["model"] == "fake-vl"
    assert body["prompt_tokens"] == 10 and body["completion_tokens"] == 5
    assert fake.seen[0]["mime"] == "image/png"
    assert fake.seen[0]["image"] == PNG


def test_ocr_reports_not_connected_with_actionable_text(client):
    """未接入视觉模型：400 + 可照做的文案（offline 档的默认状态）。"""
    c, _ = client
    assert isinstance(c.app.state.services.vision, NoVision)
    resp = _post_image(c)
    assert resp.status_code == 400, resp.text
    detail = resp.json()["error"]["message"]
    assert "视觉模型" in detail
    assert "ollama pull" in detail  # 本机那条路要给命令


def test_ocr_translates_provider_failure_to_502(client):
    """上游失败（鉴权/限流/网络）→ 502，不是 400。"""
    from mikasa.errors import ProviderError

    c, _ = client
    c.app.state.services.vision = _FakeVision(error=ProviderError("模拟上游 429"))
    resp = _post_image(c)
    assert resp.status_code == 502, resp.text
    assert "429" in resp.json()["error"]["message"]


def test_ocr_empty_reply_is_502(client):
    """模型空回复 → 502：必须如实说，不能返回空文本。"""
    c, _ = client
    c.app.state.services.vision = _FakeVision(text="   ")
    resp = _post_image(c)
    assert resp.status_code == 502
    assert "没有返回内容" in resp.json()["detail"]


@pytest.mark.parametrize(
    ("name", "data", "status"),
    [
        ("a.gif", PNG, 415),  # 后缀不在白名单
        ("a.txt", PNG, 415),  # 同上
        ("a.jpg", b"not an image at all", 415),  # 后缀像图片、内容不是（魔数拦）
        ("a.png", b"", 400),  # 空文件
    ],
)
def test_ocr_rejects_bad_input(client, name, data, status):
    c, _ = client
    c.app.state.services.vision = _FakeVision()
    resp = _post_image(c, name=name, data=data)
    assert resp.status_code == status, resp.text


def test_ocr_rejects_oversize_image(client, monkeypatch):
    """超过图片上限 → 413（这条闸门独立于 BodySizeLimit，见端点注释）。"""
    import mikasa.web.routers.documents as docs

    c, _ = client
    c.app.state.services.vision = _FakeVision()
    monkeypatch.setattr(docs, "_MAX_IMAGE_MB", 0.001)  # ≈1KB，测试别塞 13MB
    resp = _post_image(c, data=PNG + b"0" * 4096)
    assert resp.status_code == 413, resp.text
    assert "上限" in resp.json()["detail"]


def test_ocr_accepts_jpeg_magic(client):
    """JPEG 魔数同样放行（前端压缩后送的就是 JPEG）。"""
    c, _ = client
    fake = _FakeVision()
    c.app.state.services.vision = fake
    resp = _post_image(c, name="photo.JPG", data=JPEG)  # 大小写也照收
    assert resp.status_code == 200, resp.text
    assert fake.seen[0]["mime"] == "image/jpeg"
