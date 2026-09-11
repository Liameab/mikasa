/* =========================================================================
   会话树 DOM 层（qa.js 的会话栏专用模块）：文件夹树渲染 + 行内管理。

   形态 = 编译器左侧文件树：文件夹可多层嵌套（caret ▸/▾ 开合），会话
   归入文件夹批量管理。交互 = **菜单 + 拖拽双通道**（同一 PATCH 语义）：
     拖拽 —— 会话行按住拖到 文件夹行 = 移入（若该文件夹处于折叠态，
             移入成功后自动展开一次作确认）；拖到某条会话行 = 移入它所在
             的文件夹；拖到列表空白区 = 移到根级。文件夹不可拖（仍走菜单）。
     视觉约定（样式表同名规则，两侧需同步）：文件夹行 = 开合箭头套品牌
     绿圆环 + 常驻淡绿底；夹在文件夹里的会话 = 琥珀小圈（in-folder 类，
     本文件唯一新增标记位）；根级普通会话无圈、保持原样。
     ⋯ 菜单  —— 会话：重命名 / 移动到… / AI 提炼标题 / 删除
                 文件夹：展开收起 / 新建子文件夹 / 重命名 / 移动到… / 删除
     移动到… —— 子菜单列全部文件夹路径表（根级置顶；文件夹目标
                 自身+后代灰显 = 与后端 409 同源的防环判据）
     行内输入 —— 新建/重命名共用：Enter 提交、Esc 取消、点外取消

   安全纪律沿用 common.js：动态文本一律经 el() 文本节点 / esc()，
   无内联 onclick（全部 addEventListener）。与 qa.js 零循环依赖：
   qa.js 只 import 本模块公开面；会话点击等行为经 initSidebar 注入
   的回调回拨（busy 守卫在 qa.js 侧，本模块菜单弹开前用 isBusy
   预检同款静默——流式期间会话/菜单都不响应，防消息串台）。
   ========================================================================= */

import { $, apiFetch, el, esc, fmtTime, toast } from "./common.js";
import { confirmDialog } from "./confirm.js"; // 自定义确认框（替换原生 confirm）
import { buildTree, displayTitle, folderPath, subtreeIds } from "./tree.js";

/* ---------------- 状态 ---------------- */

const listBox = $("#session-list"); // 树容器（对话区归 qa.js）
let foldersCache = []; // 最近一次平铺文件夹（移动菜单路径表数据源）
let lastTree = { roots: [], unowned: [] }; // 最近一次组树结果（本地重绘用）
let expanded = new Set(); // 展开的文件夹 id（不持久化：刷新页面回到全折叠）
let activeSession = null; // 高亮同步自 qa.js（open/new/删除后 setActiveSession）
let isBusy = () => false; // initSidebar 注入：流式中禁菜单/禁切换
let onOpenSession = null; // 回拨 qa.js：点会话行 → 回放消息
let onActiveCleared = null; // 回拨 qa.js：删的恰是当前会话 → 清回欢迎态

/** 单例菜单：body 级 fixed，任何时刻至多一个（重复打开先关旧的）。 */
const menuEl = el("div", { id: "ctx-menu" });
menuEl.style.display = "none";

let anchorX = 0; // ⋯ 点击坐标（移动子菜单"← 返回"回主菜单时沿用）
let anchorY = 0;
let lastMain = null; // 主菜单快照：{kind:"folder"|"session", target, depth}

const INDENT = 16; // 每层缩进（px）

/* 拖拽（DnD）状态：dragSessionId 为空 = 非本树拖拽（dragover 不拦截） */
let dragSessionId = null; // 拖拽中的会话 id（源行）
let dragOverEl = null;    // 当前绿框高亮的目标行（换目标/离开时清除）

/* =========================================================================
   公开面：qa.js 入口接线 + 刷新 + 高亮同步
   ========================================================================= */

/**
 * 会话树入口（qa.js 启动时调用一次）。handlers：
 *   isBusy()          —— 流式期间 true：菜单/会话切换静默失效
 *   onOpenSession(id) —— 点会话行：qa.js 回放消息
 *   onActiveCleared() —— 删除的恰是当前会话：qa.js 清回欢迎态
 */
