"""Web 请求体模型（pydantic）：端点入参的声明式校验。

全部 frozen：请求体只读，路由内不修改入参（防御性惯例）。
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    # local 档专属（api 档忽略）：None = 不写进请求、跟随默认——"没配置"与"关掉"是两件事
    think: bool | None = Field(default=None, description="None=模型默认；False=关思考（快）")
    num_ctx: int | None = Field(
        default=None, ge=2048, le=131072, description="None=Ollama 默认；否则为上下文 token 数"
    )
    # 两档通用：None = 不写进覆盖层（继续跟档位默认值），数字 = 写进覆盖层
    max_tokens: int | None = Field(
        default=None,
        ge=256,
        le=32768,
        description="回答 token 上限；None=跟随档位默认",
    )


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
    think: bool | None = Field(default=None, description="local：思考模式（None=模型默认）")
    num_ctx: int | None = Field(default=None, ge=2048, le=131072, description="local：上下文长度")


class VisionSettingsIn(BaseModel):
    """视觉模型配置保存请求体（PUT /api/settings/vision，见 ADR-0027）。

    backend 允许 none（= **停止使用**视觉能力，不是删密钥）：这是"我不接
    识图了"的正常表达，与"清空密钥"是两件事。

    与模型段最关键的区别：**密钥只写不删**。SILICONFLOW_API_KEY 被
    embedding/reranker/judge 共用，在这里清空会连带打挂整条检索链，而且
    表现为"搜索结果变差"——最难联想到密钥的那种静默降级。空串直接 422
    并指路模型段（服务端拦，不只靠前端不显示那个按钮）。
    """

    model_config = ConfigDict(frozen=True)

    backend: Literal["none", "api", "local"] = Field(
        description="none=未接入；api=云端；local=本机"
    )
    base_url: str = Field(default="", max_length=500, description="OpenAI 兼容端点（含 /v1）")
    model: str = Field(default="", max_length=200, description="视觉模型名")
    api_key_env: str = Field(default="", max_length=100, description="密钥所在环境变量名")
    api_key: str | None = Field(
        default=None, max_length=500, description="None=不改；非空=写入；空串=拒绝（见类说明）"
    )


class VisionTestIn(BaseModel):
    """测试视觉连接请求体（POST /api/settings/vision/test）。"""

    model_config = ConfigDict(frozen=True)

    backend: Literal["api", "local"] = "api"
    base_url: str = Field(default="", max_length=500)
    model: str = Field(default="", max_length=200)
    api_key_env: str = Field(default="", max_length=100)
    api_key: str | None = Field(default=None, max_length=500, description="缺省=用已存密钥")


class ImageSettingsIn(BaseModel):
    """图像生成配置保存请求体（PUT /api/settings/image，见 ADR-0031）。

    backend 允许 none（= **停止使用**出图），与"清空密钥"是两件事。
    **密钥只写不删**（与 vision 段同款理由）：SILICONFLOW_API_KEY 被
    embedding/reranker/judge/vision 共用，在这里清空会连带打挂整条检索链，
    且症状是"搜索结果变差"——最难联想到密钥的那种静默降级。空串 422。
    """

    model_config = ConfigDict(frozen=True)

    backend: Literal["none", "api"] = Field(description="none=未接入；api=云端")
    base_url: str = Field(default="", max_length=500, description="OpenAI 兼容端点（含 /v1）")
    model: str = Field(default="", max_length=200, description="出图模型名")
    size: str = Field(default="", max_length=20, description="如 1328x1328（各模型推荐值不同）")
    api_key_env: str = Field(default="", max_length=100, description="密钥所在环境变量名")
    api_key: str | None = Field(
        default=None, max_length=500, description="None=不改；非空=写入；空串=拒绝（见类说明）"
    )


class ImageTestIn(BaseModel):
    """测试出图连接（POST /api/settings/image/test）。

    **不生成图片**：出图按张计费，拿它当连通性测试不合适。这里只读
    `/models` 列表并核对配置的模型名在不在其中（几百字节、不烧额度）。
    """

    model_config = ConfigDict(frozen=True)

    base_url: str = Field(default="", max_length=500)
    model: str = Field(default="", max_length=200)
    api_key_env: str = Field(default="", max_length=100)
    api_key: str | None = Field(default=None, max_length=500, description="缺省=用已存密钥")


class ImageGenerateIn(BaseModel):
    """生成图片请求体（POST /api/images/generate）。"""

    model_config = ConfigDict(frozen=True)

    prompt: str = Field(min_length=1, max_length=2000, description="提示词")
    size: str | None = Field(default=None, max_length=20, description="缺省=用配置里的 size")
    session_id: int | None = Field(
        default=None, description="给了就把这条图消息落进该会话（刷新后仍在）"
    )


class PaperFiltersIn(BaseModel):
    """在线论文检索的筛选条件（能力对齐见 papers/sources.py 的 SourceCaps）。

    各来源支持哪些只有它自己知道，**不支持的条件不会假装生效**：服务端
    把降级说明放进取响应体的 notes（见 ADR-0020）。

    日期是**完整 ISO 日期**而不是年份：界面给的是日期选择器，用户选了哪天
    就用哪天（只取年份等于悄悄丢掉一半输入）。CORE 只支持到年，那属能力
    边界、已在 caps 里声明。
    """

    model_config = ConfigDict(frozen=True)

    date_from: date | None = Field(default=None, description="起始日期（含）")
    date_to: date | None = Field(default=None, description="结束日期（含）")
    oa_only: bool = Field(default=False, description="只看开放获取全文")
    language: str | None = Field(default=None, max_length=16, description="语言代码（zh/en）")
    sort: Literal["relevance", "cited", "recent"] = Field(
        default="relevance", description="排序：相关度 / 被引 / 时间"
    )

    @model_validator(mode="after")
    def _check_date_range(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("起始日期不能晚于结束日期")
        return self


class PaperSearchIn(BaseModel):
    """在线论文检索请求体（POST /api/papers/search，见 ADR-0019/0020）。

    offset/limit 是"全局轮转窗口"（全局位置 p 归第 p % n 个来源），
    服务层换算成各源自己的 start/count。
    """

    model_config = ConfigDict(frozen=True)

    q: str = Field(min_length=1, max_length=200, description="检索词（中文或英文）")
    # 来源名单与 `papers.service.SOURCES` 的注册表必须同步——**加新来源时
    # 这里漏改的表现是"前端勾了它、请求直接 422"**（2026-09-19 加 DOAJ 时
    # 实测踩中）。tests/unit/web/test_papers_api.py 有一条守卫测试把两张表
    # 钉在一起，以后不用靠记性。
    sources: list[Literal["arxiv", "openalex", "core", "doaj"]] | None = Field(
        default=None,
        min_length=1,
        max_length=8,
        description="来源选择集；缺省 = 全部来源",
    )
    offset: int = Field(default=0, ge=0, le=100000, description="结果偏移（分页）")
    limit: int = Field(default=20, ge=1, le=50, description="每页条数")
    filters: PaperFiltersIn = Field(
        default_factory=PaperFiltersIn, description="筛选与排序（缺省 = 全默认）"
    )


class PaperImportIn(BaseModel):
    """论文导入请求体（POST /api/papers/import）。

    source+id 而非 PDF 链接：PDF 地址由服务端按 id 反查来源 API 解析，
    客户端不能塞任意 URL（SSRF 防线在入口就闭合，见 papers/download.py）。
    """

    model_config = ConfigDict(frozen=True)

    # 与 PaperSearchIn.sources 同一张名单（同样的漂移风险，同一守卫测试覆盖）
    source: Literal["arxiv", "openalex", "core", "doaj"] = Field(
        description="来源（id 必属单一来源）"
    )
    id: str = Field(
        min_length=1,
        max_length=64,
        description="论文编号（arXiv id / OpenAlex W-id / CORE 数字 id）",
    )


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


NOTE_BODY_MAX = 200_000
"""笔记正文上限（字符数，≈200KB 文本 / 10 万字中文）。

