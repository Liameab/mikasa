/* =========================================================================
   找论文页入口（papers.html）：状态、结果列表、翻页、导入、密钥。

   分页契约（**容易踩错的一点**）：offset 按**窗口大小**（limit）推进，
   不是按"已收到的条数"。某些来源耗尽或失败时页面上会有空位（留洞不补位），
   收到的条数 < limit——此时按条数推会让下一页整体左移、结果重复
   （2026-09-16 修掉的真实缺陷，服务层单测 test_exhausted_source_leaves_holes
   在守）。第 N 页 = 全局窗口 [(N-1)×50, N×50)，服务层的窗口公式自己把它
   摊到各来源上，所以"跳页"只是换一个 offset。

   总页数怎么来的（2026-09-20 用户要求"直观看到一共多少页、能跳页"）：
   各来源在响应里报自己的命中总数（界面上那行"OpenAlex 命中 6 万条"就是它），
   合并流没有精确总数——所以总页数 = 命中合计 ÷ 每页，**只在有源报总数时才给**。
   上限不写死：见 pageCap()，它由"上游按页取数的 1 万条上限 + 当前选了几个源"
   推出来（每页 50 条由 n 个源轮转着出，单源每页只消耗约 50/n 条）。翻到没有
   数据的页时只说"这一页取不到结果了"，不编末页。

   点结果行 = 浏览器新标签打开论文原页；右侧详情由行内「详情」按钮打开。

   模块分工：papers-api.js（端点）/ papers-filters.js（筛选与能力对齐）/
   papers-detail.js（右侧详情）/ 本文件（状态与装配）。
   ========================================================================= */

import { $, el, initTopbar, toast } from "./common.js";
import { fetchKeyStatus, fetchSources, importPaper, saveKey, searchPapers } from "./papers-api.js";
import { createFilters } from "./papers-filters.js";
import { citesText, renderDetail, renderEmpty } from "./papers-detail.js";
import { initOnboard } from "./onboard.js";
import { initUpdateBadge } from "./update.js"; // 更新进度胶囊（观察到别处起的下载）

// 每页条数：20 → 50（2026-09-19）。二十条在大屏上一屏半就到底、"加载更多"
// 点个不停；三源并发之后一页的成本主要是网络往返，条数翻倍几乎不增加等待。
const PAGE_SIZE = 50;
const SOURCE_LABEL = { arxiv: "arXiv", openalex: "OpenAlex", core: "CORE", doaj: "DOAJ" };

// 检索历史（v0.1.4「像知网」）：只存本机浏览器，最多 8 条。
// **联想来自历史，不走上游**——上游的 typeahead 实测十几秒（2026-09-19），
// 挂在输入框上只会让人以为卡了；历史是本地读写，零延迟且不泄露检索词。
const HISTORY_KEY = "mikasa.papers.history";
const HISTORY_MAX = 8;

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
const pagerBox = $("#paper-pager");
const pagerNums = $("#paper-pager-nums");
const pageInput = $("#paper-page-input");
const goBtn = $("#paper-page-go");
const detailBox = $("#paper-detail");
const historyBox = $("#paper-history");
const historyList = $("#paper-history-list");

// 一次检索的会话状态（翻页只动 page；改词/改条件 = 回第 1 页重来）
// pages = 算出来的总页数（拿不到上游命中数时是 null：那就只显示"第 N 页"）
const state = {
  q: "",
  page: 1,
  pages: null,
  hasMore: false,
  busy: false,
  results: [],
  selected: null,
  totals: {},
};

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

  // 详情入口（2026-09-20）：点整行现在是"去读它"（浏览器打开原页），
  // 摘要/导入/相关论文这些"看"的动作收到这枚按钮上——两种意图分开
  const detailBtn = el(
    "button",
    { class: "btn ghost paper-detail-btn", type: "button", title: "看摘要、导入知识库、相关论文" },
    "详情"
  );
  detailBtn.addEventListener("click", () => selectPaper(paper, row));
  head.append(detailBtn);

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
  // 点整行 = 浏览器新标签打开论文原页；行内链接与按钮自己处理
  row.addEventListener("click", (ev) => {
    if (ev.target.closest("a") || ev.target.closest("button")) return;
    if (paper.landing_url) {
      window.open(paper.landing_url, "_blank", "noopener");
      return;
    }
    selectPaper(paper, row); // 极少数没有原页地址的：退回详情，别点了没反应
  });
  return row;
}

