/* =========================================================================
   模型设置（设置面板「模型」段）：预设 / 自定义 → 服务端保存并热生效。

   与 settings.js 的分工：那一份是 localStorage 的浏览器偏好（昵称/外观），
   这一份走 /api/settings/*，落到服务端数据目录（config.yaml 覆盖层 + .env）。

   三条关键约定：
   1. **密钥永不回显**：GET 只回 has_api_key。输入框平时是空的，只有用户
      真的输入过（keyDirty）才随保存提交——否则一次普通的"改模型名"会把
      空值当"清除密钥"，顺手把已存密钥删掉。
   2. **切来源只填表不保存**：填好地址/模型/密钥再点「保存并生效」，避免
      点了预设就把配置切到半成品（如密钥还没填就开始计费）。
   3. **只写当前来源的密钥槽**：服务端也一样——api 档的 SILICONFLOW_API_KEY
      被检索侧（embedding/reranker/judge）共用，谁的槽归谁管。

   交互纪律：全部 addEventListener，无内联 onclick；动态文本走 el() 文本节点。
   ========================================================================= */

import { $, apiFetch, el, toast } from "./common.js";

/** 预设：与后端 _DEFAULT_KEY_ENV 的兜底槽保持一致（MIKASA_LLM_API_KEY）。 */
const PRESETS = {
  ollama: {
    backend: "local",
    baseUrl: "http://localhost:11434/v1",
    model: "qwen3:8b",
    keyEnv: "",
    hint: "本机 Ollama：免费、离线、不需要密钥。首次使用先在 Ollama 里拉好模型。",
  },
  deepseek: {
    backend: "api",
    baseUrl: "https://api.deepseek.com",
    model: "deepseek-chat",
    keyEnv: "DEEPSEEK_API_KEY",
    hint: "DeepSeek 官方接口：性价比高；密钥在平台「API keys」页创建。",
  },
  siliconflow: {
    backend: "api",
    baseUrl: "https://api.siliconflow.cn/v1",
    model: "Qwen/Qwen2.5-7B-Instruct",
    keyEnv: "SILICONFLOW_API_KEY",
    hint: "SiliconFlow：有免费模型（Qwen2.5-7B 等），注册即有额度——不想装本地模型可先用它。",
  },
  custom: {
    backend: "api",
    baseUrl: "",
    model: "",
    keyEnv: "MIKASA_LLM_API_KEY",
    hint: "任何 OpenAI 兼容服务：地址填到 /v1、模型名照服务商文档抄、密钥粘贴即可。",
  },
};

// 当前服务端配置（GET 的响应）与密钥脏标记
let current = null;
let keyDirty = false;
let activePreset = null;

/** 服务端当前的密钥槽（api 档）；local/mock 无槽位。 */
function serverKeyEnv() {
  if (!current) return "";
  return current.api_key_env || "";
}

function setResult(text, kind = "") {
  const node = $("#s-test-result");
  node.textContent = text || "";
  node.className = `s-test-result${kind ? " " + kind : ""}`;
}

function markChips() {
  document.querySelectorAll("#s-provider .s-chip").forEach((chip) => {
    chip.classList.toggle("on", chip.dataset.preset === activePreset);
  });
}

/** 由当前字段反推预设（地址/后端/密钥槽都对得上才算；对不上 = 自定义）。 */
function detectPreset() {
  const backend = current ? current.backend : "";
  const baseUrl = ($("#s-base-url").value || "").trim().replace(/\/+$/, "");
  const keyEnv = serverKeyEnv();
  for (const [name, preset] of Object.entries(PRESETS)) {
    if (name === "custom") continue;
    const sameBackend = preset.backend === backend;
    const sameUrl = preset.baseUrl.replace(/\/+$/, "") === baseUrl;
    const sameEnv = (preset.keyEnv || "") === keyEnv;
    if (sameBackend && sameUrl && sameEnv) return name;
  }
  return "custom";
}

/** 密钥行的显隐：本机 Ollama 没有"密钥"这回事，整行藏掉省得用户困惑。 */
function syncKeyRow() {
  const isLocal = activePreset === "ollama";
  $("#s-api-key").closest(".s-row").classList.toggle("hidden", isLocal);
  $("#s-models-refresh").classList.toggle("hidden", !isLocal);
}

function applyPreset(name) {
  const preset = PRESETS[name];
  if (!preset) return;
  activePreset = name;
  $("#s-base-url").value = preset.baseUrl;
  $("#s-model").value = preset.model;
  keyDirty = false; // 切来源 = 换密钥槽，之前输入的内容不再适用于新来源
  $("#s-api-key").value = "";
  $("#s-model-hint").textContent = preset.hint;
  syncKeyRow();
  markChips();
  syncKeyPlaceholder();
  setResult("");
}

