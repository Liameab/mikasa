/* =========================================================================
   公共工具库：三页共享（原生 ES Module，无构建链）。

   约定（安全纪律，读代码前先记住这三条）：
   1. 任何来自服务端 / LLM 的字符串进 innerHTML 前必须过 esc()——
      LLM 输出不可信（可能带 <script>），转义是唯一防线；
   2. 自己写的常量模板可直接拼，但含插值的模板一律显式 esc；
   3. 引用编号 [n] 的渲染走专用 renderAnswer（先转义成文本再替换），
      禁止在未转义正文上做 HTML 替换。

   对外 API：
     esc()            HTML 转义（& < > " ' `）
     el()             createElement 便捷构造（属性/子节点）
     fmtTime()        ISO 时间 → "MM-DD HH:mm"
     toast()/clear    页级消息（容器 #toast，三页 html 都有）
     apiFetch()       JSON 请求，错误归一为带中文文案的 Error
     ssePost()        POST + SSE 帧流消费（meta/delta/done/error）
     renderAnswer()   答案正文渲染（表格/代码块/标题/列表等块级 markdown，
                     代码块带复制钮；行内 code/粗体 + [n] 引用 chip）
     renderCitations() 引用明细卡列表（点 chip 高亮对应卡）
     renderMarkdown() 评测报告 markdown 轻渲染（标题/表/粗体/引用）
     initTopbar()     顶栏导航高亮 + /api/health 状态胶囊
   ========================================================================= */

/* ------------------------------------------------------------------ */
/* DOM 小工具                                                          */
/* ------------------------------------------------------------------ */

/** document.querySelector 简写。 */
export function $(sel, root = document) {
  return root.querySelector(sel);
}

/**
 * HTML 转义。所有动态文本的唯一入口：
 * 先转义再（按需）做受控替换，见模块头纪律。
 *
 * 不转义反引号：它在文本节点与"双引号包裹"的属性里都无害（XSS 逃逸
 * 需要 < 或 "，二者已转义），而转义它会破坏 renderAnswer 的行内 code
 * 识别（`` `code` `` 模式）。凡动态值进属性一律仍要 esc（" 已转义）。
 */
export function esc(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/**
 * 便捷元素构造：el("div", {class:"x", onclick:fn}, "文本", childEl)
 * 字符串子节点一律走 createTextNode（天然免疫转义遗漏）。
 */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  // ?? {}：默认参数只在省略时生效，显式传 null/undefined 也要兜底
  // （否则 Object.entries(null) 抛 TypeError，会把调用方 try 吞成静默空白）
  for (const [key, value] of Object.entries(attrs ?? {})) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "html") node.innerHTML = value; // 仅限调用方已 esc 的场景
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** ISO 时间 → "MM-DD HH:mm"（消息/会话列表的紧凑时间）。 */
export function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 时长展示：660ms（<1s，避免"0.0s"）/ 12.3s（<60s）/ 1分23秒（长等待）。 */
export function fmtSeconds(sec) {
  if (sec < 1) return `${Math.round(sec * 1000)}ms`;
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const m = Math.floor(sec / 60);
  return `${m}分${Math.round(sec - m * 60)}秒`;
}

/**
 * 延迟分段 → who 行的一行摘要（2026-09-10 用户实测：等了三四十秒却看不到
 * 任何时间信息，分不清是卡住还是在生成）。
 *
 * 为什么要从 generate 里"减出"生成时间：后端 generate 分段的计时起点取在
 * 检索之前（ask.py _finalize_latency 的 t0），检索/重排/译查询全落在它
 * 里面——直接显示会把模型生成时间报大好几倍。这里减去前置分段还原模型
 * 自身耗时；translate_answer 在 finalize 之后单独计时，不参与相减。
 * totalSec 为前端墙钟（send→done），与后端分段互为印证；回放路径没有墙钟，
 * 传 null 则只显示分段。
 */
