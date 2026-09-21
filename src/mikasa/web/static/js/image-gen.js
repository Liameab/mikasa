/* =========================================================================
   文生图入口（问答页「生成图片」按钮 + 对话框，ADR-0031）。

   形态与 confirm.js 同款：居中卡片 + 遮罩，Esc / 点遮罩 / 取消三通道关闭，
   键盘事件在**捕获阶段**拦截（模态只关自己，不惊动底下的设置面板）。

   三条纪律：
   1. **等待要看得见**（与问答流同一条）：出图是 10–60 秒级的调用，对话框里
      实时报秒数 + 阶段文案，用户才知道"在跑"而不是"卡死"；
   2. **失败不关窗**：上游报错（额度用尽 / 模型名错 / 未接入）原样贴在对话框
      里，改一下提示词或去设置面板就能重试——关掉再重开一遍最烦人；
   3. **动态文本一律 el() 文本节点**：本模块不出现 innerHTML。

   生成成功后的落点由调用方（qa.js）决定：把返回的 content 作为一条助手
   消息追加进对话，并在后端新建会话时把 activeSession 接过来。
   ========================================================================= */

import { apiFetch, el } from "./common.js";

const SIZE_PRESETS = ["1024x1024", "1328x1328", "960x1280", "1280x960", "720x1280"];

let dialog = null; // 单例：同一时刻至多一个生成对话框

/** 关掉当前对话框（若有）。 */
function closeDialog() {
  if (!dialog) return;
  document.removeEventListener("keydown", dialog.onKey, true);
  dialog.backdrop.remove();
  dialog = null;
}

/**
 * 打开生成对话框。
 *
 * @param {object} opts
 * @param {() => (number|null)} opts.getSessionId 当前会话 id（null=新会话，后端会建）
 * @param {(res: object) => void} opts.onGenerated 成功回调（res 见后端响应）
 */
function openDialog({ getSessionId, onGenerated } = {}) {
  closeDialog(); // 已有对话框在：先收掉（同 confirm 的单例纪律）

  const promptBox = el("textarea", {
    class: "img-prompt",
    rows: "3",
    maxlength: "2000",
    placeholder: "描述你想要的画面（中文即可），例如：一只坐在窗台上的白猫，简笔画风格，米色背景",
  });
  // 尺寸**留空 = 跟随设置面板里的配置**（打开对话框时把配置值显示成占位符）。
  // 写死一个默认值会静默覆盖用户在面板里选好的尺寸——各模型的推荐值不同
  // （Qwen-Image 1328x1328 / Kolors 1024x1024），猜错就是白等几十秒。
  const sizeInput = el("input", {
    id: "gen-size",
    type: "text",
    list: "gen-size-options",
    placeholder: "跟随设置",
    autocomplete: "off",
    spellcheck: "false",
  });
  const sizeList = el("datalist", { id: "gen-size-options" }, ...SIZE_PRESETS.map((s) => el("option", { value: s })));
  const status = el("span", { class: "img-status" });

  const cancelBtn = el("button", { class: "btn ghost", type: "button" }, "取消");
  const okBtn = el("button", { class: "btn primary", type: "button" }, "生成");
  cancelBtn.addEventListener("click", () => closeDialog());
  okBtn.addEventListener("click", () => void submit());

  const onKey = (ev) => {
    if (ev.key === "Escape") {
      ev.preventDefault();
      ev.stopPropagation(); // 模态：Esc 只关它自己
      closeDialog();
    }
  };

  /** 提交：进入"生成中"态（锁按钮 + 报秒），失败原地报错不关窗。 */
  async function submit() {
    const prompt = (promptBox.value || "").trim();
    if (!prompt) {
      status.textContent = "先写一句提示词";
      status.className = "img-status error";
      promptBox.focus();
      return;
    }
    okBtn.disabled = true;
    cancelBtn.disabled = true;
    const started = Date.now();
    status.className = "img-status muted";
    const tick = setInterval(() => {
      status.textContent = `生成中… ${((Date.now() - started) / 1000).toFixed(1)}s`;
    }, 100);
    status.textContent = "生成中… 0.0s";
    try {
      const res = await apiFetch("/api/images/generate", {
        method: "POST",
        body: JSON.stringify({
          prompt,
          size: (sizeInput.value || "").trim() || null,
          session_id: typeof getSessionId === "function" ? getSessionId() : null,
        }),
      });
      clearInterval(tick);
      closeDialog();
      if (typeof onGenerated === "function") onGenerated(res);
    } catch (err) {
      clearInterval(tick);
      status.textContent = `生成失败：${err.message}`;
      status.className = "img-status error";
      okBtn.disabled = false;
      cancelBtn.disabled = false;
    }
  }

  const box = el(
    "div",
    { class: "confirm-box img-box", role: "dialog", "aria-modal": "true" },
    el("div", { class: "cf-title" }, "生成图片"),
    el("div", { class: "cf-detail" }, "由设置面板里接入的图像模型出图（未接入时会提示怎么开）；提示词会发送到该服务商。"),
    promptBox,
    el("label", { class: "img-row" }, el("span", { class: "s-name" }, "图片尺寸"), sizeInput, sizeList),
    el("div", { class: "cf-actions" }, status, cancelBtn, okBtn)
  );
  const backdrop = el("div", { class: "confirm-backdrop" }, box);
  backdrop.addEventListener("click", (ev) => {
    if (ev.target === backdrop) closeDialog();
  });
  document.body.append(backdrop);
  document.addEventListener("keydown", onKey, true);
  dialog = { backdrop, onKey };
  promptBox.focus();

  // 打开时读一次当前配置（不阻塞对话框出现）：
  //   ① 尺寸占位符显示配置值，"留空"到底会用什么一目了然；
  //   ② 未接入时**当场说**，别等用户写完提示词、点了生成才报错。
  void (async () => {
    if (!dialog || dialog.backdrop !== backdrop) return; // 已经关掉了就别写
    try {
      const cfg = await apiFetch("/api/settings/image");
      if (!dialog || dialog.backdrop !== backdrop) return;
      if (cfg.size) sizeInput.placeholder = cfg.size;
      if (cfg.backend === "none") {
        status.textContent = "尚未接入图像模型：到设置面板 →「图像生成」选一个来源";
        status.className = "img-status muted";
      }
    } catch {
      // 读不到配置不影响用：提交时后端会如实报错
    }
  })();
}

/**
 * 装配「生成图片」入口。问答页启动时调用一次。
 *
 * @param {object} opts
 * @param {HTMLElement} opts.button 触发按钮
 * @param {() => (number|null)} opts.getSessionId
 * @param {(res: object) => void} opts.onGenerated
 */
export function initImageGen({ button, getSessionId, onGenerated } = {}) {
  if (!button) return;
  button.addEventListener("click", () => openDialog({ getSessionId, onGenerated }));
}
