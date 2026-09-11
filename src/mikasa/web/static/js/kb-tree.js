/* =========================================================================
   语料库树 DOM 层（documents.js 的左侧树专用模块，v3）：文件夹 × 文档行。

   形态 = 会话树（qa-tree.js）的镜像：本模块与它共用同一套视觉语言与
   DOM 类名（.session-item / .t-folder / .t-more …，样式表组件级通用），
   交互语义一一对应：
     拖拽 —— 文档行按住拖到 文件夹行 = 移入（折叠目标移入后自动展开）；
             拖到某条文档行 = 移入它所在的文件夹；拖到列表空白区 = 移回
             根级。文件夹不可拖（仍走菜单）。
     点击 —— 文档行 = 选中（高亮 + 右侧详情卡回填）；文件夹行 = 开合。
     ⋯ 菜单 —— 文档：重命名 / 移动到… / 删除
                文件夹：展开收起 / 新建子文件夹 / 重命名 / 移动到… / 删除
     查找 —— 顶部输入框实时过滤：文件夹按自身名字或可见后代匹配，文档
             按标题匹配；过滤期间强制展开匹配链，空结果显示占位行。
     行内输入 —— 新建/重命名共用：Enter 提交、Esc 取消、点外取消。

   端点（后端契约）：文件夹 /api/kb-folders（qa 的 /api/folders 镜像），
   文档行 /api/documents；PATCH 语义 = 字段出现才动（title 非空改名、
   folder_id 显式 null 移回根）。安全纪律同 common.js：动态文本一律经
   el() 文本节点 / esc()，无内联 onclick（全部 addEventListener）。
   与 documents.js 零循环依赖：选中/计数经 initCorpusTree 注入的回调回拨。
   ========================================================================= */

import { $, apiFetch, el, esc, toast } from "./common.js";
import { confirmDialog } from "./confirm.js"; // 自定义确认框（替换原生 confirm）
import { buildDocTree, folderPath, subtreeIds } from "./tree.js";

/* ---------------- 状态 ---------------- */

const listBox = $("#kb-tree"); // 树容器（右侧上传/详情归 documents.js）
const searchBox = $("#kb-search");
let foldersCache = []; // 最近一次平铺文件夹（移动菜单路径表数据源）
let docsCache = []; // 最近一次平铺文档行（计数 + 选中详情数据源）
let lastTree = { roots: [], unowned: [] }; // 最近一次组树结果（本地重绘用）
let expanded = new Set(); // 展开的文件夹 id（不持久化：刷新页面回到全折叠）
let activeDocId = null; // 选中高亮（点行 → documents.js 回填详情卡）
let searchText = ""; // 查找框当前词（小写化后）；空串 = 不过滤
let visFolders = new Set(); // 查找模式下可见的文件夹 id（祖先链展开用）
let onSelect = null; // 回拨 documents.js：选中/取消选中 → 详情卡
let onCount = null; // 回拨 documents.js：文档总数 → 标题计数 pill
// 回拨 documents.js：点文档行 → 打开阅读面板。**刻意与 onSelect 分开**：
// onSelect 会被 notifyActive() 复用，而 notifyActive 在每次 refreshCorpusTree
// （上传/删除/改名/拖拽落定后都触发）都会跑——并进 onSelect 会导致
// "拖一篇文档进文件夹就自动弹出阅读面板"。
let onOpen = null;

/** 单例菜单：body 级 fixed，任何时刻至多一个（重复打开先关旧的）。 */
const menuEl = el("div", { id: "ctx-menu" });
menuEl.style.display = "none";

let anchorX = 0; // ⋯ 点击坐标（移动子菜单"← 返回"回主菜单时沿用）
let anchorY = 0;
let lastMain = null; // 主菜单快照：{kind:"folder"|"doc", target, depth}

const INDENT = 16; // 每层缩进（px）

/* 拖拽（DnD）状态：dragDocId 为空 = 非本树拖拽（dragover 不拦截） */
let dragDocId = null; // 拖拽中的文档行 id（源行）
let dragOverEl = null; // 当前绿框高亮的目标行（换目标/离开时清除）

/* =========================================================================
   公开面：documents.js 入口接线 + 刷新
   ========================================================================= */