export function fmtLatency(latency, totalSec = null) {
  if (!latency) return "";
  // 后端 latency_ms 的键值单位是**毫秒**（ask.py 各分段统一 round(…*1000, 1)），
  // 展示前必须 /1000——直接当秒用会显示"生成 1731.0s"这种荒唐值（smoke 实抓）。
  const ms = (k) => Number(latency[k] || 0);
  const modelMs = Math.max(0, ms("generate") - ms("retrieve") - ms("rerank") - ms("translate"));
  const items = [
    ["生成", modelMs],
    ["译查询", ms("translate")],
    ["译对照", ms("translate_answer")],
    ["检索", ms("retrieve")],
    ["重排", ms("rerank")],
  ]
    // 50ms 下限：更短的分段四舍五入后会显示成"生成 0.0s"这种噪声项
    // （mock/极快轮实测），干脆不列
    .filter(([, v]) => v >= 50)
    .map(([label, v]) => `${label} ${fmtSeconds(v / 1000)}`);
  const head = totalSec === null ? "" : `用时 ${fmtSeconds(totalSec)}`;
  if (!items.length) return head;
  return head ? `${head}（${items.join(" · ")}）` : items.join(" · ");
}

/* ------------------------------------------------------------------ */
/* 页级瞬时消息                                                        */
/* ------------------------------------------------------------------ */

const TOAST_KIND = { ok: "ok", info: "warn", warn: "warn", error: "error" };

/** 弹一条右上角消息，3.6s 自动消失（kind: ok/warn/error）。 */
export function toast(message, kind = "info") {
  const box = document.getElementById("toast");
  if (!box) return; // 页面没放容器（不该发生）就静默
  const node = el("div", { class: `toast-msg ${TOAST_KIND[kind] || "warn"}` }, message);
  box.append(node);
  setTimeout(() => node.remove(), 3600);
}

/* ------------------------------------------------------------------ */
/* HTTP：JSON 请求 + SSE 流式消费                                      */
/* ------------------------------------------------------------------ */

/**
 * JSON 请求并解析；任何非 2xx 都抛 Error(中文文案)。
 * 后端错误有两种壳（路由层 HTTPException 的 detail / 异常处理器
 * 的 error.message），此处归一，页面只需 catch 一条路径。
 */
export async function apiFetch(path, options = {}) {
  const resp = await fetch(path, {
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
    ...options,
  });
  const text = await resp.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null; // 非 JSON（如 500 兜底前的中断）→ 下面按文本兜底
  }
  if (!resp.ok) {
    throw new Error(errorMessage(body, text, resp.status));
  }
  return body;
}

/**
 * 把错误响应体转成一句可直接显示的中文文案。
 *
 * FastAPI 的 422 校验错误里 body.detail 是**数组**（[{loc, msg, type}]）——
 * 直接当字符串用会渲染成 "[object Object]"，用户根本不知道发生了什么
 * （2026-09-11 修复：提问超 2000 字触发 422 时就是这么显示的）。
 */
export function errorMessage(body, text, status) {
  const detail = body?.detail;
  if (Array.isArray(detail)) {
    const msgs = detail.map((d) => d?.msg).filter(Boolean);
    if (msgs.length) return msgs.join("；");
  } else if (typeof detail === "string" && detail) {
    return detail;
  }
  return body?.error?.message || (body && text) || `请求失败（HTTP ${status}）`;
}

/**
 * POST 一个 JSON body 并以 SSE 帧流方式消费响应（EventSource 不支持
 * POST，只能 fetch + ReadableStream 手拆帧）。
 *
 * 服务端帧格式（与 web/sse.py 对偶）：帧间空行分隔，每帧：
 *   event: <类型>\ndata: <JSON 字符串>\n
 * UTF-8 多字节安全：TextDecoder(stream:true) 增量解码，按 "\n\n" 切帧
 * 时若末帧未闭，留半帧缓冲等下一段。
 *
 * onFrame(type, data)：meta / delta / done / error 四类事件分发；
 * 流自然结束（done/error 帧后服务端关闭）→ resolve。
 */
export async function ssePost(path, body, onFrame) {
  let resp;
  try {
    resp = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    throw new Error("网络中断：无法连接服务，请确认 mikasa serve 正在运行");
  }
  if (!resp.ok || !resp.body) {
    // 业务错误（预检 4xx）不流式：直接取 JSON 错误文案
    const data = await resp.json().catch(() => null);
    throw new Error(errorMessage(data, null, resp.status));
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = ""; // 半帧缓冲（一次 read 可能只到帧中间）
  for (;;) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? ""; // 末段可能是不完整帧，留到下次
    for (const frame of frames) {
      const type = (frame.match(/^event: (.+)$/m) || [])[1];
      const raw = (frame.match(/^data: (.+)$/m) || [])[1];
      if (type && raw) {
        onFrame(type, JSON.parse(raw));
        if (type === "done" || type === "error") return; // 收尾帧：服务端即将断开
      }
    }
    if (done) break;
  }
}

