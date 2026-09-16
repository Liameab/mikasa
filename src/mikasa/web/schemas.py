"""Web 请求体模型（pydantic）：端点入参的声明式校验。

全部 frozen：请求体只读，路由内不修改入参（防御性惯例）。
"""

from __future__ import annotations

from typing import Literal

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


class ModelSettingsIn(BaseModel):
    """模型配置保存请求体（PUT /api/settings/model，见 ADR-0018）。

    api_key 三段语义：**None = 本次不动密钥**；空串 = 清除；非空 = 写入。
    前端"密钥框没被碰过"就不发该字段——GET 从不回传密钥，输入框平时是空的，
    若把空值当"清除"处理，一次普通的改模型名就会顺手删掉已存密钥。
    backend 只开放 api/local：mock 是 offline 体验档的形态，属于 profile 的事。
    """

    model_config = ConfigDict(frozen=True)

    backend: Literal["api", "local"] = Field(description="api=OpenAI 兼容云端；local=本机 Ollama")
    base_url: str = Field(default="", max_length=500, description="OpenAI 兼容端点（含 /v1）")
    model: str = Field(default="", max_length=200, description="模型名")
    api_key_env: str = Field(default="", max_length=100, description="密钥所在环境变量名")
    api_key: str | None = Field(default=None, max_length=500, description="None=不改/空串=清除")


class ModelTestIn(BaseModel):
    """测试连接请求体（POST /api/settings/model/test）。

    api_key 缺省 = 用 api_key_env 指向的已存密钥（都没给则该后端视为无密钥）。
    """

    model_config = ConfigDict(frozen=True)

    backend: Literal["api", "local"] = "api"
    base_url: str = Field(default="", max_length=500)
    model: str = Field(default="", max_length=200)
    api_key_env: str = Field(default="", max_length=100)
    api_key: str | None = Field(default=None, max_length=500, description="缺省=用已存密钥")


class PaperSearchIn(BaseModel):
    """在线论文检索请求体（POST /api/papers/search，见 ADR-0019）。

    offset/limit 是"全局交错窗口"（arxiv 占偶数位、openalex 占奇数位），
    服务层换算成各源自己的 start/count；source=all 时两源合并。
    """

    model_config = ConfigDict(frozen=True)

    q: str = Field(min_length=1, max_length=200, description="检索词（中文或英文）")
    source: Literal["all", "arxiv", "openalex"] = Field(default="all", description="检索来源")
    offset: int = Field(default=0, ge=0, le=100000, description="结果偏移（分页）")
    limit: int = Field(default=20, ge=1, le=50, description="每页条数")


class PaperImportIn(BaseModel):
    """论文导入请求体（POST /api/papers/import）。

    source+id 而非 PDF 链接：PDF 地址由服务端按 id 反查来源 API 解析，
    客户端不能塞任意 URL（SSRF 防线在入口就闭合，见 papers/download.py）。
    """

    model_config = ConfigDict(frozen=True)

    source: Literal["arxiv", "openalex"] = Field(description="来源（id 必属单一来源）")
    id: str = Field(min_length=1, max_length=64, description="论文编号（arXiv id / OpenAlex W-id）")


class PaperKeyIn(BaseModel):
    """OpenAlex API 密钥保存请求体（PUT /api/papers/settings）。

    三段语义同 ModelSettingsIn：None=不动；空串=清除；非空=写入。
    """

    model_config = ConfigDict(frozen=True)

    api_key: str | None = Field(default=None, max_length=500, description="None=不改/空串=清除")


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
