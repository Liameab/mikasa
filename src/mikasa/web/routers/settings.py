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
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from mikasa.config.settings import (
    ImageConfig,
    LLMConfig,
    Settings,
    VisionConfig,
    clear_api_key,
    format_validation_error,
    load_settings,
    write_api_key,
    write_llm_overlay,
    write_section_overlay,
)
from mikasa.errors import ProviderError, ZhiwenError, strip_paths
from mikasa.providers.image import OpenAICompatImage
from mikasa.providers.llm import OpenAICompatLLM
from mikasa.providers.ollama import OllamaNativeLLM, fetch_ollama_tags
from mikasa.providers.vision import OpenAICompatVision
from mikasa.utils.net import default_port, reject_reason
from mikasa.web.deps import get_services, get_settings
from mikasa.web.schemas import (
    ImageSettingsIn,
    ImageTestIn,
    ModelSettingsIn,
    ModelTestIn,
    VisionSettingsIn,
    VisionTestIn,
)

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
        # local 档的两个旋钮（api 档为 None）：前端据此回填「思考模式 / 上下文长度」
        "think": settings.llm.think,
        "num_ctx": settings.llm.num_ctx,
        "profile": settings.profile,
        "embedding_model": settings.embedding.model,
        # locked：配置来自 --config / config.yaml 时面板不可写（PUT 会 400），
        # 前端据此把表单置灰——"能填但保存无效"比直接禁用更气人。
        "locked": settings.config_path is not None,
    }


def _guard_base_url(base_url: str) -> None:
    """面板里用户可填的 base_url 要先过出网闸门（SSRF）。

    策略本体在 `utils/net.py`（与论文下载器共用）：**公网或回环放行，内网段拒绝**。
    为什么面板也要这道闸：这些端点任何网页都能触发（`--host 0.0.0.0` 下同网段
    也能），不加闸就等于给了一个"内网探活扫描器 + 云元数据探测"的原语
    （2026-09-20 审查实测）。想指向局域网里的模型服务，请直接编辑配置文件——
    面板这条路刻意只放行公网与本机。
    """
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise HTTPException(status_code=422, detail="API 地址必须是 http(s)://… 的完整地址")
    reason = reject_reason(
        parts.hostname,
        parts.port or default_port(parts.scheme),
        allow_loopback=True,
        # 解析不了不算拒绝：探测端点"恒 200 + ok:false"的契约要保住，
        # 让用户看到"连不上"而不是参数错误（见 utils/net.py 的说明）
        strict_resolution=False,
    )
    if reason is not None:
        raise HTTPException(status_code=422, detail=f"API 地址不被允许：{reason}")


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
    if body.backend == "local":
        # 思考模式与上下文长度是本地档专属：**显式写进覆盖层**（面板即真相）。
        # api 档不写——省得给云端也塞两个没有意义的键。
        fields["think"] = body.think
        fields["num_ctx"] = body.num_ctx
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
    _guard_base_url(base_url)  # SSRF：内网段拒绝（见 helper docstring）

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
                # 带上用户当前的两个本机旋钮：否则本机思考型模型会把 20 秒预算
                # 全花在推理上（实测 qwen3:8b 光推理就 14 秒），探测必然超时
                think=body.think,
                num_ctx=body.num_ctx,
            )
            # 走**与正式问答同一条通道**：local 用 Ollama 原生接口（思考/上下文
            # 两个参数只有它认），api 用 OpenAI 兼容客户端。探测的意义正是"这条路
            # 通不通"，选错通道会给出与真实使用不符的结论。
            llm = (
                OllamaNativeLLM(cfg)
                if body.backend == "local"
                else OpenAICompatLLM(cfg, max_retries=0)  # 硬上限：一次 20s，不排队重试
            )
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
    _guard_base_url(target)  # SSRF：内网段拒绝（见 helper docstring）
    try:
        models = fetch_ollama_tags(target)
    except RuntimeError as exc:
        raise ProviderError(str(exc)) from exc
    return {"models": models}


# ---------------------------------------------------------------------------
# 视觉模型段（M6 ② 拍照/图片转笔记，ADR-0027）
# ---------------------------------------------------------------------------

