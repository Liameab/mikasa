"""会话管理端点（M4.5）：文件夹树 CRUD + 会话改名/移动/删除 + 标题提炼。

与 qa.py 的分工：qa.py 管**问答流与历史读取**（POST ask/stream、GET
sessions/messages）；本文件是**管理面**——Web 会话树侧栏的增删改移数据源。
错误语义与 documents/qa 一致（HTTPException + 中文 detail 壳）：
  404 目标不存在（会话/文件夹/父文件夹）；
  400 空文件夹名 / 空标题（想清除请显式传 null——null 是合法覆盖值）；
  409 防环（新父 ∈ 自身∪后代）与非空文件夹删除（带计数文案）。
PATCH 语义：以 model_fields_set 判定字段是否出现——显式 null = 清除/移回根，
字段缺省 = 该属性不动（两字段可任意组合单发）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from mikasa.config.settings import Settings
from mikasa.errors import StorageError
from mikasa.storage import repo
from mikasa.storage.db import open_db
from mikasa.web.deps import get_services, get_settings
from mikasa.web.schemas import FolderIn, FolderPatchIn, SessionPatchIn, TitleSuggestIn
from mikasa.web.services import AppServices

router = APIRouter(tags=["sessions"])


# ---------------------------------------------------------------------------
# 入参清洗（文件夹名/会话标题：strip 后空 → 400）
# ---------------------------------------------------------------------------


def _clean_folder_name(raw: str) -> str:
    """文件夹名：strip 后空 → 400（null 清除语义不适用于文件夹名）。"""
    name = raw.strip()
    if not name:
        raise HTTPException(status_code=400, detail="文件夹名不能为空")
    return name


def _clean_title(raw: str | None) -> str | None:
    """会话标题：显式 null → None（= 清除解锁）；空串/纯空白 → 400。"""
    if raw is None:
        return None
    title = raw.strip()
    if not title:
        raise HTTPException(status_code=400, detail="标题不能为空（清除请传 null）")
    return title


# ---------------------------------------------------------------------------
# 文件夹树（嵌套结构由前端按 parent_id 组树；后端平铺存储）
# ---------------------------------------------------------------------------


@router.get("/api/folders")
def list_folders(settings: Settings = Depends(get_settings)) -> dict:
    """全部文件夹（id 升序平铺）——树形会话栏的文件夹数据源。"""
    with open_db(settings.db_path) as conn:
        return {"folders": repo.list_folders(conn)}


@router.post("/api/folders", status_code=201)
def create_folder(
    body: FolderIn,
    settings: Settings = Depends(get_settings),
) -> dict:
    """新建文件夹（parent_id 缺省/None = 根级；父不存在 → 404）。"""
    name = _clean_folder_name(body.name)
    with open_db(settings.db_path) as conn:
        if body.parent_id is not None and repo.get_folder(conn, body.parent_id) is None:
            raise HTTPException(status_code=404, detail="父文件夹不存在")
        folder_id = repo.create_folder(conn, name, parent_id=body.parent_id)
        folder = repo.get_folder(conn, folder_id)
    assert folder is not None  # 刚创建必在
    return {"folder": folder}


@router.patch("/api/folders/{folder_id}")
def patch_folder(
    folder_id: int,
    body: FolderPatchIn,
    settings: Settings = Depends(get_settings),
) -> dict:
    """文件夹改名/移动（可只带其一）。防环：新父 ∈ 自身∪后代 → 409；
    移回根（显式 null）恒允许。"""
    with open_db(settings.db_path) as conn:
        folder = repo.get_folder(conn, folder_id)
        if folder is None:
            raise HTTPException(status_code=404, detail="文件夹不存在")
        if "name" in body.model_fields_set:
            repo.rename_folder(conn, folder_id, _clean_folder_name(body.name or ""))
        if "parent_id" in body.model_fields_set:
            parent_id = body.parent_id
            if parent_id is not None:
                if repo.get_folder(conn, parent_id) is None:
                    raise HTTPException(status_code=404, detail="父文件夹不存在")
                if parent_id in repo.folder_descendant_ids(conn, folder_id):
                    raise HTTPException(status_code=409, detail="不能把文件夹移入自身或其子文件夹")
            repo.move_folder(conn, folder_id, parent_id)
        updated = repo.get_folder(conn, folder_id)
    assert updated is not None  # 上一步 404 已挡
    return {"folder": updated}


@router.delete("/api/folders/{folder_id}")
def delete_folder(
    folder_id: int,
    settings: Settings = Depends(get_settings),
) -> dict:
    """删除**空**文件夹：直接含子文件夹或会话 → 409 带计数文案（先移出）。
    容器不连坐内容，绝不级联删除用户会话（qa_folders 外键 NO ACTION 兜底）。"""
    with open_db(settings.db_path) as conn:
        folder = repo.get_folder(conn, folder_id)
        if folder is None:
            raise HTTPException(status_code=404, detail="文件夹不存在")
        counts = repo.folder_children_counts(conn, folder_id)
        if counts["subfolders"] or counts["sessions"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"文件夹非空：含 {counts['subfolders']} 个子文件夹、"
                    f"{counts['sessions']} 个会话，请先移出后再删除"
                ),
            )
        repo.delete_folder(conn, folder_id)
    return {"deleted": folder_id, "name": folder["name"]}


# ---------------------------------------------------------------------------
# 会话管理（改名/移夹/删除）
# ---------------------------------------------------------------------------


@router.patch("/api/sessions/{session_id}")
def patch_session(
    session_id: int,
    body: SessionPatchIn,
    settings: Settings = Depends(get_settings),
) -> dict:
    """会话改名/移夹（可只带其一）。title 非空 = 手动命名并锁（自动提炼
    永不覆盖）；title 显式 null = 清除解锁；folder_id 显式 null = 移回根。"""
    with open_db(settings.db_path) as conn:
        if repo.get_session(conn, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        if "title" in body.model_fields_set:
            repo.set_session_title(conn, session_id, _clean_title(body.title))
        if "folder_id" in body.model_fields_set:
            folder_id = body.folder_id
            if folder_id is not None and repo.get_folder(conn, folder_id) is None:
                raise HTTPException(status_code=404, detail="文件夹不存在")
            repo.move_session(conn, session_id, folder_id)
        session = repo.get_session(conn, session_id)
    assert session is not None  # 上一步 404 已挡
    return {"session": session}


@router.delete("/api/sessions/{session_id}")
def delete_session(
    session_id: int,
    settings: Settings = Depends(get_settings),
) -> dict:
    """删除会话（消息由外键 CASCADE 级联清理；所属文件夹不受影响）。"""
    with open_db(settings.db_path) as conn:
        session = repo.get_session(conn, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        repo.delete_session(conn, session_id)
    return {"deleted": session_id, "title": session["title"]}


# ---------------------------------------------------------------------------
# 标题提炼（首问答自动提炼的"补跑"入口：AI 重命名按钮）
# ---------------------------------------------------------------------------


@router.post("/api/sessions/{session_id}/title/suggest")
def suggest_title(
    session_id: int,
    body: TitleSuggestIn,
    services: AppServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
) -> dict:
    """提炼会话标题并（apply=true 时）落库。offline/mock 自动截断兜底——
    合法回退而非错误，applied=true；api/local 走 LLM 提炼。手动命名锁定
    时给出建议但不落库（applied=false）。"""
    with open_db(settings.db_path) as conn:
        if repo.get_session(conn, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在")
    try:
        title, applied = services.ask.suggest_title(session_id, apply=body.apply)
    except StorageError as exc:
        # 会话还没有任何提问：业务 400（detail 壳与其它端点一致）
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"title": title, "applied": applied}