/* ---------------- 检索历史 ---------------- */

function readHistory() {
  try {
    const raw = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
    return Array.isArray(raw) ? raw.filter((x) => typeof x === "string").slice(0, HISTORY_MAX) : [];
  } catch {
    return []; // 隐私模式/坏数据：当作没有历史，不影响检索本身
  }
}

function writeHistory(items) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(items.slice(0, HISTORY_MAX)));
  } catch {
    /* 存不下只是下次没有历史，不影响使用 */
  }
}

/** 把这次检索词记进历史（去重、最近的排最前）。 */
function rememberQuery(q) {
  const items = readHistory().filter((x) => x !== q);
  items.unshift(q);
  writeHistory(items);
  renderHistory();
}

function renderHistory() {
  const items = readHistory();
  historyBox.classList.toggle("hidden", items.length === 0);
  historyBox.replaceChildren();
  // 原生 datalist：在输入框里打字时给候选（浏览器自带，无需自绘下拉）
  historyList.replaceChildren(
    ...items.map((q) => {
      const opt = document.createElement("option");
      opt.value = q;
      return opt;
    })
  );
  if (!items.length) return;
  historyBox.append(el("span", null, "最近："));
  for (const q of items) {
    const chip = el("button", { class: "ph-chip", type: "button", title: q }, q);
    chip.addEventListener("click", () => {
      input.value = q;
      void runSearch();
    });
    historyBox.append(chip);
  }
  const clear = el("button", { class: "ph-clear", type: "button", title: "清空检索历史" }, "清空");
  clear.addEventListener("click", () => {
    writeHistory([]);
    renderHistory();
  });
  historyBox.append(clear);
}

/**
 * 在详情面板里打开一条论文（来源可以是检索结果，也可以是"相关论文/被引"
 * 里的条目）。结果列表里有它就同步高亮那一行；没有就只是面板切换——
 * **不往列表里插行**：列表是"这次检索的快照"，混进引证结果会让人分不清
 * 哪条是自己搜出来的。
 */
function showPaper(paper) {
  state.selected = paper;
  const row = resultsBox.querySelector(`.paper-item[data-ref="${paper.source}:${paper.id}"]`);
  markSelected(row);
  renderDetail(detailBox, paper, { onImport: doImport, onPick: showPaper });
}

function markSelected(row) {
  for (const node of resultsBox.querySelectorAll(".paper-item")) {
    node.classList.toggle("on", node === row);
  }
}

function selectPaper(paper, row) {
  state.selected = paper;
  markSelected(row);
  renderDetail(detailBox, paper, { onImport: doImport, onPick: showPaper });
}