/* ------------------------------------------------------------------ */
/* 渲染：答案正文 + 引用                                               */
/* ------------------------------------------------------------------ */

/* ------------------------------------------------------------------ */
/* 公式渲染（KaTeX，离线内置，2026-09-20）                              */
/* ------------------------------------------------------------------ */

/**
 * 把 LLM 输出里的 LaTeX 交给本地 KaTeX 渲染（`/static/vendor/katex/`）。
 *
 * **为什么需要**：用户问数学题时模型按惯例给 `$$e^x = 1 + \frac{x^2}{2!} + \cdots$$`，
 * 而 renderAnswer 这套轻渲染只认代码/粗体/标题/列表/表格——`$...$`、`\frac{}`
 * 全被当普通文字原样输出，整页看着像源码（用户 2026-09-20 报障"排版特别的乱"）。
 *
 * 三条纪律：
 * 1. **离线**：KaTeX 随包内置（无 CDN、无构建链），与"本地优先"的产品形态一致；
 * 2. **不改代码语义**：围栏代码块与行内代码里的 `$` 一律不碰（`echo $HOME` 不是公式）；
 * 3. **失败不吞内容**：KaTeX 没加载（脚本缺失 / Node 冒烟）或渲染抛错时，
 *    原样保留 LaTeX 文本，绝不静默丢字。
 */

/** 公式占位符（renderAnswer 内部另用 `\ue000`，两者分属不同字符、互不干扰）。 */
const PH_MATH = "\ue001";
/** 抽公式前先把行内代码藏起来的占位符。 */
const PH_CODE = "\ue002";
/** 抽公式前把围栏代码块整段藏起来的占位符。 */
const PH_FENCE = "\ue003";

/**
 * `$...$` 里这一段"看起来像公式"吗——只用来挡货币写法。
 *
 * 成对的 `$` 不等于公式：`价格$5到$10之间`、`$1,000` 这类写法也成对。
 * 两道闸（第一版判据太严，实测把 `(-1, 1]`、`n!`、`2n+1`、`o(x)` 这些**真公式**
 * 全挡在门外——用户那条泰勒展开的回答里一次漏了 8 处，故改成"先排除、后放行"）：
 *   1. 内容里有中文且没有任何 LaTeX 命令 → 不是公式（`5到`、`A股`）；
 *      `$\text{对 } \frac{1}{1+x}$` 这种带命令的照常放行；
 *   2. 内容是纯数字/千分位/小数点 → 金额或编号（`5`、`1,000.50`），不是公式。
 * 其余一律放行——区间 `(-1, 1]`、阶乘 `n!`、`2n+1` 都必须算公式。
 */
function looksLikeMath(tex) {
  const t = tex.trim();
  if (!t) return false;
  if (/[\u4e00-\u9fff]/.test(t) && !t.includes("\\")) return false; // 中文夹着的钱数/编号
  if (/^[\d.,\s]+$/.test(t)) return false; // 纯数字：金额或编号
  return true;
}

/** LaTeX → KaTeX HTML；不可用或抛错时返回 null（调用方原样保留文本）。 */
function katexHtml(tex, display) {
  const katex = globalThis.katex;
  if (!katex || typeof katex.renderToString !== "function") return null;
  try {
    return katex.renderToString(tex, {
      displayMode: display,
      throwOnError: false, // 渲染不了就显示红色原文，不抛异常打断整条消息
      strict: "ignore", // 公式里出现中文/Unicode 时不刷控制台警告
    });
  } catch {
    return null;
  }
}

/**
 * 块级公式：`$$...$$` 与 `\[...\]`。**允许跨行**——模型很常写成
 *
 *     $$
 *     e^x = \sum_{n=0}^{\infty} \frac{x^n}{n!}
 *     $$
 *
 * 第一版把公式预扫描做成**逐行**的，跨行写法整段漏掉（2026-09-20 实测：用户那条
 * 本地模型回答 14 处公式**一处都没渲染**），所以这里改成整篇扫描。
 */
