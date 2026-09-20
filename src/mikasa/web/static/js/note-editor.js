/* =========================================================================
   笔记编辑器（M6 ①，2026-09-16）：知识库页直接写 Markdown → 保存即入库。

   形态：居中模态浮层（z-index 70，见 DESIGN.md 的层级阶梯），左写右预览。
   预览走 renderAnswer（与回答区同一套渲染器）：所见和看 AI 回答一致。

   三条必须守住的交互约束：
     1) 有未保存修改时，Esc / 取消 / 点遮罩 都要走 confirmDialog 二次确认
        ——笔记是用户亲手敲进去的，静默丢弃不可接受；
     2) 键盘在**捕获阶段**拦截并 stopPropagation（同 confirm.js）：模态的 Esc
        只该关它自己（本页会被误伤的其实只有 kb-tree 的"Esc 关 ⋯ 菜单"，
        但保持捕获态是模态的通用纪律）；且确认框在途时直接 return——同节点
        上的其他监听拦不住 stopPropagation，不设这个标志会变成"一次 Esc 把
        确认框和编辑器一起结算"；
     3) 保存失败**不关窗、不清空**，文案里给恢复提示——用户补一下就能重试；
        但**载入**失败必须关窗：一个从未载入成功的空白编辑器一旦被保存，
        就会整篇覆盖掉原笔记（uploads 副本是笔记唯一副本，覆盖不可恢复）。

   后端契约（web/routers/documents.py 的 notes 段）：
     - 新建 POST /api/notes {title, body, folder_id?}；
     - 编辑 PUT /api/notes/{id}：**同名替换**，documents.id 会变（后端是
       "删旧行插新行"），所以保存后必须用响应里的新 id 重新选中；
     - 回填走 GET /api/notes/{id} 读 uploads 原文（不是 /content——那里是拼
       chunk 的阅读正文，会丢 `#` 标题行与代码围栏，拿它回填等于静默重写笔记）。

   图片（M6 ②，ADR-0027）：按钮 / 拖拽 / 粘贴三条入口都汇到 addImage()——
   压缩 → 立即进缩略图条 → 调 /api/notes/ocr 识别 → 文本插到光标处。
   **识别结果只是草稿**：OCR 一定有错字（手写尤其），保存与否由用户改完再定。
   原图在保存成功后逐张 POST 到 /api/notes/{新 id}/media：正文已经落库，
   图片失败只在提示里如实说不回滚——绝不让"图片没存上"变成"笔记也没了"。
   ========================================================================= */

import { el, errorMessage, renderAnswer, toast } from "./common.js";
import { confirmDialog } from "./confirm.js";
import { compressImage } from "./image-util.js";
import { folderOptions } from "./tree.js";

const TITLE_MAX = 120; // 与后端 NoteIn.title 的 max_length 对齐
const BODY_MAX = 200000; // 与后端 schemas.NOTE_BODY_MAX 对齐（前端先拦，不惊动 422）
const PREVIEW_DEBOUNCE_MS = 120;

let current = null; // 单例：同一时刻至多开一个编辑器

/**
 * 发起一次笔记 API 调用，返回 {status, ok, body, text}。
 *
 * 刻意不走 apiFetch：那里把非 2xx 一律翻成 Error，而这里需要**按状态码
 * 分支**（400 正文为空要原样提示、404 笔记已被删要换一套文案）。
 */
async function callApi(method, path, payload) {
  const resp = await fetch(path, {
    method,
    headers: payload ? { "Content-Type": "application/json" } : undefined,
    body: payload ? JSON.stringify(payload) : undefined,
  });
  return await readResponse(resp);
}

/** resp → {status, ok, body, text}（FormData / 图片上传那条路直接用）。 */
async function readResponse(resp) {
  const text = await resp.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null; // 非 JSON（网关中断之类）→ 调用方按 text 兜底
  }
  return { status: resp.status, ok: resp.ok, body, text };
}

/**
 * 错误响应 → 可显示文案。额外认一种"不是 bug 的 bug"：
 * 前端静态文件是每次请求现读的，Python 路由却随进程加载——`git pull` 之后
 * 没重启服务时，页面已经是新的、路由还是旧的，请求命中 FastAPI 默认 404
 * （detail 就是英文 "Not Found"）。这个坑 reader.js 已经踩过并留了翻译文案
 * （"当前运行的 Mikasa 是旧构建，请重启服务后再试"），笔记链路沿用同一句。
 */
