/* =========================================================================
   首启引导（新用户第一次打开时弹一次的欢迎面板）。

   为什么需要：知识库是空的，新用户打开只看得到"没有会话 / 没有文档"，
   不知道该干什么。这份面板就是产品的"第一句话"——用三步说清楚它是什么、
   怎么用、回答从哪来。

   触发条件（两个都满足才弹）：
     1. `/api/health` 报 documents === 0（空库 = 还没开始用）
     2. localStorage 没记过"已看过"（关掉之后不再骚扰）
   用户一旦有文档，或点过"知道了"，面板永不出现——不做"每次启动都弹"的广告。

   交互纪律：全部 addEventListener；动态文本走 el() 文本节点（无 innerHTML）。
   ========================================================================= */

import { $, el } from "./common.js";

const SEEN_KEY = "mikasa.ui.onboardSeen";

/** 三步引导的文案（图标用 emoji：无构建链、跨平台、不额外请求资源）。 */
const STEPS = [
  {
    icon: "📄",
    title: "第一步 · 把资料放进来",
    body: "在「知识库」页上传论文或笔记——支持 PDF、Word、Markdown、纯文本，单个最大 500MB。上传后自动切块、建索引，几秒钟就能检索。",
  },
  {
    icon: "💬",
    title: "第二步 · 直接用中文提问",
    body: "在「问答」页问任何你想知道的内容——不必记得出自哪一篇。中文提问也能搜到英文论文（会自动翻译成第二路查询）。",
  },
  {
    icon: "🔍",
    title: "第三步 · 每个结论都能查证",
    body: "回答里的 [1] [2] 是可点的角标：点开就能看到原文那一块，并按引用跳转到 PDF 的对应页面。资料里没有的内容，它会直接说「答不上」而不是编——这是特性，不是故障。",
  },
];

/** 模型来源说明：回答质量取决于用户接哪个模型，第一次就该讲清楚。 */
const MODEL_HINT =
  "回答由你接的模型生成：装了 Ollama 就用本机模型（免费、离线）；" +
  "也可以点右上角 ⚙ 在「模型」里接入 DeepSeek、SiliconFlow 等云端 API——" +
  "密钥粘贴进面板、只存本机，不用手写任何配置文件。";

function buildPanel(onClose) {
  const steps = STEPS.map((s) =>
    el(
      "div",
      { class: "ob-step" },
      el("div", { class: "ob-icon" }, s.icon),
      el(
        "div",
        { class: "ob-text" },
        el("div", { class: "ob-title" }, s.title),
        el("p", { class: "ob-body" }, s.body)
      )
    )
  );
  const start = el("button", { class: "btn primary", type: "button" }, "开始使用");
  start.addEventListener("click", () => onClose(false));
  // "先去传文档"必须是**按钮而不是链接**：`<a href>` 直接跳转不经过 close()，
  // 于是没写"已看过"标记，跳到知识库页后又是空库 → 引导再次弹出，用户看到
  // 的现象是"弹窗关不掉"（2026-09-11 用户实测）。走 close(true) 才既标记又跳转。
  const sample = el("button", { class: "btn ghost", type: "button" }, "先去传文档");
  sample.addEventListener("click", () => onClose(true));

  return el(
    "div",
    { class: "onboard-mask", id: "onboard" },
    el(
      "div",
      { class: "onboard-card", role: "dialog", "aria-label": "欢迎使用 Mikasa" },
      el("div", { class: "ob-head" }, "欢迎使用 Mikasa"),
      el(
        "p",
        { class: "ob-lead" },
        "把论文和笔记变成能对话的知识库——每个回答都带出处。三步就能上手："
      ),
      ...steps,
      el("p", { class: "ob-hint" }, MODEL_HINT),
      el("div", { class: "ob-actions" }, start, sample)
    )
  );
}

/**
 * 装配首启引导。三页各调一次（幂等：面板已存在就直接返回）。
 * 只在"空库 + 没看过"时弹；其它情况静默跳过——它不该打断老用户。
 */
export async function initOnboard(health) {
  if ($("#onboard")) return;
  try {
    if (localStorage.getItem(SEEN_KEY)) return; // 看过就不再弹
  } catch {
    return; // 隐私模式禁用 storage：宁可不弹，也别每次骚扰
  }
  if (!health || health.documents > 0) return; // 有资料 = 老用户

  const close = (navigated) => {
    try {
      localStorage.setItem(SEEN_KEY, "1");
    } catch {
      /* 存不下也只是下次再弹一次，不影响使用 */
    }
    panel.remove();
    if (navigated) location.href = "/documents";
  };
  const panel = buildPanel(close);
  document.body.append(panel);
}

/** 供"设置 → 重看引导"调用（清掉标记即可）。 */
export function resetOnboard() {
  try {
    localStorage.removeItem(SEEN_KEY);
  } catch {
    /* 同上 */
  }
}