const MATH_BLOCK = /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]/g;
/**
 * 行内公式：`$...$` 与 `\(...\)`。
 *
 * **允许紧贴定界符的空格**（`$ e^x $` 是极常见的模型写法，第一版禁了空格 →
 * 同样是那条回答里半屏公式全漏）。货币误判（`$5 到 $10`）改由 `looksLikeMath`
 * 那道闸兜底，不再靠"不许有空格"这种误伤极大的规则。
 * `$$` 已在块级那一遍消费掉，这里的 `(?!\$)` 只是防住落单的 `$$`。
 */
const MATH_INLINE =
  /(?<!\\)\$(?!\$)((?:\\\$|[^$\n])+?)(?<!\\)\$(?!\$)|\\\(([\s\S]+?)\\\)/g;

/** 一段**非代码**正文里的公式 → 占位符；HTML 存进 tokens，由调用方最后统一还原。 */
function stashMathInText(chunk, tokens) {
  const stash = (html) => {
    tokens.push(html);
    return `${PH_MATH}${tokens.length - 1}${PH_MATH}`;
  };
  // 行内代码先藏起来：里面的 $ 不是公式
  const codes = [];
  let out = chunk.replace(/`[^`]*`/g, (m) => {
    codes.push(m);
    return `${PH_CODE}${codes.length - 1}${PH_CODE}`;
  });
  const swap = (raw, tex, display) => {
    const html = katexHtml(tex, display);
    return html === null ? raw : stash(html);
  };
  // 块级先来（可跨行）；顺序反了的话 `$$…$$` 会被当成两个行内 `$`
  out = out.replace(MATH_BLOCK, (raw, dollars, brackets) =>
    swap(raw, dollars ?? brackets, true)
  );
  out = out.replace(MATH_INLINE, (raw, dollars, parens) => {
    if (dollars !== undefined && !looksLikeMath(dollars)) return raw; // 货币写法不渲染
    return swap(raw, dollars ?? parens, false);
  });
  // 还原行内代码（公式此刻已是占位符，不会再被匹配）
  return out.replace(new RegExp(PH_CODE + "(\\d+)" + PH_CODE, "g"), (_m, idx) => codes[Number(idx)]);
}

/**
 * 整篇正文预处理：把**围栏代码块整段摘出去**，只对正文抽公式，最后原样拼回。
 *
 * 为什么不能逐行处理：块级公式经常折行（见 `MATH_BLOCK` 的说明），逐行扫描会
 * 整段漏掉。这里按行切开只为**定位围栏**，正文片段仍按整块交给公式扫描，
 * 所以跨行的 `$$…$$` 能被看见。
 */
function stashMath(text, tokens) {
  const parts = [];
  let inFence = false;
  let buf = [];
  const flush = (isCode) => {
    if (buf.length) parts.push({ code: isCode, value: buf.join("\n") });
    buf = [];
  };
  for (const line of text.split(/\r?\n/)) {
    if (/^\s*```/.test(line)) {
      flush(inFence);
      inFence = !inFence;
      // 围栏行本身也是"代码边界"，不能参与公式扫描
      parts.push({ code: true, value: line });
      continue;
    }
    buf.push(line);
  }
  flush(inFence);
  return parts
    .map((part) => (part.code ? part.value : stashMathInText(part.value, tokens)))
    .join("\n");
}

/**
 * 答案正文渲染（LLM 输出 → HTML 片段）。
 *
 * 管线：逐行块级扫描（围栏代码 / 管道表 / 标题 / 引用块 / 分割线 /
 * 列表 / 段落）→ 块内行内受控替换：`code` → <code>；**bold** →
 * <strong>；[n] → 引用 chip（有对应引用可点、无则红色警示越界编号）。
 *
 * 安全次序与以前一致：每个块取原始文本 → esc → 受控替换；围栏代码
 * 内容只 esc 不替换——代码里的 ** 与 [n] 保持字面，不误渲染。
 * 公式是这套管线**前面**的一道预处理：先按 LaTeX 抽出（`$...$` / `$$...$$`
 * 由本地 KaTeX 渲染），换成占位符再进管线，最后把 HTML 还原回去——这样
 * 转义次序不变，公式内容也不会被 esc 破坏（见上面的公式渲染段）。
 *
 * showCites 第三参门控 chip 链：kb 模式（缺省 true）输出 byte-identical；
 * free 模式（false）正文里的 [n] 原样显示——free 无注入编号协议，
 * [n] 只是模型正文，渲染成越界红标是误报（见 ADR-0013 回放契约）。
 *
 * 返回 HTML 字符串；调用方把容器上的 click 委托给 chip 高亮与
 * .btn-copy 复制按钮的处理器（见 qa.js attachBubbleActions）。
 */
