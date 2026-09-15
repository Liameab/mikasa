"""OpenAlex 检索来源（works API）。

关键事实（2026-09-15 已核实）：
- **2026-02-13 起必须免费 API key**（openalex.org 免费注册）：免费档
  每日 10 万 credits（列表查询 = 10 credits，个人使用绰绰有余）；无 key
  只有小额测试额度，超了回 409/429。key 在**调用时**读环境变量
  `OPENALEX_API_KEY`（设置面板写入 .env 后热生效，见 ADR-0018）。
- 中文检索实测可用（"水库坝" 4455 条）：中文关键词直接命中中文论文。
- 摘要存倒排索引（`abstract_inverted_index`：词 → 出现位置列表），
  需要重建；重建后是小写无标点文本（OpenAlex 不存标点与大小写）。
- 覆盖国内理工核心期刊元数据（有 DOI 的）；CSSCI 社科类 ≈ 0；知网
  独家全文拿不到——UI 与文档如实告知，给 DOI 链接跳转。
- 测试性：base URL 由 `MIKASA_PAPERS_OPENALEX_BASE` 在调用时覆盖，
  单测打桩 `_http_get` 网络缝，永不真联网。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from mikasa.papers.errors import PaperError
from mikasa.papers.sources import PaperResult

# OpenAlex id 形如 "W" + 数字（六位起）：解析与路由双重校验
_OPENALEX_ID_RE = re.compile(r"^W\d{6,}$")

# 只取用到的字段，payload 从 ~50KB 瘦到 ~10KB（额度不变，流量与解析更快）
_SELECT = (
    "id,doi,display_name,publication_year,authorships,primary_location,"
    "abstract_inverted_index,open_access,language"
)

_USER_AGENT = "Mikasa/0.1 (local paper search)"


def _base_url() -> str:
    """API 根（调用时读环境变量：E2E 可整体换成本地假源）。"""
    return os.environ.get("MIKASA_PAPERS_OPENALEX_BASE", "https://api.openalex.org")


def _openalex_key() -> str | None:
    """免费 key（调用时读取——设置面板写 .env 后无需重启）。"""
    return os.environ.get("OPENALEX_API_KEY") or None


def validate_id(paper_id: str) -> bool:
    return bool(_OPENALEX_ID_RE.fullmatch(paper_id))


def reconstruct_abstract(index: dict[str, list[int]] | None) -> str:
    """倒排索引 → 正文：每个词挂回它出现的所有位置，按位置排序拼句。

    OpenAlex 的摘要索引没有标点与大小写信息，重建为小写无标点文本
    （业界通用做法）；index 缺失/为空 → ""。位置互不重复是格式保证。
    """
    if not index:
        return ""
    positions: dict[int, str] = {}
    for word, idxs in index.items():
        for i in idxs:
            positions[i] = word
    return " ".join(positions[i] for i in sorted(positions))


def _http_get(url: str, timeout: float) -> bytes:
    """唯一网络缝：GET 并读全部字节；错误翻译成可回显中文。

    429/409/403 分有 key / 无 key 两种文案：无 key 时用户能做的事是
    注册 + 粘贴（面板有入口），有 key 时只能等额度重置。
    """
    key = _openalex_key()
    if key:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}api_key={urllib.parse.quote(key)}"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (429, 409, 403):
            if not key:
                raise PaperError(
                    "OpenAlex 需要免费 API 密钥：到 openalex.org 免费注册后，"
                    "在「OpenAlex 密钥」里粘贴保存即可（每日 10 万积分额度）"
                ) from exc
            raise PaperError(
                f"OpenAlex 请求被限流或额度用尽（HTTP {exc.code}），请稍后重试"
            ) from exc
        if exc.code == 404:
            raise PaperError("OpenAlex 未找到该论文（id 可能已变更）") from exc
        raise PaperError(f"OpenAlex 请求失败（HTTP {exc.code}）") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise PaperError("无法连接 OpenAlex（网络不可达或超时），请稍后重试") from exc


def _load_json(data: bytes) -> dict:
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PaperError("OpenAlex 返回内容无法解析（响应不是有效 JSON）") from exc
    if not isinstance(parsed, dict):
        raise PaperError("OpenAlex 返回内容无法解析（响应结构异常）")
    return parsed


def _normalize(work: dict) -> PaperResult:
    """OpenAlex work → PaperResult（缺什么补什么，不因单条元数据缺失而失败）。"""
    work_id = str(work.get("id") or "").rstrip("/")
    short_id = work_id.rsplit("/", 1)[-1]
    if not validate_id(short_id):
        raise PaperError(f"OpenAlex 返回了无法识别的论文编号：{work_id}")
    doi = str(work.get("doi") or "")
    if doi.lower().startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/") :]
    open_access = work.get("open_access") or {}
    is_oa = bool(open_access.get("is_oa"))
    oa_url = open_access.get("oa_url")
    venue = ""
    primary = work.get("primary_location") or {}
    source = primary.get("source") or {}
    if isinstance(source, dict):
        venue = str(source.get("display_name") or "")
    authors = tuple(
        str(a.get("author", {}).get("display_name"))
        for a in (work.get("authorships") or [])
        if isinstance(a, dict) and a.get("author", {}).get("display_name")
    )
    return PaperResult(
        source="openalex",
        id=short_id,
        title=str(work.get("display_name") or "(无标题)"),
        authors=authors,
        year=work.get("publication_year"),
        venue=venue,
        abstract=reconstruct_abstract(work.get("abstract_inverted_index")),
        doi=doi,
        pdf_url=str(oa_url) if (is_oa and oa_url) else None,
        landing_url=f"https://doi.org/{doi}"
        if doi
        else work_id or f"https://openalex.org/{short_id}",
        oa=is_oa,
    )


class OpenAlexSource:
    """OpenAlex 来源：search 走 /works 检索，fetch 走 /works/{id} 反查。"""

    name = "openalex"

    def search(self, q: str, start: int, count: int) -> tuple[list[PaperResult], bool]:
        page = start // count + 1
        params = urllib.parse.urlencode(
            {
                "search": q,
                "per-page": count,
                "page": page,
                "sort": "relevance_score:desc",
                "select": _SELECT,
            }
        )
        data = _load_json(_http_get(f"{_base_url()}/works?{params}", timeout=15.0))
        works = data.get("results")
        if not isinstance(works, list):
            raise PaperError("OpenAlex 返回内容无法解析（缺少 results 列表）")
        results = [_normalize(w) for w in works if isinstance(w, dict)]
        return results, len(results) == count

    def fetch(self, paper_id: str) -> PaperResult:
        if not validate_id(paper_id):
            raise PaperError(f"非法的 OpenAlex 论文编号：{paper_id}")
        params = urllib.parse.urlencode({"select": _SELECT})
        work = _load_json(_http_get(f"{_base_url()}/works/{paper_id}?{params}", timeout=15.0))
        return _normalize(work)
