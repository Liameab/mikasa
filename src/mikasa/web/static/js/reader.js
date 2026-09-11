/* =========================================================================
   文档阅读器（2026-09-10，阅读视图 + 引用跳转）。

   **一套渲染，两种呈现**（initReader 的 host 参数决定）：
     - 浮层（问答页）：不传 host → 创建 fixed 右侧抽屉 `#reader-panel`，
       ✕ / Esc / 点外部三通道关闭。点引用角标时用它，不打断问答上下文。
     - 内嵌（知识库页）：传 host → 把同一套头部/正文/状态栏挂进右栏，
       常驻显示、不遮挡，读文档就在这一栏里读，不再弹浮层。

   安全：正文与标题全来自用户语料，**本模块不出现 innerHTML**——一律走
   el() 的文本子节点；iframe 的 src 只由 Number() 化的 id 与页码拼成。

   两个视图：
     - **文本**：`/api/documents/{id}/content` 的 {text, chunks} → buildPlan
       → 每块一个 `<span class="rd-chunk" data-chunk-id="N">`。引用跳转就是
       定位到该 span、加 `.lit` 并滚到视野中央。
     - **原文件**：PDF 交给浏览器自带阅读器内嵌（零新依赖，`#page=N` 跳页）；
       md/txt 直接显示原文；docx 浏览器无预览器，给下载链接。
       **iframe 懒加载**：只在切到该视图时才建、关/换文档时整节点移除——
       28MB 的 PDF 不该常驻内存。
   ========================================================================= */

import { apiFetch, el, toast } from "./common.js";
import { buildPlan, pageMax } from "./reader-view.js";

let root = null; // 浮层的面板根 / 内嵌的容器根
let refs = null; // 内部节点引用
let cache = null; // { docId, data, location }：同文档重复打开不重拉
let currentMode = "text"; // 当前视图：text | file | page（跳页要按它分派，不看 DOM）
let isPdf = false; // 当前文档是否 PDF —— 决定第二个标签是"页面"还是"原文件"
let currentPage = 1; // 页面视图当前页
// 打开请求的序号：连点两篇文档时先发的响应可能后到，落盘前必须核对
// "我还是最新的那次请求吗"——否则用户点的是 B，看到的却是 A 的正文
let loadSeq = 0;
// 翻页方式：single=单页点按（默认）；scroll=连续滚动。切换文档保留选择
// （读了长论文的人多半想一直连续滚）。
let pageMode = "single";
let drag = null; // 放大后的拖动平移状态 {x, y, left, top}
let inited = false;
let inline = false; // 内嵌模式（知识库页）
let onCloseHook = null; // 内嵌模式的"关闭"= 交还给调用方（清空选择）

// 页面图缩放（2026-09-11 用户反馈：页面视图是位图，字号调节对它无效——改用
// 缩放；且倍数要**自己调节**，不是固定档位）。50%~300% 连续可调。
const ZOOM_MIN = 50;
const ZOOM_MAX = 300;
const ZOOM_STEP = 10; // − / ＋ 按钮的步进
let zoomPercent = 100;

function setZoom(percent) {
  const anchor = currentPage; // 缩放前读到哪一页——缩放后要回到这一页
  zoomPercent = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round(Number(percent) || 100)));
  applyZoom();
  if (!refs?.pageCanvas || currentMode !== "page") return;
  // 横向回到中间（放大后视野中心对准页面中线）
  refs.pageCanvas.scrollLeft = Math.max(
    0,
    (refs.pageCanvas.scrollWidth - refs.pageCanvas.clientWidth) / 2
  );
  // 连续模式：每页高度随缩放变化，**同样的 scrollTop 会落到别的页上**，
  // 表现为"缩放一下跳好多页"（2026-09-11 用户实测）。把缩放前那一页
  // 重新滚回视野顶部，阅读位置就稳住了。
  if (pageMode === "scroll") {
    pageNode(anchor)?.scrollIntoView({ block: "start" });
    currentPage = anchor;
    updatePageBar(cache?.data?.file_pages ?? 0);
  }
}

function stepZoom(delta) {
  setZoom(zoomPercent + delta * ZOOM_STEP);
}