/**
 * 双语对照块的首行标识（#8，2026-09-10）。后端 ask._maybe_bilingual_block
 * 固定以这一行起头，渲染时据此挂 .bilingual 类，走"长文可读"样式而非
 * 引用块的弱化色（整段原文+译文连续读，弱化色看不清）。
 *
 * 这是前后端之间的格式合同：改后端首行文案必须同步改这里，
 * 由 tools/smoke_render.mjs 与 tests/unit/pipeline/test_ask.py 双侧锁定。
 */
const BILINGUAL_TITLE = "<strong>原文与译文对照</strong>";

export function renderAnswer(text, citations, showCites = true) {
  const byMarker = showCites ? new Map(citations.map((c) => [c.marker, c])) : null;
  const mathTokens = []; // 公式的 KaTeX HTML（末段统一还原）
  // 占位符用私用区字符（正常文本与 esc 输出都不会含 ）
  const PH = "";
  const inline = (escaped) => {
    // 三段替换串行执行会互相污染（<code> 里的 [n] 也会变 chip），
    // 因此每步先抽到 tokens 数组占位，最后一次性还原。
    const tokens = [];
    const stash = (html) => {
      tokens.push(html);
      return PH + (tokens.length - 1) + PH;
    };
    escaped = escaped.replace(/`([^`]+)`/g, (_m, code) => stash(`<code>${code}</code>`));
    escaped = escaped.replace(/\*\*([^*]+)\*\*/g, (_m, bold) => stash(`<strong>${bold}</strong>`));
    if (showCites) {
      escaped = escaped.replace(/\[(\d{1,3})\]/g, (_m, n) => {
        const cite = byMarker.get(Number(n));
        if (!cite)
          return stash(`<span class="cite bad" title="服务端无此编号的引用（越界/自造）">[${n}]</span>`);
        // data-chunk-id 供阅读面板定位原文（qa.js 的 chip 点击读它）；
        // 越界红标不带该属性——它没有对应引用，无可跳转目标
        return stash(
          `<span class="cite" data-marker="${n}" data-chunk-id="${cite.chunk_id}" ` +
            `title="《${esc(cite.document_title)}》${esc(cite.section || "")}">[${n}]</span>`
        );
      });
    }
    return escaped.replace(new RegExp(PH + "(\\d+)" + PH, "g"), (_m, i) => tokens[Number(i)]);
  };

  /** 围栏代码块：独立区域 = 角标行（语言 + 复制钮）+ 等宽代码主体。
   *  content 必须已 esc；lang 是 LLM 输出，同样先 esc。复制交互靠
   *  消息级委托（.btn-copy 无内联 onclick），见 qa.js。 */
  const codeBlock = (lang, content) =>
    `<div class="code-block"><div class="code-head">` +
    (lang ? `<span class="code-lang">${esc(lang)}</span>` : "") +
    `<button type="button" class="btn-copy" title="复制代码">复制</button></div>` +
    `<pre><code>${content}</code></pre></div>`;

  /* ---- 管道表 ---- */
  const splitCells = (row) =>
    row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((s) => s.trim());
  // 表头分隔行骨架（---，可带 :对齐:），只出现在"| 起头"的表中
  const SEP = /^\s*\|?[\s:|-]+\|?\s*$/;
  /** 分隔单元格 → 列对齐（:--: 居中 / --: 右 / 其余左）。 */
  const alignOf = (cell) => {
    if (!/^:?-+:?$/.test(cell)) return "left";
    if (cell.startsWith(":") && cell.endsWith(":")) return "center";
    if (cell.endsWith(":")) return "right";
    return "left";
  };
  /** 行数组 → <table>。分隔行定位表头；找不到分隔行（LLM 只写数据行）
   *  则整表无表头。短行补空单元格，多出的格子保留（内容不丢）。 */
  const tableHtml = (rows) => {
    let sepIdx = -1;
    for (let r = 1; r < rows.length; r += 1) {
      if (SEP.test(rows[r].trim())) {
        sepIdx = r;
        break;
      }
    }
    const head = sepIdx >= 0 ? splitCells(rows[0]) : null;
    const aligns = sepIdx >= 0 ? splitCells(rows[sepIdx]).map(alignOf) : [];
    const body = [];
    for (let r = sepIdx >= 0 ? sepIdx + 1 : 0; r < rows.length; r += 1) body.push(splitCells(rows[r]));
    const width = head ? head.length : Math.max(0, ...body.map((c) => c.length));
    const pad = (cells) => {
      while (cells.length < width) cells.push("");
      return cells;
    };
    const cellHtml = (tag, c, idx) => {
      const style = aligns[idx] && aligns[idx] !== "left" ? ` style="text-align:${aligns[idx]}"` : "";
      return `<${tag}${style}>${inline(esc(c))}</${tag}>`;
    };
    const thead = head ? `<thead><tr>${pad(head).map((c, idx) => cellHtml("th", c, idx)).join("")}</tr></thead>` : "";
    const tbody = `<tbody>${body
      .map((cells) => `<tr>${pad(cells).map((c, idx) => cellHtml("td", c, idx)).join("")}</tr>`)
      .join("")}</tbody>`;
    return `<div class="md-table-wrap"><table class="md-table">${thead}${tbody}</table></div>`;
  };

  /** 列表项识别：无序（行首 -、*、+ 接空格）、有序（1. 或 1) 接空格）。 */
  const listMatch = (line) => {
    let m = line.match(/^\s*[-*+]\s+(.*)$/);
    if (m) return { tag: "ul", text: m[1] };
    m = line.match(/^\s*\d{1,3}[.)]\s+(.*)$/);
    if (m) return { tag: "ol", text: m[1] };
    return null;
  };

  const out = [];
  const lines = stashMath(String(text), mathTokens).split(/\r?\n/);
  let para = []; // 段落行缓冲（非空时 list 必为 null，二者互斥）
  let list = null; // { tag: "ul"|"ol", items: [原始行文本...] }
  const flushPara = () => {
    if (!para.length) return;
    out.push(`<p>${inline(esc(para.join(" ")))}</p>`);
    para = [];
  };
  const flushList = () => {
    if (!list) return;
    const lis = list.items.map((item) => `<li>${inline(esc(item))}</li>`).join("");
    out.push(`<${list.tag}>${lis}</${list.tag}>`);
    list = null;
  };
  const flush = () => {
    flushList();
    flushPara();
  };

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    // 围栏代码：```lang 起、下一个 ``` 行止；LLM 漏写闭围栏则收到底（兜底不丢代码）
    const fence = line.match(/^\s*```(\S*)/);
    if (fence) {
      flush();
      let j = i + 1;
      while (j < lines.length && !/^\s*```/.test(lines[j])) j += 1;
      const body = lines.slice(i + 1, j).join("\n").replace(/^\n+|\n+$/g, ""); // g：首尾都要剥
      out.push(codeBlock(fence[1], esc(body)));
      i = j + 1;
      continue;
    }

    // 空行：段落/列表在此收口
    if (!line.trim()) {
      flush();
      i += 1;
      continue;
    }

    // 标题：#~#### → h3~h6（页面里 h1/h2 留给栏目级结构，气泡内不再外借）
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      flush();
      out.push(`<h${Math.min(2 + heading[1].length, 6)}>${inline(esc(heading[2]))}</h${Math.min(2 + heading[1].length, 6)}>`);
      i += 1;
      continue;
    }

    // 分割线（纯 ---/***/___；在表行判断之前——表格分隔行以 | 起头，不冲突）
    if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      flush();
      out.push("<hr>");
      i += 1;
      continue;
    }

    // 管道表：连续"| 起头"的行整块收集
    if (line.trim().startsWith("|")) {
      flush();
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        let row = lines[i];
        // LLM 长行折行：本行未以 | 收尾才收后续行拼回（至多 3 行，
        // 不吞新块起始行——空行/|、#、>、`、列表标记、分割线都停）
        let folds = 0;
        while (!row.trim().endsWith("|") && i + 1 < lines.length && folds < 3) {
          const nxt = lines[i + 1].trim();
          if (
            !nxt ||
            /^[|#>`]/.test(nxt) ||
            /^[-*+]\s/.test(nxt) ||
            /^\d{1,3}[.)]\s/.test(nxt) ||
            /^(?:-{3,}|\*{3,}|_{3,})\s*$/.test(nxt)
          )
            break;
          row += " " + nxt;
          i += 1;
          folds += 1;
        }
        rows.push(row);
        i += 1;
      }
      out.push(tableHtml(rows));
      continue;
    }

    // 引用块：连续 > 行合成一段（行内仍可加粗/代码）
    if (/^\s*>/.test(line.trim())) {
      flush();
      const parts = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) {
        const content = lines[i].trim().replace(/^>\s?/, "");
        if (content) parts.push(inline(esc(content)));
        i += 1;
      }
      const cls = parts[0] === BILINGUAL_TITLE ? ' class="bilingual"' : "";
      out.push(`<blockquote${cls}>${parts.map((p) => `<p>${p}</p>`).join("")}</blockquote>`);
      continue;
    }

    // 列表项：起新列表或并入同类型旧列表；换类型先收口旧列表
    const item = listMatch(line);
    if (item) {
      flushPara(); // 列表可打断段落（markdown 语义）
      if (list && list.tag !== item.tag) flushList();
      if (!list) list = { tag: item.tag, items: [] };
      list.items.push(item.text);
      i += 1;
      continue;
    }

    // 列表的惰性续行：无空行紧跟的内容折回当前项（块起始行已在上面各
    // 分支收口，到不了这里）
    if (list) {
      list.items[list.items.length - 1] += " " + line;
      i += 1;
      continue;
    }

    // 普通段落行（相邻无空行行并成一段，与旧版 split(/\n{2,}/) 语义一致）
    para.push(line);
    i += 1;
  }
  flush(); // 文末收口残段
  const html = out.join("");
  if (!mathTokens.length) return html;
  return html.replace(
    new RegExp(PH_MATH + "(\\d+)" + PH_MATH, "g"),
    (_m, idx) => mathTokens[Number(idx)]
  );
}

