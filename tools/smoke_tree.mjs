#!/usr/bin/env node
/* =========================================================================
   会话树纯函数冒烟（M4.5 可选工具，不进 CI）。

   用法：node tools/smoke_tree.mjs   （无需任何依赖，node ≥ 18）

   仓库无 package.json → static/js/tree.js 对 node 是 CJS 而源码用 ESM
   export 语法。因此这里不走 import 相对路径，而是读源码原文、以
   data: URL 形式按 ESM 求值——断言跑的是浏览器同款字节，无第二份拷贝。
   ========================================================================= */

import fs from "node:fs";

const source = fs.readFileSync(
  new URL("../src/mikasa/web/static/js/tree.js", import.meta.url),
  "utf8"
);
const mod = await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));
const { buildTree, buildDocTree, folderPath, subtreeIds, displayTitle, isNote, folderOptions } =
  mod;

let passed = 0;
const assert = (cond, msg) => {
  if (!cond) {
    console.error(`✗ FAIL: ${msg}`);
    process.exit(1);
  }
  passed += 1;
};

// 树结构：复习 → 数学/英语；AI 复试；会话 9 挂数学、8 挂英语、7 未归档
const folders = [
  { id: 1, name: "复习", parent_id: null },
  { id: 2, name: "数学", parent_id: 1 },
  { id: 3, name: "英语", parent_id: 1 },
  { id: 4, name: "AI 复试", parent_id: null },
];
const sessions = [
  { id: 9, title: null, folder_id: 2, created_at: "" },
  { id: 8, title: "背单词", folder_id: 3, created_at: "" },
  { id: 7, title: "老会话", folder_id: null, created_at: "" },
];

const tree = buildTree(folders, sessions);
assert(tree.roots.length === 2, "根级文件夹数量");
assert(tree.roots[0].folder.id === 1 && tree.roots[1].folder.id === 4, "根级按 id 升序");
assert(
  tree.roots[0].children.map((c) => c.folder.id).join() === "2,3",
  "子文件夹挂在父下、顺序保持"
);
assert(tree.roots[0].children[0].sessions[0].id === 9, "会话归入所属文件夹");
assert(tree.roots[0].children[1].sessions[0].id === 8, "英语夹子会话");
assert(tree.unowned.map((s) => s.id).join() === "7", "未归档会话单列");

assert(folderPath(folders, 4).join("/") === "AI 复试", "根级路径");
assert(folderPath(folders, 2).join("/") === "复习/数学", "多级路径");
assert(folderPath(folders, 99).length === 0, "缺失文件夹 → 空路径");

assert([...subtreeIds(folders, 1)].sort((a, b) => a - b).join() === "1,2,3", "subtree 含自身+后代");
assert([...subtreeIds(folders, 3)].join() === "3", "叶子仅自身");

assert(displayTitle({ id: 5, title: null }) === "新会话 #5", "title null → 占位");
assert(displayTitle({ id: 5, title: "  L2 正则化  " }) === "L2 正则化", "title trim");

// 成环脏数据：路径放弃而非死循环
const loop = [
  { id: 1, name: "a", parent_id: 2 },
  { id: 2, name: "b", parent_id: 1 },
];
assert(folderPath(loop, 1).length === 0, "成环路径安全放弃");

// ---- buildDocTree（v3 语料树数据源：同一 group 内核，槽位换成 docs） ----
// API 给序 = 新建倒序（id 大在前），docs 分组后须保持该序
const docs = [
  { id: 30, title: "最新笔记", folder_id: 2 }, // 数学夹
  { id: 29, title: "L2 笔记", folder_id: 2 }, // 数学夹（更旧 → 排在 30 后）
  { id: 28, title: "自我介绍", folder_id: null }, // 未归档
  { id: 27, title: "幽灵夹文档", folder_id: 99 }, // 指向不存在的夹 → 未归档兜底
];
const docTree = buildDocTree(folders, docs);
assert(docTree.roots.length === 2, "文档树根级文件夹数量");
assert(docTree.roots[0].children[0].docs.map((d) => d.id).join() === "30,29", "文档归入夹且保序");
assert(docTree.unowned.map((d) => d.id).join() === "28,27", "未归档与孤儿夹文档单列");
assert("docs" in docTree.roots[0] && !("sessions" in docTree.roots[0].children[0]), "文档树节点带 docs 槽");
// 与 buildTree 互不污染：同名核心函数产出各自的槽位
const tree2 = buildTree(folders, sessions);
assert("sessions" in tree2.roots[0].children[0], "会话树节点带 sessions 槽");

// ---- isNote（M6 ①：笔记判据，与后端 NOTE_REF_PREFIX 是跨端合同） ----
assert(isNote({ source_ref: "note:abc123" }) === true, "note: 前缀 = 笔记");
assert(isNote({ source_ref: "arxiv:2401.12345" }) === false, "论文导入不是笔记");
assert(isNote({ source_ref: null }) === false, "上传件（NULL）不是笔记");
assert(isNote({}) === false, "缺字段的行不炸且不是笔记");
assert(isNote(null) === false, "null 行不炸");

// ---- folderOptions（笔记编辑器"保存到"下拉） ----
const opts = folderOptions(folders);
assert(opts.map((o) => o.id).join() === "1,2,3,4", "深度优先、父在子前");
assert(opts[0].depth === 0 && opts[1].depth === 1 && opts[2].depth === 1, "层级深度");
assert(opts[1].label === "　数学", "子级用全角空格缩进");
assert(opts[0].label === "复习", "根级不缩进");
assert(folderOptions([]).length === 0, "空列表不炸");

// 父不存在的脏数据 → 当根级挂出（与 group 同口径），不许丢
const orphan = folderOptions([{ id: 7, name: "孤儿夹", parent_id: 99 }]);
assert(orphan.length === 1 && orphan[0].depth === 0, "父缺失的文件夹当根级列出");
// 成环：两个都要出现在列表里（宁位置不完美，不能让用户找不到）
const cycleOpts = folderOptions([
  { id: 1, name: "a", parent_id: 2 },
  { id: 2, name: "b", parent_id: 1 },
]);
assert(cycleOpts.length === 2, "成环文件夹不丢、不死循环");

console.log(`tree.js 冒烟通过：${passed} 条断言全绿`);
