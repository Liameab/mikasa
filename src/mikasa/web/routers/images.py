"""图像生成端点（文生图，ADR-0031）。

两条路径：

  - `POST /api/images/generate`        提示词 → 图片（落盘；给了 session 就落库）
  - `GET  /api/images/generated/{name}` 把落盘的图片发给浏览器

**为什么必须落盘**：上游（SiliconFlow）返回的图片链接**只有 1 小时有效期**，
原样写进回答就是"一小时后变裂图"。落盘后回答里引用的是本机地址，永久有效；
顺带把"生成过什么"留在自己的数据目录里（`data_dir/generated/`，与 uploads
分开——uploads 是 ingest 的输入，图片进去会被 reindex 当文档扫）。

**取图的纪律**（与 note-media 同款）：只认**服务端生成的文件名形状**
（正则白名单 → 目录拼接 → is_file），inline + nosniff + 私有缓存。
文件名由服务端拼（`日期-内容哈希.扩展名`），所以路径穿越在这里没有入口。
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from mikasa.config.settings import Settings
from mikasa.providers.vision import ext_for_mime, mime_for_suffix
from mikasa.storage import repo
from mikasa.storage.db import open_db
from mikasa.web.deps import get_services, get_settings
from mikasa.web.schemas import ImageGenerateIn
from mikasa.web.services import AppServices

router = APIRouter()

# 服务端自己拼的文件名：`20260921-1a2b3c4d.png`。读端点只认这个形状，
# 于是"用户能不能用 ../ 取别的文件"这个问题在正则这一层就没了。
_GENERATED_NAME_RE = re.compile(r"^\d{8}-[0-9a-f]{8}\.(?:png|jpg|webp)$")


def _image_markdown(prompt: str, url: str, model: str, size: str) -> str:
    """把一次出图变成一条可渲染、可回放的助手消息。

    alt 文本要洗：提示词里的 `]`、换行会把 Markdown 拆坏（渲染器只做转义，
    不负责猜意图）；顺带截断——alt 是给读屏与图片加载失败时看的，不需要全文。

    说明行**不写 `*斜体*`**：本项目的气泡渲染器只认 `**粗体**`/`` `代码` ``
    （没有斜体支持），写斜体只会露出星号。
    """
    alt = re.sub(r"[\[\]\r\n]+", " ", prompt).strip()[:60]
    return f"![{alt}]({url})\n\n（{model} · {size}）"


@router.post("/api/images/generate")
def generate_image(
    body: ImageGenerateIn,
    services: AppServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """生成一张图并落盘（可选落库到会话）。"""
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=422, detail="提示词不能为空")
    started = time.monotonic()
    # 未接入 → ConfigError(400)、上游失败 → ProviderError(502)：都由 app 层
    # 的 ZhiwenError 处理器翻译，这里不散落 try/except
    image = services.image.generate(prompt, size=body.size)
    latency_ms = int((time.monotonic() - started) * 1000)

    settings.ensure_dirs()
    # 文件名 = 日期 + 内容哈希：同一张图重复生成不会堆积副本（写第二个
    # 相同内容的文件没有意义），不同图同一天也不会撞
    digest = hashlib.sha256(image.data).hexdigest()[:8]
    name = f"{datetime.now():%Y%m%d}-{digest}{ext_for_mime(image.mime)}"
    path: Path = settings.generated_dir / name
    if not path.is_file():
        # 先写临时文件再原子替换：中途失败不会留下半张图（同配置覆盖层的写法）
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(image.data)
        os.replace(tmp, path)
    url = f"/api/images/generated/{name}"
    content = _image_markdown(prompt, url, image.model, image.size)

    # 铁律：文件操作排在改库之前——上面已经落盘，这里才动库。
    # 没给会话就**新建一个**（与"首问自动建会话"同一套语义）：图片生成出来
    # 却不落库，刷新一下就没了——用户要的是"这条对话里有这张图"。会话只在
    # 生成**成功之后**才建，失败路径一个空会话都不留（同 ask 的
    # _drop_session_if_empty 纪律；open_db 出错不提交，建会话会被回滚）。
    with open_db(settings.db_path) as conn:
        session_id = body.session_id
        if session_id is None:
            session_id = repo.create_session(conn, settings.profile)
        elif repo.get_session(conn, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        repo.insert_qa_message(
            conn,
            session_id=session_id,
            role="assistant",
            content=content,
        )
        message_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return {
        "name": name,
        "url": url,
        "content": content,
        "model": image.model,
        "size": image.size,
        "latency_ms": latency_ms,
        "session_id": session_id,
        "message_id": message_id,
    }


@router.get("/api/images/generated/{name}")
def get_generated_image(
    name: str,
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    """把落盘的生成图发给浏览器（只认服务端拼的名字）。"""
    if not _GENERATED_NAME_RE.fullmatch(name):
        raise HTTPException(status_code=404, detail="图片不存在")
    path = settings.generated_dir / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(
        path,
        media_type=mime_for_suffix(Path(name).suffix) or "application/octet-stream",
        content_disposition_type="inline",
        headers={
            # 同源存储型的 XSS 在图片目录这里没有入口（类型白名单 + nosniff）
            "X-Content-Type-Options": "nosniff",
            # 生成图属于个人内容，只让本机浏览器缓存
            "Cache-Control": "private, max-age=3600",
        },
    )