export function initSidebar(handlers) {
  isBusy = handlers.isBusy;
  onOpenSession = handlers.onOpenSession;
  onActiveCleared = handlers.onActiveCleared;
  document.body.append(menuEl);
  document.addEventListener("click", (ev) => {
    if (!ev.target.closest("#ctx-menu")) closeMenu(); // 点菜单外关闭
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") closeMenu();
  });
  $("#new-folder").addEventListener("click", () => {
    if (isBusy()) return;
    beginCreateInput(null, 0); // 根级新建：输入行插到树顶
  });
  attachDragHandlers();
}

/* 会话行拖拽（DnD，桌面交互）：事件委托在树容器上，行重建后监听不丢。
   协议要点：dragover 必须 preventDefault 才放行 drop；行点击与拖拽天然
   互斥（真实拖动后浏览器不再派发 click）。文件夹不可拖（仍走菜单）。 */
function attachDragHandlers() {
  listBox.addEventListener("dragstart", (ev) => {
    const row = ev.target.closest(".session-item");
    if (!row || isBusy()) return; // 流式中禁拖（与菜单同款守卫）
    dragSessionId = Number(row.dataset.sessionId);
    row.classList.add("dragging"); // 源行半透明
    ev.dataTransfer.effectAllowed = "move";
    ev.dataTransfer.setData("text/plain", String(dragSessionId));
  });
  listBox.addEventListener("dragover", (ev) => {
    if (dragSessionId === null) return; // 非本树拖拽：交给浏览器默认行为
    const t = dragTarget(ev);
    if (!t) return; // 拖到自己上：不放行，无绿框
    ev.preventDefault(); // 允许 drop（DnD 协议：不放行则 drop 不触发）
    ev.dataTransfer.dropEffect = "move";
    if (dragOverEl !== t.row) {
      if (dragOverEl) dragOverEl.classList.remove("drag-over");
      dragOverEl = t.row;
      t.row?.classList.add("drag-over"); // row 为 null（空白区）= 移到根级，无框
    }
  });
  listBox.addEventListener("drop", async (ev) => {
    if (dragSessionId === null) return;
    const t = dragTarget(ev);
    if (!t) return;
    ev.preventDefault();
    const from = dragSessionId;
    clearDrag(); // 先收尾：源行将随重绘消失，dragend 可能不再触发
    if (t.expandId !== null && !expanded.has(t.expandId)) expanded.add(t.expandId);
    await moveTo("session", from, t.folderId); // 与菜单"移动到…"同一 PATCH 语义
  });
  listBox.addEventListener("dragend", clearDrag); // 取消/拖到树外兜底
}

/** drop 目标解析：文件夹行=移入它；会话行=与它同文件夹；空白=根级。
 *  返回 null 表示"拖到自己上"（无意义，不放行）。 */
function dragTarget(ev) {
  const row = ev.target.closest(".t-folder, .session-item");
  if (!row) return { row: null, folderId: null, expandId: null }; // 空白 → 根级
  if (row.classList.contains("t-folder")) {
    const id = Number(row.dataset.folderId);
    return { row, folderId: id, expandId: id }; // expandId：折叠目标移入后自动展开确认
  }
  if (Number(row.dataset.sessionId) === dragSessionId) return null; // 拖到自己
  const target = [...walkSessions(lastTree)].find(
    (s) => s.id === Number(row.dataset.sessionId)
  );
  return { row, folderId: target?.folder_id ?? null, expandId: null };
}

/** 清拖拽态：解除源行半透明、移除目标行绿框、复位会话 id。 */
function clearDrag() {
  dragSessionId = null;
  if (dragOverEl) {
    dragOverEl.classList.remove("drag-over");
    dragOverEl = null;
  }
  for (const e of listBox.querySelectorAll(".dragging")) e.classList.remove("dragging");
}

