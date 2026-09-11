#!/usr/bin/env node
/* =========================================================================
   阅读视图渲染计划冒烟（2026-09-10，阅读视图 + 引用跳转；不进 CI）。

   用法：node tools/smoke_reader.mjs   （无需任何依赖，node ≥ 18）

   与 smoke_render.mjs 同款：仓库无 package.json，reader-view.js 对 node
   是 CJS 而源码用 ESM，故读源码原文以 data: URL 求值——断言跑的是浏览器
   同款字节。该模块刻意零 DOM 零 fetch，正是为了能在 node 直接求值。

   锁的契约：计划项的顺序与内容必须与后端 /content 的偏移严格一致
   （后端保证 spans 首 0 末 len、相邻 end===start，见 ingest/stitch.py）。
   ========================================================================= */

import fs from "node:fs";

const source = fs.readFileSync(
  new URL("../src/mikasa/web/static/js/reader-view.js", import.meta.url),
  "utf8"
);
const mod = await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));
const { buildPlan, headingLabel, pageMax } = mod;

let passed = 0;
const assert = (cond, msg) => {
  if (!cond) {
    console.error(`✗ FAIL: ${msg}`);
    process.exit(1);
  }
  passed += 1;
};

/* 三段等长正文，偏移首 0 末 len、相邻连续（后端不变量） */
const TEXT = "第一段正文。第二段正文。第三段正文。";
const mdChunks = [
  { chunk_id: 1, seq: 0, start: 0, end: 6, heading_path: "甲章", page_number: null },
  { chunk_id: 2, seq: 1, start: 6, end: 12, heading_path: "甲章", page_number: null },
  { chunk_id: 3, seq: 2, start: 12, end: 18, heading_path: "乙章", page_number: null },
];

/* ---- md 形态：标题变化处插小标题，同标题不重复插 ---- */
{
  const plan = buildPlan(TEXT, mdChunks);
  const kinds = plan.map((p) => p.kind);
  assert(
    JSON.stringify(kinds) === JSON.stringify(["head", "chunk", "chunk", "head", "chunk"]),
    "md：标题变化处插 head，同标题不重复"
  );
  assert(plan[0].label === "甲章" && plan[0].title === "甲章", "head 携带末级标签与全路径");
  assert(plan[3].label === "乙章", "第二个标题正确");
  assert(
    plan.filter((p) => p.kind === "chunk").map((p) => p.chunkId).join(",") === "1,2,3",
    "chunk 项按序携带 chunkId（锚点用）"
  );
}

/* ---- PDF 形态：heading 为 null → 无 head；页码变化处插 page ---- */
{
  const pdfChunks = [
    { chunk_id: 11, seq: 0, start: 0, end: 6, heading_path: null, page_number: 1 },
    { chunk_id: 12, seq: 1, start: 6, end: 12, heading_path: null, page_number: 1 },
    { chunk_id: 13, seq: 2, start: 12, end: 18, heading_path: null, page_number: 2 },
  ];
  const plan = buildPlan(TEXT, pdfChunks);
  assert(
    JSON.stringify(plan.map((p) => p.kind)) ===
      JSON.stringify(["page", "chunk", "chunk", "page", "chunk"]),
    "pdf：页码变化处插 page，同页不重复插"
  );
  assert(plan[0].n === 1 && plan[3].n === 2, "page 项携带页码");
  assert(!plan.some((p) => p.kind === "head"), "pdf 无标题路径 → 不产生 head");
  assert(pageMax(pdfChunks) === 2, "pageMax 取最大页码（跳页输入框上界）");
}

/* ---- 切片与偏移严格一致（前端按 start/end 切全文的唯一依据） ---- */
{
  const plan = buildPlan(TEXT, mdChunks);
  const pieces = plan.filter((p) => p.kind === "chunk");
  assert(pieces[0].text === "第一段正文。", "首块按偏移切片");
  assert(pieces[1].text === "第二段正文。", "中间块按偏移切片");
  assert(pieces[2].text === "第三段正文。", "末块按偏移切片");
  assert(pieces.map((p) => p.text).join("") === TEXT, "拼接恒等于全文");
}

/* ---- 边界 ---- */
{
  assert(buildPlan("", []).length === 0, "空 chunks → 空计划");
  assert(pageMax([]) === null, "无块 → pageMax null");
  assert(pageMax(mdChunks) === null, "全 null 页码 → pageMax null（md/txt）");
  const one = buildPlan("独块。", [
    { chunk_id: 9, seq: 0, start: 0, end: 3, heading_path: null, page_number: null },
  ]);
  assert(one.length === 1 && one[0].text === "独块。", "单块无标题无页码");
  assert(headingLabel("") === "" && headingLabel(null) === "", "空标题路径 → 空标签");
  assert(
    headingLabel("1. 模型 > 1.2 注意力") === "1.2 注意力",
    "多级标题取末级（全路径留给 title 属性）"
  );
}

/* ---- 计划层零 HTML：正文原样是字符串，转义责任在渲染侧 ---- */
{
  const evil = "<script>alert(1)</script>";
  const plan = buildPlan(evil, [
    { chunk_id: 1, seq: 0, start: 0, end: evil.length, heading_path: null, page_number: null },
  ]);
  assert(plan[0].text === evil, "计划层不做转义也不拼 HTML（渲染侧用文本节点）");
}

console.log(`✓ smoke_reader：${passed} 条断言全部通过`);
