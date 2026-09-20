/* =========================================================================
   聊天设置（问答页右上齿轮）：昵称 / 消息字号 / 对话框背景色 / 背景图。

   面板另含「模型」段（服务端配置，见 js/model-settings.js 的分工说明）。

   全部存 localStorage（mikasa.ui.*）——本机浏览器级偏好，不上服务器；
   昵称字段为将来账号体系预留：账号上线后由用户表带入，读同一取值口
   nickname()（qa.js 气泡署名处唯一调用点，将来替换成账号数据即可）。

   背景只作用于 .msg-scroll（对话消息区）：颜色/图片都走 CSS 变量
   （--chat-bg / --chat-img），图片经 canvas 压缩为 jpeg dataURL 再落
   localStorage（上限保护），清除时把变量置回 :root 默认。

   交互纪律：全部 addEventListener，无内联 onclick。
   ========================================================================= */

import { $, el, toast } from "./common.js";
import { imageToDataUrl } from "./image-util.js";
import { initModelSettings, refreshModelSettings } from "./model-settings.js";
import { initVisionSettings, refreshVisionSettings } from "./vision-settings.js";

const KEY = {
  nick: "mikasa.ui.nick",
  font: "mikasa.ui.font",
  bgColor: "mikasa.ui.bgColor",
  bgImage: "mikasa.ui.bgImage",
};

// 预设背景色（暖米系统的近亲色：都比画布深一档、文字对比友好；"" = 恢复默认）
// 换版时同步换过一轮——旧的一组是暗色调，在暖米画布上会像贴错地方。
const SWATCHES = [
  { label: "默认", color: "" },
  { label: "米白", color: "#f7f4ec" },
  { label: "暖沙", color: "#f3ece0" },
  { label: "浅陶", color: "#f6e9e2" },
  { label: "淡青", color: "#eaf2ef" },
  { label: "雾灰", color: "#efeee9" },
];

const rootStyle = document.documentElement.style;

/* ---------------- 读取口（qa.js 与将来账号功能共用） ---------------- */

/** 当前昵称（未设置返回空串，由调用方回退显示"我"）。 */
export function nickname() {
  return (localStorage.getItem(KEY.nick) || "").trim();
}

/* ---------------- 应用与持久化 ---------------- */

function setVar(name, value) {
  // 空值 = 清掉内联覆写，回到 :root 的默认（transparent / none）
  if (!value) rootStyle.removeProperty(name);
  else rootStyle.setProperty(name, value);
}

function saveFont(size) {
  localStorage.setItem(KEY.font, String(size));
  setVar("--chat-font", `${size}px`);
  $("#s-font-val").textContent = String(size);
}

function saveBgColor(color) {
  if (color) localStorage.setItem(KEY.bgColor, color);
  else localStorage.removeItem(KEY.bgColor);
  setVar("--chat-bg", color);
  // 色板高亮圈随动（"默认"项 color 为空）
  document.querySelectorAll(".s-sw").forEach((sw) => {
    sw.classList.toggle("on", (sw.dataset.color || "") === (color || ""));
  });
}

function saveBgImage(dataUrl) {
  if (dataUrl) localStorage.setItem(KEY.bgImage, dataUrl);
  else localStorage.removeItem(KEY.bgImage);
  // 必须包 url("…")：dataURL 前缀含分号（data:image/jpeg;base64,），
  // 裸放会截断顶层声明、被 setProperty 静默丢弃（2026-09-09 实踩）
  setVar("--chat-img", dataUrl ? `url("${dataUrl}")` : "");
  const preview = $("#s-bgimg-preview");
  preview.classList.toggle("hidden", !dataUrl);
  if (dataUrl) preview.src = dataUrl;
}

/** 读回已存偏好（页面加载时调用一次）。 */
function restore() {
  const font = localStorage.getItem(KEY.font);
  if (font) saveFont(Number(font)); // 直接走 setVar，避免重复存
  else setVar("--chat-font", null);
  saveBgColor(localStorage.getItem(KEY.bgColor) || "");
  saveBgImage(localStorage.getItem(KEY.bgImage) || "");
  const nick = nickname();
  if (nick) $("#s-nick").value = nick;
}

/* ---------------- 昵称改动即时生效 ---------------- */

