"""Web 请求体模型（pydantic）：端点入参的声明式校验。

全部 frozen：请求体只读，路由内不修改入参（防御性惯例）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from mikasa.pipeline.ask import AnswerMode


class QuestionIn(BaseModel):
    """提问请求体（非流式 ask 与流式 ask/stream 共用）。

    question 必填非空（min_length=1；纯空白由 AskService.strip 后抛
    业务错误）；session_id 缺省表示新建会话（响应里带回新会话 id）；
    mode 缺省 kb（知识库检索）；非法字面量由 pydantic 直接 422
    （问答模式单一事实源在 pipeline.ask.AnswerMode，见 ADR-0013）。
    """

    model_config = ConfigDict(frozen=True)

    question: str = Field(min_length=1, max_length=2000, description="问题正文")
    session_id: int | None = Field(default=None, description="续问会话；缺省新建")
    mode: AnswerMode = Field(default="kb", description="kb=知识库检索；free=自由问答")


class FolderIn(BaseModel):
    """新建文件夹请求体（POST /api/folders）。

    parent_id 缺省/None = 根级；父文件夹不存在由路由层查 404。
    纯空白名由路由层 strip 后判 400（pydantic 只保证长度 ≥1）。
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=50, description="文件夹名")
    parent_id: int | None = Field(default=None, description="父文件夹 id；缺省=根级")


class FolderPatchIn(BaseModel):
    """文件夹改名/移动（PATCH /api/folders/{id}，两字段可任意单发）。

    覆盖语义以"字段是否出现"判定（model_fields_set）：显式 null parent_id
    = 移回根级；**字段缺省 = 该属性不动**。改名必须给非空值（null/空串
    → 400：清除语义不适用于文件夹名）。
    """

    model_config = ConfigDict(frozen=True)

    name: str | None = Field(default=None, min_length=1, max_length=50, description="新名")
    parent_id: int | None = Field(default=None, description="新父文件夹；显式 null=移回根")


class SessionPatchIn(BaseModel):
    """会话改名/移夹（PATCH /api/sessions/{id}，两字段可任意单发）。

    title 显式 null = 清除标题并解锁（回"未命名"，下轮问答自动重新补名）；
    title 非空 = 手动命名并锁 title_manual（自动提炼永不覆盖）。
    folder_id 显式 null = 移回根级。
    """

    model_config = ConfigDict(frozen=True)

    title: str | None = Field(default=None, max_length=100, description="新标题；null=清除")
    folder_id: int | None = Field(default=None, description="目标文件夹；null=移回根")


class TitleSuggestIn(BaseModel):
    """标题提炼请求体（POST /api/sessions/{id}/title/suggest）。"""

    model_config = ConfigDict(frozen=True)

    apply: bool = Field(default=True, description="true=提炼并落库（手动命名锁除外）")


class DocumentPatchIn(BaseModel):
    """文档改名/移夹（PATCH /api/documents/{id}，两字段可任意单发）。

    覆盖语义同 SessionPatchIn（model_fields_set 判定字段是否出现）：
    显式 null folder_id = 移回根级；字段缺省 = 该属性不动。
    title 与文件夹改名同语义：必须给非空值——文档标题没有"清除"
    概念（标题即文档身份，空标题在树上无从展示），null/空串 → 400。
    """

    model_config = ConfigDict(frozen=True)

    title: str | None = Field(default=None, max_length=120, description="新标题（非空）")
    folder_id: int | None = Field(default=None, description="目标文件夹；null=移回根")
