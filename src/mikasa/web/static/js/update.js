/* =========================================================================
   应用内更新（ADR-0022 / ADR-0024）：启动检查 → 弹窗 → 后台下载 → 启动安装器。

   **观察窗模型**（ADR-0024）：下载是**服务端的后台任务**，界面只是观察窗。
   弹窗可以关（关掉 = 转后台继续下），顶栏留一个跨页胶囊；切页、刷新、关
   弹窗都不打断下载，回到任何一页都能从胶囊接着看、接着装。

   触发：
     - 问答页加载拿到 health 后静默调 /api/update/check（失败静默；
       离线用户不该被网络错误打扰），只有用户主动点「检查更新」才说出来；
     - 其余三页只挂胶囊、只问 /api/update/download/status（本地端点）——
       **不打 check**：设置里关掉"启动时检查"之后，任何页面都不该偷偷联网；
     - 跟随任务一律走 status 快照：任务在服务进程里，与页面无关。

   localStorage：  mikasa.ui.checkUpdates / mikasa.ui.skipVersion（v0.1.1 起）
   sessionStorage：mikasa.ui.updDismissed = 被用户收起过弹窗的版本号
                   （只影响"回到问答页自不自动重开"，胶囊一直都在）

   完成那一刻弹窗还开着才自动装：安装器第一件事是 taskkill 掉 Mikasa
   （它自己的 [Code] 段），用户不在场时把他的应用杀掉是冒犯——后台下载完
   只把胶囊换成「更新包已就绪 · 点此安装」。

   安全：版本号/发布说明都来自 GitHub API，一律走 esc / renderAnswer
   （renderAnswer 先整体转义），本模块不出现裸 innerHTML；下载地址永远
   不出现在这里（服务端挑 URL，客户端连碰都碰不到）。
   ========================================================================= */

import { $, apiFetch, el, renderAnswer, toast } from "./common.js";

const CHECK_KEY = "mikasa.ui.checkUpdates";
const SKIP_KEY = "mikasa.ui.skipVersion";
const DISMISS_KEY = "mikasa.ui.updDismissed";
const POLL_MS = 1000; // 跟随任务时的轮询间隔
const IDLE_POLL_MS = 15000; // 无任务时的低频轮询（别处起的下载也能被发现）
const MB = 1024 * 1024;

function storageGet(key, store) {
  try {
    return store.getItem(key);
  } catch {
    return null; // 隐私模式禁用 storage：当作默认值
  }
}

function storageSet(key, value, store) {
  try {
    store.setItem(key, value);
  } catch {
    /* 存不下只是下次再提示一次，不影响使用 */
  }
}

function storageDel(key, store) {
  try {
    store.removeItem(key);
  } catch {
    /* 同上 */
  }
}

/** 启动时是否检查更新（默认开；设置面板里可关）。 */
export function checkUpdatesEnabled() {
  return storageGet(CHECK_KEY, localStorage) !== "0";
}

export function setCheckUpdatesEnabled(on) {
  storageSet(CHECK_KEY, on ? "1" : "0", localStorage);
}

/** 被"跳过"的版本号（空串 = 没有跳过任何版本）。 */
export function skippedVersion() {
  return storageGet(SKIP_KEY, localStorage) || "";
}

function clearSkippedVersion() {
  storageDel(SKIP_KEY, localStorage);
}

/* ------------------------------------------------------------------ */
/* 观察窗：一个 watcher 同时驱动顶栏胶囊与弹窗                          */
/* ------------------------------------------------------------------ */

let watcher = null;

/** 当前观察窗（没有就建一个；胶囊与轮询句柄挂在它身上）。 */
function ensureWatcher() {
  if (watcher) return watcher;
  watcher = {
    st: null, // 最近一次 status 快照
    info: null, // 最近一次 /api/update/check 结果（可能没有）
    current: "", // 当前版本号（问答页从 health 拿到）
    dialog: null, // { mask, ui } 弹窗开着时才有
    timer: null,
    pill: null,
    lastBytes: 0,
    lastTime: performance.now(),
    speed: 0,
    advancedAt: performance.now(),
    installing: false, // 安装阶段：文案与按钮都按住
    pending: false, // POST 在飞：按住主按钮，防连点
    infoAsked: false,
  };
  return watcher;
}

