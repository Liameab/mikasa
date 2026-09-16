/* =========================================================================
   知识库页逻辑（documents.html 的入口模块）。

   页面 = 左侧语料文件夹树（管理交互全在 js/kb-tree.js）+ 右侧两块卡：
   上传区 + 选中文档详情卡。本文件职责：
     1) 上传（multipart POST /api/documents）—— 201 新入库 / 200 同内容
        跳过双语义，消息直接展示服务端文案；
     2) 选中详情卡渲染（kb-tree 经 initCorpusTree 回调推数据）；
     3) 顶栏初始化；
     4) 阅读面板接线（点文档行 → 打开右侧阅读面板，见 js/reader.js）。
   删除/改名/移夹/文件夹管理由 kb-tree.js 的 ⋯ 菜单与拖拽完成（含二次
   确认与错误 toast），上传/删除后树自动刷新（refreshCorpusTree）。
   ========================================================================= */

import { $, el, initTopbar, toast } from "./common.js";
import { clearDocSelection, initCorpusTree, refreshCorpusTree } from "./kb-tree.js";
import { initPapersPanel } from "./papers.js"; // 在线找论文（M7）
import { initReader, openDocument, updateLocation } from "./reader.js"; // 阅读器（两页共用）
import { initOnboard } from "./onboard.js"; // 首启引导（空库时弹一次）

const dropzone = $("#dropzone");
const fileInput = $("#file-input");
const detailBox = $("#doc-detail");
const docCountPill = $("#doc-count");
const uploadNote = $("#upload-note");

/* ---------------- 上传 ---------------- */

/** 上传一个 File：multipart；错误文案（415/413/400）从 detail 取。 */
async function upload(file) {
  const form = new FormData();
  form.append("file", file);
  uploadNote.textContent = `正在上传并入库：${file.name}…`;
  try {
    const resp = await fetch("/api/documents", { method: "POST", body: form });
    const body = await resp.json().catch(() => null);
    if (!resp.ok) {
      const message = body?.detail || body?.error?.message || `HTTP ${resp.status}`;
      toast(`上传失败：${message}`, "error");
      return;
    }
    // 201 入库 / 200 同内容跳过：文案都是服务端中文消息，直接展示
    toast(body.message, resp.status === 201 ? "ok" : "warn");
  } catch {
    toast("上传失败：网络中断", "error");
  } finally {
    uploadNote.textContent = "";
    refreshCorpusTree(); // 新文档出现在树根级（选中态由 kb-tree 自行维护）
  }
}

dropzone.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => {
  for (const file of fileInput.files) upload(file);
  fileInput.value = ""; // 允许重复选择同一文件
});
// 拖拽：阻止浏览器默认打开文件行为，落区高亮提示
["dragenter", "dragover"].forEach((evt) =>
  dropzone.addEventListener(evt, (ev) => {
    ev.preventDefault();
    dropzone.classList.add("drag");
  })
);
["dragleave", "drop"].forEach((evt) =>
  dropzone.addEventListener(evt, (ev) => {
    ev.preventDefault();
    dropzone.classList.remove("drag");
  })
);
dropzone.addEventListener("drop", (ev) => {
  for (const file of ev.dataTransfer.files) upload(file);
});

/* ---------------- 选中 → 右栏直接读正文（2026-09-10） ---------------- */

const introCard = $("#intro-card");
const papersCard = $("#papers-card");
const listCard = $("#list-card");
const readingCard = $("#reading-card");
const readerHost = $("#reader-host");

/** 空态引导（未选中文档时显示在语料总览卡里）。 */
function renderEmptyDetail() {
  detailBox.innerHTML = "";
  detailBox.append(
    el("div", { class: "empty" }, "未选中文档——点左侧文档行即可在右栏读原文"),
    el(
      "div",
      { class: "muted small", style: "text-align:center" },
      "拖文档行到左侧文件夹即可归档；行尾 ⋯ 菜单可重命名 / 移动 / 删除"
    )
  );
}

/** 右栏在"上传 + 在线找论文 + 语料总览"与"阅读区"之间二选一。 */
function showReading(on) {
  readingCard.classList.toggle("hidden", !on);
  introCard.classList.toggle("hidden", on);
  papersCard.classList.toggle("hidden", on);
  listCard.classList.toggle("hidden", on);
}

/** kb-tree onSelect：取消选中时收起阅读区；有选中时只把位置推给状态栏。
 *  notifyActive 刷新重绘时只会在**有选中**时回调，故不会被误触收起；
 *  拖拽移夹/改名后状态栏的"位置"靠这里的 updateLocation 跟进。 */
function onSelect(doc, location) {
  if (!doc) {
    renderEmptyDetail();
    showReading(false);
    return;
  }
  updateLocation(location);
}

/* ---------------- 启动 ---------------- */

initTopbar("documents").then((health) => void initOnboard(health));
initPapersPanel(); // 在线找论文：检索/导入/OpenAlex 密钥
// 内嵌模式：阅读器挂进右栏，收起时交还上传/总览视图
initReader(readerHost, {
  onClose: () => {
    showReading(false);
    clearDocSelection();
  },
});
initCorpusTree({
  onSelect,
  // 点行 → 右栏读原文。**与 onSelect 分开**：onSelect 被 notifyActive 复用、
  // 每次刷新重绘都会跑，并进去会"拖文档进文件夹就自动切到阅读视图"。
  onOpen: (doc, location) => {
    showReading(true);
    void openDocument(doc.id, { location });
  },
  onCount: (n) => {
    docCountPill.textContent = `${n} 篇`;
  },
});
refreshCorpusTree(); // 树拉数据后经 onCount 回填计数
renderEmptyDetail(); // 详情卡初始空态
