"""模型配置端点（设置面板的"模型"段，见 ADR-0018）。

四件事：
  GET  /api/settings/model          当前模型配置（**密钥只回有无，绝不回值**）
  PUT  /api/settings/model          保存：写覆盖层 + 密钥写 .env + 热生效
  POST /api/settings/model/test     测试连接（一次性客户端，不落盘不碰状态）
  GET  /api/settings/ollama/models  本机 Ollama 模型列表（模型下拉）

**热生效机制**（PUT）：写文件 → 直接写 os.environ（写入/清除它自己管的
那一个键——load_dotenv 默认不覆盖已存在变量，不写环境就等于"只对新进程
生效"）→ load_settings 重载 → 先换 services.settings 再 rebuild_ask
（rebuild 读 self.settings，顺序反了会用旧配置重建）→ 换 app.state.settings。

v1 只开放 llm 段：embedding 一动就是向量维度变化 → 必须全量重索引
（ADR-0014 红线），reranker/judge 随档位。前端预设与后端白名单一致。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from mikasa.config.settings import (
    LLMConfig,
    Settings,
    clear_api_key,
    format_validation_error,
    load_settings,
    write_api_key,
    write_llm_overlay,
)
from mikasa.errors import ProviderError, ZhiwenError, strip_paths
from mikasa.providers.llm import OpenAICompatLLM
from mikasa.providers.ollama import fetch_ollama_tags
from mikasa.web.deps import get_services, get_settings
from mikasa.web.schemas import ModelSettingsIn, ModelTestIn

router = APIRouter(tags=["settings"])

# 保存动作串行化：写文件 + 重载 + 换服务必须是一个整体，
# 两个并发 PUT 交错会写出"文件与内存配置不是同一份"的窗口。
_APPLY_LOCK = threading.Lock()

# 测试连接也串行：探测用同一个临时环境变量名，两个请求并发会互相摘掉
# 对方的密钥（一个请求的 finally 清掉另一个还在用的值）。
_PROBE_LOCK = threading.Lock()

# 探测用的临时密钥变量名：用完即摘。api 档无密钥的情况下**先预检返回人话**，
# 不让这个名字有机会出现在 ConfigError 文案里（用户从没见过这个变量）。
_TEST_KEY_ENV = "MIKASA_SETTINGS_TEST_KEY"

# api 后端没给 api_key_env 时的兜底存储槽（前端隐藏该字段，预设自带变量名）
_DEFAULT_KEY_ENV = "MIKASA_LLM_API_KEY"


def _model_payload(settings: Settings) -> dict[str, Any]:
    """当前模型配置的展示体。密钥只有 has_api_key 布尔，永不回值。"""
    return {
        "backend": settings.llm.backend,
        "base_url": settings.llm.base_url or "",
        "model": settings.llm.model,
        "api_key_env": settings.llm.api_key_env or "",
        "has_api_key": settings.llm.api_key is not None,
        "profile": settings.profile,
        "embedding_model": settings.embedding.model,
        # locked：配置来自 --config / config.yaml 时面板不可写（PUT 会 400），
        # 前端据此把表单置灰——"能填但保存无效"比直接禁用更气人。
        "locked": settings.config_path is not None,
    }


def _key_env_name(body: ModelSettingsIn) -> str:
    """本次提交使用的密钥变量名（local 后端无槽位 = 空串）。"""
    if body.backend != "api":
        return ""  # local（Ollama）免密钥：没有"密钥槽位"这回事
    return body.api_key_env.strip() or _DEFAULT_KEY_ENV


def _validated_llm_fields(body: ModelSettingsIn) -> dict[str, Any]:
    """校验并组装要写进覆盖层的 llm 字段。

    temperature/max_tokens/timeout 不在面板里，写回时**不带**这三个键——
    覆盖层是增量合并，没写的键继续取 profile 的值（带上的话就把档位
    调优过的数字固化了，将来改档位默认值会被这份旧快照压住）。
    """
    model = body.model.strip()
    base_url = body.base_url.strip()
    if not model:
        raise HTTPException(status_code=422, detail="模型名不能为空")
    if not base_url:
        raise HTTPException(status_code=422, detail="API 地址（base_url）不能为空")
    fields: dict[str, Any] = {
        "backend": body.backend,
        "base_url": base_url,
        "api_key_env": _key_env_name(body),
        "model": model,
    }
    try:
        LLMConfig(**fields)
    except ValidationError as exc:
        # 面板与后端是两条独立入口，类型/长度约束在两边都要拦（中文 422）
        raise HTTPException(status_code=422, detail=format_validation_error(exc)) from exc
    return fields


def _apply_api_key(env_name: str, body: ModelSettingsIn) -> None:
    """密钥三段语义落盘 + 同步进程环境（api_key=None 时整段跳过）。

    只动**本次提交的这一个变量**：换供应商也绝不顺手清旧变量——api 档的
    SILICONFLOW_API_KEY 被 embedding/reranker/judge 共用，误清会连带打挂
    检索侧，且表现为"检索结果变差"这种最难联想到密钥的静默降级。
    """
    if body.api_key is None or not env_name:
        return
    if body.api_key == "":
        clear_api_key(env_name)
        os.environ.pop(env_name, None)  # 进程内的旧值也要摘：否则"清除"只对新进程生效
        return
    write_api_key(env_name, body.api_key)
    os.environ[env_name] = body.api_key  # 立即可用（load_dotenv 不覆盖已存在变量）


@router.get("/api/settings/model")
def get_model_settings(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """当前模型配置（密钥永不下发，只回 has_api_key）。"""
    return _model_payload(settings)


@router.put("/api/settings/model")
def save_model_settings(
    body: ModelSettingsIn,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """保存模型配置并热生效（免重启，见模块头说明）。"""
    if settings.config_path is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"当前配置来自显式配置文件（{settings.config_path}）："
                "设置面板的改动会被它覆盖。请直接编辑该文件，或改用默认 profile 启动。"
            ),
        )

    fields = _validated_llm_fields(body)
    with _APPLY_LOCK:
        write_llm_overlay(fields)
        _apply_api_key(fields["api_key_env"], body)
        new_settings = load_settings(settings.profile, data_dir=settings.data_dir)
        services = get_services(request)
        # 顺序敏感：rebuild_ask 用 self.settings 重建，必须先换 settings
        services.settings = new_settings
        services.rebuild_ask()
        request.app.state.settings = new_settings
    return _model_payload(new_settings)


@router.post("/api/settings/model/test")
def test_model_settings(
    body: ModelTestIn,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """发一个极短请求验证"这组参数能不能通"。

    不落盘、不碰 app.state：测试失败不该影响正在生效的配置。
    响应恒为 200（ok 布尔在体内）——"连不上"是预期内的探测结果而非服务端
    错误，前端一个 pill 直接渲染，不用去解错误壳。
    """
    model = body.model.strip()
    base_url = body.base_url.strip()
    if not model:
        raise HTTPException(status_code=422, detail="模型名不能为空")
    if not base_url:
        raise HTTPException(status_code=422, detail="API 地址（base_url）不能为空")

    env_name = body.api_key_env.strip()
    if body.api_key:
        key: str | None = body.api_key
    elif env_name:
        key = os.environ.get(env_name) or None
    else:
        key = settings.llm.api_key if body.backend == settings.llm.backend else None

    if body.backend == "api" and not key:
        # 预检：不构造 LLMConfig 就给结果，避免临时变量名泄进 ConfigError 文案
        return {"ok": False, "model": model, "error": "未填写 API 密钥（本机 Ollama 不需要密钥）"}

    with _PROBE_LOCK:
        os.environ[_TEST_KEY_ENV] = key or ""
        try:
            cfg = LLMConfig(
                backend=body.backend,
                base_url=base_url,
                api_key_env=_TEST_KEY_ENV,
                model=model,
                temperature=0.0,
                max_tokens=8,  # 探测只要"有回应"，不求内容
                timeout_seconds=20.0,
            )
            llm = OpenAICompatLLM(cfg, max_retries=0)  # 硬上限：一次 20s，不排队重试
            started = time.monotonic()
            result = llm.complete(
                [{"role": "user", "content": "你好"}], temperature=0.0, max_tokens=8
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            return {
                "ok": True,
                "model": model,
                "latency_ms": latency_ms,
                "reply": result.text.strip()[:60],
            }
        except ZhiwenError as exc:
            return {"ok": False, "model": model, "error": strip_paths(str(exc))}
        except Exception as exc:  # noqa: BLE001 - 探测端点：任何异常都是"连不通"的一种
            return {"ok": False, "model": model, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            os.environ.pop(_TEST_KEY_ENV, None)


@router.get("/api/settings/ollama/models")
def ollama_models(base_url: str = "") -> dict[str, Any]:
    """本机 Ollama 已拉取的模型列表（面板模型下拉）。

    base_url 缺省用 local 档的默认端点；探测失败转 ProviderError（502），
    文案就是 doctor 那份"怎么启动 Ollama"的指引。
    """
    target = base_url.strip() or "http://localhost:11434/v1"
    try:
        models = fetch_ollama_tags(target)
    except RuntimeError as exc:
        raise ProviderError(str(exc)) from exc
    return {"models": models}