# 视觉段没给 api_key_env 时的兜底槽位（前端预设自带变量名）
_DEFAULT_VISION_KEY_ENV = "MIKASA_VISION_API_KEY"


def _vision_payload(settings: Settings) -> dict[str, Any]:
    """当前视觉配置的展示体（密钥同样只回有无）。"""
    env = settings.vision.api_key_env or ""
    retrieval_envs = {
        settings.embedding.api_key_env,
        settings.reranker.api_key_env,
        settings.judge.api_key_env,
    }
    return {
        "backend": settings.vision.backend,
        "base_url": settings.vision.base_url or "",
        "model": settings.vision.model,
        "api_key_env": env,
        "has_api_key": settings.vision.api_key is not None,
        # 与检索侧共用密钥槽位时为真：前端据此常显提示（在那个槽位上清空
        # 密钥会连带打挂 embedding/reranker/judge，且故障表现为"检索变差"）
        "key_shared_with_retrieval": bool(env) and env in retrieval_envs,
        "profile": settings.profile,
        "locked": settings.config_path is not None,
    }


def _validated_vision_fields(body: VisionSettingsIn) -> dict[str, Any]:
    """校验并组装要写进覆盖层的 vision 字段（校验先于任何写入）。"""
    if body.backend == "none":
        # 停止使用：这一整段被替换成只有 backend（之前填的地址/模型不保留——
        # 留着一串用不上的字段，下次打开面板会看到"我都关了怎么还有地址"。
        # 以后要接回来，在面板里重新选个预设即可）。
        return {"backend": "none"}
    model = body.model.strip()
    base_url = body.base_url.strip()
    if not model:
        raise HTTPException(status_code=422, detail="视觉模型名不能为空")
    if not base_url:
        raise HTTPException(status_code=422, detail="API 地址（base_url）不能为空")
    env_name = body.api_key_env.strip() or (
        _DEFAULT_VISION_KEY_ENV if body.backend == "api" else ""
    )
    if body.backend == "api" and body.api_key == "":
        # 空串在模型段是"清除"，在这里必须拒绝：桶位可能是检索侧共用的
        raise HTTPException(
            status_code=422,
            detail=(
                f"视觉模型与检索侧共用同一个密钥槽位（{env_name}），"
                "这里不能清空——要清除请到「模型」段操作。"
            ),
        )
    fields: dict[str, Any] = {
        "backend": body.backend,
        "base_url": base_url,
        "api_key_env": env_name,
        "model": model,
    }
    try:
        VisionConfig(**fields)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=format_validation_error(exc)) from exc
    return fields


def _apply_vision_key(env_name: str, body: VisionSettingsIn) -> None:
    """视觉段的密钥**只写不删**（清除是模型段的职责，见 VisionSettingsIn）。"""
    if body.api_key is None or not env_name:
        return
    write_api_key(env_name, body.api_key)
    os.environ[env_name] = body.api_key  # 立即可用（load_dotenv 不覆盖已存在变量）


@router.get("/api/settings/vision")
def get_vision_settings(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """当前视觉模型配置（密钥永不下发，只回 has_api_key）。"""
    return _vision_payload(settings)


@router.put("/api/settings/vision")
def save_vision_settings(
    body: VisionSettingsIn,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """保存视觉配置并热生效（免重启；顺序与模型段同款）。"""
    if settings.config_path is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"当前配置来自显式配置文件（{settings.config_path}）："
                "设置面板的改动会被它覆盖。请直接编辑该文件，或改用默认 profile 启动。"
            ),
        )
    fields = _validated_vision_fields(body)
    with _APPLY_LOCK:
        write_section_overlay("vision", fields)
        _apply_vision_key(fields.get("api_key_env", ""), body)
        new_settings = load_settings(settings.profile, data_dir=settings.data_dir)
        services = get_services(request)
        # 顺序敏感（同模型段）：rebuild_vision 读 self.settings，必须先换它
        services.settings = new_settings
        services.rebuild_vision()
        request.app.state.settings = new_settings
    return _vision_payload(new_settings)


