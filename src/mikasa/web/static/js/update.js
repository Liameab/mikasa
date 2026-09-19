/* =========================================================================
   应用内更新（ADR-0022）：启动检查 → 弹窗 → 一键下载并安装。

   触发：问答页加载拿到 health 后静默调 /api/update/check。**检查失败
   什么也不弹**（离线用户不该被网络错误打扰），只在控制台留一行；只有
   用户主动点「检查更新」时才把失败 toast 出来。

   localStorage：
     mikasa.ui.checkUpdates   "0" = 启动时不检查（默认开）
     mikasa.ui.skipVersion    "0.1.2" = 这个版本别再提示（手动检查仍提示）

   下载：POST /api/update/download → 每 0.5s 轮询 /status → done 后
   POST /api/update/install 启动安装器。**安装器会先 taskkill 掉本进程**
   （它自己的 [Code] 段），所以最后一步之后界面停在"正在启动安装程序"，
   不再期待任何响应——也不该再发请求。

   安全：弹窗里的版本号/发布说明都来自 GitHub API，一律走 esc/渲染器
   （renderAnswer 先整体转义），本模块不出现裸 innerHTML。
   ========================================================================= */

import { $, apiFetch, el, renderAnswer, toast } from "./common.js";

const CHECK_KEY = "mikasa.ui.checkUpdates";
const SKIP_KEY = "mikasa.ui.skipVersion";
const POLL_MS = 500;
const DOWNLOAD_TIMEOUT_MS = 30 * 60 * 1000; // 30 分钟不落定就判超时
const MB = 1024 * 1024;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function storageGet(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null; // 隐私模式禁用 storage：当作默认值
  }
}

function storageSet(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* 存不下只是下次再提示一次，不影响使用 */
  }
}

/** 启动时是否检查更新（默认开；设置面板里可关）。 */
export function checkUpdatesEnabled() {
  return storageGet(CHECK_KEY) !== "0";
}

export function setCheckUpdatesEnabled(on) {
  storageSet(CHECK_KEY, on ? "1" : "0");
}

/** 被"跳过"的版本号（空串 = 没有跳过任何版本）。 */
export function skippedVersion() {
  return storageGet(SKIP_KEY) || "";
}

function clearSkippedVersion() {
  try {
    localStorage.removeItem(SKIP_KEY);
  } catch {
    /* 同上 */
  }
}

/**
 * 查一次最新版本。返回检查结果（失败返回 null）。
 *
 * @param {object} [opts]
 * @param {boolean} [opts.manual] 用户主动点「检查更新」：结果（含"已是最新"）
 *                                与失败都用 toast 说出来；启动时的静默检查不打扰。
 */
export async function checkForUpdate({ manual = false } = {}) {
  let info;
  try {
    info = await apiFetch(`/api/update/check${manual ? "?force=true" : ""}`);
  } catch (err) {
    if (manual) toast(`检查更新失败：${err.message}`, "error");
    else console.info("[update] 检查更新失败（已静默忽略）：", err.message);
    return null;
  }

  if (manual) clearSkippedVersion(); // 手动检查是"我现在就想知道"，跳过标记作废

  if (!info.update_available) {
    if (manual) toast(`已是最新版本（v${info.current}）`, "ok");
    return info;
  }
  if (!manual && info.latest === skippedVersion()) return info; // 用户说过别再提
  if ($("#update-dialog") || $("#onboard")) return info; // 已有弹窗在（引导优先）
  showDialog(info);
  return info;
}

// 当前弹窗的收尾句柄（单例）：关闭时连同键盘监听一起摘掉。
// 不用 DOMNodeRemovedFromDocument 之类的突变事件——浏览器已废弃它们。
let activeDialog = null;

/** 关闭当前弹窗（幂等；无弹窗时什么也不做）。 */
function closeDialog() {
  if (!activeDialog) return;
  const { close } = activeDialog;
  activeDialog = null;
  close();
}