/**
 * 语料树入口（documents.js 启动时调用一次）。handlers：
 *   onSelect(doc, location) —— 点文档行：documents.js 回填右侧详情卡；
 *                              取消选中（删除/空库）回调 onSelect(null, "")；
 *   onCount(n)               —— 文档总数（右侧标题计数 pill）。
 *   onOpen(doc, location)    —— 点文档行：在右栏打开阅读区（只在真实点行时
 *                               触发，刷新重绘走 notifyActive 不触发）。
 */
export function initCorpusTree(handlers) {
  onSelect = handlers.onSelect;
  onCount = handlers.onCount;
  onOpen = handlers.onOpen;
  document.body.append(menuEl);
  document.addEventListener("click", (ev) => {
    if (!ev.target.closest("#ctx-menu")) closeMenu(); // 点菜单外关闭
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") closeMenu();
  });
  $("#new-kb-folder").addEventListener("click", () => {
    beginCreateInput(null, 0); // 根级新建：输入行插到树顶
  });
  // 查找：实时过滤 + Esc 清空（type=search 自带 ✕ 清空钮，同走 input 事件）
  searchBox.addEventListener("input", () => {
    searchText = searchBox.value.trim().toLowerCase();
    renderTree();
  });
  attachDragHandlers();
}

/* 文档行拖拽（DnD，桌面交互）：事件委托在树容器上，行重建后监听不丢。
   协议要点：dragover 必须 preventDefault 才放行 drop；行点击与拖拽天然
   互斥（真实拖动后浏览器不再派发 click）。文件夹不可拖（仍走菜单）。 */
function attachDragHandlers() {
  listBox.addEventListener("dragstart", (ev) => {
    const row = ev.target.closest(".doc-item");
    if (!row) return;
    dragDocId = Number(row.dataset.docId);
    row.classList.add("dragging"); // 源行半透明
    ev.dataTransfer.effectAllowed = "move";
    ev.dataTransfer.setData("text/plain", String(dragDocId));
  });
  listBox.addEventListener("dragover", (ev) => {
    if (dragDocId === null) return; // 非本树拖拽：交给浏览器默认行为
    const t = dragTarget(ev);
    if (!t) return; // 拖到自己上：不放行，无绿框
    ev.preventDefault(); // 允许 drop（DnD 协议：不放行则 drop 不触发）
    ev.dataTransfer.dropEffect = "move";
    if (dragOverEl !== t.row) {
      if (dragOverEl) dragOverEl.classList.remove("drag-over");
      dragOverEl = t.row;
      t.row?.classList.add("drag-over"); // row 为 null（空白区）= 移回根，无框
    }
  });
  listBox.addEventListener("drop", async (ev) => {
    if (dragDocId === null) return;
    const t = dragTarget(ev);
    if (!t) return;
    ev.preventDefault();
    const from = dragDocId;
    clearDrag(); // 先收尾：源行将随重绘消失，dragend 可能不再触发
    if (t.expandId !== null && !expanded.has(t.expandId)) expanded.add(t.expandId);
    await moveTo("doc", from, t.folderId); // 与菜单"移动到…"同一 PATCH 语义
  });
  listBox.addEventListener("dragend", clearDrag); // 取消/拖到树外兜底
}

/** drop 目标解析：文件夹行=移入它；文档行=与它同夹；空白=根级。
 *  返回 null 表示"拖到自己上"（无意义，不放行）。 */
function dragTarget(ev) {
  const row = ev.target.closest(".t-folder, .doc-item");
  if (!row) return { row: null, folderId: null, expandId: null }; // 空白 → 根级
  if (row.classList.contains("t-folder")) {
    const id = Number(row.dataset.folderId);
    return { row, folderId: id, expandId: id }; // expandId：折叠目标移入后自动展开确认
  }
  if (Number(row.dataset.docId) === dragDocId) return null; // 拖到自己
  const target = [...walkDocs(lastTree)].find(
    (d) => d.id === Number(row.dataset.docId)
  );
  return { row, folderId: target?.folder_id ?? null, expandId: null };
}

/** 清拖拽态：解除源行半透明、移除目标行绿框、复位文档 id。 */
function clearDrag() {
  dragDocId = null;
  if (dragOverEl) {
    dragOverEl.classList.remove("drag-over");
    dragOverEl = null;
  }
  for (const e of listBox.querySelectorAll(".dragging")) e.classList.remove("dragging");
}