/** 缩放基准宽：100% 时页面图的显示宽度（图片原始像素，受容器宽限制）。 */
function pageBaseWidth() {
  const img = refs?.pageCanvas.querySelector(".rd-page-img");
  if (!img) return 0;
  const natural = img.naturalWidth || 0;
  const avail = Math.max(0, refs.pageCanvas.clientWidth - 24); // canvas 左右各 12px 内边距
  return natural > 0 ? Math.min(natural, avail) : avail;
}

function applyZoom() {
  const z = zoomPercent / 100;
  // 以**图片原始显示宽度**为基准（100% = 1:1）。不能让 100% 走 fit-content、
  // 其余走"容器比例"——两套模型在 100% 处会跳变：实测容器 1827px、页面图
  // 910px 时，滑条从 95%(1736px) 拖到 100% 反而缩到 910px（用户会当成 bug）。
  refs?.pageCanvas.style.setProperty("--page-base", `${Math.round(pageBaseWidth())}px`);
  refs?.pageCanvas.style.setProperty("--page-zoom", String(z));
  refs?.pageCanvas.classList.toggle("zoomed", z !== 1);
  // 放大后才可拖动平移（未放大时是 grab 光标会误导）
  refs?.pageCanvas.classList.toggle("pannable", z > 1);
  if (refs?.zoomRange) refs.zoomRange.value = String(zoomPercent);
  if (refs?.zoomLabel) refs.zoomLabel.textContent = `${zoomPercent}%`;
}

/**
 * 幂等初始化。两页入口各调一次，但语义不同：
 *   initReader()                    → 浮层（问答页）
 *   initReader(host, {onClose})     → 内嵌进 host（知识库页右栏）
 */
