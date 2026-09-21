/* =========================================================================
   图像生成设置（设置面板「图像生成」段，ADR-0031）。

   形状照抄 vision-settings.js 那套"预设芯片 + 表单 + 测试连接 + 保存并生效"，
   三处刻意不同——

   1. **多一个「图片尺寸」**：SiliconFlow 的出图端点把 image_size 当**必填**，
      且各模型推荐值不同（Qwen-Image 1328x1328 / Kolors 1024x1024），
      写错了上游只回一句"参数错误"。
   2. **密钥只写不删**（同视觉段）：SILICONFLOW_API_KEY 被检索/识图共用，
      在这里清空会打挂整条链路。前端不发空密钥（服务端另有 422 兜底）。
   3. **「测试连接」不生成图片**：出图按张计费或耗免费额度，拿它当探针不合适；
      这里只读 /models 列表并核对模型名在不在其中（几百字节）。

   交互纪律：全部 addEventListener，无内联 onclick；动态文本走 el() 文本节点。
   ========================================================================= */

import { $, apiFetch, el, toast } from "./common.js";

/** 预设：keyEnv 与后端 _DEFAULT_IMAGE_KEY_ENV 的兜底槽一致。 */
const PRESETS = {
  none: {
    backend: "none",
    baseUrl: "",
    model: "",
    size: "1024x1024",
    keyEnv: "",
    hint: "未接入：问答页的「生成图片」会说明怎么开。不影响提问、检索与写作。",
  },
  siliconflow: {
    backend: "api",
    baseUrl: "https://api.siliconflow.cn/v1",
    model: "Qwen/Qwen-Image",
    size: "1328x1328",
    keyEnv: "SILICONFLOW_API_KEY",
    hint: "SiliconFlow 出图：Qwen-Image（1328x1328）质量好；换 Kwai-Kolors/Kolors 请把尺寸改回 1024x1024。提示词会发到云端。",
  },
  custom: {
    backend: "api",
    baseUrl: "",
    model: "",
    size: "1024x1024",
    keyEnv: "MIKASA_IMAGE_API_KEY",
    hint: "任何 OpenAI 兼容风格的出图端点：地址填到 /v1，模型名与尺寸照服务商文档抄。",
  },
};

/** 尺寸下拉的常用值（datalist 只是提示，手填也可以）。 */
const SIZES = ["1024x1024", "1328x1328", "960x1280", "1280x960", "720x1280", "1280x720"];

let current = null; // 服务端当前配置（GET 的响应）
let keyDirty = false; // 密钥框被碰过没有（没碰过就不提交该字段）
let activePreset = null;

function setResult(text, kind = "") {
  const node = $("#g-test-result");
  node.textContent = text || "";
  node.className = `s-test-result${kind ? " " + kind : ""}`;
}