/** 引用明细卡（回答下方的"参考资料"栏）。marker 数字作为卡 id。
 *
 * 卡上带 `data-chunk-id`，**点击整张卡 = 点击 [n] 角标**：在阅读面板里打开
 * 该块（qa.js 的 click 委托统一处理，与 chip 同一条路径，2026-09-11）。 */
export function renderCitations(citations) {
  const cards = citations.map(
    (c) => `<div class="cite-card" id="cite-${c.marker}" data-chunk-id="${c.chunk_id}">
      <div class="c-head">
        <span class="pill">[${c.marker}]</span>
        <span class="c-title">${esc(c.document_title)}</span>
        <span class="muted small">${esc(c.section || "")}${c.page ? ` · 第 ${c.page} 页` : ""}</span>
      </div>
      <div class="c-snippet">${esc(c.snippet)}</div>
    </div>`
  );
  if (!cards.length) return "";
  return `<div class="ref-shelf">${cards.join("")}</div>`;
}

/* ------------------------------------------------------------------ */
/* 渲染：评测报告 markdown 轻渲染                                     */
/* ------------------------------------------------------------------ */

/**
 * 评测报告 markdown 渲染（不通用——只覆盖 render_report 的固定规约，
 * 见 eval/report.py 模块 docstring：一级/二级标题、管道表、粗体段、
 * 引用块、段落）。
 *
 * 容错点：异常明细表的单元格内含换行文本（render_report 直塞），
 * 表现为"表行未以 | 收尾即换行"——解析器把这类续行拼回上一单元格。
 *
 * 安全：输入先整体 esc，识别出的结构（表格/粗体）在转义后的文本上
 * 做替换，不含任何未转义注入面。
 */