export function initReader(host = null, { onClose = null } = {}) {
  if (inited) return;
  inited = true;
  inline = Boolean(host);
  onCloseHook = onClose;

  const title = el("div", { class: "rd-title" }, "文档阅读");
  const tabText = el("button", { class: "btn ghost active", type: "button", id: "rd-tab-text" }, "文本");
  // 「页面」= PDF 合并视图（渲染图 + 引用高亮 + 可选中文字）
  const tabPage = el("button", { class: "btn ghost", type: "button", id: "rd-tab-page" }, "页面");
  // 「原文件」= 内嵌浏览器自带的 PDF 阅读器：支持**高亮 / 画笔 / 搜索**
  // （2026-09-11 用户反馈"之前能像双击论文那样用笔标注"，那个视图就是它——
  // 之前合并视图时被移除，现恢复，与「页面」并存、各司其职）
  const tabFile = el("button", { class: "btn ghost", type: "button", id: "rd-tab-file" }, "原文件");
  const pageInput = el("input", { type: "number", min: "1", id: "rd-page", "aria-label": "页码" });
  const pageGo = el("button", { class: "btn ghost", type: "button", id: "rd-go" }, "跳转");
  const jump = el("div", { class: "rd-page-jump hidden" }, "第", pageInput, "页", pageGo);
  const close = el("button", {
    class: "icon-btn",
    type: "button",
    id: "rd-close",
    title: inline ? "收起（Esc）" : "关闭（Esc）",
    "aria-label": inline ? "收起阅读区" : "关闭阅读面板",
  }, "✕");

  // 页面图缩放（页面视图专用；文本视图按默认字号排）
  const zoomOut = el("button", { class: "btn", type: "button", id: "rd-zoom-out", title: "缩小页面" }, "−");
  const zoomIn = el("button", { class: "btn", type: "button", id: "rd-zoom-in", title: "放大页面" }, "＋");
  // 滑条让倍数连续可调（用户要求），百分比同步显示
  const zoomRange = el("input", {
    type: "range",
    min: String(ZOOM_MIN),
    max: String(ZOOM_MAX),
    step: "5",
    id: "rd-zoom-range",
    "aria-label": "缩放比例",
  });
  const zoomLabel = el("span", { class: "rd-zoom-label" }, "100%");
  // 一键回到 100%（用户要求）：点一下就从任意倍数复位
  const zoomReset = el(
    "button",
    { class: "btn", type: "button", id: "rd-zoom-reset", title: "恢复原始大小（100%）" },
    "原始大小"
  );
  const zoomBox = el("div", { class: "rd-zoom" }, zoomOut, zoomRange, zoomLabel, zoomIn, zoomReset);

  const head = el(
    "div",
    { class: "rd-head" },
    title,
    el("div", { class: "rd-tabs" }, tabText, tabPage, tabFile),
    jump,
    zoomBox,
    close
  );
  const text = el("div", { class: "rd-text" });
  const orig = el("div", { class: "rd-orig hidden" });
  const foot = el("div", { class: "rd-foot hidden" }); // 内嵌模式的元数据状态栏
  // 页面视图（PDF 合并视图）：原样渲染的页面图 + 引用高亮覆盖层。
  // 页面节点由 renderPages 按翻页模式动态生成（单页 = 一个节点；连续 = 全部页）
  const pageCanvas = el("div", { class: "rd-page-canvas" });
  const pagePrev = el("button", { class: "btn", type: "button", id: "rd-prev", title: "上一页" }, "上一页");
  const pageNext = el("button", { class: "btn", type: "button", id: "rd-next", title: "下一页" }, "下一页");
  // 翻页方式切换：单页点按 ↔ 连续滚动（按钮文字显示"点了会变成什么"）
  const pageModeBtn = el(
    "button",
    { class: "btn", type: "button", id: "rd-mode", title: "切换翻页方式" },
    "切换为连续滚动"
  );
  const pageLabel = el("span", { class: "rd-page-label" }, "");
  const pageView = el(
    "div",
    { class: "rd-page-view hidden" },
    el(
      "div",
      { class: "rd-page-bar" },
      el("div", { class: "rd-pager" }, pagePrev, pageNext),
      pageLabel,
      pageModeBtn
    ),
    pageCanvas
  );
  const body = el("div", { class: "rd-body" }, text, orig, pageView);

  if (inline) {
    root = el("div", { class: "reader-inline", id: "reader-inline" }, head, body, foot);
    host.append(root);
  } else {
    root = el(
      "div",
      { class: "reader-panel hidden", id: "reader-panel", role: "dialog", "aria-label": "文档阅读" },
      head,
      body
    );
    document.body.append(root);
  }
  refs = {
    title,
    tabText,
    tabPage,
    tabFile,
    pageInput,
    pageGo,
    jump,
    close,
    text,
    orig,
    foot,
    pageView,
    pageCanvas,
    pagePrev,
    pageNext,
    pageModeBtn,
    pageLabel,
    zoomOut,
    zoomIn,
    zoomRange,
    zoomLabel,
    zoomReset,
  };

  close.addEventListener("click", () => closeReader());
  tabText.addEventListener("click", () => setMode("text"));
  tabPage.addEventListener("click", () => setMode("page"));
  tabFile.addEventListener("click", () => setMode("file"));
  pageGo.addEventListener("click", () => jumpToPage());
  pageInput.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") jumpToPage();
  });
  pagePrev.addEventListener("click", () => showPage(currentPage - 1));
  pageNext.addEventListener("click", () => showPage(currentPage + 1));
  pageModeBtn.addEventListener("click", () =>
    setPageMode(pageMode === "single" ? "scroll" : "single")
  );
  // 拖动平移：仅在放大状态启用（未放大时内容不溢出，拖动没有意义）
  pageCanvas.addEventListener("mousedown", (ev) => {
    if (!pageCanvas.classList.contains("pannable") || ev.button !== 0) return;
    drag = {
      x: ev.clientX,
      y: ev.clientY,
      left: pageCanvas.scrollLeft,
      top: pageCanvas.scrollTop,
    };
    pageCanvas.classList.add("dragging");
    ev.preventDefault(); // 拖动期间不触发文字选择（松手即恢复，见 CSS）
  });
  // 连续滚动：滚动时把"第 N 页"标签跟到视口中线所在的那一页
  pageCanvas.addEventListener("scroll", () => {
    if (pageMode !== "scroll" || !pageCanvas.children.length) return;
    const mid = pageCanvas.scrollTop + pageCanvas.clientHeight / 2;
    let seen = currentPage;
    for (const node of pageCanvas.children) {
      if (node.offsetTop <= mid) seen = Number(node.dataset.page);
      else break;
    }
    if (seen !== currentPage) {
      currentPage = seen;
      updatePageBar(cache?.data?.file_pages ?? 0);
    }
  });
  zoomOut.addEventListener("click", () => stepZoom(-1));
  zoomIn.addEventListener("click", () => stepZoom(1));
  zoomRange.addEventListener("input", () => setZoom(zoomRange.value));
  zoomReset.addEventListener("click", () => setZoom(100));
  // 容器尺寸变了要重算基准宽：缩放态的宽是像素值，不会自己跟随窗口
  window.addEventListener("resize", () => {
    if (pageMode && refs && zoomPercent !== 100) applyZoom();
  });
  applyZoom();

  // 放大后按住拖动平移（浏览器原生只给滚动条，拖不动——2026-09-11 用户反馈）
  window.addEventListener("mousemove", (ev) => {
    if (!drag) return;
    refs.pageCanvas.scrollLeft = drag.left - (ev.clientX - drag.x);
    refs.pageCanvas.scrollTop = drag.top - (ev.clientY - drag.y);
  });
  window.addEventListener("mouseup", () => {
    if (!drag) return;
    drag = null;
    refs.pageCanvas.classList.remove("dragging");
  });

  // 浮层专属：点外部 / Esc 关闭。内嵌模式不挂——它是页面的一部分，
  // 点别处不该把它收掉（收起交给 ✕ 与文档行的选择状态）。
  if (!inline) {
    // 点外部关闭。白名单必须含**开启源**（引用角标/引用卡/文档行）：它们的
    // click 处理先于本处理器执行，不放行就会"刚打开就被这里关掉"。
    document.addEventListener("click", (ev) => {
      if (root.classList.contains("hidden")) return;
      if (ev.target.closest("#reader-panel, .cite, .cite-card, .doc-item, #toast")) return;
      closeReader();
    });
    // Esc 关闭。设置面板的 Esc 监听注册更早且无条件关闭，这里拦不住，
    // 故接受"Esc 收掉所有浮层"——面板 z-index 更高，一起收掉符合直觉。
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && !root.classList.contains("hidden")) closeReader();
    });
  }
}