def _probe_png(size: int = 64) -> bytes:
    """现造一张纯白 PNG（stdlib 的 zlib 足够，不为探针引 Pillow）。

    探针必须发**真图片**：纯文本 ping 在纯文本模型上也会成功，那测的是
    "端点通不通"，测不出"这组参数能不能识图"——而用户点"测试连接"想知道
    的正是后者。
    """
    import struct
    import zlib

    raw = b"".join(b"\x00" + b"\xff\xff\xff" * size for _ in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8bit RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@router.post("/api/settings/vision/test")
def test_vision_settings(body: VisionTestIn) -> dict[str, Any]:
    """发一张真图片验证"这组参数能不能识图"。响应恒 200（ok 在体内）。"""
    model = body.model.strip()
    base_url = body.base_url.strip()
    if not model:
        raise HTTPException(status_code=422, detail="视觉模型名不能为空")
    if not base_url:
        raise HTTPException(status_code=422, detail="API 地址（base_url）不能为空")
    _guard_base_url(base_url)  # SSRF：内网段拒绝（见 helper docstring）
    env_name = body.api_key_env.strip()
    if body.api_key:
        key: str | None = body.api_key
    elif env_name:
        key = os.environ.get(env_name) or None
    else:
        key = None
    if body.backend == "api" and not key:
        return {"ok": False, "model": model, "error": "未填写 API 密钥（本机 Ollama 不需要密钥）"}

    with _PROBE_LOCK:
        os.environ[_TEST_KEY_ENV] = key or ""
        try:
            cfg = VisionConfig(
                backend=body.backend,
                base_url=base_url,
                api_key_env=_TEST_KEY_ENV,
                model=model,
                max_tokens=32,  # 探测只要"有回应"，不求内容
                timeout_seconds=30.0,
            )
            vision = OpenAICompatVision(cfg, max_retries=0)  # 硬上限：一次 30s，不排队重试
            started = time.monotonic()
            result = vision.describe(_probe_png(), mime="image/png")
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


# ---------------------------------------------------------------------------
# 图像生成段（文生图，ADR-0031）
# ---------------------------------------------------------------------------

# 图像段没给 api_key_env 时的兜底槽位（前端预设自带变量名）
_DEFAULT_IMAGE_KEY_ENV = "MIKASA_IMAGE_API_KEY"


def _image_payload(settings: Settings) -> dict[str, Any]:
    """当前出图配置的展示体（密钥只回有无，永不下发）。"""
    env = settings.image.api_key_env or ""
    shared_envs = {
        settings.embedding.api_key_env,
        settings.reranker.api_key_env,
        settings.judge.api_key_env,
        settings.vision.api_key_env,
    }
    return {
        "backend": settings.image.backend,
        "base_url": settings.image.base_url or "",
        "model": settings.image.model,
        "size": settings.image.size,
        "api_key_env": env,
        "has_api_key": settings.image.api_key is not None,
        # 与检索/识图共用密钥槽位时为真：前端据此常显提示（在那个槽位上清空
        # 会连带打挂 embedding/reranker/judge/vision）
        "key_shared_with_retrieval": bool(env) and env in shared_envs,
        "profile": settings.profile,
        "locked": settings.config_path is not None,
    }


def _validated_image_fields(body: ImageSettingsIn) -> dict[str, Any]:
    """校验并组装要写进覆盖层的 image 字段（校验先于任何写入）。"""
    if body.backend == "none":
        # 停止使用出图：整段塌缩成只有 backend（理由同视觉段：留一串用不上的
        # 字段，下次打开面板会看到"我都关了怎么还有地址"）
        return {"backend": "none"}
    model = body.model.strip()
    base_url = body.base_url.strip()
    if not model:
        raise HTTPException(status_code=422, detail="出图模型名不能为空")
    if not base_url:
        raise HTTPException(status_code=422, detail="API 地址（base_url）不能为空")
    env_name = body.api_key_env.strip() or _DEFAULT_IMAGE_KEY_ENV
    if body.api_key == "":
        raise HTTPException(
            status_code=422,
            detail=(
                f"图像生成与检索/识图共用同一个密钥槽位（{env_name}），"
                "这里不能清空——要清除请到「模型」段操作。"
            ),
        )
    size = body.size.strip() or "1024x1024"
    # 形状校验：SiliconFlow 要 `宽x高`，写错了上游回的是"参数错误"，
    # 而那个报错不会告诉用户该写什么
    parts = size.lower().split("x")
    if len(parts) != 2 or not all(p.isdigit() and 64 <= int(p) <= 4096 for p in parts):
        raise HTTPException(
            status_code=422, detail=f"图片尺寸要写成「宽x高」（如 1328x1328），收到：{size}"
        )
    fields: dict[str, Any] = {
        "backend": body.backend,
        "base_url": base_url,
        "api_key_env": env_name,
        "model": model,
        "size": size,
    }
    try:
        ImageConfig(**fields)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=format_validation_error(exc)) from exc
    return fields