export function renderMarkdown(md) {
  const lines = String(md).split(/\r?\n/);
  const out = [];
  let i = 0;

  /** 收集一段"表行"：从 lines[i]（以 | 开头）起，续行直到以 | 收尾。 */
  function collectRow() {
    let row = lines[i];
    while (!row.trim().endsWith("|") && i + 1 < lines.length) {
      row += " " + lines[i + 1].trim();
      i += 1;
    }
    return row;
  }
  /** 管道行 → 单元格数组（去首尾空）。 */
  function cells(row) {
    return row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((s) => s.trim());
  }
  /** 单元格文本：esc 之后只回填 **粗体**（报告表格用它标"自动生成"一类记号）。 */
  function mdCell(text) {
    return esc(text).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  }

  while (i < lines.length) {
    const line = lines[i];

    // 标题（报告规约只用 # / ## 两级）
    const heading = line.match(/^(#{1,2}) (.*)$/);
    if (heading) {
      const level = heading[1].length === 1 ? 3 : 4; // h3 留 h1 级、h4 留二级
      out.push(`<h${level}>${esc(heading[2])}</h${level}>`);
      i += 1;
      continue;
    }

    // 管道表：连续"| 开头"的行（含跨行单元格），紧跟 --- 表头分隔
    if (line.trim().startsWith("|")) {
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        rows.push(collectRow());
        i += 1;
      }
      const head = cells(rows[0]);
      // 分隔行判据：整行只由 | 、- 、空格（及对齐冒号）组成。字符类里的 `-`
      // 必须放末尾——写成 `[\s:-|]` 会被当作 `:` 到 `|` 的**范围**，`-` 自己
      // 反而不在其中，于是 `| --- | --- |` 匹配不上、每张表都多渲染一行
      // 分隔行（2026-09-20 E2E 截图实拍到，"---" 当成数据行显示）。
      const body = rows.slice(1).filter((r) => !/^\s*\|[\s|:-]+\|\s*$/.test(r));
      const htmlRows = [
        `<thead><tr>${head.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead>`,
        `<tbody>${body
          .map((r) => `<tr>${cells(r).map((c) => `<td>${mdCell(c)}</td>`).join("")}</tr>`)
          .join("")}</tbody>`,
      ];
      out.push(`<table>${htmlRows.join("")}</table>`);
      continue;
    }

    // 引用块（附录提示行）
    if (line.trim().startsWith(">")) {
      out.push(`<blockquote>${esc(line.trim().replace(/^>\s?/, ""))}</blockquote>`);
      i += 1;
      continue;
    }

    // 粗体独立段（如"**按难度分层（recall 均值）**"）
    const bold = line.match(/^\*\*(.*)\*\*$/);
    if (bold) {
      out.push(`<p class="md-note"><strong>${esc(bold[1])}</strong></p>`);
      i += 1;
      continue;
    }

    // 空行跳过
    if (!line.trim()) {
      i += 1;
      continue;
    }

    // 其余为普通段落（报告规约中为纯文本行）
    out.push(`<p>${esc(line.trim())}</p>`);
    i += 1;
  }
  return out.join("\n");
}

/* ------------------------------------------------------------------ */
/* 顶栏初始化（三页共用的 header 片段）                                */
/* ------------------------------------------------------------------ */

/**
 * 顶栏装配：当前导航高亮 + 拉 /api/health 填状态胶囊
 * （profile / 模型 / 语料规模——演示第一屏的信息面）。
 *
 * 自愈逻辑：拉取失败显示"服务未连接"，3s 快速档持续重试；一旦连通切
 * 20s 低频保活刷新——服务冷启动/重启后胶囊自动恢复，不再卡死在静态
 * 占位的"正在连接服务…"（2026-09-09 用户实测暴露：一次性拉取在
 * 服务重启空档加载的页面会永远停在占位文案）。
 *
 * 返回 health（成功）或 null（失败）：问答页据 profile 决定
 * "自由问答"按钮可用性（渐进增强；权威仍是后端守卫）。
 */
export async function initTopbar(active) {
  for (const a of document.querySelectorAll("nav.main a")) {
    if (a.dataset.page === active) a.classList.add("active");
  }
  const pill = $(".health-pill");
  const render = (health) => {
    if (!pill) return;
    pill.innerHTML = ""; // 静态占位（"正在连接…"）清掉，换成实况
    if (!health) {
      pill.append(el("span", null, "服务未连接，自动重试中…"));
      return;
    }
    pill.append(
      el("span", { class: "dot" }),
      el("span", null, `${health.profile} · ${health.llm_model}`),
      el("span", null, `语料 ${health.documents} 篇 / ${health.chunks} 块`)
    );
  };
  const tick = async () => {
    const h = await apiFetch("/api/health").catch(() => null);
    render(h);
    return h;
  };
  let health = await tick();
  const loop = async () => {
    health = await tick();
    // 断开快重试（3s）、连通低频保活（20s）：链式定时无重叠
    setTimeout(loop, health ? 20000 : 3000);
  };
  setTimeout(loop, health ? 20000 : 3000);
  return health;
}