/** 拉文档 + kb 文件夹 → 组树重绘（首载/上传/改名/移动/删除后各一次）。 */
export async function refreshCorpusTree() {
  try {
    const [docsResp, foldersResp] = await Promise.all([
      apiFetch("/api/documents"),
      apiFetch("/api/kb-folders"),
    ]);
    docsCache = docsResp.documents;
    foldersCache = foldersResp.folders;
    lastTree = buildDocTree(foldersCache, docsCache);
    if (activeDocId !== null && !docsCache.some((d) => d.id === activeDocId)) {
      clearSelection(); // 选中行已被删除：详情卡同步清空
    }
    renderTree();
    onCount?.(docsCache.length);
    notifyActive(); // 行数据变了（改名/移动后）详情卡跟着刷新
  } catch (err) {
    toast(`语料加载失败：${err.message}`, "error");
  }
}

/** 清除选中（删除当前文档/树被清空时）：本地态 + 详情卡清空。 */
function clearSelection() {
  activeDocId = null;
  onSelect?.(null, "");
}

/** 清空选中并重绘（阅读区 ✕ 收起时由 documents.js 调用，行高亮一并去掉）。 */
export function clearDocSelection() {
  clearSelection();
  renderTree();
}

/** 选中态同步：活动文档仍在库 → 把最新行数据推给详情卡（改名/移夹后
 *  详情卡不用等重新点击就自我刷新）。 */
function notifyActive() {
  const doc = activeDocId !== null ? docsCache.find((d) => d.id === activeDocId) : null;
  if (doc) onSelect?.(doc, locationText(doc));
}

/** 文档位置文案（详情卡"位置"行）：文件夹路径或"根级"。 */
function locationText(doc) {
  if (doc.folder_id === null || doc.folder_id === undefined) return "根级（未归档）";
  const path = folderPath(foldersCache, doc.folder_id);
  return path.length ? path.join(" / ") : "根级（未归档）"; // 脏数据兜底
}

/* =========================================================================
   渲染：文件夹递归 + 文档行（查找模式过滤 + 强制展开匹配链）
   ========================================================================= */

/** 文档是否匹配当前查找词（标题是文档的显示身份）。 */
function docMatch(doc) {
  return !searchText || String(doc.title || "").toLowerCase().includes(searchText);
}

/** 查找模式可见性预扫：文件夹可见 = 自身名字命中 ∨ 任一后代文件夹可见 ∨
 *  直接夹着命中文档。结果存 visFolders；非查找模式跳过（全可见）。 */
function scanVisible() {
  visFolders = new Set();
  if (!searchText) return;
  const scan = (node) => {
    const selfMatch = String(node.folder.name || "").toLowerCase().includes(searchText);
    let any = selfMatch;
    for (const child of node.children) if (scan(child)) any = true;
    if (node.docs.some(docMatch)) any = true;
    if (any) visFolders.add(node.folder.id);
    return any;
  };
  for (const root of lastTree.roots) scan(root);
}

function renderTree() {
  scanVisible();
  listBox.innerHTML = "";
  // 判据必须是**建好的树**而不是文档数：语料库可以一篇文档都没有却有文件夹
  // （用户新建夹子就是合法操作），按 docsCache 判会连文件夹一起挡掉——建夹
  // 请求 201 成功、行也进了库，界面上却始终只有"还没有文档"，用户以为没建成
  // 便反复新建（2026-09-11 实测：库里躺着连建的同名文件夹 5 个）。与
  // qa-tree.js 的 renderTree 对齐（那边一直是判 lastTree）。
  if (!searchText && !lastTree.roots.length && !lastTree.unowned.length) {
    listBox.append(el("div", { class: "empty" }, "还没有文档——先在右侧上传资料"));
    return;
  }
  // 统计实际渲染的可见行：查找模式下全被滤掉时给"无匹配"占位
  let shown = 0;
  for (const node of lastTree.roots) {
    if (searchText && !visFolders.has(node.folder.id)) continue;
    renderFolder(listBox, node, 0);
    shown += 1;
  }
  for (const doc of lastTree.unowned) {
    if (!docMatch(doc)) continue;
    listBox.append(docRow(doc, 0));
    shown += 1;
  }
  if (searchText && !shown) {
    listBox.append(el("div", { class: "empty" }, "没有匹配的文档或文件夹"));
  }
}

/** 文件夹节点：caret + 名字 + ⋯ 一行；子夹/文档在展开容器里递归。
 *  返回渲染出的条目数（空结果提示用）。查找模式下 open 恒真（强制展开
 *  命中链），此时行点击开合被 toggleFolder 守卫忽略。 */