/** 浮层是否开着（内嵌模式恒 false——它没有"开/关"语义）。 */
export function isReaderOpen() {
  return Boolean(root) && !inline && !root.classList.contains("hidden");
}

/**
 * 打开某篇文档；chunkId 非空时定位到该块（引用跳转）。
 *
 * 拉取失败 / pending 文档都在正文区给文案——用户视线在阅读区里，不用 toast。
 * `location` 是调用方知道的归档位置（内嵌模式的状态栏要显示），浮层不用。
 */
export async function openDocument(docId, { chunkId = null, mode = null, location = "" } = {}) {
  initReader();
  const id = Number(docId);
  const seq = ++loadSeq; // 本次请求的序号（见 loadSeq 声明处的说明）
  if (!inline) root.classList.remove("hidden");

  if (!cache || cache.docId !== id) {
    refs.title.textContent = "加载中…";
    // 加载中/失败/无正文的文案都写在文本视图里——页面/原文件标签下
    // refs.text 是 display:none，不先切过来用户只会看到上一篇的残留内容
    // （2026-09-11 修复：读取失败要能看见，不然像"点开没反应"）
    forceTextMode();
    refs.text.replaceChildren(el("div", { class: "empty" }, "正在读取文档…"));
    refs.foot.classList.add("hidden");
    try {
      const data = await apiFetch(`/api/documents/${id}/content`);
      if (seq !== loadSeq) return; // 过期响应：用户已点开别的文档，丢弃
      cache = { docId: id, data, location };
    } catch (err) {
      if (seq !== loadSeq) return; // 同上：错误也不该覆盖新文档的界面
      refs.title.textContent = "读取失败";
      forceTextMode(); // 同上：错误必须落在可见视图
      refs.text.replaceChildren(el("p", { class: "rd-error" }, `✗ ${readError(err)}`));
      return;
    }
  } else {
    cache.location = location || cache.location;
  }

  const data = cache.data;
  isPdf = data.document.file_type === "pdf";
  // 标签随格式变：md/txt/docx = 文本 + 原文件；PDF = 页面（高亮/选字）+ 原文件（可标注）
  refs.tabText.classList.toggle("hidden", isPdf);
  refs.tabPage.classList.toggle("hidden", !isPdf);
  refs.title.textContent = data.document.title;
  refs.orig.replaceChildren(); // 换文档后原文件视图需重建
  refs.orig.dataset.built = "";
  delete refs.orig.dataset.framePage; // 页码也要清：否则 iframe 打开在上一篇的第 N 页
  // 跳页上界优先用**原件**页数（file_pages）：page_max 只是解析出正文的最大
  // 页码，参考文献区被剔除后会明显小于原文件（doc 71 实测 189 vs 301）。
  const pageCap = data.file_pages ?? data.page_max;
  refs.jump.classList.toggle("hidden", pageCap === null);
  if (pageCap !== null) {
    refs.pageInput.max = String(pageCap);
    refs.pageInput.placeholder = `1-${pageCap}`;
  }
  renderFoot(data);

  if (!data.chunks.length) {
    forceTextMode(); // 同上：无正文提示也得在可见视图里
    refs.text.replaceChildren(
      el("div", { class: "empty" }, "该文档尚未完成入库，暂时没有正文可读。")
    );
    if (chunkId !== null) refs.text.append(el("p", { class: "rd-note" }, "（引用片段同样不可用）"));
    return;
  }

  renderText(data);
  currentPage = 1;
  // PDF 默认开"页面"视图：那里是原样渲染 + 可高亮（用户要的"合并"）
  setMode(mode ?? (isPdf ? "page" : "text"));
  if (chunkId !== null) {
    // 引用跳转：PDF 走页面高亮；定位不到（或非 PDF）退回文本视图高亮
    if (isPdf && (await locateAndShow(chunkId))) return;
    highlightChunk(chunkId);
  }
}

