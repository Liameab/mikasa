/* =========================================================================
   树纯函数层：平铺数据（文件夹 + 条目：会话/文档）→ 嵌套树。
   零 DOM、零 fetch：只做可单测的确定性换算，渲染交互在 qa-tree.js
   （会话）与 kb-tree.js（语料文档）各自进行。
   ========================================================================= */

/**
 * 通用分组（buildTree / buildDocTree 的共享实现）：按 folder_id 把条目
 * 归入文件夹节点，无主条目单列。slot = 节点上放条目的键名。
 *
 * 防御性兜底（正常库中不可能触发，仅防脏数据崩渲染）：
 *   文件夹指向不存在的父 → 当根级挂出；
 *   条目指向不存在的文件夹 → 当根级条目挂出。
 */
function group(folders, items, slot) {
  /** 顶层目录：先根级文件夹（id 升序），后未归档条目。 */
  const roots = [];
  const byId = new Map(); // folder id → FolderNode（含未定父的，父就位后补挂）
  for (const folder of folders) {
    byId.set(folder.id, { folder, children: [], [slot]: [] });
  }
  for (const node of byId.values()) {
    const parentId = node.folder.parent_id;
    const parent = parentId !== null ? byId.get(parentId) : null;
    if (parent) parent.children.push(node);
    else roots.push(node); // 根级或脏数据兜底
  }
  const unowned = [];
  for (const item of items) {
    const node = item.folder_id !== null ? byId.get(item.folder_id) : null;
    if (node) node[slot].push(item);
    else unowned.push(item); // 未归档条目（或脏数据兜底）
  }
  return { roots, unowned };
}

/**
 * 平铺文件夹 + 会话 → 嵌套树（会话栏数据源）。
 *
 * 输入约定（后端契约）：
 *   folders  —— id 升序平铺，每行 {id, name, parent_id}；子文件夹的父
 *               必先于子（外键保证父 id < 子 id），无需二次排序；
 *   sessions —— id 降序（新建在前），每行 {id, title, folder_id, ...}。
 *
 * 产出 FolderNode = { folder, children: FolderNode[], sessions: rows[] }。
 */
export function buildTree(folders, sessions) {
  return group(folders, sessions, "sessions");
}

/**
 * 平铺文件夹 + 文档 → 嵌套树（语料库页数据源，v3）。
 *
 * documents 行（GET /api/documents，新建倒序）须带 folder_id 与展示用
 * 字段（title/chunk_count/…）；产出的 FolderNode.docs 是文档行数组，
 * 分组后保持 API 给序（同夹内即入库新→旧）。
 */
export function buildDocTree(folders, documents) {
  return group(folders, documents, "docs");
}

/**
 * 文件夹 id → 自根向下的名字路径（移动菜单的路径表 / 灰显排除用）。
 * 找不到（脏数据）返回 []；parent 链用 seen 集合防坏数据的无限循环。
 */
export function folderPath(folders, folderId) {
  const byId = new Map(folders.map((f) => [f.id, f]));
  const names = [];
  const seen = new Set();
  let cur = byId.get(folderId);
  while (cur) {
    if (seen.has(cur.id)) return []; // 成环脏数据：放弃路径
    seen.add(cur.id);
    names.unshift(cur.name);
    cur = cur.parent_id !== null ? byId.get(cur.parent_id) : null;
  }
  return names;
}

/**
 * 文件夹 id 的自身 + 全部后代集合（防环判据与移动菜单灰显共用）。
 * 与后端 repo.folder_descendant_ids 同语义：自身也计入，调用方
 * 一条 `targets.has(候选 id)` 即可过滤"自身或后代"。
 */
export function subtreeIds(folders, folderId) {
  const byId = new Map(folders.map((f) => [f.id, f]));
  const out = new Set([folderId]);
  let grown = true;
  while (grown) {
    grown = false;
    for (const f of folders) {
      if (!out.has(f.id) && f.parent_id !== null && out.has(f.parent_id)) {
        out.add(f.id);
        grown = true;
      }
    }
  }
  return out;
}

/**
 * 会话行 → 侧栏显示名。title 可能为 null（空会话/刚建），回退成
 * "新会话 #id" 占位——首轮问答后自动标题即落库，占位只是瞬态。
 */
export function displayTitle(session) {
  const title = String(session.title || "").trim();
  return title || `新会话 #${session.id}`;
}