function renderFolder(parent, node, depth) {
  const { folder } = node;
  const open = searchText || expanded.has(folder.id);
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
    openFolderMenu(ev, node, depth);
  });
  row.append(more);

  // 行点击 = 开合（按钮区除外：⋯ 自己处理；查找模式强制展开、不可收；
  // 行内改名输入框内点击留给光标定位，不得触发重绘——与 qa-tree 同源 bug）
  row.addEventListener("click", (ev) => {
    if (ev.target.closest("button, .tree-input")) return;
    toggleFolder(folder.id);
  });

  const wrap = el("div"); // 展开容器：子夹在前、子文档在后
  if (open) {
    for (const child of node.children) {
      if (!searchText || visFolders.has(child.folder.id)) {
        renderFolder(wrap, child, depth + 1);
      }
    }
    for (const doc of node.docs) {
      if (docMatch(doc)) wrap.append(docRow(doc, depth + 1));
    }
    // 查找模式下子项只是被过滤了，不是真空——不标"空文件夹"
    if (!wrap.childElementCount && !searchText) {
      wrap.append(el("div", { class: "t-empty" }, "空文件夹"));
    }
  }
  parent.append(el("div", { class: "t-node" }, row, wrap));
}

/**
 * 文档行：标题 + 元信息（类型 · 分块 · 字符）+ ⋯。DOM 类沿用会话行
 * （.doc-item .session-item …）：样式组件级通用——夹内文档配琥珀圈
 * （in-folder）、选中绿框（active）、拖拽源半透明（dragging）全继承。
 */
function docRow(doc, depth) {
  const item = el("div", {
    class: `doc-item session-item${doc.id === activeDocId ? " active" : ""}${depth > 0 ? " in-folder" : ""}`,
    role: "treeitem",
    "data-doc-id": doc.id,
  });
  item.style.paddingLeft = `${10 + depth * INDENT}px`;
  item.setAttribute("draggable", "true"); // 文档行可拖（文件夹行不设）
  item.title = "按住拖到文件夹或空白处可移动"; // 拖拽入门的轻提示

  const meta = el(
    "div",
    { class: "s-meta" },
    `${doc.file_type} · ${doc.chunk_count} 块 · ${doc.char_count} 字符`
  );
  if (doc.ingest_status !== "done") {
    // 非终态行（CLI 并行入库的瞬态/失败残留）：后缀标注，失败用红字
    const status = doc.ingest_status === "failed" ? "入库失败" : "入库中…";
    const mark = el("span", { class: doc.ingest_status === "failed" ? "meta-bad" : "meta-busy" }, ` · ${status}`);
    if (doc.ingest_status === "failed") item.title = doc.error_message || "入库失败";
    meta.append(mark);
  }
  const body = el(
    "div",
    { class: "s-body" },
    el("div", { class: "s-title" }, displayTitle(doc)),
    meta
  );
  const more = el("button", { type: "button", class: "t-more", title: "更多操作" }, "⋯");
  more.addEventListener("click", (ev) => {
    ev.stopPropagation();
    openDocMenu(ev, doc, depth);
  });
  item.append(body, more);
  item.addEventListener("click", (ev) => {
    if (ev.target.closest("button, .tree-input")) return; // 改名输入框同理（见 row 注释）
    selectDoc(doc);
  });
  return item;
}

/** 文档显示名：title 理论非空（表 NOT NULL），空串脏数据兜底占位。 */
function displayTitle(doc) {
  const title = String(doc.title || "").trim();
  return title || `未命名 #${doc.id}`;
}

/** 选中文档：本地高亮 + 详情卡回填（onSelect）+ 打开阅读面板（onOpen）。 */
function selectDoc(doc) {
  activeDocId = doc.id;
  renderTree(); // 高亮重绘（只差 active 类，全量重绘最省心）
  const location = locationText(doc);
  onSelect?.(doc, location);
  onOpen?.(doc, location);
}

function toggleFolder(id) {
  if (searchText) return; // 查找模式强制展开：行点击不可收起
  if (expanded.has(id)) expanded.delete(id);
  else expanded.add(id);
  renderTree();
}