/** 拉 sessions + folders → 组树重绘（首载/列表/新建/改名/移动后各一次）。 */
export async function refreshSessions() {
  try {
    const [foldersResp, sessionsResp] = await Promise.all([
      apiFetch("/api/folders"),
      apiFetch("/api/sessions"),
    ]);
    foldersCache = foldersResp.folders;
    lastTree = buildTree(foldersCache, sessionsResp.sessions);
    renderTree();
  } catch (err) {
    toast(`会话列表加载失败：${err.message}`, "error");
  }
}

/** 高亮同步（qa.js 打开/新建/删当前会话后调用）：无网络，只本地重绘。 */
export function setActiveSession(id) {
  activeSession = id;
  renderTree();
}

/* =========================================================================
   渲染：文件夹递归 + 会话行（data-id 供查找助手定位）
   ========================================================================= */

function renderTree() {
  listBox.innerHTML = "";
  if (!lastTree.roots.length && !lastTree.unowned.length) {
    listBox.append(el("div", { class: "empty" }, "还没有会话，发第一问试试"));
    return;
  }
  for (const node of lastTree.roots) listBox.append(renderFolder(node, 0));
  for (const session of lastTree.unowned) listBox.append(sessionRow(session, 0));
}

/** 文件夹节点：caret + 名字 + ⋯ 一行；子夹/会话在展开容器里递归。 */
function renderFolder(node, depth) {
  const { folder } = node;
  const open = expanded.has(folder.id);
  const row = el(
    "div",
    { class: "t-row t-folder", role: "treeitem", "data-folder-id": folder.id },
    el("span", { class: "t-caret", "aria-hidden": "true" }, open ? "▾" : "▸"),
    el(
      "span",
      { class: "t-name", title: folderPath(foldersCache, folder.id).join(" / ") },
      folder.name
    ),
    el("span", { class: "grow" })
  );
  row.style.paddingLeft = `${10 + depth * INDENT}px`;
  const more = el("button", { type: "button", class: "t-more", title: "更多操作" }, "⋯");
  more.addEventListener("click", (ev) => {
    ev.stopPropagation();
    if (isBusy()) return; // 流式中菜单静默失效
    openFolderMenu(ev, node, depth);
  });
  row.append(more);

  // 行点击 = 开合（按钮区除外：⋯ 自己处理；行内改名输入框内点击要留给
  // 光标定位，不得触发重绘——2026-09-09 实测：漏排 input 则一点就关）
  row.addEventListener("click", (ev) => {
    if (ev.target.closest("button, .tree-input")) return;
    toggleFolder(folder.id);
  });

  const wrap = el("div"); // 展开容器：子夹在前、子会话在后
  if (open) {
    for (const child of node.children) wrap.append(renderFolder(child, depth + 1));
    for (const session of node.sessions) wrap.append(sessionRow(session, depth + 1));
    if (!wrap.childElementCount) wrap.append(el("div", { class: "t-empty" }, "空文件夹"));
  }
  return el("div", { class: "t-node" }, row, wrap);
}

/** 会话行：标题（displayTitle 处理 title=null）+ 时间计数 + ⋯。 */
function sessionRow(session, depth) {
  // depth > 0 = 夹在文件夹内 → 加 in-folder 类（CSS 配琥珀圈）；
  // 根级普通会话（depth 0）不加类，样式保持原样
  const item = el("div", {
    class: `session-item${session.id === activeSession ? " active" : ""}${depth > 0 ? " in-folder" : ""}`,
    role: "treeitem",
    "data-session-id": session.id,
  });
  item.style.paddingLeft = `${10 + depth * INDENT}px`;
  item.setAttribute("draggable", "true"); // 会话行可拖（文件夹行不设）
  item.title = "按住拖到文件夹或空白处可移动"; // 拖拽入门的轻提示
  const body = el(
    "div",
    { class: "s-body" },
    el("div", { class: "s-title" }, displayTitle(session)),
    el(
      "div",
      { class: "s-meta" },
      `${fmtTime(session.created_at)} · ${session.message_count} 条消息`
    )
  );
  const more = el("button", { type: "button", class: "t-more", title: "更多操作" }, "⋯");
  more.addEventListener("click", (ev) => {
    ev.stopPropagation();
    if (isBusy()) return;
    openSessionMenu(ev, session, depth);
  });
  item.append(body, more);
  item.addEventListener("click", (ev) => {
    if (ev.target.closest("button, .tree-input")) return; // 含改名输入框（同 folder 行）
    if (isBusy()) return; // 流式中不切会话（qa.js openSession 也有同款守卫）
    onOpenSession(session.id);
  });
  return item;
}

