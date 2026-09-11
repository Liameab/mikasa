"""问答与会话端点：非流式（调试）、SSE 流式（页面主路径）、会话历史。

SSE 协议（与 ask.StreamEvent / sse.py / 前端解析严格对偶）：
  meta（session_id）→ delta×n（text 增量）→ done（answer 全文 +
  session_id）；中途失败 → error 收尾（message 中文）。
端点内 try/except 只在 SSE 生成器里做"异常 → error 帧"的翻译——
流已开始后异常处理器不再生效，必须就地收尾。
"""

from __future__ import annotations

import json
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException

from mikasa.config.settings import Settings
from mikasa.errors import ZhiwenError
from mikasa.pipeline.ask import AnswerMode
from mikasa.storage import repo
from mikasa.storage.db import open_db
from mikasa.web.deps import get_services, get_settings
from mikasa.web.schemas import QuestionIn
from mikasa.web.services import AppServices
from mikasa.web.sse import sse_event, sse_response

router = APIRouter(tags=["qa"])


# ---------------------------------------------------------------------------
# 非流式问答与会话（保留调试/自动化；页面走流式端点）
# ---------------------------------------------------------------------------


@router.post("/api/ask")
def ask(
    body: QuestionIn,
    services: AppServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
) -> dict:
    """一次问答（非流式）。无 session_id 时新建会话并在响应中带回。"""
    session_id, is_new = _resolve_session(settings, body.session_id)
    try:
        answer = services.ask.ask(body.question, session_id=session_id, mode=body.mode)
    except ZhiwenError:
        if is_new:
            _drop_session_if_empty(settings, session_id)  # 同上：失败不留空会话
        raise
    return {"session_id": session_id, "answer": answer.model_dump(mode="json")}


@router.get("/api/sessions")
def list_sessions(
    settings: Settings = Depends(get_settings),
) -> dict:
    """会话列表（新建倒序，附消息数）——前端"历史会话"侧栏数据源。"""
    with open_db(settings.db_path) as conn:
        sessions = repo.list_sessions(conn)
    return {"sessions": sessions}


@router.get("/api/sessions/{session_id}/messages")
def session_messages(
    session_id: int,
    settings: Settings = Depends(get_settings),
) -> dict:
    """会话内全部消息：json 列解码、refused 转 bool（前端直接渲染）。"""
    with open_db(settings.db_path) as conn:
        if repo.get_session(conn, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        rows = repo.messages_by_session(conn, session_id)
    return {"session_id": session_id, "messages": [_decode_message(r) for r in rows]}


# ---------------------------------------------------------------------------
# SSE 流式问答（页面主路径）
# ---------------------------------------------------------------------------


@router.post("/api/ask/stream")
def ask_stream(
    body: QuestionIn,
    services: AppServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
):
    """单轮提问，SSE 流式返回（新会话或续问均可用）。"""
    session_id, is_new = _resolve_session(settings, body.session_id)
    return sse_response(
        _to_frames(
            services, settings, body.question, session_id, mode=body.mode, new_session=is_new
        )
    )


@router.post("/api/sessions/{session_id}/chat/stream")
def chat_stream(
    session_id: int,
    body: QuestionIn,
    services: AppServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
):
    """会话内续问（多轮历史），SSE 流式返回。会话不存在 → 404。"""
    with open_db(settings.db_path) as conn:
        if repo.get_session(conn, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在")
    # 续问：会话是用户既有的，new_session=False —— 失败也不删（默认值即此）
    return sse_response(_to_frames(services, settings, body.question, session_id, mode=body.mode))


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------


def _new_session(settings: Settings) -> int:
    """新建会话并返回 id（ask 路径需要响应里带回新会话 id）。"""
    with open_db(settings.db_path) as conn:
        return repo.create_session(conn, settings.profile)


def _resolve_session(settings: Settings, session_id: int | None) -> tuple[int, bool]:
    """续问目标会话：缺省新建；给定但不存在 → 404。

    校验必要：qa_messages.session_id 有外键，塞不存在的会话会
    IntegrityError → 500。给前端明确的 404 更诚实。

    返回 (会话 id, 是否本次新建)：新建的那条若"一个事件都没产出就失败"
    需要删掉——失败不该在会话树里留下一条空对话（见 _to_frames）。
    """
    if session_id is None:
        return _new_session(settings), True
    with open_db(settings.db_path) as conn:
        if repo.get_session(conn, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在")
    return session_id, False


def _drop_session_if_empty(settings: Settings, session_id: int) -> None:
    """删除"一条消息都没有"的会话（失败清理专用，双保险再查一次消息表）。"""
    with open_db(settings.db_path) as conn:
        if repo.get_session(conn, session_id) is None:
            return
        if repo.messages_by_session(conn, session_id):
            return  # 已经有落库消息：那是用户的东西，绝不删
        repo.delete_session(conn, session_id)
        conn.commit()


def _to_frames(
    services: AppServices,
    settings: Settings,
    question: str,
    session_id: int,
    *,
    mode: AnswerMode = "kb",
    new_session: bool = False,
) -> Iterator[str]:
    """AskService 事件流 → SSE 帧流；异常就地转 error 帧收尾。

    mode 透传给 ask_stream：守卫（free+mock → ConfigError）在生成器
    首个 yield 前抛出 → try 兜底产出恰一帧 error、零 meta（前端契约）。

    new_session：本次提问刚建的会话。若一个事件都没产出就失败（空库 /
    守卫拒绝 / 上游立刻报错），把这条空会话删掉——与 CLI chat 的
    "早失败不落审计轨迹"同款纪律，否则用户每失败一次就在树里多一条
    空对话（2026-09-11 修复）。
    """
    produced = False
    try:
        for ev in services.ask.ask_stream(question, session_id=session_id, mode=mode):
            produced = True
            if ev.kind == "meta":
                yield sse_event("meta", {"session_id": ev.session_id})
            elif ev.kind == "delta":
                yield sse_event("delta", {"text": ev.text})
            else:  # done：完整 Answer 一次性给全（引用/拒答/延迟）
                yield sse_event(
                    "done",
                    {
                        "session_id": ev.session_id,
                        "answer": ev.answer.model_dump(mode="json") if ev.answer else None,
                    },
                )
    except Exception as exc:  # noqa: BLE001 - 流已开始，异常处理器不生效
        # 业务错误（空库/空问题/上游失败）消息本就脱敏可回显；未预期
        # 异常不回显细节（日志已由 AskService/上层记录）
        message = str(exc) if isinstance(exc, ZhiwenError) else "生成中断：服务器内部错误"
        yield sse_event("error", {"message": message})
    finally:
        # 清理必须放 finally：客户端中途断开（关标签页/刷新，前端没有 AbortController，
        # 只能这样中断）时生成器被 close()，抛的是 **GeneratorExit**——它是
        # BaseException，`except Exception` 接不住，于是"一个事件都没产出"的空会话
        # 会留在会话树里（2026-09-11 审查发现）。
        if new_session and not produced:
            _drop_session_if_empty(settings, session_id)


def _decode_message(row: dict) -> dict:
    """qa_messages 行 → JSON 友好 dict：json 列解码、refused 转 bool。"""
    message = dict(row)
    raw_citations = message.pop("citations_json", None)
    raw_latency = message.pop("latency_ms_json", None)
    message["citations"] = json.loads(raw_citations) if raw_citations else []
    message["latency_ms"] = json.loads(raw_latency) if raw_latency else {}
    if message.get("refused") is not None:
        message["refused"] = bool(message["refused"])
    return message
