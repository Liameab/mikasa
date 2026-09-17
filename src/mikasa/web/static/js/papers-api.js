/* =========================================================================
   找论文页的 API 客户端：**只有这个模块知道端点的形状**。

   四个端点（见文档 docs/design-decisions.md 的 ADR-0019 / ADR-0020）：
     GET  /api/papers/sources   来源目录 + 能力（前端据此渲染筛选器）
     POST /api/papers/search    {q, sources, offset, limit, filters} →
                                {results[], errors{}, notes{}, has_more}
     POST /api/papers/import    {source, id} → 201/200/409/502（正文即中文消息）
     GET/PUT /api/papers/settings  OpenAlex 密钥（GET 只回布尔，值永不回显）

   检索与导入**刻意不走 apiFetch**（它把非 2xx 一律抛错）：检索要把
   errors（逐源失败）与 notes（能力降级）当正常数据读；导入要按状态码
   分流（201 新入库 / 200 内容重复 / 409 无全文 / 502 下载失败）。
   ========================================================================= */

import { apiFetch } from "./common.js";

/** POST 一个 JSON 请求，返回 {status, ok, body}（body 解析失败为 null）。 */
async function postJson(path, payload) {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await resp.json().catch(() => null);
  return { status: resp.status, ok: resp.ok, body };
}

/**
 * 检索一页。
 * `filters` 里只放用户真的设过的条件（空值不传），服务端按各源能力翻译。
 */
export async function searchPapers({ q, sources, offset, limit, filters }) {
  const { status, ok, body } = await postJson("/api/papers/search", {
    q,
    sources,
    offset,
    limit,
    filters,
  });
  if (!ok) {
    // 502（全部来源失败）与 422（参数非法）都是服务端中文文案
    throw new Error(body?.detail || body?.error?.message || `HTTP ${status}`);
  }
  return body;
}

/** 导入一篇。返回 {ok, status, message}——文案由服务端给，前端只负责展示。 */
export async function importPaper(source, id) {
  const { status, ok, body } = await postJson("/api/papers/import", { source, id });
  return {
    ok,
    status,
    message: body?.detail || body?.message || `HTTP ${status}`,
    document: body?.document || null,
  };
}

/** 来源目录（名字/展示名/能力）。 */
export async function fetchSources() {
  const body = await apiFetch("/api/papers/sources");
  return body.sources || [];
}

/** OpenAlex 密钥：查询只回布尔。 */
export async function fetchKeyStatus() {
  const body = await apiFetch("/api/papers/settings");
  return Boolean(body.has_api_key);
}

/** OpenAlex 密钥：三段语义（null=不动 / ""=清除 / 值=写入）。 */
export async function saveKey(apiKey) {
  const body = await apiFetch("/api/papers/settings", {
    method: "PUT",
    body: JSON.stringify(apiKey === null ? {} : { api_key: apiKey }),
  });
  return Boolean(body.has_api_key);
}