/** 已存密钥的占位提示（值本身永不下发，输入框只能靠 placeholder 表达状态）。 */
function syncKeyPlaceholder() {
  const has = !!(current && current.has_api_key);
  const input = $("#s-api-key");
  if (keyDirty && input.value) {
    input.placeholder = has ? "已保存（保存后替换为新密钥）" : "保存后写入";
  } else {
    input.placeholder = has ? "已保存 · 输入新密钥可更换，清空后保存则删除" : "粘贴密钥";
  }
}

/** 拉取服务端当前配置并回填表单（打开面板时调用，保存后再调一次）。 */
export async function refreshModelSettings() {
  try {
    current = await apiFetch("/api/settings/model");
  } catch (err) {
    setResult(`读取配置失败：${err.message}`, "error");
    return;
  }
  keyDirty = false;
  $("#s-api-key").value = "";
  $("#s-base-url").value = current.base_url || "";
  $("#s-model").value = current.model || "";
  const hint = $("#s-model-hint");
  if (current.backend === "mock") {
    hint.textContent = "当前是离线体验档（mock，不调用真实模型）：选一个来源，保存后即可问答。";
  } else {
    hint.textContent =
      `当前档位 ${current.profile}；检索仍用向量模型 ${current.embedding_model}` +
      "（改它需要重建索引，不在本面板）。";
  }
  activePreset = detectPreset();
  syncKeyRow();
  markChips();
  syncKeyPlaceholder();
  setResult("");

  const locked = !!current.locked;
  for (const id of ["#s-base-url", "#s-model", "#s-api-key", "#s-save-btn", "#s-test-btn"]) {
    $(id).disabled = locked;
  }
  document.querySelectorAll("#s-provider .s-chip").forEach((chip) => {
    chip.disabled = locked;
  });
  if (locked) setResult("配置来自显式配置文件，面板只读", "error");
}

/** 组装保存/测试共用的字段（api_key 只在用户改过时才带上）。 */
function collectFields() {
  const preset = PRESETS[activePreset] || PRESETS.custom;
  const body = {
    backend: preset.backend,
    base_url: ($("#s-base-url").value || "").trim(),
    model: ($("#s-model").value || "").trim(),
    api_key_env: preset.keyEnv,
  };
  if (keyDirty) body.api_key = $("#s-api-key").value;
  return body;
}

async function testConnection() {
  const body = collectFields();
  setResult("测试中…", "muted");
  try {
    const res = await apiFetch("/api/settings/model/test", {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (res.ok) setResult(`已连通 · ${res.latency_ms}ms`, "ok");
    else setResult(res.error || "连接失败", "error");
  } catch (err) {
    setResult(`测试失败：${err.message}`, "error");
  }
}

async function saveSettings() {
  const body = collectFields();
  try {
    const saved = await apiFetch("/api/settings/model", {
      method: "PUT",
      body: JSON.stringify(body),
    });
    current = saved;
    keyDirty = false;
    $("#s-api-key").value = "";
    syncKeyPlaceholder();
    toast("模型设置已保存并生效", "ok");
    setResult("");
    activePreset = detectPreset();
    markChips();
  } catch (err) {
    setResult(`保存失败：${err.message}`, "error");
  }
}

async function loadOllamaModels() {
  const base = ($("#s-base-url").value || "").trim() || "http://localhost:11434/v1";
  setResult("读取本机模型…", "muted");
  try {
    const res = await apiFetch(`/api/settings/ollama/models?base_url=${encodeURIComponent(base)}`);
    const list = $("#s-model-options");
    list.replaceChildren(...res.models.map((name) => el("option", { value: name })));
    setResult(res.models.length ? `本机 ${res.models.length} 个模型，点模型名可从下拉选` : "本机还没有模型", "ok");
  } catch (err) {
    setResult(err.message, "error");
  }
}

/** 装配「模型」段。问答页启动时调用一次（settings.js 转调）。 */
export function initModelSettings() {
  document.querySelectorAll("#s-provider .s-chip").forEach((chip) => {
    chip.addEventListener("click", () => applyPreset(chip.dataset.preset));
  });

  const keyInput = $("#s-api-key");
  keyInput.addEventListener("input", () => {
    keyDirty = true;
    syncKeyPlaceholder();
  });

  $("#s-test-btn").addEventListener("click", () => void testConnection());
  $("#s-save-btn").addEventListener("click", () => void saveSettings());
  $("#s-models-refresh").addEventListener("click", () => void loadOllamaModels());
}
