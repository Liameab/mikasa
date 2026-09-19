/* =========================================================================
   找论文页入口（papers.html）：状态、结果列表、分页、导入、密钥。

   分页契约（**容易踩错的一点**）：offset 按**窗口大小**（limit）推进，
   不是按"已收到的条数"。某些来源耗尽或失败时页面上会有空位（留洞不补位），
   收到的条数 < limit——此时按条数推会让下一页整体左移、结果重复
   （2026-09-16 修掉的真实缺陷，服务层单测 test_exhausted_source_leaves_holes
   在守）。

   模块分工：papers-api.js（端点）/ papers-filters.js（筛选与能力对齐）/
   papers-detail.js（右侧详情）/ 本文件（状态与装配）。
   ========================================================================= */

import { $, el, initTopbar, toast } from "./common.js";
import { fetchKeyStatus, fetchSources, importPaper, saveKey, searchPapers } from "./papers-api.js";
import { createFilters } from "./papers-filters.js";
import { citesText, renderDetail, renderEmpty } from "./papers-detail.js";
import { initOnboard } from "./onboard.js";

// 每页条数：20 → 50（2026-09-19）。二十条在大屏上一屏半就到底、"加载更多"
// 点个不停；三源并发之后一页的成本主要是网络往返，条数翻倍几乎不增加等待。
const PAGE_SIZE = 50;
const SOURCE_LABEL = { arxiv: "arXiv", openalex: "OpenAlex", core: "CORE" };

/** 各源上游命中总数 → "OpenAlex 命中 69,966 条"（拿不到总数的源不出现）。 */
function totalsText(totals) {
  return Object.entries(totals || {})
    .map(([name, n]) => `${SOURCE_LABEL[name] || name} 命中 ${n.toLocaleString("zh-CN")} 条`)
    .join(" · ");
}

const form = $("#paper-form");
const input = $("#paper-q");
const searchBtn = $("#paper-search-btn");
const statusLine = $("#paper-status");
const notesLine = $("#paper-notes");
const resultsBox = $("#paper-results");
const moreBtn = $("#paper-more");
const detailBox = $("#paper-detail");

// 一次检索的会话状态（翻页只动 offset；改词/改条件 = 从 0 重来）
const state = { q: "", offset: 0, hasMore: false, busy: false, results: [], selected: null, totals: {} };

const filters = createFilters({
  onChange: ({ reSearch }) => {
    if (reSearch && state.q) void runSearch();
  },
});

/* ---------------- 结果行 ---------------- */

function resultRow(paper) {
  const key = `${paper.source}:${paper.id}`;
  const titleNode = paper.landing_url
    ? el(
        "a",
        {
          class: "paper-title",
          href: paper.landing_url,
          target: "_blank",
          rel: "noopener noreferrer",
        },
        paper.title
      )
    : el("span", { class: "paper-title" }, paper.title);

  const head = el(
    "div",
    { class: "paper-head" },
    titleNode,
    el("span", { class: "pill paper-src" }, SOURCE_LABEL[paper.source] || paper.source)
  );
  if (paper.in_library) head.append(el("span", { class: "pill paper-inlib" }, "已在库中"));

  const authors = paper.authors || [];
  const authorText = authors.length
    ? authors.slice(0, 3).join("、") + (authors.length > 3 ? ` 等 ${authors.length} 人` : "")
    : "作者未提供";
  const metaBits = [authorText, paper.year ? String(paper.year) : "", paper.venue || ""].filter(
    Boolean
  );

  const row = el(
    "div",
    { class: "paper-item", "data-ref": key },
    head,
    el(
      "div",
      { class: "paper-meta muted small" },
      metaBits.join(" · "),
      " · ",
      el("span", { class: "paper-cites" }, citesText(paper))
    ),
    paper.abstract ? el("div", { class: "paper-snippet" }, paper.abstract) : null
  );
  // 点整行 = 选中（右侧看详情）；行内链接不拦截，照常新标签打开
  row.addEventListener("click", (ev) => {
    if (ev.target.closest("a")) return;
    selectPaper(paper, row);
  });
  return row;
}

function markSelected(row) {
  for (const node of resultsBox.querySelectorAll(".paper-item")) {
    node.classList.toggle("on", node === row);
  }
}

function selectPaper(paper, row) {
  state.selected = paper;
  markSelected(row);
  renderDetail(detailBox, paper, { onImport: doImport });
}

/** 命中"已在库中"后就地更新那一行（不重查——服务端下次检索自然一致）。 */
function markInLibrary(paper) {
  paper.in_library = true;
  const row = resultsBox.querySelector(`.paper-item[data-ref="${paper.source}:${paper.id}"]`);
  if (row && !row.querySelector(".paper-inlib")) {
    row.querySelector(".paper-head").append(el("span", { class: "pill paper-inlib" }, "已在库中"));
  }
  renderDetail(detailBox, paper, { onImport: doImport });
}

/* ---------------- 导入 ---------------- */

async function doImport(paper, btn) {
  btn.disabled = true;
  btn.textContent = "正在下载并入库…";
  const { ok, status, message } = await importPaper(paper.source, paper.id);
  if (ok) {
    toast(message, status === 201 ? "ok" : "warn");
    markInLibrary(paper);
  } else {
    // 409 无全文 / 502 下载失败：文案是服务端中文消息，就地展示
    toast(`导入失败：${message}`, "error");
    btn.disabled = false;
    btn.textContent = "导入知识库";
  }
}

/* ---------------- 检索与分页 ---------------- */