function fetchStatus() {
  return apiFetch("/api/update/download/status").catch(() => null);
}

function fmtBytes(bytes) {
  if (bytes >= 1024 * MB) return `${(bytes / (1024 * MB)).toFixed(2)} GB`;
  return `${(bytes / MB).toFixed(1)} MB`;
}

function fmtDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  if (seconds < 60) return `${Math.ceil(seconds)} 秒`;
  const whole = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return rest ? `${whole} 分 ${rest} 秒` : `${whole} 分钟`;
}

/**
 * 百分比：**小值必须给一位小数**。
 *
 * 80MB 的包取整，意味着"下到 838KB 之前一直显示 0%"——而国内直连 GitHub 实测
 * 一条连接可能只有几 KB/s，那 0% 要挂十几分钟，用户看到的就是"卡死了"
 * （2026-09-20 用户原话）。所以 10% 以下给一位小数，让它诚实地慢慢动。
 */
function fmtPercent(pct) {
  return pct >= 10 ? `${Math.floor(pct)}%` : `${pct.toFixed(1)}%`;
}

/** 版本号：优先用 check 的结果，只有快照时用快照里的。 */
function versionOf(w) {
  return (w.info && w.info.latest) || (w.st && w.st.version) || "";
}

/** 发布页地址：只有 check 的结果里有（客户端不许自己拼仓库地址）。 */
function pageUrlOf(info) {
  return (info && info.release_url) || "";
}

/* ---------------- 顶栏胶囊 ---------------- */

/**
 * 顶栏胶囊（独立容器，**不能塞进 .health-pill**——那个被 initTopbar 每
 * 20s 用 innerHTML="" 重绘）。四页共用一套，页面里不用放占位。
 */
function ensurePill() {
  const w = ensureWatcher();
  if (w.pill && w.pill.isConnected) return w.pill;
  const node = el("button", { class: "upd-pill hidden", type: "button", id: "update-pill" });
  node.addEventListener("click", () => openUpdateDialog());
  const anchor = $(".health-pill");
  if (anchor && anchor.parentNode) anchor.before(node);
  else {
    const bar = $(".topbar");
    if (bar) bar.append(node);
  }
  w.pill = node;
  return node;
}

function pillText(st) {
  if (!st || st.status === "idle") return "";
  if (st.status === "done") return "更新包已就绪 · 点此安装";
  if (st.status === "error") return "更新下载失败 · 点此重试";
  if (st.status === "verifying") return "正在校验更新包…";
  const label =
    st.total > 0
      ? fmtPercent(Math.min(100, (st.downloaded / st.total) * 100))
      : fmtBytes(st.downloaded);
  return st.stalled ? `更新下载中 ${label}（可能停住了）` : `更新下载中 ${label}`;
}

function renderPill(st) {
  const w = ensureWatcher();
  const pill = ensurePill();
  const text = pillText(st);
  pill.textContent = text || "";
  pill.classList.toggle("hidden", !text);
  pill.classList.toggle("ok", Boolean(st) && st.status === "done");
  pill.classList.toggle("warn", Boolean(st) && (st.status === "error" || st.stalled));
  pill.title = st && st.status === "error" ? st.error || "" : "应用内更新";
  // 弹窗开着的时候不重复提示（遮罩已经盖住顶栏了）
  if (w.dialog) pill.classList.add("hidden");
}

/* ---------------- 弹窗 ---------------- */

/**
 * 打开（或重开）更新弹窗。模式由**任务状态**决定，不由入口决定：
 * 有任务就是进度窗，没有就是"发现新版本"的告知窗。
 */