function markChips() {
  document.querySelectorAll("#g-provider .s-chip").forEach((chip) => {
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

/** 行的显隐：未接入时没有地址/模型/尺寸/密钥可填。 */
function syncRows() {
  const isNone = activePreset === "none";
  for (const id of ["#g-base-url", "#g-model", "#g-size", "#g-api-key"]) {
    $(id).closest(".s-row").classList.toggle("hidden", isNone);
  }
  $("#g-test-btn").classList.toggle("hidden", isNone); // 未接入没什么可测的
}

function syncHint() {
  const preset = PRESETS[activePreset] || PRESETS.custom;
  const shared = !!(current && current.key_shared_with_retrieval && activePreset !== "none");
  const note = shared ? " ｜ 与检索侧共用同一密钥槽位：要清除请到「模型」段。" : "";
  $("#g-model-hint").textContent = preset.hint + note;
}

function syncKeyPlaceholder() {
  const has = !!(current && current.has_api_key);
  const input = $("#g-api-key");
  if (keyDirty && input.value) {
    input.placeholder = has ? "已保存（保存后替换为新密钥）" : "保存后写入";
  } else {
    input.placeholder = has ? "已保存 · 输入新密钥可更换" : "粘贴密钥";
  }
}

function applyPreset(name) {
  const preset = PRESETS[name];
  if (!preset) return;
  activePreset = name;
  $("#g-base-url").value = preset.baseUrl;
  $("#g-model").value = preset.model;
  $("#g-size").value = preset.size;
  keyDirty = false; // 切来源 = 换密钥槽
  $("#g-api-key").value = "";
  syncRows();
  markChips();
  syncKeyPlaceholder();
  syncHint();
  setResult("");
}

/** 拉取服务端当前出图配置并回填（打开面板时调用，保存后再调一次）。 */
export async function refreshImageSettings() {
  try {
    current = await apiFetch("/api/settings/image");
  } catch (err) {
    setResult(`读取配置失败：${err.message}`, "error");
    return;
  }
  keyDirty = false;
  $("#g-api-key").value = "";
  $("#g-base-url").value = current.base_url || "";
  $("#g-model").value = current.model || "";
  $("#g-size").value = current.size || "";
  activePreset = detectPreset();
  syncRows();
  markChips();
  syncKeyPlaceholder();
  syncHint();
  setResult("");

  const locked = !!current.locked;
  for (const id of ["#g-base-url", "#g-model", "#g-size", "#g-api-key", "#g-save-btn", "#g-test-btn"]) {
    $(id).disabled = locked;
  }
  document.querySelectorAll("#g-provider .s-chip").forEach((chip) => {
    chip.disabled = locked;
  });
  if (locked) setResult("配置来自显式配置文件，面板只读", "error");
}

/** 组装保存/测试共用的字段（密钥只在用户改过且非空时才带上）。 */
function collectFields() {
  const preset = PRESETS[activePreset] || PRESETS.custom;
  const body = { backend: preset.backend, base_url: "", model: "", size: "", api_key_env: "" };
  if (preset.backend !== "none") {
    body.base_url = ($("#g-base-url").value || "").trim();
    body.model = ($("#g-model").value || "").trim();
    body.size = ($("#g-size").value || "").trim();
    body.api_key_env = preset.keyEnv;
  }
  const typed = ($("#g-api-key").value || "").trim();
  if (keyDirty && typed) body.api_key = typed; // 空串从不提交（共用槽位不许清空）
  return body;
}

async function testConnection() {
  const body = collectFields();
  setResult("检查地址与密钥…（不生成图片）", "muted");
  try {
    const res = await apiFetch("/api/settings/image/test", {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      setResult(res.error || "连接失败", "error");
      return;
    }
    const tail = res.error ? `（${res.error}）` : "";
    setResult(`已连通 · ${res.latency_ms}ms${tail}`, res.error ? "muted" : "ok");
  } catch (err) {
    setResult(`测试失败：${err.message}`, "error");
  }
}

async function saveSettings() {
  const body = collectFields();
  try {
    const saved = await apiFetch("/api/settings/image", {
      method: "PUT",
      body: JSON.stringify(body),
    });
    current = saved;
    keyDirty = false;
    $("#g-api-key").value = "";
    syncKeyPlaceholder();
    syncHint();
    activePreset = detectPreset();
    markChips();
    syncRows();
    toast(saved.backend === "none" ? "已停止使用图像生成" : "图像生成已保存并生效", "ok");
    setResult("");
  } catch (err) {
    setResult(`保存失败：${err.message}`, "error");
  }
}

/** 装配「图像生成」段。问答页启动时调用一次（settings.js 转调）。 */
export function initImageSettings() {
  document.querySelectorAll("#g-provider .s-chip").forEach((chip) => {
    chip.addEventListener("click", () => applyPreset(chip.dataset.preset));
  });
  $("#g-api-key").addEventListener("input", () => {
    keyDirty = true;
    syncKeyPlaceholder();
  });
  $("#g-test-btn").addEventListener("click", () => void testConnection());
  $("#g-save-btn").addEventListener("click", () => void saveSettings());

  const list = $("#g-size-options");
  if (list) list.replaceChildren(...SIZES.map((s) => el("option", { value: s })));
}
