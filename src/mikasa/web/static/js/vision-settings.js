/* =========================================================================
   视觉模型设置（设置面板「视觉模型」段，M6 ②，ADR-0027）。

   与 model-settings.js 并列的两段：形状照抄那套"预设芯片 + 表单 + 测试连接
   + 保存并生效"，但语义有两处刻意不同——

   1. **密钥只写不删**：api 档的 SILICONFLOW_API_KEY 被检索侧
      （embedding/reranker/judge）共用，在这里清空会打挂整条检索链，故障
      表现却是"搜索结果变差"。所以前端根本不发空密钥（服务端另有 422 兜底）；
      要清除请去「模型」段。
   2. **多一个「未接入」预设**（backend=none）：那是"我不接识图了"的正常
      表达，与"删密钥"是两件事。

   交互纪律：全部 addEventListener，无内联 onclick；动态文本走 el() 文本节点。
   ========================================================================= */

import { $, apiFetch, el, toast } from "./common.js";

/** 预设：keyEnv 与后端 _DEFAULT_VISION_KEY_ENV 的兜底槽一致。 */
const PRESETS = {
  none: {
    backend: "none",
    baseUrl: "",
    model: "",
    keyEnv: "",
    hint: "未接入：笔记里的「识别图片」会说明怎么开。这不影响提问、检索与写作。",
  },
  siliconflow: {
    backend: "api",
    baseUrl: "https://api.siliconflow.cn/v1",
    model: "Qwen/Qwen2.5-VL-32B-Instruct",
    keyEnv: "SILICONFLOW_API_KEY",
    hint: "SiliconFlow 的 Qwen2.5-VL：识中文板书/书页效果好。低配可换 7B、更准换 72B。",
  },
  ollama: {
    backend: "local",
    baseUrl: "http://localhost:11434/v1",
    model: "qwen2.5vl:7b",
    keyEnv: "",
    hint: "本机 Ollama 视觉模型：免费离线，但要先 `ollama pull qwen2.5vl:7b`（约 6GB）。",
  },
  custom: {
    backend: "api",
    baseUrl: "",
    model: "",
    keyEnv: "MIKASA_VISION_API_KEY",
    hint: "任何 OpenAI 兼容的多模态端点：地址填到 /v1、模型名照服务商文档抄。",
  },
};

let current = null; // 服务端当前配置（GET 的响应）
let keyDirty = false; // 密钥框被碰过没有（没碰过就不提交该字段）
let activePreset = null;

function setResult(text, kind = "") {
  const node = $("#v-test-result");
  node.textContent = text || "";
  node.className = `s-test-result${kind ? " " + kind : ""}`;
}

function markChips() {
  document.querySelectorAll("#v-provider .s-chip").forEach((chip) => {
    chip.classList.toggle("on", chip.dataset.preset === activePreset);
  });
}

/** 由当前配置反推预设（地址/后端/密钥槽都对得上才算；对不上 = 自定义）。 */
function detectPreset() {
  if (!current) return "none";
  if (current.backend === "none") return "none";
  const baseUrl = (current.base_url || "").trim().replace(/\/+$/, "");
  const keyEnv = current.api_key_env || "";
  for (const [name, preset] of Object.entries(PRESETS)) {
    if (name === "none" || name === "custom") continue;
    if (
      preset.backend === current.backend &&
      preset.baseUrl.replace(/\/+$/, "") === baseUrl &&
      (preset.keyEnv || "") === keyEnv
    ) {
      return name;
    }
  }
  return "custom";
}

/** 行的显隐：未接入时没有地址/模型/密钥可填；本机 Ollama 没有"密钥"这回事。 */
function syncRows() {
  const isNone = activePreset === "none";
  const isLocal = activePreset === "ollama";
  $("#v-base-url").closest(".s-row").classList.toggle("hidden", isNone);
  $("#v-model").closest(".s-row").classList.toggle("hidden", isNone);
  $("#v-api-key").closest(".s-row").classList.toggle("hidden", isNone || isLocal);
  $("#v-models-refresh").classList.toggle("hidden", !isLocal);
  $("#v-test-btn").classList.toggle("hidden", isNone); // 未接入没什么可测的
}

function syncHint() {
  const preset = PRESETS[activePreset] || PRESETS.custom;
  const shared = !!(current && current.key_shared_with_retrieval && activePreset !== "none");
  const note = shared ? " ｜ 与检索侧共用同一密钥槽位：要清除请到「模型」段。" : "";
  $("#v-model-hint").textContent = preset.hint + note;
}