不是"能不能存下"的约束（SQLite 与 500MB 的上传上限都远大于此），而是
**单次保存的嵌入耗时上界**：正文每次保存都要重新分块 + 重新嵌入，20 万字符
≈ 500 块，本地 CPU 嵌入约 1-2 秒、远端嵌入接口约十几秒。笔记体裁远达不到
这个量级，真到那个量级应该走"上传 .md 文件"（走同一条入库链路）。
"""


class NoteIn(BaseModel):
    """新建笔记（POST /api/notes）：标题 + Markdown 正文 + 可选落夹。

    title 的长度上限走 Pydantic（→422），空值判定走路由层（→400，
    文案要面向笔记用户：空正文的 422 说不出"整篇都是代码块"这回事）。
    """

    model_config = ConfigDict(frozen=True)

    title: str = Field(max_length=120, description="笔记标题（非空）")
    body: str = Field(description="Markdown 正文（见 _body_within_limit 的长度校验）")
    folder_id: int | None = Field(default=None, description="落到哪个语料文件夹；null=根级")

    @field_validator("body")
    @classmethod
    def _body_within_limit(cls, value: str) -> str:
        """正文长度上限走校验器而不是 Field(max_length=…)：Pydantic 的默认文案是
        英文（"String should have at most 200000 characters"），会原样弹到中文界面
        上；而且默认约束错误会把整个正文回显进响应体（20 万字符 ≈ 600KB）。"""
        if len(value) > NOTE_BODY_MAX:
            raise ValueError(f"笔记正文过长（上限 {NOTE_BODY_MAX} 字符）")
        return value


class NoteUpdateIn(BaseModel):
    """编辑笔记（PUT /api/notes/{id}）：标题与正文**全量**提交。

    刻意不做"只传改动字段"的部分更新：编辑器的提交单位就是"这一屏内容"，
    全量提交让"改标题不重入库"这类分支不存在。归属（folder_id）不在本模型里
    ——移动是树的职责（拖拽/PATCH），编辑器的职责只有内容。
    """

    model_config = ConfigDict(frozen=True)

    title: str = Field(max_length=120, description="笔记标题（非空）")
    body: str = Field(description="Markdown 正文（长度校验同 NoteIn）")
    rev: str | None = Field(
        default=None,
        max_length=32,
        description="乐观锁令牌：GET 笔记时下发，保存时回传；不一致说明别处改过（409）",
    )

    @field_validator("body")
    @classmethod
    def _body_within_limit(cls, value: str) -> str:
        """同 NoteIn：中文文案 + 不回显正文。"""
        if len(value) > NOTE_BODY_MAX:
            raise ValueError(f"笔记正文过长（上限 {NOTE_BODY_MAX} 字符）")
        return value