export function openUpdateDialog() {
  const w = ensureWatcher();
  if (w.dialog) {
    render(w);
    return;
  }
  const st = w.st;
  const active = Boolean(st) && st.status !== "idle" && st.status !== "error";
  if (!active && !(w.info && w.info.update_available)) return; // 没料可弹

  const head = el("div", { class: "upd-head" });
  const sub = el("div", { class: "upd-sub" });
  const notes = el("div", { class: "upd-notes" });
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
  const keepBtn = el("button", { class: "btn ghost", type: "button" }, "后台继续");
  const primaryBtn = el("button", { class: "btn primary", type: "button" }, "下载并安装");
  const actions = el("div", { class: "upd-actions" }, laterBtn, skipBtn, keepBtn, pageBtn, primaryBtn);

  const card = el(
    "div",
    { class: "upd-card", role: "dialog", "aria-modal": "true", "aria-label": "应用更新" },
    head,
    sub,
    notes,
    progressBox,
    errorBox,
    actions
  );
  const mask = el("div", { class: "upd-backdrop", id: "update-dialog" }, card);

  const onKey = (ev) => {
    if (ev.key !== "Escape") return;
    // 捕获阶段拦截：Esc 只收它自己，不把底下的设置/阅读面板一起收掉
    ev.preventDefault();
    ev.stopPropagation();
    dismissDialog();
  };
  document.addEventListener("keydown", onKey, true);
  mask.addEventListener("click", (ev) => {
    if (ev.target === mask) dismissDialog();
  });

  // 三个收尾按钮都走 dismissDialog（不取消下载，只是收起观察窗）
  laterBtn.addEventListener("click", () => dismissDialog());
  keepBtn.addEventListener("click", () => dismissDialog());
  skipBtn.addEventListener("click", () => {
    const version = versionOf(w);
    storageSet(SKIP_KEY, version, localStorage);
    toast(`v${version} 不再提示（设置里点「检查更新」可恢复）`, "ok");
    dismissDialog({ keep: true }); // 跳过 = 这次别再自己弹回来
  });
  pageBtn.addEventListener("click", () => {
    const url = pageUrlOf(w.info);
    if (url) window.open(url, "_blank", "noopener");
  });
  primaryBtn.addEventListener("click", () => {
    if (w.st && w.st.status === "done") void installNow(w);
    else void startOrAdopt(w);
  });

  w.dialog = {
    mask,
    ui: {
      head,
      sub,
      notes,
      bar,
      progressBox,
      progressText,
      errorBox,
      actions,
      laterBtn,
      skipBtn,
      keepBtn,
      pageBtn,
      primaryBtn,
    },
    close: () => {
      document.removeEventListener("keydown", onKey, true);
      mask.remove();
      w.dialog = null;
    },
  };
  document.body.append(mask);
  render(w);
  ensureInfo(w);
  primaryBtn.focus();
}

/** 收起弹窗：不取消下载，只是把观察窗收进顶栏胶囊。 */
function dismissDialog({ keep = false } = {}) {
  const w = ensureWatcher();
  if (!w.dialog) return;
  if (!keep) {
    const version = versionOf(w);
    if (version && w.st && w.st.status !== "idle") {
      // 记住"这次下载的弹窗被收起过"，回到问答页不再自动弹回来
      storageSet(DISMISS_KEY, version, sessionStorage);
    }
  }
  w.dialog.close();
  render(w);
}

/** 只有快照（从别页回来）时才需要补一次 check：要的是发布页地址与说明。 */
function ensureInfo(w) {
  if (w.info || w.infoAsked) return;
  w.infoAsked = true;
  apiFetch("/api/update/check")
    .then((info) => {
      w.info = info;
      if (w.dialog) render(w);
    })
    .catch(() => {
      /* 拿不到就不显示发布说明；「打开发布页」保持禁用 */
    });
}

/* ---------------- 渲染 ---------------- */