/** 引用跳转入口：chunk_id → 所属文档 + 定位（Citation 不带 document_id）。 */
export async function openChunk(chunkId, { mode = "text" } = {}) {
  const seq = loadSeq; // 同 locateAndShow：先点 A 后点 B 时，A 的响应后到会把人拽回 A
  try {
    const info = await apiFetch(`/api/chunks/${Number(chunkId)}`);
    if (seq !== loadSeq) return; // 已过期：用户点了别的
    await openDocument(info.document_id, { chunkId: Number(chunkId), mode });
  } catch (err) {
    initReader();
    if (!inline) root.classList.remove("hidden");
    refs.title.textContent = "无法定位";
    forceTextMode(); // 同上：定位失败的原因要看得见
    refs.text.replaceChildren(el("p", { class: "rd-error" }, `✗ ${readError(err)}`));
  }
}

/**
 * 更新状态栏里的归档位置（内嵌模式）。
 *
 * 为什么要单独一个口：状态栏只在 openDocument 时构建，而"拖文档进文件夹"
 * 不会重新打开文档——旧版详情卡靠 onSelect 重渲染自动跟进，改内嵌后这个
 * 行为会丢。调用方在 onSelect 里把新位置推过来即可。
 */
export function updateLocation(location) {
  if (!inline || !cache || !location) return;
  cache.location = location;
  renderFoot(cache.data);
}

/** 关闭：浮层是隐藏自己；内嵌是清空内容并交还调用方（切回上传/空态）。 */
export function closeReader() {
  if (!root) return;
  refs.orig.replaceChildren(); // 移除 iframe：28MB PDF 的阅读器不必常驻
  refs.orig.dataset.built = "";
  delete refs.orig.dataset.framePage;
  // 页面视图同理：连续模式会建几百个页面节点 + 文字层的 ResizeObserver，
  // 关面板要一起放掉（否则已解码的位图与观察者会挂到下次 showPage 为止）
  refs.pageCanvas?.querySelectorAll(".rd-textlayer").forEach((l) => l._ro?.disconnect());
  refs.pageCanvas?.replaceChildren();
  refs.foot.classList.add("hidden");
  cache = null; // 语料可能已重新入库，下次重新拉
  if (inline) onCloseHook?.();
  else root.classList.add("hidden");
}

/* ------------------------------ 内部 ------------------------------ */

/**
 * 把后端错误翻成用户看得懂的话。
 *
 * 为什么需要（2026-09-10 实踩）：静态 JS 是每次请求从磁盘读的，**进程里的
 * Python 路由是启动时加载的**——服务没重启时会出现"新前端 + 旧后端"，旧
 * 后端没有阅读接口，FastAPI 回它自己的默认 `{"detail":"Not Found"}`，前端
 * 原样显示成"Not Found"，用户完全不知道自己该做什么。
 */
function readError(err) {
  const raw = err?.message || String(err);
  if (raw === "Not Found") {
    return "服务端没有阅读接口——当前运行的 Mikasa 是旧构建，请重启服务后再试";
  }
  return raw;
}