function toggleFolder(id) {
  if (expanded.has(id)) expanded.delete(id);
  else expanded.add(id);
  renderTree();
}

/* =========================================================================
   菜单（⋯）：构建 × 三类 + 位置显示
   ========================================================================= */

function closeMenu() {
  menuEl.style.display = "none";
  menuEl.innerHTML = "";
}

/** 显示菜单：先 display:block 才能量到尺寸，再夹在视口内。 */
function showMenuAt(x, y) {
  menuEl.style.display = "block";
  const w = menuEl.offsetWidth || 180;
  const h = menuEl.offsetHeight || 40;
  menuEl.style.left = `${Math.max(4, Math.min(x, window.innerWidth - w - 6))}px`;
  menuEl.style.top = `${Math.max(4, Math.min(y, window.innerHeight - h - 6))}px`;
}

/** 菜单项：label + 行为；disabled 灰显不响应（aria-disabled 语义）。 */
function menuItem(label, onPick, { danger = false, disabled = false, title = "" } = {}) {
  const node = el("div", {
    class: `ctx-item${danger ? " danger" : ""}${disabled ? " disabled" : ""}`,
    title,
    role: "menuitem",
  }, label);
  node.addEventListener("click", (ev) => {
    ev.stopPropagation();
    if (disabled) return;
    closeMenu();
    onPick();
  });
  return node;
}

function openFolderMenu(ev, node, depth) {
  anchorX = ev.clientX;
  anchorY = ev.clientY;
  lastMain = { kind: "folder", target: node, depth };
  buildFolderMenu(node, depth);
  showMenuAt(anchorX, anchorY);
}

function buildFolderMenu(node, depth) {
  const { folder } = node;
  const open = expanded.has(folder.id);
  const hasChildren = node.children.length + node.sessions.length > 0;
  menuEl.innerHTML = "";
  if (hasChildren) {
    menuEl.append(menuItem(open ? "收起" : "展开", () => toggleFolder(folder.id)));
  }
  menuEl.append(
    menuItem("新建子文件夹", () => beginCreateInput(folder.id, depth + 1)),
    menuItem("重命名", () => beginRenameFolder(folder)),
    menuItem("移动到…", () => buildMoveMenu({ kind: "folder", id: folder.id })),
    el("div", { class: "ctx-sep" }),
    menuItem("删除", () => deleteFolder(folder), { danger: true })
  );
}

function openSessionMenu(ev, session, depth) {
  anchorX = ev.clientX;
  anchorY = ev.clientY;
  lastMain = { kind: "session", target: session, depth };
  buildSessionMenu(session, depth);
  showMenuAt(anchorX, anchorY);
}

function buildSessionMenu(session, depth) {
  menuEl.innerHTML = "";
  menuEl.append(
    menuItem("重命名", () => beginRenameSession(session)),
    menuItem("移动到…", () => buildMoveMenu({ kind: "session", id: session.id })),
    el("div", { class: "ctx-sep" }),
    menuItem("✨ AI 提炼标题", () => suggestTitle(session.id)),
    menuItem("删除", () => deleteSession(session), { danger: true })
  );
}

/**
 * 移动子菜单：原地替换菜单内容（"← 返回"回主菜单）。
 * 目标 = {kind: folder|session, id}；blockedIds = 文件夹的自身+后代
 * （防环：与后端 409 同源判据）；当前位置灰显。
 */