/* =========================================================================
   菜单（⋯）：构建 × 三类 + 位置显示（与 qa-tree.js 同构）
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
  const open = searchText || expanded.has(folder.id);
  const hasChildren = node.children.length + node.docs.length > 0;
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

function openDocMenu(ev, doc, depth) {
  anchorX = ev.clientX;
  anchorY = ev.clientY;
  lastMain = { kind: "doc", target: doc, depth };
  buildDocMenu(doc, depth);
  showMenuAt(anchorX, anchorY);
}

function buildDocMenu(doc, depth) {
  menuEl.innerHTML = "";
  menuEl.append(
    menuItem("重命名", () => beginRenameDoc(doc)),
    menuItem("移动到…", () => buildMoveMenu({ kind: "doc", id: doc.id })),
    el("div", { class: "ctx-sep" }),
    menuItem("删除", () => deleteDoc(doc), { danger: true })
  );
}

/**
 * 移动子菜单：原地替换菜单内容（"← 返回"回主菜单）。
 * 目标 = {kind: folder|doc, id}；blockedIds = 文件夹的自身+后代
 * （防环：与后端 409 同源判据）；当前位置灰显。
 */
function buildMoveMenu(target) {
  const kind = target.kind;
  const id = target.id;
  const targetRow =
    kind === "folder"
      ? foldersCache.find((f) => f.id === id)
      : docsCache.find((d) => d.id === id);
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
  else buildDocMenu(lastMain.target, lastMain.depth);
  showMenuAt(anchorX, anchorY);
}

/** 移动：文件夹 PATCH kb-folders parent_id；文档 PATCH documents folder_id。 */
async function moveTo(kind, id, parentId) {
  const path = kind === "folder" ? `kb-folders/${id}` : `documents/${id}`;
  const body = kind === "folder" ? { parent_id: parentId } : { folder_id: parentId };
  try {
    await apiFetch(`/api/${path}`, { method: "PATCH", body: JSON.stringify(body) });
    refreshCorpusTree();
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
    await apiFetch("/api/kb-folders", {
      method: "POST",
      body: JSON.stringify({ name, parent_id: parentId }),
    });
    refreshCorpusTree();
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
    await apiFetch(`/api/kb-folders/${folder.id}`, {
      method: "PATCH",
      body: JSON.stringify({ name }),
    });
    refreshCorpusTree();
  }, restore);
  span.replaceWith(input);
  input.focus();
}

/** 文档行内改名：s-title span → input（cancel 还原 span）。 */
function beginRenameDoc(doc) {
  const span = findDocTitleSpan(doc.id);
  if (!span) return;
  const restore = () => {
    if (input.isConnected) input.replaceWith(span);
  };
  const { input } = editInput(displayTitle(doc), 120, async (title) => {
    await apiFetch(`/api/documents/${doc.id}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    });
    refreshCorpusTree();
  }, restore);
  span.replaceWith(input);
  input.focus();
}

/* =========================================================================
   删除
   ========================================================================= */

async function deleteDoc(doc) {
  const title = displayTitle(doc);
  const ok = await confirmDialog({
    title: `删除文档《${title}》？`,
    detail: "其全部检索分块与库内副本将一并清除，此操作不可恢复。",
  });
  if (!ok) return;
  try {
    await apiFetch(`/api/documents/${doc.id}`, { method: "DELETE" });
    if (doc.id === activeDocId) clearSelection(); // 删的是选中文档：详情卡清空
    refreshCorpusTree();
  } catch (err) {
    toast(`删除失败：${err.message}`, "error");
  }
}

async function deleteFolder(folder) {
  const ok = await confirmDialog({
    title: `删除文件夹「${folder.name}」？`,
    detail: "只允许删除空文件夹：内含子文件夹或文档会被拒绝（409），需先移出。",
  });
  if (!ok) return;
  try {
    await apiFetch(`/api/kb-folders/${folder.id}`, { method: "DELETE" });
    refreshCorpusTree();
  } catch (err) {
    // 非空 409：后端文案自带子夹/文档计数
    toast(`删除失败：${err.message}`, "error");
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

function findDocTitleSpan(docId) {
  const row = [...listBox.querySelectorAll(".doc-item")].find(
    (r) => r.dataset.docId === String(docId)
  );
  return row?.querySelector(".s-title") ?? null;
}

/** 树内全部文档行（深遍历；unowned 根级文档在 roots 之外单列）。 */
function* walkDocs(tree) {
  const visit = function* (node) {
    yield* node.docs;
    for (const child of node.children) yield* visit(child);
  };
  for (const root of tree.roots) yield* visit(root);
  yield* tree.unowned;
}