/** 内嵌模式的状态栏：位置 · 类型 · 分块 · 字符 · 状态（浮层不显示）。 */
function renderFoot(data) {
  if (!inline) return;
  const doc = data.document;
  const bits = [
    cache.location,
    doc.file_type,
    `${doc.chunk_count} 块`,
    `${doc.char_count} 字符`,
    doc.ingest_status === "done" ? "已入库" : doc.ingest_status,
  ].filter(Boolean);
  refs.foot.replaceChildren(...bits.map((b, i) => (i ? ` · ${b}` : String(b))));
  refs.foot.classList.remove("hidden");
}

function renderText(data) {
  const nodes = [];
  for (const item of buildPlan(data.text, data.chunks)) {
    if (item.kind === "head") {
      // heading_path 的层级深度（"A > B > C" → 3）决定小标题字号档位
      const level = String(item.title || "").split(">").length;
      nodes.push(
        el(
          "div",
          { class: "rd-head-mark", "data-level": String(level), title: item.title },
          item.label
        )
      );
    } else if (item.kind === "page") {
      nodes.push(el("div", { class: "rd-page-mark" }, `— 第 ${item.n} 页 —`));
    } else {
      const span = el("span", {
        class: "rd-chunk",
        id: `chunk-${item.chunkId}`,
        "data-chunk-id": String(item.chunkId),
      });
      span.append(item.text);
      nodes.push(span);
    }
  }
  refs.text.replaceChildren(...nodes); // 一次性挂载：866 块也只是一次重排
}

/**
 * 强制切到文本视图，并让「文本」标签可见。
 *
 * 单独开一个口的原因：PDF 的页面图已叠了文字层（能选字、能复制），平时
 * **隐藏**文本标签；但"读取失败 / 没有正文 / 定位失败"这些情况只有文本
 * 视图能说明白，必须把标签露出来，否则用户不知道自己被切到了哪一屏
 * （2026-09-11，用户要求页面图可选中后就删掉文本标签）。
 */
function forceTextMode() {
  refs.tabText.classList.remove("hidden");
  setMode("text");
}

function setMode(mode) {
  currentMode = mode === "page" || mode === "file" ? mode : "text";
  const isText = currentMode === "text";
  refs.tabText.classList.toggle("active", isText);
  refs.tabPage.classList.toggle("active", currentMode === "page");
  refs.tabFile.classList.toggle("active", currentMode === "file");
  refs.text.classList.toggle("hidden", !isText);
  refs.orig.classList.toggle("hidden", currentMode !== "file");
  refs.pageView.classList.toggle("hidden", currentMode !== "page");
  if (currentMode === "file") void renderFile();
  else if (currentMode === "page") void showPage(currentPage);
  else if (cache) {
    // 从别的视图切回文本：把当前高亮块重新滚进视野
    refs.text.querySelector(".rd-chunk.lit")?.scrollIntoView({ block: "center" });
  }
}

/** 造一页的节点：图 + 高亮覆盖层（两层必须同容器，见 CSS 的 .rd-page-fit 注释）。 */
function buildPageNode(pageNo) {
  const img = el("img", {
    class: "rd-page-img",
    alt: `第 ${pageNo} 页`,
    loading: "lazy", // 连续模式下几百页：交给浏览器按需加载，不预取全部
  });
  img.src = `/api/documents/${cache.docId}/page/${pageNo}.png`;
  return el(
    "div",
    { class: "rd-page-fit", "data-page": String(pageNo) },
    img,
    el("div", { class: "rd-page-layer" })
  );
}

/** 取某页的节点（连续模式下画高亮/定位用）；不在当前渲染集合里返回 null。 */
function pageNode(n) {
  return refs.pageCanvas.querySelector(`.rd-page-fit[data-page="${n}"]`);
}

function updatePageBar(total) {
  refs.pageLabel.textContent = total ? `第 ${currentPage} / ${total} 页` : `第 ${currentPage} 页`;
  refs.pagePrev.disabled = currentPage <= 1;
  // 总页数未知（原件缺失等）时页面视图只有第 1 页：下一页没有可去之处，禁掉
  // 比"能点但没反应"诚实（2026-09-11 打包前审查发现）
  refs.pageNext.disabled = !total || currentPage >= total;
}