def _apply_image_key(env_name: str, body: ImageSettingsIn) -> None:
    """图像段的密钥**只写不删**（清除是模型段的职责，见 ImageSettingsIn）。"""
    if body.api_key is None or not env_name:
        return
    write_api_key(env_name, body.api_key)
    os.environ[env_name] = body.api_key  # 立即可用（load_dotenv 不覆盖已存在变量）


@router.get("/api/settings/image")
def get_image_settings(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """当前图像生成配置（密钥永不下发，只回 has_api_key）。"""
    return _image_payload(settings)


@router.put("/api/settings/image")
def save_image_settings(
    body: ImageSettingsIn,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """保存出图配置并热生效（免重启；顺序与模型段同款）。"""
    if settings.config_path is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"当前配置来自显式配置文件（{settings.config_path}）："
                "设置面板的改动会被它覆盖。请直接编辑该文件，或改用默认 profile 启动。"
            ),
        )
    fields = _validated_image_fields(body)
    with _APPLY_LOCK:
        write_section_overlay("image", fields)
        _apply_image_key(fields.get("api_key_env", ""), body)
        new_settings = load_settings(settings.profile, data_dir=settings.data_dir)
        services = get_services(request)
        # 顺序敏感（同模型段）：rebuild_image 读 self.settings，必须先换它
        services.settings = new_settings
        services.rebuild_image()
        request.app.state.settings = new_settings
    return _image_payload(new_settings)


@router.post("/api/settings/image/test")
def test_image_settings(body: ImageTestIn) -> dict[str, Any]:
    """测出图连接：**不生成图片**（按张计费，不适合当探针），只读 /models。

    响应恒 200（ok 在体内），与其余探测端点同一契约。
    """
    model = body.model.strip()
    base_url = body.base_url.strip()
    if not model:
        raise HTTPException(status_code=422, detail="出图模型名不能为空")
    if not base_url:
        raise HTTPException(status_code=422, detail="API 地址（base_url）不能为空")
    _guard_base_url(base_url)  # SSRF：内网段拒绝（见 helper docstring）
    env_name = body.api_key_env.strip()
    if body.api_key:
        key: str | None = body.api_key
    elif env_name:
        key = os.environ.get(env_name) or None
    else:
        key = None
    if not key:
        return {"ok": False, "model": model, "error": "未填写 API 密钥"}

    with _PROBE_LOCK:
        os.environ[_TEST_KEY_ENV] = key
        try:
            cfg = ImageConfig(
                backend="api",
                base_url=base_url,
                api_key_env=_TEST_KEY_ENV,
                model=model,
                timeout_seconds=20.0,
            )
            started = time.monotonic()
            names = OpenAICompatImage(cfg).list_models()
            latency_ms = int((time.monotonic() - started) * 1000)
            if not names:
                return {
                    "ok": True,
                    "model": model,
                    "latency_ms": latency_ms,
                    "error": "地址与密钥可用；该服务没有 /models 列表，"
                    "模型名是否有效要等真出图才知道",
                }
            present = model in names
            return {
                "ok": True,
                "model": model,
                "latency_ms": latency_ms,
                "model_available": present,
                "error": ""
                if present
                else f"连通正常，但模型列表里没有「{model}」（可能名字写错）",
            }
        except ZhiwenError as exc:
            return {"ok": False, "model": model, "error": strip_paths(str(exc))}
        except Exception as exc:  # noqa: BLE001 - 探测端点：任何异常都是"连不通"的一种
            return {"ok": False, "model": model, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            os.environ.pop(_TEST_KEY_ENV, None)