function progressLine(w, st) {
  const pct = st.total > 0 ? Math.min(100, (st.downloaded / st.total) * 100) : null;
  let text =
    pct === null
      ? `正在下载 ${fmtBytes(st.downloaded)}…`
      : `正在下载 ${fmtPercent(pct)} · ${fmtBytes(st.downloaded)} / ${fmtBytes(st.total)}`;
  if (w.speed > 0) text += ` · ${fmtBytes(w.speed)}/s`;
  if (pct !== null && w.speed > 0 && st.total > st.downloaded) {
    const eta = fmtDuration((st.total - st.downloaded) / w.speed);
    if (eta) text += ` · 剩余约 ${eta}`;
  }
  if (st.attempt > 1) text = `连接中断，正在重试（第 ${st.attempt} 次）… ${text}`;
  if (st.stalled) text += "（这条连接好像卡住了，服务端会换一条接着下）";
  return text;
}

function render(w) {
  const st = w.st || { status: "idle" };
  renderPill(st);
  const d = w.dialog;
  if (!d) return;
  const { ui } = d;
  const version = versionOf(w);
  const active = st.status === "running" || st.status === "verifying";
  const info = w.info;

  if (w.installing) ui.head.textContent = "正在安装"; // 标题由 installNow 定，这里别改回去
  else if (st.status === "error") ui.head.textContent = "更新未完成";
  else if (active) ui.head.textContent = `正在下载更新 v${version}`;
  else if (st.status === "done") ui.head.textContent = "更新包已就绪";
  else ui.head.textContent = `发现新版本 v${version}`;

  const parts = [];
  if (w.current) parts.push(`当前 v${w.current}`);
  if (info && info.published_at) parts.push(`发布于 ${info.published_at.slice(0, 10)}`);
  ui.sub.textContent = parts.join(" · ");

  // 发布说明：走统一渲染器（renderAnswer 先整体转义），拿不到就整块不显示
  if (info && info.notes) ui.notes.innerHTML = renderAnswer(info.notes, [], false);
  else ui.notes.innerHTML = "";
  ui.notes.classList.toggle("hidden", ui.notes.innerHTML === "");

  ui.progressBox.classList.toggle("hidden", !(active || st.status === "done"));
  ui.errorBox.classList.toggle("hidden", st.status !== "error");
  if (st.status === "error") {
    // 有发布页地址才提那句兜底——拿不到地址还让人去点，是句空话
    const fallback = pageUrlOf(info) ? "——可以点「打开发布页」手动下载安装。" : "";
    ui.errorBox.textContent = `${st.error || "下载失败"}${fallback}`;
  }
  if (active || st.status === "done") {
    const done = st.status === "done";
    // 不取整：慢链路上哪怕 0.3% 也要让进度条真的往前挪一点点（同 fmtPercent 的理由）
    const pct = done ? 100 : st.total > 0 ? Math.min(100, (st.downloaded / st.total) * 100) : 0;
    ui.bar.style.width = `${pct}%`;
    if (w.installing) {
      ui.progressText.textContent =
        "安装程序已启动：它会先关闭 Mikasa，装好后自动打开新版本（你的资料不受影响）。";
    } else if (st.status === "verifying") {
      ui.progressText.textContent = "下载完成，正在校验安装包…";
    } else {
      ui.progressText.textContent = done ? "下载完成，可以安装了" : progressLine(w, st);
    }
  }

  // 按钮：一套按钮，按状态决定"显示哪些、叫什么、能不能点"。
  // offer = "还没在下载"（done 也算：下完了照样可以「跳过此版本」/「以后再说」）
  const offer = !active && st.status !== "error";
  const ready = st.status === "done" && Boolean(st.ready);
  // 自动安装只在"Windows + 这个版本确实带了安装包资产"时成立；任一不成立
  // 就诚实地摆出「打开发布页」，而不是给一个点了会失败的按钮
  const canInstall = Boolean(info && info.asset) && Boolean(info && info.install_supported);
  const pageUrl = pageUrlOf(info);
  const showPrimary = ready || (offer && canInstall);
  ui.laterBtn.classList.toggle("hidden", !offer);
  ui.skipBtn.classList.toggle("hidden", !offer);
  ui.keepBtn.classList.toggle("hidden", !active);
  ui.primaryBtn.classList.toggle("hidden", active || !showPrimary);
  ui.primaryBtn.textContent =
    st.status === "done" ? "立即安装" : st.status === "error" ? "重新下载" : "下载并安装";
  ui.pageBtn.classList.toggle("hidden", !pageUrl && !offer);
  // 安装阶段所有按钮都禁用（点什么都不该有反应）；POST 在飞时只按住主按钮
  ui.actions.querySelectorAll(".btn").forEach((b) => {
    b.disabled = Boolean(w.installing);
  });
  ui.primaryBtn.disabled = Boolean(w.installing || w.pending);
  ui.pageBtn.disabled = Boolean(w.installing || w.pending || !pageUrl);

  if (offer && !ready && !canInstall) {
    if (!ui.hint) {
      ui.hint = el("div", { class: "upd-hint" });
      ui.actions.before(ui.hint);
    }
    ui.hint.textContent = "当前平台不支持自动安装，请到发布页下载安装包。";
  } else if (ui.hint) {
    ui.hint.remove();
    ui.hint = null;
  }
}

