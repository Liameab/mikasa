/* =========================================================================
   在线找论文（知识库页「在线找论文」面板，M7 前端）。

   后端契约（src/mikasa/web/routers/papers.py；设计取舍见 ADR-0019）：
     POST /api/papers/search  {q, source, offset, limit} →
          {results[], errors{}, has_more}——逐源降级：单源失败照常 200
          （errors 里是中文文案），两源全灭才 502；
     POST /api/papers/import  {source, id} → 201 新入库 / 200 同内容跳过 /
          409 无开放获取全文 / 502 下载失败（body.detail 是中文消息，
          与上传端点逐字节同形的响应形状）；
     GET/PUT /api/papers/settings  OpenAlex 密钥（GET **只回布尔**，值永不
          回显——已保存状态只靠 placeholder 表达）。

   翻页语义：offset 是**全局交错窗口**（arXiv 占偶数位、OpenAlex 占奇数位），
   前端只管把 offset 推进"已收到的条数"，换算在服务端。

   交互纪律：全部 addEventListener；标题/摘要/作者来自外部 API，一律走
   el() 文本节点，不拼 HTML（这是天然的注入入口）。
   ========================================================================= */

import { $, apiFetch, el, toast } from "./common.js";
import { refreshCorpusTree } from "./kb-tree.js";

const PAGE_SIZE = 20;
const SOURCE_LABEL = { arxiv: "arXiv", openalex: "OpenAlex" };

// 本面板的会话状态：翻页 = offset 前进；改词/改来源 = 从 0 重来
const state = { q: "", source: "all", offset: 0, count: 0, busy: false };
// 密钥三段语义（同 model-settings.js）：未动过=不提交；动过且为空=清除；
// 动过且非空=写入。不记这个标记，一次普通的保存会把空值当"清除密钥"。
let keyDirty = false;
let keySaved = false;

const form = $("#paper-form");
const input = $("#paper-q");
const searchBtn = $("#paper-search-btn");
const sourceBox = $("#paper-sources");
const statusLine = $("#paper-status");
const resultsBox = $("#paper-results");
const moreBtn = $("#paper-more");

/* ---------------- 结果行 ---------------- */

/** 作者行：最多三个名字，更多则缀"等 N 人"。 */
function authorsLine(paper) {
  const names = paper.authors || [];
  if (!names.length) return "作者未提供";
  const head = names.slice(0, 3).join("、");
  return names.length > 3 ? `${head} 等 ${names.length} 人` : head;
}

/** 元信息行：年份 · 期刊/会议 · 是否有开放获取全文。 */
function metaLine(paper) {
  return [
    paper.year ? String(paper.year) : "",
    paper.venue,
    paper.oa ? "开放获取" : "无全文",
  ]
    .filter(Boolean)
    .join(" · ");
}

/** 导入一篇：下载 PDF 并入库（服务端文案直接展示，同上传交互）。 */
async function importPaper(paper, btn, statusNode) {
  if (!paper.pdf_url) return;
  btn.disabled = true;
  statusNode.textContent = "正在下载并入库…";
  try {
    const resp = await fetch("/api/papers/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: paper.source, id: paper.id }),
    });
    const body = await resp.json().catch(() => null);
    const message = body?.detail || body?.message || `HTTP ${resp.status}`;
    if (resp.ok) {
      // 201 新入库 / 200 同内容跳过：文案都是服务端中文消息
      statusNode.textContent = "";
      btn.textContent = "已导入";
      toast(message, resp.status === 201 ? "ok" : "warn");
      refreshCorpusTree(); // 新文档出现在树根级，拖进文件夹即可归档
    } else {
      // 409 无开放获取 / 502 下载失败：留在行内，用户能对着这条结果读
      statusNode.textContent = message;
      btn.disabled = false;
      toast(`导入失败：${message}`, "error");
    }
  } catch {
    statusNode.textContent = "导入失败：网络中断";
    btn.disabled = false;
  }
}