/** 把已有消息气泡上的"我"换成新昵称（改设置后历史消息不用重载）。 */
function applyNickToHistory() {
  document.querySelectorAll(".msg.user .who").forEach((who) => {
    // 用户署名就是昵称本身（可能带时间/延迟段，用 " · " 分隔）——首段换名、
    // 其余原样保留即可。**不能再要求"必须带分隔符"**：用户消息的署名常常
    // 只有昵称一段，旧条件（parts.length > 1）会让改名对历史消息永不生效
    // （2026-09-11 设置 E2E 抓到：改昵称后气泡署名仍是"我"）。
    const parts = who.textContent.split(" · ");
    parts[0] = nickname() || "我";
    who.textContent = parts.join(" · ");
  });
}

/* ---------------- 面板装配 ---------------- */

function buildSwatches() {
  const box = $("#s-swatches");
  // 预设色板 + 自定义取色器（原生 color input 当"最后一块"用）
  for (const { label, color } of SWATCHES) {
    const sw = el("button", {
      type: "button",
      class: `s-sw${color ? "" : " clear"}`,
      title: label,
      "aria-label": `背景色 ${label}`,
    });
    if (color) sw.style.background = color;
    sw.dataset.color = color;
    sw.addEventListener("click", () => saveBgColor(color));
    box.append(sw);
  }
  const picker = el("input", { type: "color", class: "s-sw", title: "自定义颜色", "aria-label": "自定义背景色" });
  box.append(picker);
  picker.addEventListener("input", () => saveBgColor(picker.value));
}

function openPanel(open) {
  $("#settings-panel").classList.toggle("hidden", !open);
  if (open) {
    // 面板刚打开时同步一次外部态（如换页残留的旧值、阅读器 A-/A+ 改过的字号）
    $("#s-nick").value = nickname();
    $("#s-font").value = localStorage.getItem(KEY.font) || "14.5";
    $("#s-font-val").textContent = $("#s-font").value;
    void refreshModelSettings(); // 「模型」段：从服务端拉当前配置回填
    void refreshVisionSettings(); // 「视觉模型」段：同上
  }
}

/**
 * 装配设置面板。问答页启动时调用一次（qa.js）。
 * 监听：齿轮开合 / ✕ / Esc / 点外部关闭；控件改动即时生效并落盘。
 */
export function initSettings() {
  buildSwatches();
  initModelSettings(); // 「模型」段（服务端配置，见 js/model-settings.js）
  initVisionSettings(); // 「视觉模型」段（识图来源，见 js/vision-settings.js）
  $("#s-font-val").textContent = $("#s-font").value = localStorage.getItem(KEY.font) || "14.5";

  $("#btn-settings").addEventListener("click", () => openPanel($("#settings-panel").classList.contains("hidden")));
  $("#s-close").addEventListener("click", () => openPanel(false));
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") openPanel(false);
  });
  document.addEventListener("click", (ev) => {
    const panel = $("#settings-panel");
    if (panel.classList.contains("hidden")) return;
    if (!panel.contains(ev.target) && !ev.target.closest("#btn-settings")) openPanel(false);
  });

  // 昵称：失焦/回车落盘 + 历史气泡即时换名
  const nickInput = $("#s-nick");
  const commitNick = () => {
    const value = nickInput.value.trim().slice(0, 20);
    if (value) localStorage.setItem(KEY.nick, value);
    else localStorage.removeItem(KEY.nick);
    applyNickToHistory();
  };
  nickInput.addEventListener("change", commitNick);
  nickInput.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") nickInput.blur();
  });

  // 字号：拖动即时预览，松手落盘
  $("#s-font").addEventListener("input", () => saveFont(Number($("#s-font").value)));
  $("#s-font").addEventListener("change", () => saveFont(Number($("#s-font").value)));

  // 背景图：按钮唤起文件选择 → 压缩 → 应用；移除按钮清空
  $("#s-bgimg-btn").addEventListener("click", () => $("#s-bgimg").click());
  $("#s-bgimg").addEventListener("change", async () => {
    const file = $("#s-bgimg").files[0];
    if (!file) return;
    try {
      saveBgImage(await imageToDataUrl(file));
      toast("背景图已应用（仅存本机）", "ok");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      $("#s-bgimg").value = ""; // 同图可再次选择
    }
  });
  $("#s-bgimg-clear").addEventListener("click", () => {
    saveBgImage("");
    toast("已移除背景图", "ok");
  });

  restore(); // 末尾：把已存偏好应用到页面
}
