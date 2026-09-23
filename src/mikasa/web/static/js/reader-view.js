/* =========================================================================
   阅读视图的"渲染计划"纯函数（零 DOM、零 fetch，可被 node 直接求值测试）。

   为什么单独一层：正文来自后端 `/api/documents/{id}/content` 的
   {text, chunks[{chunk_id, seq, start, end, heading_path, page_number}]}，
   而"哪里插小标题、哪里标页码、每块切哪一段"是纯数据变换——拆出来后
   reader.js 只负责把计划变成 DOM，变换本身可用 smoke_reader.mjs 锁死
   （与 js/tree.js 同款分层，见 docs/architecture.md 前端约定）。

   与后端的契约：`start`/`end` 是**半开区间**，后端保证
   spans[0].start === 0、spans[-1].end === text.length、相邻 end === start
   （见 src/mikasa/ingest/stitch.py 的不变量），故此处直接切片即可，
   不需要任何防御性对齐。
   ========================================================================= */

/**
 * 把后端返回的正文与块偏移编成顺序渲染计划。
 *
 * 产出的项（按渲染顺序）：
 *   {kind:"head", label, title}  —— heading_path 变化处的小标题
 *   {kind:"page", n}             —— page_number 变化处的页码标记（PDF）
 *   {kind:"chunk", chunkId, seq, text} —— 一块正文（text 已按偏移切好）
 *
 * heading_path 为 null 的语料（PDF/txt）不会产生 head 项，靠 page 项分段；
 * 反过来 md 没有 page 项。两者都可以为空（纯正文）。
 */
export function buildPlan(text, chunks) {
  const plan = [];
  let lastHeading = null;
  let lastPage = null;
  for (const c of chunks) {
    if (c.heading_path && c.heading_path !== lastHeading) {
      plan.push({ kind: "head", label: headingLabel(c.heading_path), title: c.heading_path });
      lastHeading = c.heading_path;
    }
    if (c.page_number !== null && c.page_number !== undefined && c.page_number !== lastPage) {
      plan.push({ kind: "page", n: c.page_number });
      lastPage = c.page_number;
    }
    plan.push({
      kind: "chunk",
      chunkId: c.chunk_id,
      seq: c.seq,
      text: text.slice(c.start, c.end),
    });
  }
  return plan;
}

/** heading_path → 末级标题（"1. 模型 > 1.2 注意力" → "1.2 注意力"）。
 *  多级路径的完整形态挂在 title 属性上，正文里只显示末级免得重复。 */
export function headingLabel(path) {
  if (!path) return "";
  const parts = String(path).split(">");
  return parts[parts.length - 1].trim();
}

/** 最大页码（PDF 的跳页输入框上界）；全为 null 时返回 null。 */
export function pageMax(chunks) {
  let max = null;
  for (const c of chunks) {
    if (c.page_number !== null && c.page_number !== undefined) {
      max = max === null ? c.page_number : Math.max(max, c.page_number);
    }
  }
  return max;
}

/**
 * 用户在正文里选中的文字 → 可当上下文的单行文本（「边看边问」，A 档 c）。
 *
 * 三步：折叠所有空白（选区跨段落时会带一堆换行缩进，进提示词只会浪费
 * token）、trim、超长截断补省略号。结果为空串 = 没有可用上下文，调用方
 * 按"没选中"处理——**纯空白选区必须与没选中完全等价**（否则会带一个
 * 空上下文进请求，服务端 `_reader_query` 里也是同样的判空口径）。
 *
 * limit 由调用方给（与服务端 `READER_CONTEXT_MAX` 对齐）：这里是纯函数，
 * 不知道服务端上限，也不该知道。
 */
export function normalizeSelection(raw, limit) {
  const text = String(raw ?? "")
    .split(/\s+/)
    .filter(Boolean)
    .join(" ");
  if (!text) return "";
  const cap = Number(limit) > 0 ? Number(limit) : text.length;
  if (text.length <= cap) return text;
  // 省略号也要占位：截到 cap 个**字符**再补省略号就会超上限（服务端 422）
  return text.slice(0, Math.max(1, cap - 1)) + "…";
}