function buildMoveMenu(target) {
  const kind = target.kind;
  const id = target.id;
  const targetRow =
    kind === "folder"
      ? foldersCache.find((f) => f.id === id)
      : [...walkSessions(lastTree)].find((s) => s.id === id);
  if (!targetRow) return; // 缓存过期找不到：不弹
  const currentParent = kind === "folder" ? targetRow.parent_id : targetRow.folder_id;
  const blocked = kind === "folder" ? subtreeIds(foldersCache, id) : new Set();

  menuEl.innerHTML = "";
  menuEl.append(
    el(
      "div",
      { class: "ctx-head" },
      `移动「${esc(kind === "folder" ? targetRow.name : displayTitle(targetRow))}」`
    ),
    menuItem("← 返回", () => showMainMenu()),
    menuItem("（根级）", () => moveTo(kind, id, null), {
      disabled: currentParent === null,
      title: "移到根级：不进任何文件夹",
    })
  );
  for (const f of foldersCache) {
    menuEl.append(
      menuItem(folderPath(foldersCache, f.id).join(" / "), () => moveTo(kind, id, f.id), {
        disabled: f.id === currentParent || blocked.has(f.id),
        title: blocked.has(f.id) ? "不能移入自身或其子文件夹" : "",
      })
    );
  }
  showMenuAt(anchorX, anchorY); // 原地换内容，位置不变
}

/** "← 返回"：按 lastMain 快照重建主菜单（沿用 ⋯ 时的坐标）。 */
function showMainMenu() {
  if (!lastMain) return;
  if (lastMain.kind === "folder") buildFolderMenu(lastMain.target, lastMain.depth);
  else buildSessionMenu(lastMain.target, lastMain.depth);
  showMenuAt(anchorX, anchorY);
}

async function moveTo(kind, id, parentId) {
  const path = kind === "folder" ? `folders/${id}` : `sessions/${id}`;
  const body = kind === "folder" ? { parent_id: parentId } : { folder_id: parentId };
  try {
    await apiFetch(`/api/${path}`, { method: "PATCH", body: JSON.stringify(body) });
    refreshSessions();
  } catch (err) {
    toast(`移动失败：${err.message}`, "error");
  }
}

/* =========================================================================
   行内输入（新建 / 重命名共用交互）：Enter 提交、Esc 取消、点外取消
   ========================================================================= */

/**
 * 构造行内输入框并接好键盘/失焦行为。
 * initial：现值（空串 = 新建）；onSubmit(value)：非空且有改动才调用。
 * cancel：恢复原状（新建 = 移除输入行；改名 = 还原原 span）。改动为空
 * 或未改一律走 cancel 语义（空名 400 不该发出去——清除是显式菜单操作）。
 */
function editInput(initial, maxlength, onSubmit, cancel) {
  const input = el("input", {
    class: "tree-input",
    type: "text",
    maxlength,
    placeholder: "名称，Enter 确定",
  });
  input.value = initial;
  const submit = () => {
    const value = input.value.trim();
    if (!value || value === initial) {
      cancel();
      return;
    }
    if (input.disabled) return; // 请求在途防重复
    input.disabled = true;
    onSubmit(value).catch((err) => {
      // 失败：恢复可编辑并把错误原因吐给用户
      input.disabled = false;
      input.focus();
      toast(`保存失败：${err.message}`, "error");
    });
  };
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      submit();
    } else if (ev.key === "Escape") {
      ev.preventDefault();
      cancel();
    }
  });
  // 点外取消；isConnected 守卫：树整刷后输入框已被移除，blur 不再生效
  input.addEventListener("blur", () => {
    if (input.isConnected && !input.disabled) cancel();
  });
  return { input, submit };
}

/** 新建文件夹输入行（parentId=null 根级；子级先展开文件夹再插入）。 */
function beginCreateInput(parentId, depth) {
  const row = el("div", { class: "tree-edit-row" });
  row.style.paddingLeft = `${10 + depth * INDENT}px`;
  const { input } = editInput("", 50, async (name) => {
    await apiFetch("/api/folders", {
      method: "POST",
      body: JSON.stringify({ name, parent_id: parentId }),
    });
    refreshSessions();
  }, () => row.remove());
  row.append(input);
  if (parentId === null) {
    listBox.prepend(row); // 根级：树顶输入行
  } else {
    expanded.add(parentId); // 展开保证新输入行可见
    const wrap = folderWrap(parentId);
    if (wrap) {
      wrap.prepend(row);
    } else {
      renderTree();
      folderWrap(parentId)?.prepend(row); // 树刚渲染完，wrap 必在
    }
  }
  input.focus();
}