function renderNotes(body, append) {
  // 逐源降级（errors）与能力降级（notes）都要说清楚，但两者语义不同
  const parts = [];
  for (const [name, msg] of Object.entries(body.errors || {})) {
    parts.push(`${SOURCE_LABEL[name] || name} 暂时不可用：${msg}`);
  }
  for (const [name, msg] of Object.entries(body.notes || {})) {
    parts.push(`${SOURCE_LABEL[name] || name}：${msg}`);
  }
  if (!append) notesLine.textContent = parts.join("；");
  else if (parts.length) notesLine.textContent = parts.join("；");
}

function renderPage(body, append) {
  const results = body.results || [];
  if (!append) {
    resultsBox.replaceChildren();
    state.results = [];
    state.selected = null;
    renderEmpty(detailBox);
  }
  for (const paper of results) {
    state.results.push(paper);
    resultsBox.append(resultRow(paper));
  }
  state.hasMore = Boolean(body.has_more);
  // 各源总数是"这次查询"级的常量，翻页时以最后一次响应为准即可
  if (body.totals) state.totals = body.totals;
  moreBtn.classList.toggle("hidden", !state.hasMore);
  renderNotes(body, append);

  if (!results.length && !append) {
    resultsBox.append(
      el("div", { class: "empty" }, "没有找到相关论文——换个关键词，或放宽筛选条件")
    );
    statusLine.textContent = "";
  } else {
    const totals = totalsText(state.totals);
    statusLine.textContent =
      `已显示 ${state.results.length} 条${state.hasMore ? "，可继续加载" : ""}` +
      (totals ? ` · ${totals}` : "");
  }
}

/** 在途计时：真实来源要 5-30 秒（arXiv 3 秒节流 + 网络），没有计时会像卡死。 */
let ticker = null;

function startTicker(prefix) {
  const t0 = Date.now();
  stopTicker();
  const paint = () => {
    statusLine.textContent = `${prefix}… ${((Date.now() - t0) / 1000).toFixed(1)}s`;
  };
  paint();
  ticker = setInterval(paint, 200);
}

function stopTicker() {
  if (ticker) {
    clearInterval(ticker);
    ticker = null;
  }
}

async function runSearch({ append = false } = {}) {
  const q = input.value.trim();
  if (!q) {
    toast("请先输入检索词", "warn");
    return;
  }
  if (state.busy) return; // 连点保护：翻页/换词都串行（按钮同时置灰，见下）
  state.q = q;
  state.busy = true;
  // 忙时把两个按钮都禁掉：**点击绝不能被静默吞掉**（2026-09-16 用户报
  // "加载更多点了没用"——真实来源要跑十几秒，期间点下去没有任何反馈）
  searchBtn.disabled = true;
  moreBtn.disabled = true;
  if (append) moreBtn.textContent = "加载中…";
  if (!append) {
    state.offset = 0;
    resultsBox.replaceChildren();
    state.results = [];
    moreBtn.classList.add("hidden");
    statusLine.textContent = "";
  }
  startTicker(append ? "正在加载下一页" : "正在检索");
  try {
    const body = await searchPapers({
      q,
      sources: filters.readSources(),
      offset: state.offset,
      limit: PAGE_SIZE,
      filters: filters.readFilters(),
    });
    renderPage(body, append);
    // **按窗口大小推进**：有空洞时收到的条数 < limit，按条数推会重复（见文件头）
    state.offset += PAGE_SIZE;
  } catch (err) {
    // 错误**写进状态行**而不只是 toast：toast 3.6 秒后消失，用户常常错过，
    // 然后以为"点了没用"（502 全灭、参数被拒都走这条路）
    statusLine.textContent = `检索失败：${err.message}`;
    toast(`检索失败：${err.message}`, "error");
  } finally {
    stopTicker();
    state.busy = false;
    searchBtn.disabled = false;
    moreBtn.disabled = false;
    moreBtn.textContent = "加载更多";
  }
}

/* ---------------- OpenAlex 密钥（可选） ---------------- */

let keyDirty = false;
let keySaved = false;

function syncKeyPlaceholder() {
  const node = $("#paper-key-input");
  const typed = keyDirty && node.value;
  if (typed) node.placeholder = keySaved ? "已保存（保存后替换为新密钥）" : "保存后写入";
  else
    node.placeholder = keySaved
      ? "已保存 · 输入新密钥可更换，清空后保存则删除"
      : "粘贴 OpenAlex 密钥（在 openalex.org 免费注册）";
}

async function refreshKeyStatus() {
  try {
    keySaved = await fetchKeyStatus();
  } catch {
    keySaved = false; // 读不到就当没存：placeholder 不至于骗人
  }
  syncKeyPlaceholder();
}

async function onSaveKey() {
  const value = keyDirty ? $("#paper-key-input").value : null; // null = 本次不动
  try {
    keySaved = await saveKey(value);
    keyDirty = false;
    $("#paper-key-input").value = "";
    syncKeyPlaceholder();
    $("#paper-key-note").textContent = keySaved
      ? "已保存：检索会带上密钥（提升额度，立即生效）"
      : "已清除：回到匿名限速通道";
  } catch (err) {
    $("#paper-key-note").textContent = `保存失败：${err.message}`;
  }
}

/* ---------------- 装配 ---------------- */

function bind() {
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    void runSearch();
  });
  moreBtn.addEventListener("click", () => void runSearch({ append: true }));

  const keyRow = $("#paper-key-row");
  $("#paper-key-toggle").addEventListener("click", () => keyRow.classList.toggle("hidden"));
  const keyInput = $("#paper-key-input");
  keyInput.addEventListener("input", () => {
    keyDirty = true;
    syncKeyPlaceholder();
  });
  keyInput.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      void onSaveKey();
    }
  });
  $("#paper-key-save").addEventListener("click", () => void onSaveKey());
}

initTopbar("papers").then((health) => void initOnboard(health));
bind();
renderEmpty(detailBox);
void refreshKeyStatus();
void filters.init(fetchSources);