/** 归一化矩形 → 高亮块（百分比定位，与渲染 DPI 无关）。 */
function makeHighlights(rects) {
  return rects.map(([x0, y0, x1, y1]) =>
    el("div", {
      class: "rd-hl",
      style:
        `left:${x0 * 100}%;top:${y0 * 100}%;` +
        `width:${(x1 - x0) * 100}%;height:${(y1 - y0) * 100}%`,
    })
  );
}

/**
 * 页面视图：按当前翻页方式渲染 PDF 页，并可叠加引用高亮。
 *
 * 高亮矩形来自后端 `/locate/{chunk_id}`（归一化 0~1，按行分组）——用百分比
 * 定位，故与渲染 DPI 无关，改 DPI 不用重算坐标。
 * 连续模式一次建全部页节点，但图片是 `loading="lazy"`：滚到哪儿才加载哪儿，
 * 不会把 301 页全拉下来。
 */
async function showPage(n, rects = null) {
  if (!cache || !isPdf) return;
  const total = cache.data.file_pages ?? 0;
  const page = Math.min(Math.max(1, Number(n) || 1), total || 1);
  currentPage = page;
  updatePageBar(total);

  // 换页/换模式前先断开旧文字层的尺寸观察者（否则观察者会一直持有已移除的节点）
  refs.pageCanvas.querySelectorAll(".rd-textlayer").forEach((l) => l._ro?.disconnect());

  const wanted =
    pageMode === "scroll" && total ? Array.from({ length: total }, (_, i) => i + 1) : [page];
  refs.pageCanvas.replaceChildren(...wanted.map(buildPageNode));
  const fit = pageNode(page);
  if (rects && rects.length) {
    fit?.querySelector(".rd-page-layer")?.append(...makeHighlights(rects));
  }
  // 文字层只在单页模式铺：连续模式几百页会有几万个 span，DOM 撑不住
  // （要选字复制就切单页——按钮就在翻页栏里）
  if (pageMode === "single" && fit) void renderTextLayer(page, fit);
  // 有高亮时居中（把命中的行摆进视野中央），否则页顶对齐
  fit?.scrollIntoView({ block: rects?.length ? "center" : "start" });
}

/**
 * 文字层：把 PDF 的真实文字按原位透明铺在页面图上，使"页面视图可选中复制"。
 *
 * 字号换算是关键：后端给的 `size` 按页高归一化，这里把层的 font-size 设成
 * "图片高度的 1%"，每个 span 用 `size*100` em —— 图片随窗口缩放时字号自动
 * 跟随，不必重算（ResizeObserver 只在图片尺寸真的变了时更新基准）。
 */
async function renderTextLayer(pageNo, fit) {
  let data;
  try {
    data = await apiFetch(`/api/documents/${cache.docId}/page/${pageNo}/text`);
  } catch {
    return; // 文字层是增强项：取不到就保持纯图，不影响看图与高亮
  }
  if (cache?.docId == null || !fit.isConnected) return; // 期间换了文档/换了页
  const layer = el("div", { class: "rd-textlayer" });
  for (const s of data.spans) {
    layer.append(
      el(
        "span",
        {
          style:
            `left:${s.x * 100}%;top:${s.y * 100}%;` +
            `width:${s.w * 100}%;height:${s.h * 100}%;font-size:${s.size * 100}em;`,
        },
        s.t
      )
    );
  }
  fit.append(layer);
  const sync = () => {
    const h = fit.clientHeight;
    if (h) layer.style.fontSize = `${h / 100}px`; // span 的 em = 图片高度的 1%
  };
  sync();
  if (typeof ResizeObserver !== "undefined") {
    const ro = new ResizeObserver(sync);
    ro.observe(fit);
    layer._ro = ro; // 换页时由 showPage 断开
  }
}

/** 切换翻页方式（保持当前页）：单页点按 ↔ 连续滚动。 */
function setPageMode(mode) {
  pageMode = mode === "scroll" ? "scroll" : "single";
  refs.pageModeBtn.textContent = pageMode === "single" ? "切换为连续滚动" : "切换为单页翻页";
  if (currentMode === "page") void showPage(currentPage);
}