/**
 * 更新弹窗。返回一个 ui 句柄（progress/fail/launched），供下载流程改状态。
 */
function showDialog(info) {
  const head = el("div", { class: "upd-head" }, `发现新版本 v${info.latest}`);
  const sub = el(
    "div",
    { class: "upd-sub" },
    `当前 v${info.current}` + (info.published_at ? ` · 发布于 ${info.published_at.slice(0, 10)}` : "")
  );
  // 发布说明是 GitHub 上的 Markdown：走统一渲染器（先整体 esc，再识别结构）
  const notes = el("div", { class: "upd-notes" });
  notes.innerHTML = renderAnswer(info.notes || "（这个版本没有写更新说明）", [], false);

  const bar = el("i");
  const progressText = el("div", { class: "upd-progress-text" }, "准备中…");
  const progressBox = el(
    "div",
    { class: "upd-progress hidden" },
    el("div", { class: "upd-bar" }, bar),
    progressText
  );
  const errorBox = el("div", { class: "upd-error hidden" });

  const laterBtn = el("button", { class: "btn ghost", type: "button" }, "以后再说");
  const skipBtn = el("button", { class: "btn ghost", type: "button" }, "跳过此版本");
  const pageBtn = el("button", { class: "btn ghost", type: "button" }, "打开发布页");
  const installBtn = el("button", { class: "btn primary", type: "button" }, "下载并安装");
  const actions = el("div", { class: "upd-actions" }, laterBtn, skipBtn, pageBtn, installBtn);

  const children = [head, sub];
  // 自动安装只在"Windows + 这个版本确实带了安装包资产"时成立；
  // 任一不成立就诚实地摆出「打开发布页」，而不是给一个点了会失败的按钮。
  const canInstall = Boolean(info.asset) && Boolean(info.install_supported);
  if (!canInstall) {
    installBtn.classList.add("hidden");
    children.push(
      el("div", { class: "upd-hint" }, "当前平台不支持自动安装，请到发布页下载安装包。")
    );
  }
  children.push(notes, progressBox, errorBox, actions);

  const card = el(
    "div",
    { class: "upd-card", role: "dialog", "aria-modal": "true", "aria-label": "发现新版本" },
    ...children
  );
  const mask = el("div", { class: "upd-backdrop", id: "update-dialog" }, card);

  let busy = false; // 下载/安装进行中：不许关闭（否则"点了没反应"的错觉）
  const onKey = (ev) => {
    if (ev.key !== "Escape" || busy) return;
    // 捕获阶段拦截：Esc 只关它自己，不把底下的设置/阅读面板一起收掉
    // （与 confirm.js 同一纪律）
    ev.preventDefault();
    ev.stopPropagation();
    closeDialog();
  };
  document.addEventListener("keydown", onKey, true);
  mask.addEventListener("click", (ev) => {
    if (ev.target === mask && !busy) closeDialog();
  });

  laterBtn.addEventListener("click", closeDialog);
  skipBtn.addEventListener("click", () => {
    storageSet(SKIP_KEY, info.latest);
    toast(`v${info.latest} 不再提示（设置里点「检查更新」可恢复）`, "ok");
    closeDialog();
  });
  pageBtn.addEventListener("click", () => {
    window.open(info.release_url, "_blank", "noopener");
  });
  installBtn.addEventListener("click", () => {
    installBtn.disabled = true;
    skipBtn.disabled = true;
    pageBtn.disabled = true;
    void runDownload(info, { bar, progressText, progressBox, errorBox, actions, head, card });
  });

  activeDialog = {
    close: () => {
      document.removeEventListener("keydown", onKey, true);
      mask.remove();
    },
  };

  document.body.append(mask);
  installBtn.focus();
}

/** 字节数 → 人读的 MB/GB。 */
function fmtBytes(bytes) {
  if (bytes >= 1024 * MB) return `${(bytes / (1024 * MB)).toFixed(2)} GB`;
  return `${(bytes / MB).toFixed(1)} MB`;
}