/* ---------------- 跟随任务 ---------------- */

function scheduleTick(w, delay) {
  if (w.timer !== null) clearTimeout(w.timer);
  w.timer = setTimeout(() => void tick(w), delay);
}

async function tick(w) {
  w.timer = null;
  const st = await fetchStatus();
  if (st) applyStatus(w, st);
  const active = Boolean(w.st) && (w.st.status === "running" || w.st.status === "verifying");
  scheduleTick(w, active ? POLL_MS : IDLE_POLL_MS);
}

/** 把一份快照装进观察窗并渲染（含"完成那一刻弹窗开着就自动装"）。 */
function applyStatus(w, st) {
  const before = w.st;
  const now = performance.now();
  if (st.status === "running" || st.status === "verifying") {
    if (w.lastBytes > 0 && st.downloaded < w.lastBytes) {
      // 从头重来了（毒前缀/换包）：速度归零，采样基准也得跟着落回去
      w.speed = 0;
      w.lastBytes = st.downloaded;
      w.lastTime = now;
    }
    if (st.downloaded > w.lastBytes) {
      const dt = (now - w.lastTime) / 1000;
      const instant = dt > 0 ? (st.downloaded - w.lastBytes) / dt : 0;
      if (instant > 0) w.speed = w.speed ? w.speed * 0.7 + instant * 0.3 : instant;
      w.lastBytes = st.downloaded;
      w.lastTime = now;
      w.advancedAt = now;
    }
  }
  w.st = st;
  render(w);
  const finished = st.status === "done" && (!before || before.status !== "done");
  if (finished && w.dialog && w.st.ready) void installNow(w);
}

/** 只跟随状态，不发 POST：任务已在跑（或已经下完）时这就是全部该做的事。 */
async function followExisting(w) {
  const st = await fetchStatus();
  if (st) applyStatus(w, st);
  // 无条件重排：空闲档是 15 秒一次，刚点了下载还按那个节奏走就会"卡着不动"
  // （scheduleTick 会清掉在等的那一个，所以不会叠出第二个循环）
  scheduleTick(w, 0);
}

/* ---------------- 动作 ---------------- */

async function startOrAdopt(w) {
  const st = w.st;
  if (st && (st.status === "running" || st.status === "verifying")) {
    await followExisting(w); // 已经在下了：接上就行
    return;
  }
  if (st && st.status === "done") {
    await installNow(w);
    return;
  }
  w.pending = true;
  render(w);
  try {
    await apiFetch("/api/update/download", { method: "POST" });
  } catch (err) {
    // 服务端可能已经把任务跑起来了（响应丢了、或它回了个"已在进行"）：
    // 先看状态再决定要不要报错——别对着一个正在下的任务说"启动失败"
    const st2 = await fetchStatus();
    if (!st2 || st2.status === "idle" || st2.status === "error") {
      w.pending = false;
      showError(w, `启动下载失败：${err.message}`);
      return;
    }
  }
  w.pending = false;
  await followExisting(w);
}

