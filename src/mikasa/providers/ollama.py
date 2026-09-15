"""Ollama 原生 API 帮手（模型列表探测）。

与 providers/llm.py 的分工：问答走的是 Ollama 的 **OpenAI 兼容**端点
（`{base_url}/v1/chat/completions`，配置里 base_url 就带 /v1）；而"本机有
哪些模型"只有**原生** API 能答（`GET {根}/api/tags`），两套端点不同根。

这里原本住在 cli/__init__.py（doctor 用）；Web 设置面板也要列模型，
按"被 web 依赖的代码不能住在 cli 里"的口径搬来 providers——
cli 侧保留同名下划线别名，既有 monkeypatch 单测零改动。
"""

from __future__ import annotations


def ollama_api_root(base_url: str) -> str:
    """OpenAI 兼容 base_url（…/v1）→ Ollama 原生 API 根（…/api）。

    单测关注点：/v1 去除、容忍尾斜杠、非 /v1 结尾直接追加。
    """
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    return f"{root}/api"


def fetch_ollama_tags(base_url: str) -> list[str]:
    """GET {根}/api/tags → 本机已拉取模型名列表。

    失败抛 RuntimeError：文案带可执行下一步——Windows 下 Ollama 若只绑定
    IPv6 回环（localhost→::1 连不上），给 OLLAMA_BASE_URL 逃生口指引
    （http://127.0.0.1:11434/v1）。3s 超时：本地服务探测不等网络。
    """
    import json
    import urllib.error
    import urllib.request

    url = f"{ollama_api_root(base_url)}/tags"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        # 连接拒绝/域名解析失败等（HTTPError 是 URLError 子类，一并覆盖）
        raise RuntimeError(
            f"Ollama 服务不可达（{url}）：{exc.reason}。\n"
            "  请先启动 Ollama（退出系统托盘图标后重启应用）再试；若 Windows 下\n"
            "  localhost 连不上（服务仅绑 IPv6 回环），可设环境变量\n"
            "  OLLAMA_BASE_URL=http://127.0.0.1:11434/v1 后重跑"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - 读超时/HTTP/JSON 解析等统一翻译为可执行文案
        raise RuntimeError(f"Ollama 服务响应异常（{url}）：{exc}") from exc
    return [str(model["name"]) for model in data.get("models", [])]