/**
 * 下载 → 轮询进度 → 启动安装器。任何一步失败都停在弹窗里给出文案 +
 * 「打开发布页」回退（用户至少能手动下）。
 */
async function runDownload(info, ui) {
  const fail = (message) => {
    ui.errorBox.textContent = `${message}——可以点「打开发布页」手动下载安装。`;
    ui.errorBox.classList.remove("hidden");
    ui.progressBox.classList.add("hidden");
    ui.card.querySelectorAll(".upd-actions .btn").forEach((b) => (b.disabled = false));
    ui.head.textContent = "更新未完成";
  };

  try {
    await apiFetch("/api/update/download", { method: "POST" });
  } catch (err) {
    fail(`启动下载失败：${err.message}`);
    return;
  }

  ui.progressBox.classList.remove("hidden");
  let lastBytes = 0;
  let lastTime = performance.now();
  let speed = 0; // 平滑后的速度（B/s）：单次采样抖动大，指数平滑一下

  const deadline = Date.now() + DOWNLOAD_TIMEOUT_MS;
  while (Date.now() < deadline) {
    await sleep(POLL_MS);
    let st;
    try {
      st = await apiFetch("/api/update/download/status");
    } catch {
      continue; // 单次轮询失败不判死：服务端还在下，下一轮再问
    }
    if (st.status === "error") {
      fail(st.error || "下载失败");
      return;
    }
    if (st.status === "done") {
      ui.bar.style.width = "100%";
      ui.progressText.textContent = "下载完成，正在启动安装程序…";
      await install(ui, fail);
      return;
    }
    const now = performance.now();
    const dt = (now - lastTime) / 1000;
    if (dt > 0 && st.downloaded >= lastBytes) {
      const instant = (st.downloaded - lastBytes) / dt;
      speed = speed ? speed * 0.7 + instant * 0.3 : instant;
    }
    lastBytes = st.downloaded;
    lastTime = now;

    if (st.total > 0) {
      const pct = Math.min(100, Math.floor((st.downloaded / st.total) * 100));
      ui.bar.style.width = `${pct}%`;
      ui.progressText.textContent =
        `正在下载 ${pct}% · ${fmtBytes(st.downloaded)} / ${fmtBytes(st.total)}` +
        (speed > 0 ? ` · ${fmtBytes(speed)}/s` : "");
    } else {
      ui.progressText.textContent = `正在下载 ${fmtBytes(st.downloaded)}…`;
    }
  }
  fail("下载超时");
}

/** 启动安装器。成功后本进程很快会被安装器结束（它先 taskkill Mikasa）。 */
async function install(ui, fail) {
  try {
    await apiFetch("/api/update/install", { method: "POST" });
  } catch (err) {
    fail(`启动安装程序失败：${err.message}`);
    return;
  }
  ui.head.textContent = "正在安装";
  ui.progressText.textContent =
    "安装程序已启动：它会先关闭 Mikasa，装好后自动打开新版本（你的资料不受影响）。";
  ui.actions.querySelectorAll(".btn").forEach((b) => (b.disabled = true));
}

/**
 * 问答页启动时调用（qa.js 的 initTopbar().then）。
 * @param {object|null} health /api/health 的响应（版本号显示用，可为空）
 */
export function initUpdate(health) {
  const versionLabel = $("#s-version");
  if (versionLabel && health?.version) versionLabel.textContent = `v${health.version}`;

  const box = $("#s-check-updates");
  if (box) {
    box.checked = checkUpdatesEnabled();
    box.addEventListener("change", () => setCheckUpdatesEnabled(box.checked));
  }
  const btn = $("#s-check-update");
  if (btn) btn.addEventListener("click", () => void checkForUpdate({ manual: true }));

  if (!checkUpdatesEnabled()) return;
  // 静默检查放在最后：它可能弹窗，用户的注意力应先在主界面上
  void checkForUpdate();
}
