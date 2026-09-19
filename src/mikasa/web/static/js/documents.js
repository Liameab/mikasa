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
import {
  clearDocSelection,
  initCorpusTree,
  refreshCorpusTree,
  selectDocById,
} from "./kb-tree.js";
import { openNoteEditor } from "./note-editor.js"; // 笔记编辑器（M6 ①）
import { initReader, openDocument, updateLocation } from "./reader.js"; // 阅读器（两页共用）
import { initOnboard } from "./onboard.js"; // 首启引导（空库时弹一次）
import { initUpdateBadge } from "./update.js"; // 更新进度胶囊（观察到别处起的下载）

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

/** 右栏在"上传 + 语料总览"与"阅读区"之间二选一。 */
function showReading(on) {
  readingCard.classList.toggle("hidden", !on);
  introCard.classList.toggle("hidden", on);
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

/* ---------------- 笔记（M6 ①） ---------------- */

/**
 * 保存成功后的接续动作（新建与编辑共用）。
 *
 * 必须用**响应里的新 id** 重新选中：编辑走的是同名替换，后端删旧行插新行，
 * 旧 id 在 refreshCorpusTree 里会被判成"选中行已被删除"→ 清空选中（顺带
 * 收起阅读区）→ 用户正在读的那篇突然从右栏消失，像是保存把它弄丢了。
 * 所以：先记住阅读区原状态，刷新后用新 id 重选，并把阅读区**恢复原样**
 * （原本开着就重新打开，原本关着就别自作主张弹出来）。
 */
async function onNoteSaved(doc) {
  const wasReading = !readingCard.classList.contains("hidden");
  await refreshCorpusTree();
  if (doc?.id == null) return;
  if (!selectDocById(doc.id, { open: wasReading })) {
    // 刷新后仍定位不到新行：说明列表没跟上（刷新请求本身失败之类），
    // 此时右栏已经因为"选中行不存在"被收起，必须给一句话解释
    toast("已保存，但列表未刷新到最新，请手动刷新页面", "warn");
  }
}

/* ---------------- 启动 ---------------- */

initTopbar("documents").then((health) => void initOnboard(health));
initUpdateBadge(); // 应用更新进度（本页不打 check，只看本地状态）
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
  // ⋯ 菜单「编辑笔记」（只有笔记行有这个菜单项）
  onEditNote: (doc) => void openNoteEditor({ docId: doc.id, onSaved: onNoteSaved }),
});
$("#new-note").addEventListener("click", () => void openNoteEditor({ onSaved: onNoteSaved }));
refreshCorpusTree(); // 树拉数据后经 onCount 回填计数
renderEmptyDetail(); // 详情卡初始空态