/** 引用跳转的 PDF 分支：定位该块 → 切到页面视图并高亮。定位不到返回 false。 */
async function locateAndShow(chunkId) {
  const seq = loadSeq; // 发起时的文档序号：期间用户换了文档就别渲染（会张冠李戴）
  let loc;
  try {
    loc = await apiFetch(`/api/documents/${cache.docId}/locate/${Number(chunkId)}`);
  } catch {
    return false; // 定位是增强项：失败降级到文本视图高亮
  }
  if (seq !== loadSeq) return true; // 过期响应：丢弃（否则 A 的页码/高亮会画到 B 身上）
  if (!loc.page) return false;
  setMode("page");
  await showPage(loc.page, loc.rects);
  return true;
}

/** 原文件视图（懒加载：只在切过来时构建一次，换文档由 openDocument 重置）。 */
async function renderFile() {
  if (!cache || refs.orig.dataset.built === "1") return;
  refs.orig.dataset.built = "1";
  const doc = cache.data.document;
  const url = `/api/documents/${cache.docId}/file`;
  const docIdAtStart = cache.docId; // 响应回来时核对文档是否已被换掉

  // PDF：内嵌浏览器自带阅读器（高亮/画笔/搜索），起始页取 framePage（跳页时重建用）
  if (doc.file_type === "pdf") {
    const start = refs.orig.dataset.framePage || String(currentPage);
    refs.orig.replaceChildren(
      el("iframe", { class: "rd-frame", src: `${url}#page=${start}`, title: "PDF 原文件" })
    );
    return;
  }
  if (doc.file_type === "docx") {
    refs.orig.replaceChildren(
      el("p", { class: "rd-note" }, "浏览器无法直接预览 Word 文档，可下载后查看："),
      el("a", { class: "btn ghost", href: url, download: "" }, "下载原文件")
    );
    return;
  }

  refs.orig.replaceChildren(el("div", { class: "empty" }, "正在读取原文件…"));
  try {
    const resp = await fetch(url);
    if (!resp.ok) {
      // 正文来自 DB，原文件缺失不影响文本视图——只在这里提示
      const body = await resp.json().catch(() => null);
      throw new Error(body?.detail || `原文件不可用（HTTP ${resp.status}）`);
    }
    const raw = await resp.text();
    if (cache?.docId !== docIdAtStart) return; // 过期响应：期间用户换了文档
    refs.orig.replaceChildren(el("pre", { class: "rd-raw" }, raw));
  } catch (err) {
    if (cache?.docId !== docIdAtStart) return;
    refs.orig.replaceChildren(el("p", { class: "rd-error" }, `✗ ${readError(err)}`));
  }
}

/**
 * 跳页。**两个视图各有各的跳法**（2026-09-11 用户实测"原文件能跳、文本不能"）：
 *   - 页面：换渲染的那一页（原样排版 + 保留高亮）
 *   - 文本：滚到该页第一个块并高亮（PDF 的块带 page_number）
 * 初版只做了 iframe 那条，且用 `if (!frame) return` 判定——文本视图下 iframe
 * 不存在，于是点了毫无反应。**判定必须看当前视图而不是看 DOM**。
 */
function jumpToPage() {
  if (!cache || !isPdf) return;
  const data = cache.data;
  const max = data.file_pages ?? pageMax(data.chunks) ?? data.page_max;
  const n = Math.min(Math.max(1, Number(refs.pageInput.value) || 1), max || 1);
  refs.pageInput.value = String(n);

  if (currentMode === "page") {
    void showPage(n);
    return;
  }
  if (currentMode === "file") {
    // 跳页必须**重建 iframe**：只改 src 的 #page 片段不触发文档重新加载，
    // Chrome 内置阅读器收不到导航事件（2026-09-11 实测的真实 bug）
    refs.orig.dataset.framePage = String(n);
    refs.orig.dataset.built = "";
    void renderFile();
    return;
  }
  const hit = data.chunks.find((c) => c.page_number != null && c.page_number >= n);
  if (!hit) {
    toast(`第 ${n} 页没有解析出的正文（如参考文献区被剔除）`);
    return;
  }
  highlightChunk(hit.chunk_id);
}

function highlightChunk(chunkId) {
  const node = refs.text.querySelector(`[data-chunk-id="${Number(chunkId)}"]`);
  if (!node) return;
  for (const lit of refs.text.querySelectorAll(".rd-chunk.lit")) lit.classList.remove("lit");
  node.classList.add("lit");
  node.scrollIntoView({ behavior: "smooth", block: "center" });
}
