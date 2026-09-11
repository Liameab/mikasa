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
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mikasa.errors import ConfigError

# 包所在位置向上三级即仓库根目录（src/mikasa/config/settings.py -> <repo>）
REPO_ROOT = Path(__file__).resolve().parents[3]

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
    data_dir: Path = REPO_ROOT / "data"

    chunking: ChunkingConfig = ChunkingConfig()
    retrieval: RetrievalConfig = RetrievalConfig()
    reranker: RerankerConfig = RerankerConfig()
    llm: LLMConfig = LLMConfig()
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
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure_dirs(self) -> None:
        """创建运行所需目录（幂等）。"""
        for path in (self.data_dir, self.index_dir, self.uploads_dir, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 加载入口
# ---------------------------------------------------------------------------


def _secret_from_env(env_name: str | None) -> str | None:
    """从环境读取密钥（None/空串视为未配置）。"""
    if not env_name:
        return None
    return os.environ.get(env_name) or None


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

    for cwd_file in (Path.cwd() / "config" / "config.yaml", REPO_ROOT / "config" / "config.yaml"):
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


def _format_validation_error(exc: ValidationError) -> str:
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
    merged["profile"] = active_profile
    if declared_path is not None:
        merged["config_path"] = declared_path

    try:
        settings = Settings(**merged)
    except ValidationError as exc:
        # pydantic 的报错面向开发者（英文 + loc/type 结构），CLI/Web 只接得住
        # ZhiwenError —— 不翻译就变成裸 traceback 或 500（2026-09-11 修复）
        raise ConfigError(f"配置项非法（{path}）：{_format_validation_error(exc)}") from exc
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
        "embedding": EmbeddingConfig().model_dump(),
        "judge": JudgeConfig().model_dump(),
        "web": WebConfig().model_dump(),
        "answer": AnswerConfig().model_dump(),
    }


def load_dotenv_file() -> None:
    """把仓库根目录 .env 载入环境变量（python-dotenv，只在文件存在时生效）。

    密钥按设计只经环境变量注入，.env 提供"复制 .env.example 即用"的本地体验。
    不覆盖进程里已显式 export 的同名变量（load_dotenv 默认 override=False）。
    CLI 入口（cli.main）与 Web reload 工厂（web.app.serve_app_factory）
    都调它——两条服务路径的密钥装载行为必须一致。
    """
    try:
        from dotenv import load_dotenv

        load_dotenv(REPO_ROOT / ".env")
    except ImportError:  # python-dotenv 缺失（老环境未重装）时降级，别让命令崩
        from mikasa.utils.logging import get_logger

        get_logger("config").warning("python-dotenv 未安装：.env 不会自动加载，请显式 export 密钥")