function syncKeyPlaceholder() {
  const has = !!(current && current.has_api_key);
  const input = $("#v-api-key");
  if (keyDirty && input.value) {
    input.placeholder = has ? "已保存（保存后替换为新密钥）" : "保存后写入";
  } else {
    // 这里刻意不说"清空可删除"：共用槽位的清空在「模型」段
    input.placeholder = has ? "已保存 · 输入新密钥可更换" : "粘贴密钥";
  }
}

function applyPreset(name) {
  const preset = PRESETS[name];
  if (!preset) return;
  activePreset = name;
  $("#v-base-url").value = preset.baseUrl;
  $("#v-model").value = preset.model;
  keyDirty = false; // 切来源 = 换密钥槽
  $("#v-api-key").value = "";
  syncRows();
  markChips();
  syncKeyPlaceholder();
  syncHint();
  setResult("");
}

/** 拉取服务端当前视觉配置并回填（打开面板时调用，保存后再调一次）。 */
export async function refreshVisionSettings() {
  try {
    current = await apiFetch("/api/settings/vision");
  } catch (err) {
    setResult(`读取配置失败：${err.message}`, "error");
    return;
  }
  keyDirty = false;
  $("#v-api-key").value = "";
  $("#v-base-url").value = current.base_url || "";
  $("#v-model").value = current.model || "";
  activePreset = detectPreset();
  syncRows();
  markChips();
  syncKeyPlaceholder();
  syncHint();
  setResult("");

  const locked = !!current.locked;
  for (const id of ["#v-base-url", "#v-model", "#v-api-key", "#v-save-btn", "#v-test-btn"]) {
    $(id).disabled = locked;
  }
  document.querySelectorAll("#v-provider .s-chip").forEach((chip) => {
    chip.disabled = locked;
  });
  if (locked) setResult("配置来自显式配置文件，面板只读", "error");
}

/** 组装保存/测试共用的字段（密钥只在用户改过且非空时才带上）。 */
function collectFields() {
  const preset = PRESETS[activePreset] || PRESETS.custom;
  const body = { backend: preset.backend, base_url: "", model: "", api_key_env: "" };
  if (preset.backend !== "none") {
    body.base_url = ($("#v-base-url").value || "").trim();
    body.model = ($("#v-model").value || "").trim();
    body.api_key_env = preset.keyEnv;
  }
  const typed = ($("#v-api-key").value || "").trim();
  if (keyDirty && typed) body.api_key = typed; // 空串从不提交（共用槽位不许清空）
  return body;
}

async function testConnection() {
  const body = collectFields();
  setResult("测试中…（要传一张真图片，可能十几秒）", "muted");
  try {
    const res = await apiFetch("/api/settings/vision/test", {
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
    const saved = await apiFetch("/api/settings/vision", {
      method: "PUT",
      body: JSON.stringify(body),
    });
    current = saved;
    keyDirty = false;
    $("#v-api-key").value = "";
    syncKeyPlaceholder();
    syncHint();
    activePreset = detectPreset();
    markChips();
    syncRows();
    toast(saved.backend === "none" ? "已停止使用视觉模型" : "视觉模型已保存并生效", "ok");
    setResult("");
  } catch (err) {
    setResult(`保存失败：${err.message}`, "error");
  }
}

async function loadOllamaModels() {
  const base = ($("#v-base-url").value || "").trim() || "http://localhost:11434/v1";
  setResult("读取本机模型…", "muted");
  try {
    const res = await apiFetch(`/api/settings/ollama/models?base_url=${encodeURIComponent(base)}`);
    $("#v-model-options").replaceChildren(
      ...res.models.map((name) => el("option", { value: name }))
    );
    setResult(
      res.models.length ? `本机 ${res.models.length} 个模型，点模型名可从下拉选` : "本机还没有模型",
      "ok"
    );
  } catch (err) {
    setResult(err.message, "error");
  }
}

/** 装配「视觉模型」段。问答页启动时调用一次（settings.js 转调）。 */
export function initVisionSettings() {
  document.querySelectorAll("#v-provider .s-chip").forEach((chip) => {
    chip.addEventListener("click", () => applyPreset(chip.dataset.preset));
  });
  $("#v-api-key").addEventListener("input", () => {
    keyDirty = true;
    syncKeyPlaceholder();
  });
  $("#v-test-btn").addEventListener("click", () => void testConnection());
  $("#v-save-btn").addEventListener("click", () => void saveSettings());
  $("#v-models-refresh").addEventListener("click", () => void loadOllamaModels());
}