/** 命中"已在库中"后就地更新那一行（不重查——服务端下次检索自然一致）。 */
function markInLibrary(paper) {
  paper.in_library = true;
  const row = resultsBox.querySelector(`.paper-item[data-ref="${paper.source}:${paper.id}"]`);
  if (row && !row.querySelector(".paper-inlib")) {
    row.querySelector(".paper-head").append(el("span", { class: "pill paper-inlib" }, "已在库中"));
  }
  renderDetail(detailBox, paper, { onImport: doImport, onPick: showPaper });
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

function renderNotes(body) {
  // 逐源降级（errors）与能力降级（notes）都要说清楚，但两者语义不同
  const parts = [];
  for (const [name, msg] of Object.entries(body.errors || {})) {
    parts.push(`${SOURCE_LABEL[name] || name} 暂时不可用：${msg}`);
  }
  for (const [name, msg] of Object.entries(body.notes || {})) {
    parts.push(`${SOURCE_LABEL[name] || name}：${msg}`);
  }
  notesLine.textContent = parts.join("；");
}

/**
 * 可翻深度的上限（页）。
 *
 * 硬约束在**上游**：按页取数的来源（OpenAlex / DOAJ）文档上限都是第 1 万条，
 * 更深的翻页只能靠 cursor，而 cursor 跳不到任意页——所以"再多也翻不动"。
 * 每页 50 条由 n 个来源轮转着出，单个来源每页只消耗约 50/n 条，于是 n 个来源
 * 合起来能到 10_000×n/50 = 200n 页。来源选得少，深度就浅——这是实情，不硬撑。
 */
function pageCap() {
  const n = Math.max(1, filters.sourceCount());
  return 200 * n;
}

/** 上游命中总数 → {hits, pages, capped}；没有源报总数 → null（只显示"第 N 页"）。 */
function pageInfo() {
  const hits = Object.values(state.totals || {}).reduce((a, b) => a + b, 0);
  if (!hits) return null;
  const cap = pageCap();
  const raw = Math.ceil(hits / PAGE_SIZE);
  return { hits, pages: Math.max(1, Math.min(cap, raw)), capped: raw > cap };
}

/** 页码条画哪些页：首末页 + 当前页左右各两页（其余位置用省略号）。 */
function pageNumbers(current, total) {
  const nums = new Set([1, total]);
  for (let p = current - 2; p <= current + 2; p += 1) {
    if (p >= 1 && p <= total) nums.add(p);
  }
  return [...nums].sort((a, b) => a - b);
}

/** 页码条：渲染与置灰状态一起管（忙时每个可点的东西都要看得出来）。 */
function renderPager() {
  const total = state.pages;
  // 一页就装得下（或压根没有结果）就不摆翻页条；拿不到总数时按 has_more 决定
  const show = state.results.length > 0 && (total ? total > 1 : state.hasMore);
  pagerBox.classList.toggle("hidden", !show);
  if (!show) return;

  const nodes = [];
  const prev = el("button", { class: "btn ghost", type: "button", "data-nav": "prev" }, "上一页");
  prev.disabled = state.busy || state.page <= 1;
  nodes.push(prev);
  if (total) {
    let last = 0;
    for (const n of pageNumbers(state.page, total)) {
      if (n - last > 1) nodes.push(el("span", { class: "pg-gap" }, "…"));
      const btn = el(
        "button",
        { class: `pg-num${n === state.page ? " on" : ""}`, type: "button" },
        String(n)
      );
      btn.disabled = state.busy;
      btn.addEventListener("click", () => goToPage(n));
      nodes.push(btn);
      last = n;
    }
  } else {
    nodes.push(el("span", { class: "pg-num on" }, String(state.page)));
  }
  const next = el("button", { class: "btn ghost", type: "button", "data-nav": "next" }, "下一页");
  next.disabled = state.busy || !state.hasMore || (total ? state.page >= total : false);
  nodes.push(next);
  pagerNums.replaceChildren(...nodes);

  pageInput.value = String(state.page);
  if (total) pageInput.max = String(total);
  pageInput.disabled = state.busy;
  goBtn.disabled = state.busy;
}

/** 跳到第 page 页；越界或点的是当前页 → 就地收敛，不发请求。 */
function goToPage(page) {
  if (state.busy) return;
  const total = state.pages;
  const wanted = Number.isFinite(page) ? Math.floor(page) : state.page;
  const target = Math.max(1, total ? Math.min(total, wanted) : wanted);
  if (target === state.page) {
    pageInput.value = String(state.page); // 手输了个越界/同一个页码 → 回到当前值
    return;
  }
  void runSearch({ page: target });
}

/** 状态行：页码 + 本页条数 + 各源命中数 +（封顶时）为什么不再往下翻。 */
function statusText() {
  const info = pageInfo();
  const bits = [info ? `第 ${state.page} / ${info.pages} 页` : `第 ${state.page} 页`];
  bits.push(`本页 ${state.results.length} 条`);
  const totals = totalsText(state.totals);
  if (totals) bits.push(totals);
  if (info && info.capped) {
    const cap = pageCap();
    bits.push(
      `上游合计 ${info.hits.toLocaleString("zh-CN")} 条，可翻深度封顶第 ${cap} 页` +
        `（${filters.sourceCount()} 个来源各按页取数最多到第 1 万条）`
    );
  }
  if (!state.hasMore) bits.push("没有更多了");
  return bits.join(" · ");
}

function renderPage(body, page) {
  const results = body.results || [];
  resultsBox.replaceChildren();
  state.results = [];
  state.selected = null;
  state.page = page;
  state.hasMore = Boolean(body.has_more);
  // 各源总数是"这次查询"级的常量，逐源合并（不是整份替换）：深翻页时某个源
  // 可能这一页不再报总数（它先触到自己的深度上限），替换会让页数凭空缩水
  state.totals = { ...state.totals, ...(body.totals || {}) };
  const info = pageInfo();
  // 总页数是**按上游自报的命中数算出来的估计**（状态行里写明来路），拿不到就没有
  state.pages = info ? info.pages : null;

  for (const paper of results) {
    state.results.push(paper);
    resultsBox.append(resultRow(paper));
  }
  renderEmpty(detailBox); // 列表换了，右侧详情跟着回到空态
  renderHistory(); // 检索历史（本地）
  renderNotes(body);

  if (!results.length) {
    if (page > 1) {
      // 翻到空页 = 上游提前耗尽（报的命中数比真给得出的结果多）。
      // **不编"结果到第几页为止"**：我们只知道首页到不了这儿，不知道具体停在哪
      resultsBox.append(
        el("div", { class: "empty" }, "这一页已经取不到结果了——往前翻，或收窄筛选条件")
      );
      statusLine.textContent = `第 ${page} 页没有结果`;
    } else {
      resultsBox.append(
        el("div", { class: "empty" }, "没有找到相关论文——换个关键词，或放宽筛选条件")
      );
      statusLine.textContent = "";
    }
  } else {
    statusLine.textContent = statusText();
  }
  renderPager();
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

async function runSearch({ page = 1 } = {}) {
  const q = input.value.trim();
  if (!q) {
    toast("请先输入检索词", "warn");
    return;
  }
  if (state.busy) return; // 连点保护：翻页/换词都串行（忙时每个可点的都置灰）
  const newQuery = q !== state.q;
  state.q = q;
  state.busy = true;
  // 忙时把按钮都禁掉：**点击绝不能被静默吞掉**（2026-09-16 用户报"加载更多
  // 点了没用"——真实来源要跑十几秒，期间点下去没有任何反馈）
  searchBtn.disabled = true;
  renderPager();
  startTicker(page > 1 ? `正在加载第 ${page} 页` : "正在检索");
  try {
    const body = await searchPapers({
      q,
      sources: filters.readSources(),
      // 第 N 页 = 全局窗口 [(N-1)×50, N×50)；服务层的窗口公式自己摊到各来源上
      offset: (page - 1) * PAGE_SIZE,
      limit: PAGE_SIZE,
      filters: filters.readFilters(),
    });
    renderPage(body, page);
    if (newQuery && page === 1) rememberQuery(q); // 历史只记"新检索"，翻页不算
    resultsBox.scrollTop = 0; // 换页后回到列表顶部
  } catch (err) {
    // 错误**写进状态行**而不只是 toast：toast 3.6 秒后消失，用户常常错过，
    // 然后以为"点了没用"（502 全灭、参数被拒都走这条路）
    statusLine.textContent = `检索失败：${err.message}`;
    toast(`检索失败：${err.message}`, "error");
  } finally {
    stopTicker();
    state.busy = false;
    searchBtn.disabled = false;
    renderPager();
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
  // 翻页：上一页/下一页走事件委托，页码按钮各自带句柄（见 renderPager）
  pagerBox.addEventListener("click", (ev) => {
    const nav = ev.target.closest("button[data-nav]");
    if (!nav || nav.disabled) return;
    goToPage(state.page + (nav.dataset.nav === "next" ? 1 : -1));
  });
  goBtn.addEventListener("click", () => goToPage(Number(pageInput.value)));
  pageInput.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      goToPage(Number(pageInput.value));
    }
  });

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
initUpdateBadge(); // 应用更新进度（本页不打 check，只看本地状态）
bind();
renderEmpty(detailBox);
void refreshKeyStatus();
void filters.init(fetchSources);