/** 文件夹行内改名：t-name span → input（cancel 还原 span）。 */
function beginRenameFolder(folder) {
  const span = findFolderNameSpan(folder.id);
  if (!span) return;
  const restore = () => {
    if (input.isConnected) input.replaceWith(span);
  };
  const { input } = editInput(folder.name, 50, async (name) => {
    await apiFetch(`/api/folders/${folder.id}`, {
      method: "PATCH",
      body: JSON.stringify({ name }),
    });
    refreshSessions();
  }, restore);
  span.replaceWith(input);
  input.focus();
}

/** 会话行内改名：s-title span → input（cancel 还原 span）。 */
function beginRenameSession(session) {
  const span = findSessionTitleSpan(session.id);
  if (!span) return;
  const restore = () => {
    if (input.isConnected) input.replaceWith(span);
  };
  const { input } = editInput(displayTitle(session), 100, async (title) => {
    await apiFetch(`/api/sessions/${session.id}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    });
    refreshSessions();
  }, restore);
  span.replaceWith(input);
  input.focus();
}

/* =========================================================================
   删除 / AI 提炼标题
   ========================================================================= */

async function deleteSession(session) {
  const title = displayTitle(session);
  const ok = await confirmDialog({
    title: `删除会话「${title}」？`,
    detail: "其中的提问与回答将一并删除，不可恢复。",
  });
  if (!ok) return;
  try {
    await apiFetch(`/api/sessions/${session.id}`, { method: "DELETE" });
    if (session.id === activeSession) onActiveCleared(); // 删的是当前会话：清回欢迎态
    refreshSessions();
  } catch (err) {
    toast(`删除失败：${err.message}`, "error");
  }
}

async function deleteFolder(folder) {
  const ok = await confirmDialog({
    title: `删除文件夹「${folder.name}」？`,
    detail: "只允许删除空文件夹：内含子文件夹或会话会被拒绝（409），需先移出。",
  });
  if (!ok) return;
  try {
    await apiFetch(`/api/folders/${folder.id}`, { method: "DELETE" });
    refreshSessions();
  } catch (err) {
    // 非空 409：后端文案自带子夹/会话计数
    toast(`删除失败：${err.message}`, "error");
  }
}

/** AI 提炼标题（POST title/suggest）：offline 走截断兜底、api 走 LLM。 */
async function suggestTitle(sessionId) {
  try {
    const body = await apiFetch(`/api/sessions/${sessionId}/title/suggest`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    if (body.applied) {
      toast(`已提炼标题：「${body.title}」`);
    } else {
      // 手动命名锁着：建议给出来但不覆盖（title_manual 语义）
      toast(`建议标题：「${body.title}」（已手动命名，自动提炼不覆盖）`, "warn");
    }
    refreshSessions();
  } catch (err) {
    toast(`提炼失败：${err.message}`, "error");
  }
}

/* =========================================================================
   查找助手（重绘后定位节点；找不到静默放弃——竞态窗口极小）
   ========================================================================= */

function findFolderRow(folderId) {
  return [...listBox.querySelectorAll(".t-folder")].find(
    (row) => row.dataset.folderId === String(folderId)
  );
}

function findFolderNameSpan(folderId) {
  return findFolderRow(folderId)?.querySelector(".t-name") ?? null;
}

/** 文件夹的展开容器（其 <div class="t-node"> 的第二个子节点）。 */
function folderWrap(folderId) {
  const row = findFolderRow(folderId);
  const node = row?.parentElement;
  return node?.lastElementChild ?? null;
}

function findSessionTitleSpan(sessionId) {
  const row = [...listBox.querySelectorAll(".session-item")].find(
    (r) => r.dataset.sessionId === String(sessionId)
  );
  return row?.querySelector(".s-title") ?? null;
}

/** 树内全部会话行（深遍历；unowned 根级会话在 roots 之外单列）。 */
function* walkSessions(tree) {
  const visit = function* (node) {
    yield* node.sessions;
    for (const child of node.children) yield* visit(child);
  };
  for (const root of tree.roots) yield* visit(root);
  yield* tree.unowned;
}