/**
 * 编辑保存时 id 失效 → 按标记找回"当前那一行"的 id，返回 null 表示真的不在了。
 *
 * 为什么会失效：笔记的编辑是**同名替换**，后端删旧行插新行，所以每次保存
 * documents.id 都会变。于是"响应丢了再重试""两个标签页各开一份"这类场景下，
 * 手里这个旧 id 的 PUT 会拿到 404——正文哪儿也去不了，用户按提示新建还会多出
 * 一篇重复（审查实测）。`source_ref` 是跨编辑稳定的身份（同名替换会继承它），
 * 因此拿它反查当前行是可靠的；查不到才说明笔记真的被删了。
 */
async function resolveNoteId(ref) {
  if (!ref) return null;
  try {
    const list = await callApi("GET", "/api/documents");
    const row = (list.body?.documents || []).find((d) => d.source_ref === ref);
    return row?.id ?? null;
  } catch {
    return null;
  }
}

function apiMessage(res) {
  const msg = errorMessage(res.body, res.text, res.status);
  if (res.status === 404 && msg.trim() === "Not Found") {
    return "服务端没有笔记接口——当前运行的 Mikasa 是旧构建，请重启服务后再试";
  }
  return msg;
}

/** 打开笔记编辑器：docId 缺省 = 新建，给了 = 先回填再编辑。 */
export async function openNoteEditor({ docId = null, onSaved = null } = {}) {
  if (current) return; // 已开着：忽略（模态是单例，不叠加）

  const editing = docId !== null;
  const titleInput = el("input", {
    class: "note-title",
    type: "text",
    maxlength: String(TITLE_MAX),
    placeholder: "笔记标题",
    "aria-label": "笔记标题",
  });
  const editor = el("textarea", {
    class: "note-input",
    spellcheck: "false",
    maxlength: String(BODY_MAX),
    placeholder: "用 Markdown 写正文……（预览在右侧实时更新）",
    "aria-label": "笔记正文",
  });
  // bubble 类是有意复用的：回答气泡里的 Markdown 样式（.md-table / .code-block /
  // 标题 / 列表）全部挂在 .bubble 作用域下，带上它预览才和看回答长得一样。
  // aria-hidden：预览只是正文的视觉呈现，读屏读一遍正文框就够了
  const preview = el("div", { class: "note-preview bubble", "aria-hidden": "true" });
  const hint = el("div", { class: "note-hint muted small" }, "");
  const folderSelect = el(
    "select",
    { class: "note-folder", "aria-label": "保存到文件夹" },
    el("option", { value: "" }, "根级") // 空值 = 不落夹（后端 folder_id=null）
  );
  const folderWrap = el("label", { class: "note-folder-wrap" }, "保存到 ", folderSelect);
  const cancelBtn = el("button", { class: "btn ghost", type: "button" }, "取消");
  const saveBtn = el("button", { class: "btn primary", type: "button" }, "保存");
  const ocrBtn = el("button", { class: "btn ghost", type: "button" }, "识别图片");
  // class 里的 note-image-input 是 E2E 的稳定抓手（CDP 喂文件按选择器找）
  const fileInput = el("input", {
    type: "file",
    accept: "image/*",
    class: "hidden note-image-input",
  });
  const mediaBar = el("div", { class: "note-media hidden" }); // 原图缩略图条

  const box = el(
    "div",
    { class: "note-box", role: "dialog", "aria-modal": "true", "aria-label": "笔记编辑器" },
    el("div", { class: "note-head" }, titleInput),
    el("div", { class: "note-split" }, editor, preview),
    mediaBar,
    el(
      "div",
      { class: "note-foot" },
      editing ? el("span", { class: "note-folder-hint muted small" }, "归属在树里拖动调整") : folderWrap,
      ocrBtn,
      fileInput,
      hint,
      el("span", { class: "grow" }),
      cancelBtn,
      saveBtn
    )
  );
  const backdrop = el("div", { class: "note-backdrop" }, box);

  // 快照 = "上次保存过的内容"，脏判定与取消确认都以它为准
  let snapshot = { title: "", body: "" };
  // 保存目标：**可变的**——同名替换每次都会换 documents.id，旧 id 的 PUT 会
  // 404；source_ref 是跨编辑稳定的身份，用来把新 id 找回来（见 resolveNoteId）
  let targetId = docId;
  let targetRef = null;
  // 打开时那一版的乐观锁令牌（GET /api/notes 下发）：保存时回传，让服务端判断
  // "我打开之后有没有别人改过"。**保存成功后编辑器即关闭**，所以拿到新 rev 的
  // 时机是下次打开，不需要保存响应回传。
  let loadedRev = null;
  let pendingConfirm = false;
  let saving = false; // 保存请求在途：此时"放弃修改"是假承诺（PUT 已经发出去了）
  let previewTimer = null;
  const opener = document.activeElement; // 关闭后把焦点还回去（键盘用户不迷路）

  const isDirty = () =>
    titleInput.value.trim() !== snapshot.title || editor.value !== snapshot.body;

  const renderPreview = () => {
    // renderAnswer 的输出是"先 esc 再受控替换"的安全 HTML（同回答气泡）
    preview.innerHTML = renderAnswer(editor.value, [], false);
  };

  const schedulePreview = () => {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(renderPreview, PREVIEW_DEBOUNCE_MS);
  };

  const updateHint = () => {
    hint.textContent = `${editor.value.length} 字符`;
  };

  /* ---- 图片（M6 ② 拍照转笔记，ADR-0027）---- */

  // 缩略图条一条渲染路径管两种条目：
  //   待保存（本机 blob 预览，× = 这次不存它）
  //   已存（服务端图，× = 立即 DELETE；服务端删不掉就别从界面上拿掉——那会
  //        让用户以为删了，下次打开又冒出来）
  let mediaItems = []; // {url, blob?, name?}
  let ocrBusy = false; // 一次只识别一张（串行：识别十几秒，并发只会互相排队）

  const renderMediaBar = () => {
    mediaBar.replaceChildren(
      ...mediaItems.map((item) => {
        const thumb = el("img", { class: "note-media-thumb", src: item.url, alt: "原图" });
        const drop = el("button", { class: "note-media-x", type: "button" }, "✕");
        drop.title = item.name ? "删除这张原图" : "这次不保存这张原图";
        drop.addEventListener("click", () => void removeMedia(item));
        return el("span", { class: "note-media-item" }, thumb, drop);
      })
    );
    mediaBar.classList.toggle("hidden", mediaItems.length === 0);
  };

  async function removeMedia(item) {
    if (item.name && targetId !== null) {
      try {
        const res = await readResponse(
          await fetch(`/api/notes/${targetId}/media/${item.name}`, { method: "DELETE" })
        );
        if (!res.ok) {
          toast(`原图删不掉：${apiMessage(res)}`, "error");
          return;
        }
      } catch (err) {
        toast(`原图删不掉：${err.message || err}`, "error");
        return;
      }
    } else if (item.url.startsWith("blob:")) {
      URL.revokeObjectURL(item.url);
    }
    mediaItems = mediaItems.filter((x) => x !== item);
    renderMediaBar();
  }

  /** 识别文本插到光标处（没光标就追加到末尾）；input 事件让预览与字数跟上。 */
  function insertAtCursor(text) {
    const start = editor.selectionStart ?? editor.value.length;
    const end = editor.selectionEnd ?? start;
    const before = editor.value.slice(0, start);
    const after = editor.value.slice(end);
    const sep = before && !before.endsWith("\n") ? "\n\n" : "";
    editor.value = `${before}${sep}${text}\n${after}`;
    const caret = (before + sep + text + "\n").length;
    editor.setSelectionRange?.(caret, caret);
    editor.dispatchEvent(new Event("input", { bubbles: true }));
    editor.focus();
  }

  /** 图片入口（按钮 / 拖拽 / 粘贴共用）：压缩 → 进缩略图条 → 识别 → 插文本。 */
  async function addImage(file) {
    if (ocrBusy) {
      toast("上一张还在识别中，请稍等", "warn");
      return;
    }
    if (!file.type.startsWith("image/")) {
      toast("只认图片文件（jpg / png / webp）", "warn");
      return;
    }
    ocrBusy = true;
    ocrBtn.disabled = true;
    ocrBtn.textContent = "识别中…";
    try {
      const blob = await compressImage(file);
      mediaItems.push({ url: URL.createObjectURL(blob), blob });
      renderMediaBar();
      const fd = new FormData();
      fd.append("file", blob, "note-image.jpg");
      const res = await readResponse(await fetch("/api/notes/ocr", { method: "POST", body: fd }));
      if (!backdrop.isConnected) return; // 等待期间编辑器被关掉了
      if (!res.ok) {
        // 识别失败**保留原图**：用户可能本来就想把这张照片存进来，而且还要能重试
        toast(`识别失败：${apiMessage(res)}（原图会随笔记一起保存）`, "error");
        return;
      }
      const text = (res.body?.text || "").trim();
      if (!text) {
        toast("识别结果是空的（原图会随笔记一起保存）", "warn");
        return;
      }
      insertAtCursor(text);
    } catch (err) {
      toast(`识别失败：${err.message || err}`, "error");
    } finally {
      ocrBusy = false;
      ocrBtn.disabled = false;
      ocrBtn.textContent = "识别图片";
    }
  }

  /** 保存成功后把待存原图逐张传给服务端；失败如实说，**不回滚正文**。 */
  async function uploadPendingImages(docId) {
    const pending = mediaItems.filter((item) => item.blob);
    if (!pending.length) return;
    let failed = 0;
    let reason = "";
    for (const item of pending) {
      try {
        const fd = new FormData();
        fd.append("file", item.blob, "note-image.jpg");
        const res = await readResponse(
          await fetch(`/api/notes/${docId}/media`, { method: "POST", body: fd })
        );
        if (!res.ok) {
          failed += 1;
          reason ||= apiMessage(res);
        }
      } catch (err) {
        failed += 1;
        reason ||= err.message || String(err);
      }
    }
    if (failed) {
      toast(`笔记已保存，但有 ${failed} 张原图没存上：${reason}（可重开这篇再传一次）`, "warn");
    }
  }

  /** 打开已有笔记时把已存原图拉进缩略图条（拉不到就静默：不挡编辑）。 */
  async function loadExistingMedia(id) {
    try {
      const res = await readResponse(await fetch(`/api/notes/${id}/media`));
      if (!backdrop.isConnected || !res.ok || !Array.isArray(res.body?.items)) return;
      for (const entry of res.body.items) {
        mediaItems.push({ url: `/api/notes/${id}/media/${entry.name}`, name: entry.name });
      }
      renderMediaBar();
    } catch {
      /* 原图列表拉不到不影响写笔记 */
    }
  }

  /* ---- 开合 ---- */

  function close() {
    clearTimeout(previewTimer);
    document.removeEventListener("keydown", onKey, true);
    // 本机预览的 object URL 要回收：不回收就一直占着那份 blob 的内存
    for (const item of mediaItems) {
      if (item.url.startsWith("blob:")) URL.revokeObjectURL(item.url);
    }
    backdrop.remove();
    // 只清"自己这一份"的单例：保存续作可能在编辑器已被关掉、且用户又开了
    // 新的一个之后才返回，无条件 current = null 会把**新的那个**的状态抹掉
    if (current?.box === box) current = null;
    if (opener instanceof HTMLElement && opener.isConnected) opener.focus();
  }

  async function askClose() {
    if (saving) return; // 保存中不接放弃请求：请求已发出，说"已放弃"是骗人
    if (pendingConfirm) return; // 确认框已弹出：连点取消/遮罩不该让它"瞬开瞬关"
    if (!isDirty()) {
      close();
      return;
    }
    pendingConfirm = true;
    const ok = await confirmDialog({
      title: "放弃未保存的修改？",
      detail: editing ? "这篇笔记的改动还没保存，离开后无法恢复。" : "新笔记还没保存，离开后内容会丢失。",
      okText: "放弃修改",
      cancelText: "继续编辑",
    });
    pendingConfirm = false;
    if (ok) close();
  }

  function onKey(ev) {
    if (ev.key !== "Escape" || pendingConfirm) return; // 确认框在途：交给它自己处理
    ev.preventDefault();
    ev.stopPropagation(); // 模态：只关自己
    void askClose();
  }

  /* ---- 保存 ---- */

  async function save() {
    const title = titleInput.value.trim();
    if (!title) {
      toast("标题不能为空", "warn");
      titleInput.focus();
      return;
    }
    saving = true;
    saveBtn.disabled = true;
    cancelBtn.disabled = true;
    saveBtn.textContent = "保存中…";
    const payload = { title, body: editor.value };
    if (editing && loadedRev) payload.rev = loadedRev; // 乐观锁：服务端据此判"别处改过没有"
    if (!editing) payload.folder_id = folderSelect.value ? Number(folderSelect.value) : null;

    let doc = null;
    let message = "笔记已保存";
    let noop = false; // 200 = 后端"内容没有变化"短路（与 201 入库区分展示色）
    try {
      let res = editing
        ? await callApi("PUT", `/api/notes/${targetId}`, payload)
        : await callApi("POST", "/api/notes", payload);
      if (editing && res.status === 404 && targetRef) {
        // 旧 id 已失效（同名替换换 id / 响应丢失后重试）：按标记找回当前行，
        // 重投一次。查不到就不重投——那是"笔记真的被删了"，照原样报 404。
        const fresh = await resolveNoteId(targetRef);
        if (!backdrop.isConnected) return;
        if (fresh !== null && fresh !== targetId) {
          // **重投前先看一眼盘上那份还是不是我打开的那一版**（乐观锁的真落点：
          // 同名替换会把 id 换掉，所以"有人先存过"在服务端表现为换 id，而不是
          // rev 不一致）。不是 → 停下来问用户，绝不"最后写入者胜"：
          // uploads 副本是笔记唯一的副本，覆盖掉就没了。
          const now = await callApi("GET", `/api/notes/${fresh}`);
          if (!backdrop.isConnected) return;
          const disk = now.ok ? now.body?.body : null;
          if (typeof disk === "string" && disk !== snapshot.body) {
            toast(
              `保存失败：这条笔记在别处被改过了（多半是另一个标签页）——` +
                `已拦下这次保存，免得覆盖那份改动。请复制你写的正文，关掉后重新打开再粘贴。`,
              "error"
            );
            return;
          }
          targetId = fresh;
          // 重投不带 rev：库里那版内容与打开时一致（上面刚确认过），
          // 多半就是"响应丢了、同一份内容再送一次"。
          const retry = { ...payload };
          delete retry.rev;
          res = await callApi("PUT", `/api/notes/${targetId}`, retry);
        }
      }
      if (!res.ok) {
        const msg = apiMessage(res);
        // 只有"笔记真被删了"才劝另存；旧构建的 404 另存也是 404，别把人带进沟里
        if (res.status === 404 && editing && !msg.includes("旧构建")) {
          toast(`保存失败：${msg}（可复制正文后点「新建笔记」另存）`, "error");
        } else {
          toast(`保存失败：${msg}`, "error");
        }
        return; // 不关窗、不清空：用户补一下就能重试
      }
      doc = res.body?.document || null;
      message = res.body?.message || message;
      noop = res.status === 200;
      snapshot = { title, body: editor.value };
    } catch (err) {
      // fetch 本身失败 = **请求可能已经送达**（响应丢在回来的路上）。
      // 提示要如实：直接重试可能存出第二篇（POST 每次生成新 key，force=True
      // 又绕过了内容去重，这是编辑链路必需的设计）。
      toast(`保存失败：${err.message || err}（若稍后列表里已出现这篇，请勿重复保存）`, "error");
      return;
    } finally {
      saving = false;
      saveBtn.disabled = false;
      cancelBtn.disabled = false;
      saveBtn.textContent = "保存";
    }

    if (!backdrop.isConnected) return; // 等待期间编辑器已被关掉：不再动界面
    toast(message, noop ? "warn" : "ok");
    close();
    // 原图随笔记保存（M6 ②）：正文已经落库，图片这一步失败只如实提示。
    // 顺序刻意放在 close() 之后：上传可能十几秒，不该把窗口卡在那儿。
    if (doc && doc.id) {
      try {
        await uploadPendingImages(doc.id);
      } catch {
        toast("笔记已保存，但原图上传出错了（可重开这篇再传一次）", "warn");
      }
    }
    if (doc && onSaved) {
      // 续作放在 try 之外：入库已经成功，后续刷新/选中失败不该被报成"保存失败"
      try {
        await onSaved(doc);
      } catch {
        toast("笔记已保存，但列表刷新失败，请手动刷新页面", "warn");
      }
    }
  }

  /* ---- 装配 ---- */

  titleInput.addEventListener("input", schedulePreview);
  titleInput.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      editor.focus();
    }
  });
  editor.addEventListener("input", () => {
    schedulePreview();
    updateHint();
  });
  cancelBtn.addEventListener("click", () => void askClose());
  saveBtn.addEventListener("click", () => void save());
  backdrop.addEventListener("click", (ev) => {
    if (ev.target === backdrop) void askClose(); // 点卡片内部不关
  });

  // 图片三条入口（按钮 / 粘贴 / 拖拽）——都汇到 addImage
  ocrBtn.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    const file = fileInput.files?.[0];
    fileInput.value = ""; // 同一张图能再选一次
    if (file) void addImage(file);
  });
  editor.addEventListener("paste", (ev) => {
    const item = [...(ev.clipboardData?.items || [])].find((it) => it.type.startsWith("image/"));
    if (!item) return; // 纯文本粘贴照旧走浏览器默认
    ev.preventDefault(); // 别让图片被当成文本塞进 textarea
    const file = item.getAsFile();
    if (file) void addImage(file);
  });
  box.addEventListener("dragover", (ev) => {
    ev.preventDefault(); // 不 preventDefault 的话浏览器会直接打开这张图
    box.classList.add("note-drop");
  });
  box.addEventListener("dragleave", (ev) => {
    if (ev.target === box) box.classList.remove("note-drop");
  });
  box.addEventListener("drop", (ev) => {
    ev.preventDefault();
    box.classList.remove("note-drop");
    const file = [...(ev.dataTransfer?.files || [])].find((f) => f.type.startsWith("image/"));
    if (file) void addImage(file);
  });
  document.addEventListener("keydown", onKey, true);

  document.body.append(backdrop);
  current = { docId: targetId, box }; // box 供 close() 认领"自己那一份"单例

  if (editing) {
    // 回填：标题 + 原文（逐字符，供用户接着改）。
    // **载入失败绝不能留下一个"空白但可保存"的编辑器**：那等于让用户拿空白
    // 快照覆盖掉整篇笔记（uploads 副本是笔记的唯一权威副本，覆盖不可恢复）。
    // 所以：网络异常 / 非 JSON 响应 / 缺字段，三条路都关窗退出。
    let loaded = null;
    try {
      loaded = await callApi("GET", `/api/notes/${docId}`);
    } catch (err) {
      toast(`打开失败：${err.message || err}`, "error");
      close();
      return;
    }
    if (!backdrop.isConnected) return; // 等待期间用户已关掉编辑器
    if (!loaded.ok || typeof loaded.body?.body !== "string" || !loaded.body?.document) {
      toast(`打开失败：${apiMessage(loaded)}`, "error");
      close();
      return;
    }
    titleInput.value = loaded.body.document.title || "";
    targetRef = loaded.body.document.source_ref || null;
    editor.value = loaded.body.body;
    loadedRev = loaded.body.rev || null;
    snapshot = { title: titleInput.value.trim(), body: editor.value };
    await loadExistingMedia(docId); // 已存原图进缩略图条（拉不到不挡编辑）
  } else {
    try {
      const folders = await callApi("GET", "/api/kb-folders");
      if (!backdrop.isConnected) return; // 同上：等待期间已关闭
      for (const opt of folderOptions(folders.body?.folders || [])) {
        folderSelect.append(el("option", { value: String(opt.id) }, opt.label));
      }
    } catch {
      /* 文件夹列表拉不到不影响保存（落根级），静默即可 */
    }
  }
  renderPreview();
  updateHint();
  (editing ? editor : titleInput).focus();
}