/** 一条结果的 DOM（标题/作者/年份/来源 + 摘要开合 + 导入/打开）。 */
function resultRow(paper) {
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
  const abstractBox = el(
    "div",
    { class: "paper-abstract hidden" },
    paper.abstract || "（该来源没有提供摘要）"
  );
  const statusNode = el("div", { class: "paper-status muted small" });

  const abstractBtn = el("button", { class: "btn ghost", type: "button" }, "摘要");
  abstractBtn.addEventListener("click", () => {
    const hidden = abstractBox.classList.toggle("hidden");
    abstractBtn.textContent = hidden ? "摘要" : "收起";
  });

  const importBtn = el("button", { class: "btn primary", type: "button" }, "导入");
  importBtn.addEventListener("click", () => void importPaper(paper, importBtn, statusNode));
  if (!paper.pdf_url) {
    // 无开放获取：按钮禁用并在行内说明，比"点了才 409"更早告知
    importBtn.disabled = true;
    importBtn.title = "该论文没有开放获取全文";
  }

  const actions = el("div", { class: "paper-actions" }, abstractBtn, importBtn);
  if (paper.landing_url) {
    actions.append(
      el(
        "a",
        { class: "btn ghost", href: paper.landing_url, target: "_blank", rel: "noopener noreferrer" },
        "打开"
      )
    );
  }

  return el(
    "div",
    { class: "paper-item" },
    el(
      "div",
      { class: "paper-head" },
      titleNode,
      el("span", { class: "pill paper-src" }, SOURCE_LABEL[paper.source] || paper.source)
    ),
    el("div", { class: "paper-meta muted small" }, `${authorsLine(paper)} · ${metaLine(paper)}`),
    actions,
    abstractBox,
    statusNode
  );
}

/* ---------------- 检索 ---------------- */

function renderPage(body, append) {
  const results = body.results || [];
  if (!append) resultsBox.replaceChildren();
  for (const paper of results) resultsBox.append(resultRow(paper));
  state.count = append ? state.count + results.length : results.length;
  state.offset = state.count;
  moreBtn.classList.toggle("hidden", !body.has_more);

  // 逐源降级提示：单源失败不影响其余结果，但要让用户知道少了一个源
  const notes = Object.entries(body.errors || {}).map(
    ([src, msg]) => `${SOURCE_LABEL[src] || src}：${msg}`
  );
  if (!results.length && !notes.length) {
    resultsBox.append(
      el("div", { class: "empty" }, "没有找到相关论文——换个更具体的中文或英文关键词试试")
    );
  }
  if (notes.length) {
    statusLine.textContent = `部分来源暂时不可用（${notes.join("；")}），以上为其余来源的结果`;
  } else if (results.length) {
    statusLine.textContent = `已显示 ${state.count} 条${body.has_more ? "，可继续加载" : ""}`;
  } else {
    statusLine.textContent = "";
  }
}

/** 检索（append=true 为「加载更多」）。 */
async function runSearch({ append = false } = {}) {
  const q = input.value.trim();
  if (!q) {
    toast("请先输入检索词", "warn");
    return;
  }
  if (state.busy) return; // 连点保护：翻页/换词都串行
  state.q = q;
  if (!append) {
    state.offset = 0;
    state.count = 0;
    resultsBox.replaceChildren();
    moreBtn.classList.add("hidden");
  }
  state.busy = true;
  searchBtn.disabled = true;
  statusLine.textContent = append ? "正在加载下一页…" : "正在检索…";
  try {
    const body = await apiFetch("/api/papers/search", {
      method: "POST",
      body: JSON.stringify({
        q: state.q,
        source: state.source,
        offset: state.offset,
        limit: PAGE_SIZE,
      }),
    });
    renderPage(body, append);
  } catch (err) {
    // 502 全灭（detail 是逐源文案）与网络中断都到这里
    statusLine.textContent = "";
    toast(`检索失败：${err.message}`, "error");
  } finally {
    state.busy = false;
    searchBtn.disabled = false;
  }
}

/* ---------------- OpenAlex 密钥（可选） ---------------- */

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
    const body = await apiFetch("/api/papers/settings");
    keySaved = !!body.has_api_key;
  } catch {
    keySaved = false; // 读不到就当没存：placeholder 不至于骗人
  }
  syncKeyPlaceholder();
}

async function saveKey() {
  const body = {};
  if (keyDirty) body.api_key = $("#paper-key-input").value; // 空串 = 清除
  try {
    const res = await apiFetch("/api/papers/settings", {
      method: "PUT",
      body: JSON.stringify(body),
    });
    keySaved = !!res.has_api_key;
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

/** 知识库页启动时调用一次（documents.js）。 */
export function initPapersPanel() {
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    void runSearch();
  });
  moreBtn.addEventListener("click", () => void runSearch({ append: true }));

  sourceBox.addEventListener("click", (ev) => {
    const chip = ev.target.closest(".s-chip");
    if (!chip) return;
    state.source = chip.dataset.source;
    sourceBox
      .querySelectorAll(".s-chip")
      .forEach((c) => c.classList.toggle("on", c === chip));
    if (state.count) void runSearch(); // 已有结果才自动重搜（没搜过不代跑）
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
      void saveKey();
    }
  });
  $("#paper-key-save").addEventListener("click", () => void saveKey());
  void refreshKeyStatus();
}