async function installNow(w) {
  if (w.installing) return;
  w.installing = true;
  render(w); // 标题转"正在安装"、文案转"安装程序已启动"，按钮全部按住
  try {
    await apiFetch("/api/update/install", { method: "POST" });
  } catch (err) {
    w.installing = false;
    showError(w, `启动安装程序失败：${err.message}`);
  }
}

function showError(w, message) {
  w.st = { status: "error", error: message, version: versionOf(w), downloaded: 0, total: 0 };
  if (w.dialog) render(w);
  renderPill(w.st);
  toast(message, "error");
}

/* ---------------- 检查更新 ---------------- */

/**
 * 查一次最新版本。返回检查结果（失败返回 null）。
 *
 * @param {object} [opts]
 * @param {boolean} [opts.manual] 用户主动点「检查更新」：结果（含"已是最新"）
 *                                与失败都用 toast 说出来；启动时的静默检查不打扰。
 */
export async function checkForUpdate({ manual = false } = {}) {
  const w = ensureWatcher();
  let info;
  try {
    info = await apiFetch(`/api/update/check${manual ? "?force=true" : ""}`);
  } catch (err) {
    if (manual) toast(`检查更新失败：${err.message}`, "error");
    else console.info("[update] 检查更新失败（已静默忽略）：", err.message);
    return null;
  }
  w.info = info;

  if (manual) clearSkippedVersion(); // 手动检查是"我现在就想知道"，跳过标记作废

  if (!info.update_available) {
    if (manual) toast(`已是最新版本（v${info.current}）`, "ok");
    return info;
  }
  if (!manual && info.latest === skippedVersion()) return info; // 用户说过别再提
  if ($("#onboard") && !w.st) return info; // 首启引导优先（下载中除外）
  openUpdateDialog();
  return info;
}

/* ---------------- 页面接线 ---------------- */

/**
 * 其余三页的入口：只挂胶囊 + 跟随已有任务。
 * **不打 /api/update/check**——关掉"启动时检查"之后就不该有任何自动联网。
 */
export function initUpdateBadge() {
  const w = ensureWatcher();
  ensurePill();
  renderPill(w.st);
  void followExisting(w);
}

/**
 * 问答页启动时调用（qa.js 的 initTopbar().then）。
 * @param {object|null} health /api/health 的响应（版本号显示用，可为空）
 */
export function initUpdate(health) {
  const w = ensureWatcher();
  const versionLabel = $("#s-version");
  if (versionLabel && health?.version) versionLabel.textContent = `v${health.version}`;
  w.current = health?.version || "";

  const box = $("#s-check-updates");
  if (box) {
    box.checked = checkUpdatesEnabled();
    box.addEventListener("change", () => setCheckUpdatesEnabled(box.checked));
  }
  const btn = $("#s-check-update");
  if (btn) btn.addEventListener("click", () => void checkForUpdate({ manual: true }));

  ensurePill();
  void (async () => {
    // 恢复态：服务进程里可能还有任务在跑（切页/刷新回来，或上次没下完）。
    // 正在下载就自动把弹窗接回来（用户主动收起过就不再弹，胶囊一直在）。
    await followExisting(w);
    const active = w.st && (w.st.status === "running" || w.st.status === "verifying");
    if (active && storageGet(DISMISS_KEY, sessionStorage) !== w.st.version) openUpdateDialog();
    if (!checkUpdatesEnabled()) return;
    // 静默检查放在最后：它可能弹窗，用户的注意力应先在主界面上
    await checkForUpdate();
  })();
}
