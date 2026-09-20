"""配置加载：YAML profile 文件 + 环境变量展开。

设计要点（详见 docs/design-decisions.md ADR-0001/ADR-0002）：
- 三套 profile（api / local / offline）对应"云端 API / 本地推理 / 零密钥离线"，
  运行时只需 --profile 切换，模型名与端点全部走配置，代码零硬编码；
- 配置文件中支持 ${ENV_VAR} 与 ${ENV_VAR:-默认值} 占位符展开，
  密钥永不写入 YAML，只经环境变量（.env）注入；
- 全部模型使用 pydantic v2（frozen），类型即文档。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mikasa.errors import ConfigError


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包产物里（exe），而不是源码仓库中。"""
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """**只读**资源根：随包发布的文件（profile 配置、示例语料、黄金集）。

    开发时 = 仓库根（src/mikasa/config/settings.py 向上三级）；
    打包后 = 解包目录（sys._MEIPASS）——PyInstaller 把 --add-data 的内容
    放在那里，而 `__file__` 的 `parents[N]` 会算到临时目录的上一级，不能再用。
    """
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[3]


def user_data_root() -> Path:
    """**可写**数据根：数据库 / uploads / 索引快照 / 日志。

    开发时 = 仓库根/data（保持原有行为，备份就是复制 data/）；
    打包后 = 用户可写目录——exe 可能装在 Program Files（无写权限），
    而 onefile 的临时解包目录会被系统清理，都不能当数据家。
    Windows 用 %LOCALAPPDATA%\\Mikasa，其他平台用 ~/.local/share/Mikasa。
    环境变量 MIKASA_DATA_DIR 在两种形态下都可显式覆盖。
    """
    override = os.environ.get("MIKASA_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if is_frozen():
        if sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        else:
            base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
        return base / "Mikasa"
    return resource_root() / "data"


# 兼容别名：只读资源的既有引用点（黄金集、示例语料）继续用它；
# **可写路径一律走 user_data_root()**，别再往这里写东西。
REPO_ROOT = resource_root()

VALID_PROFILES = ("api", "local", "offline")

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env_vars(value: Any) -> Any:
    """递归展开字符串中的 ${VAR} / ${VAR:-default} 占位符（值来自 os.environ）。"""

    def _sub(match: re.Match[str]) -> str:
        var, default = match.group(1), match.group(2)
        found = os.environ.get(var)
        return found if found is not None else (default or "")

    if isinstance(value, str):
        return _ENV_PATTERN.sub(_sub, value)
    if isinstance(value, dict):
        return {k: expand_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env_vars(v) for v in value]
    return value


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并两个 dict；profile 文件只需写想覆盖的字段。"""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# 各功能块配置模型
# ---------------------------------------------------------------------------


class ChunkingConfig(BaseModel):
    """分块参数（ingest 阶段）。"""

    model_config = ConfigDict(frozen=True)

    # Annotated + 纯量默认值：约束进类型元数据（构造即校验），默认值保持 mypy 可见
    size: Annotated[int, Field(gt=0)] = 400
    overlap: Annotated[int, Field(ge=0)] = 50
    title_prefix: bool = True

    def validate_self(self) -> None:
        if self.size <= 0 or self.overlap < 0 or self.overlap >= self.size:
            raise ConfigError(f"非法的分块参数：size={self.size}, overlap={self.overlap}")
        return None


class RetrievalConfig(BaseModel):
    """双路检索与融合参数。"""

    model_config = ConfigDict(frozen=True)

    bm25_top_k: int = 20
    dense_top_k: int = 20
    dense_enabled: bool = True
    fusion_top_k: int = 8
    fusion_k: int = 60
    # 跨语言检索（2026-09-10）：中文问题自动翻译成英文作第二路查询，
    # 命中英文文献块（bge-small-zh 对英文弱、BM25 词面跨语言零匹配）。
    # 默认关：翻译依赖真实 LLM，offline/mock 档保持零调用；local/api 档
    # 在各自 profile yaml 里开启。英文路只在英文块上有分，不会挤占主路
    # 中文结果（RRF 两路合流，见 retriever.retrieve second_query）。
    crosslingual: bool = False


class RerankerConfig(BaseModel):
    """重排器配置：api（SiliconFlow bge-reranker，免费）/ local（fastembed）/ none。"""

    model_config = ConfigDict(frozen=True)

    backend: Literal["api", "local", "none"] = "none"
    model: str = "BAAI/bge-reranker-v2-m3"
    base_url: str | None = None
    api_key_env: str | None = None
    top_n: int = 3

    @property
    def api_key(self) -> str | None:
        return _secret_from_env(self.api_key_env)


class AnswerConfig(BaseModel):
    """回答呈现层配置。"""

    model_config = ConfigDict(frozen=True)

    # 英文语料块命中时回答末尾附"原文+中文翻译"对照（2026-09-10，#8）。
    # 默认关：翻译依赖真实 LLM，offline/mock 档保持零额外调用；api/local
    # 档在各自 profile yaml 里开启。翻译是呈现层增强，任何失败都退化为
    # "无对照块"，问答主链与引用协议不受影响（见 ask._maybe_bilingual_block）。
    bilingual: bool = False


class LLMConfig(BaseModel):
    """生成端大模型配置。backend: api / local（Ollama，OpenAI 兼容）/ mock。"""

    model_config = ConfigDict(frozen=True)

    backend: Literal["api", "local", "mock"] = "mock"
    base_url: str | None = None
    api_key_env: str | None = None
    model: str = "mock-zh-1"
    temperature: float = 0.1
    max_tokens: int = 1024
    timeout_seconds: float = 60.0

    @property
    def api_key(self) -> str | None:
        return _secret_from_env(self.api_key_env)


class VisionConfig(BaseModel):
    """视觉模型配置（图片 → 文本，M6 ② 拍照转笔记）。

    backend: api / local（OpenAI 兼容多模态端点）/ none（未接入，默认）。
    **默认 none 是刻意的**：老的 --config 文件没有这一段时行为不漂移，offline
    档也天然守住"零外部调用"承诺（见 ADR-0027）。api 档默认接 SiliconFlow 的
    Qwen2.5-VL（与检索侧共用 SILICONFLOW_API_KEY）。

    与 LLMConfig 分开而不是复用：两者的"模型"不是一回事（一个收文本、一个
    收图片），混在一个段里会让"生成用 DeepSeek + 识图用 SiliconFlow"这种
    真实组合无处落脚（用户日常 local 档 + 云端识图，也是这个形状）。
    """

    model_config = ConfigDict(frozen=True)

    backend: Literal["api", "local", "none"] = "none"
    base_url: str | None = None
    api_key_env: str | None = None
    model: str = ""
    max_tokens: int = 2048
    # 识图比文本生成慢（大图编码 + 视觉 token），单独给一个超时；
    # 借 ask 的默认会在手机拍的大图上一片超时
    timeout_seconds: float = 60.0

    @property
    def api_key(self) -> str | None:
        return _secret_from_env(self.api_key_env)


class EmbeddingConfig(BaseModel):
    """词向量配置：api（SiliconFlow bge-m3 免费）/ local（fastembed bge-small-zh）/ none。"""

    model_config = ConfigDict(frozen=True)

    backend: Literal["api", "local", "none"] = "none"
    model: str = "BAAI/bge-m3"
    base_url: str | None = None
    api_key_env: str | None = None
    query_prefix: str = ""

    @property
    def api_key(self) -> str | None:
        return _secret_from_env(self.api_key_env)


class JudgeConfig(BaseModel):
    """评测裁判配置：建议与生成端不同厂商，抵消 LLM-as-Judge 自偏好偏差。"""

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    backend: Literal["api", "local"] = "api"
    base_url: str | None = None
    api_key_env: str | None = None
    model: str = "Qwen/Qwen2.5-72B-Instruct"
    temperature: float = 0.0
    template_version: str = "2026-09-01"

    @property
    def api_key(self) -> str | None:
        return _secret_from_env(self.api_key_env)


class WebConfig(BaseModel):
    """Web 服务参数。"""

    model_config = ConfigDict(frozen=True)

    host: str = "127.0.0.1"
    port: int = 8000
    # 单文件上传上限（MB）：上传端点 seek 量真实字节数判超限（413）、
    # 流式拷贝落盘，判定与拷贝都不整载入内存——上限大小只影响解析与
    # 嵌入耗时，不再吃内存（500MB ≈ 单本扫描书；更大档案建议拆分或走
    # CLI ingest）。profiles yaml 可覆写 web 段改默认值。
    upload_max_mb: int = 500


# ---------------------------------------------------------------------------
# 顶层 Settings
# ---------------------------------------------------------------------------


class Settings(BaseModel):
    """运行时总配置。由 load_settings() 依据 profile 构造，禁止手工实例化。"""

    profile: Literal["api", "local", "offline"]
    config_path: Path | None = None
    # 可写数据根（打包后自动落到用户目录，见 user_data_root 的说明）
    data_dir: Path = Field(default_factory=user_data_root)

    chunking: ChunkingConfig = ChunkingConfig()
    retrieval: RetrievalConfig = RetrievalConfig()
    reranker: RerankerConfig = RerankerConfig()
    llm: LLMConfig = LLMConfig()
    vision: VisionConfig = VisionConfig()
    embedding: EmbeddingConfig = EmbeddingConfig()
    judge: JudgeConfig = JudgeConfig()
    web: WebConfig = WebConfig()
    answer: AnswerConfig = AnswerConfig()

    # ---- 派生路径 --------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "mikasa.db"

    @property
    def index_dir(self) -> Path:
        return self.data_dir / "indexes"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def note_media_dir(self) -> Path:
        """笔记原图目录（M6 ② 拍照转笔记）：下面按**笔记 key** 分子目录。

        放在 uploads 之外是刻意的：uploads 是 ingest 的输入（reindex 会扫它
        重建文档），图片不是可解析文档，混进去会被当成待入库文件。
        """
        return self.data_dir / "note-media"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure_dirs(self) -> None:
        """创建运行所需目录（幂等）。"""
        for path in (
            self.data_dir,
            self.index_dir,
            self.uploads_dir,
            self.note_media_dir,
            self.log_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 加载入口
# ---------------------------------------------------------------------------


def _secret_from_env(env_name: str | None) -> str | None:
    """从环境读取密钥（None/空串视为未配置）。"""
    if not env_name:
        return None
    return os.environ.get(env_name) or None


# ---------------------------------------------------------------------------
# 用户配置覆盖层与密钥写回（Web 设置面板的落盘目标，见 ADR-0018）
# ---------------------------------------------------------------------------

# 覆盖层头注释：给任何打开这个文件的人（包括未来的我）交代它是谁写的
_OVERLAY_HEADER = (
    "# Mikasa 用户配置覆盖层（Web 设置面板自动生成，也可手工编辑）\n"
    "# 只覆盖 profile 文件里同名的字段；API 密钥不在这里，在数据目录的 .env。\n"
)

# .env 头注释：说明这个文件的性质（本机、明文、不入库）
_ENV_HEADER = (
    "# Mikasa 本地密钥文件（Web 设置面板写入，也可手工编辑）\n"
    "# 明文只存本机，不入库、不上传；密钥按 ADR-0002 只经环境变量注入。\n"
    "# 同名变量若已在系统里 export 过，系统的那份优先（python-dotenv 默认不覆盖）。\n"
)


def user_config_path() -> Path:
    """用户配置覆盖层的路径（设置面板「保存」的写入目标）。

    放在数据目录而不是随包资源根：打包后 resource_root() 是只读解包目录，
    用户既找不到也改不了；user_data_root() 才是他可写的家
    （%LOCALAPPDATA%\\Mikasa）。开发时 = 仓库 data/ 下，与 .env 的查找链
    共用同一套"数据根"心智模型。
    """
    return user_data_root() / "config.yaml"


def user_env_path() -> Path:
    """密钥 .env 的写入目标：读取链的第一候选。

    MIKASA_ENV_FILE 显式指定时写它（读取链也以它为先，读写永远一致）；
    否则写数据目录下的 .env。这里刻意**不返回"第一个已存在的文件"**——
    打包后第一个存在的可能是随包只读资源里的 .env，写它会直接失败。
    """
    explicit = os.environ.get("MIKASA_ENV_FILE")
    if explicit:
        return Path(explicit).expanduser()
    return user_data_root() / ".env"


def write_section_overlay(section: str, fields: dict[str, Any]) -> Path:
    """把某个配置段写进用户配置覆盖层（tmp + 原子替换，杜绝写一半的坏文件）。

    只写传入的字段：覆盖层是 _deep_merge 叠加在 profile 之上的，没写的键
    继续取 profile 的值。面板只开放 llm 与 vision 两段——embedding/reranker/
    judge 仍随档位（换 embedding 模型会因向量维度不同要求全量重索引，ADR-0014）。

    **必须是"读-改-写"**：覆盖层是**一份** YAML，早先的 llm 专用实现直接覆写
    整份文件。泛化时若照抄这个写法，"保存视觉段"就会把已经配好的 llm 段静默
    抹掉——用户看到的是"刚配好的生成模型自己变回去了"，而没有任何报错。
    所以先读现有内容、只替换目标段，头注释随写保留。
    """
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"用户配置读取失败（{path}）：{exc}") from exc
        if isinstance(loaded, dict):
            existing = loaded
    existing[section] = fields
    body = yaml.safe_dump(existing, allow_unicode=True, sort_keys=False)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(_OVERLAY_HEADER + body, encoding="utf-8")
    os.replace(tmp, path)  # 同目录原子替换：读到的永远是完整 YAML
    return path


def write_llm_overlay(fields: dict[str, Any]) -> Path:
    """把 llm 段写进用户配置覆盖层（write_section_overlay 的薄包装）。"""
    return write_section_overlay("llm", fields)


def write_api_key(env_name: str, value: str) -> Path:
    """把密钥写进 .env（新建时带头注释；已存在则就地更新该键）。

    用 python-dotenv 的 set_key 负责转义与保留其它行——手写字符串拼接
    迟早会在含引号/井号/空格的密钥上翻车。set_key 遇到文件不存在的行为
    跨版本不一致（旧版本直接抛错），所以先显式建好带注释的空文件。
    """
    try:
        from dotenv import set_key
    except ImportError as exc:  # 读取链早已依赖它，这里只是兜底
        raise ConfigError("python-dotenv 未安装：无法保存密钥，请先安装依赖") from exc
    path = user_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        path.write_text(_ENV_HEADER, encoding="utf-8")
    set_key(str(path), env_name, value, quote_mode="always")
    return path


def clear_api_key(env_name: str) -> Path:
    """从 .env 里删除某个密钥行（清空密钥 = 删行，而不是留空值）。

    留 `KEY=` 空值行与"未配置"在下游判空逻辑里表现不一致；直接删行语义
    最干净。文件不存在时静默返回——"清除一个本来就没有的东西"不是错误。
    """
    path = user_env_path()
    if not path.is_file():
        return path
    try:
        from dotenv import unset_key
    except ImportError as exc:
        raise ConfigError("python-dotenv 未安装：无法清除密钥，请先安装依赖") from exc
    unset_key(str(path), env_name, quote_mode="always")
    return path


def _load_user_overlay() -> dict[str, Any]:
    """读用户覆盖层（不存在返回空 dict）。

    只在**基底是 profile 文件**时参与合并（调用方判断）：--config 与
    config.yaml 是"完整替换"语义（README、E2E 工具、迁移演练都依赖它），
    再叠一层的话，显式指定的配置就不再是显式配置了。
    覆盖层里的 `profile:` 键一律忽略——档位决定 embedding/检索整套，
    允许面板改档会让新档的 embedding 与旧档建的索引对不上（ADR-0014），
    档位选择权只留给 --profile / config.yaml。
    """
    path = user_config_path()
    if not path.is_file():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"用户配置读取失败（{path}）：{exc}") from exc
    if raw is None:  # 空文件（比如只有头注释）
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"用户配置必须是键值结构（{path}）")
    raw.pop("profile", None)
    expanded: dict[str, Any] = expand_env_vars(raw)
    return expanded


def _config_lookup(profile: str, config_path: Path | None) -> tuple[Path, str, str | None]:
    """返回 (配置文件路径, 生效 profile, 配置内声明路径)。

    查找顺序：
      1. 显式 --config 参数
      2. 当前目录 config/config.yaml（运行时覆盖层）
      3. 仓库根 config/config.yaml
      4. 仓库根 config/profiles/<profile>.yaml
    """
    if config_path is not None:
        if not config_path.is_file():
            raise ConfigError(f"配置文件不存在：{config_path}")
        return config_path, profile, str(config_path)

    # cwd 只在开发时参与查找：打包后双击 exe 的 cwd 可能是 System32 之类，
    # 命中无关 config.yaml 还会按它的 profile 字段静默改档（打包审查发现）
    candidates = [REPO_ROOT / "config" / "config.yaml"]
    if not is_frozen():
        candidates.insert(0, Path.cwd() / "config" / "config.yaml")
    for cwd_file in candidates:
        if cwd_file.is_file():
            declared = _declared_profile(cwd_file)
            return cwd_file, declared or profile, str(cwd_file)

    profile_file = REPO_ROOT / "config" / "profiles" / f"{profile}.yaml"
    if not profile_file.is_file():
        raise ConfigError(
            f"找不到 profile 配置文件：{profile_file}（合法 profile：{'/'.join(VALID_PROFILES)}）"
        )
    return profile_file, profile, None


def _declared_profile(path: Path) -> str | None:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    declared = data.get("profile")
    return declared if isinstance(declared, str) else None


def format_validation_error(exc: ValidationError) -> str:
    """pydantic 校验错误 → 一行可读文本（最多列 5 条，防刷屏）。"""
    parts: list[str] = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(x) for x in err.get("loc", ()))
        parts.append(f"{loc or '(根)'}: {err.get('msg', '')}")
    return "；".join(parts)


def load_settings(
    profile: str = "api",
    config_path: Path | None = None,
    *,
    data_dir: Path | None = None,
) -> Settings:
    """加载指定 profile 的配置并合并环境变量。

    参数：
        profile:   api / local / offline
        config_path: 显式指定 YAML（绕过默认查找链）
        data_dir:  覆盖数据目录（测试隔离用）
    """
    if profile not in VALID_PROFILES:
        raise ConfigError(f"未知 profile：{profile}（合法值：{'/'.join(VALID_PROFILES)}）")

    path, active_profile, declared_path = _config_lookup(profile, config_path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"配置文件读取失败（{path}）：{exc}") from exc
    raw = expand_env_vars(raw)

    merged = _deep_merge(_default_section_dict(), raw)
    if declared_path is None:
        # 基底是 profile 文件 → 叠加用户覆盖层（设置面板写的那份）；
        # --config / config.yaml 走"完整替换"，不叠（见 _load_user_overlay）
        merged = _deep_merge(merged, _load_user_overlay())
    merged["profile"] = active_profile
    if declared_path is not None:
        merged["config_path"] = declared_path

    try:
        settings = Settings(**merged)
    except ValidationError as exc:
        # pydantic 的报错面向开发者（英文 + loc/type 结构），CLI/Web 只接得住
        # ZhiwenError —— 不翻译就变成裸 traceback 或 500（2026-09-11 修复）
        raise ConfigError(f"配置项非法（{path}）：{format_validation_error(exc)}") from exc
    if data_dir is not None:
        settings = settings.model_copy(update={"data_dir": data_dir})
    if isinstance(settings.chunking, ChunkingConfig):
        settings.chunking.validate_self()
    return settings


def _default_section_dict() -> dict[str, Any]:
    """各功能块的默认值；profile 文件只需覆盖差异字段。"""
    return {
        "profile": "offline",
        "chunking": ChunkingConfig().model_dump(),
        "retrieval": RetrievalConfig().model_dump(),
        "reranker": RerankerConfig().model_dump(),
        "llm": LLMConfig().model_dump(),
        "vision": VisionConfig().model_dump(),
        "embedding": EmbeddingConfig().model_dump(),
        "judge": JudgeConfig().model_dump(),
        "web": WebConfig().model_dump(),
        "answer": AnswerConfig().model_dump(),
    }


def _dotenv_candidates() -> list[Path]:
    """`.env` 的查找顺序：显式指定 → 用户数据目录 → 随包资源根 → exe 同级。

    打包后 `REPO_ROOT` 指向解包目录，用户既找不到也改不了那里的 `.env`；
    而数据目录（%LOCALAPPDATA%\\Mikasa）才是他能写的地方。开发时数据根就是
    仓库根/data，因此"仓库根 .env"这条仍然命中，行为不变。
    """
    candidates: list[Path] = []
    explicit = os.environ.get("MIKASA_ENV_FILE")
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(user_data_root() / ".env")
    candidates.append(resource_root() / ".env")
    if is_frozen():
        candidates.append(Path(sys.executable).parent / ".env")  # 绿色版：放 exe 旁边
    return candidates


def load_dotenv_file() -> None:
    """按查找链载入**所有存在**的 `.env`（python-dotenv；不存在的跳过）。

    密钥按设计只经环境变量注入，.env 提供"复制 .env.example 即用"的本地体验。
    不覆盖进程里已显式 export 的同名变量（load_dotenv 默认 override=False）。

    **不再"命中第一个就停"**（2026-09-15 修）：设置面板把新密钥写进数据目录
    的 .env 后，开发模式（数据根 = 仓库 data/）下仓库根 .env 里的**其它**密钥
    会被静默屏蔽——api 档的 embedding/reranker/judge 密钥都在同一份 .env 里，
    一屏蔽就是整条检索链报"未配置 API 密钥"。按链序全部加载则各文件的键
    自然合流；同名键由先加载者胜出（override=False 时后加载不覆盖），
    与旧语义（链序在前者优先）一致。

    保存后热刷新不走这里：设置端点直接写 os.environ（写入/清除它自己管的那
    一个键）。若在这里用 override=True 重载，链序靠后的文件反而会压过靠前的，
    优先级就反了。
    CLI 入口（cli.main）与 Web reload 工厂（web.app.serve_app_factory）
    都调它——两条服务路径的密钥装载行为必须一致。
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv 缺失（老环境未重装）时降级，别让命令崩
        from mikasa.utils.logging import get_logger

        get_logger("config").warning("python-dotenv 未安装：.env 不会自动加载，请显式 export 密钥")
        return

    for path in _dotenv_candidates():
        if path.is_file():
            load_dotenv(path)
