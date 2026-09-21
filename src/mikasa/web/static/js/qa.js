/* =========================================================================
   问答页逻辑（index.html 的入口模块）。

   两条消息链路（复用同一渲染路径）：
   - 发送提问 → SSE 流式（新会话走 /api/ask/stream，meta 帧带回新建
     会话 id；续问走 /api/sessions/{id}/chat/stream）→ 面板流式刷新；
   - 点击历史会话 → GET /api/sessions/{id}/messages 全量回放（与流式
     结果同构的 JSON，直接同一套渲染）。

   状态机：activeSession = null（新会话）| number（续问目标）；
   currentMode = kb | free（问答模式，body 随每条提问上送，见 ADR-0013）。
   流式期间的"当前答案"由 textBuf 累积 delta 显示，done 帧携带完整
   Answer（引用/延迟一次给全），整块替换为最终渲染——协议保证
   deltas 拼接 === done.text，所以中途与收尾内容恒一致。
   ========================================================================= */

import {
  $,
  apiFetch,
  el,
  esc,
  fmtLatency,
  fmtSeconds,
  fmtTime,
  initTopbar,
  renderAnswer,
  renderCitations,
  ssePost,
  toast,
} from "./common.js";
import { initSidebar, refreshSessions, setActiveSession } from "./qa-tree.js"; // 会话树（文件夹 + 行内管理）
import { initSettings, nickname } from "./settings.js"; // 聊天设置（昵称/字号/背景）
import { initReader, openChunk } from "./reader.js"; // 阅读器（两页共用）
import { initOnboard } from "./onboard.js"; // 首启引导（空库时弹一次）
import { initUpdate } from "./update.js"; // 更新提示（启动静默检查，ADR-0022）
import { initImageGen } from "./image-gen.js"; // 文生图入口（ADR-0031）

/* ---------------- 状态与 DOM 引用 ---------------- */

let activeSession = null; // null=新会话；number=会话 id（续问）
// 会话加载序号：连点两个会话时"后点的赢"（响应的到达顺序是随机的）
let sessionSeq = 0;
let busy = false; // 发送进行中（禁输入防重入）
let currentMode = "kb"; // 当前问答模式：kb=知识库检索 / free=自由问答（ADR-0013）
let freeAllowed = true; // offline（mock）下自由问答置灰（health 到达前默认放行，守卫兜底）
const messagesBox = $("#messages");
const questionInput = $("#question");
const sendBtn = $("#send");
const modeKbBtn = $("#mode-kb");
const modeFreeBtn = $("#mode-free");
const welcomeHTML = messagesBox.innerHTML; // 欢迎态快照（新建/删当前会话时还原）

/* ---------------- 问答模式切换（会话内随时切，每条提问跟随开关） ---------------- */

/** 模式切换：互斥高亮 + 输入框 placeholder 随动。busy/置灰由调用方保证。 */
function setMode(mode) {
  if (mode !== "kb" && mode !== "free") return;
  currentMode = mode;
  for (const [m, btn] of [["kb", modeKbBtn], ["free", modeFreeBtn]]) {
    btn.classList.toggle("active", m === mode);
    btn.setAttribute("aria-pressed", String(m === mode));
  }
  questionInput.placeholder =
    mode === "free"
      ? "直接提问（不检索知识库）：原理、扩展、任何话题…"
      : "输入你的问题（Enter 发送，Shift+Enter 换行）…";
}

/**
 * 依据 /api/health 的 profile 决定 free 可用性（渐进增强）。
 * offline ⇔ MockLLM（无语义）；profile 是便捷信号，最终权威是后端守卫
 * （ConfigError → 400/error 帧）。health 拉取失败（null）→ 不置灰。
 */
function applyModeAvailability(health) {
  const blocked = health !== null && health.profile === "offline";
  freeAllowed = !blocked;
  modeFreeBtn.disabled = blocked;
  modeFreeBtn.title = blocked
    ? "offline（MockLLM 无语义）不支持自由问答——请用 mikasa serve --profile api / local"
    : "";
  if (blocked && currentMode === "free") setMode("kb"); // 晚到回拨：置灰态不该停在 free
}

/** 历史消息的模式判定：latency_ms 含 retrieve 键 = kb 轮（回放契约）。 */
function isKbMessage(m) {
  return Boolean(m.latency_ms && "retrieve" in m.latency_ms);
}


/* ---------------- 会话栏（渲染与管理在 qa-tree.js，此处只管行为回拨） ----------------
   会话树模块经 initSidebar 注入三个回拨，见启动段；本模块不再碰列表 DOM。
   refreshSessions / setActiveSession 为 qa-tree.js 的公开面：
   - refreshSessions  拉 sessions+folders 全树重绘（发送后/改名后…）；
   - setActiveSession 本地高亮重绘（打开/新建/删当前会话后调用）。      */

/** 打开历史会话：回放消息并设为续问目标。 */
async function openSession(id) {
  if (busy) return; // 流式中不切会话（避免消息串台）
  activeSession = id;
  // 过期守卫：会话树里快速连点 A、B 时两次请求都在途，谁先回来是随机的。
  // 没有这道守卫的话 A 的响应后到会把界面画成 A，而 activeSession 已是 B
  // ——**屏幕上显示 A、下一句提问却发往 B**（2026-09-11 审查发现；
  // reader.js 的 openDocument 早就有同款守卫，这里漏了）。
  const seq = ++sessionSeq;
  try {
    const { messages } = await apiFetch(`/api/sessions/${id}/messages`);
    if (seq !== sessionSeq) return; // 期间又点了别的会话：丢弃，别把人拽回去
    renderThread(messages);
    setActiveSession(id);
  } catch (err) {
    if (seq !== sessionSeq) return;
    toast(`会话加载失败：${err.message}`, "error");
  }
}

/** 新建会话：清空消息区回欢迎态。 */
function newSession() {
  if (busy) return;
  activeSession = null;
  sessionSeq += 1; // 作废在途的会话加载：否则它回来会盖掉欢迎态
  messagesBox.innerHTML = welcomeHTML;
  questionInput.focus();
  setActiveSession(null);
}

/* ---------------- 消息区渲染 ---------------- */

/** 整条线程渲染（历史回放）。messages 行：role/content/refused/citations/latency_ms。 */
function renderThread(messages) {
  messagesBox.innerHTML = "";
  messagesBox.append(
    ...messages.map((m) =>
      m.role === "user"
        ? bubble(m.content, "user")
        : bubble(
            m.content,
            "assistant",
            m.citations || [],
            m.refused,
            m.created_at,
            isKbMessage(m),
            m.latency_ms
          )
    )
  );
  scrollBottom();
}

/** 一条消息气泡：user 纯文本；assistant 走引用渲染 + 可选引用卡。
 *
 * showCites 由消息的 latency_ms 键判定（kb 含 retrieve / free 无）——
 * free 轮回放的正文 [n] 不渲染成 chip（当时没有注入编号协议），
 * kb 越界红标纪律不受损（回放契约见 ADR-0013）。
 */
function bubble(content, role, citations = [], refused = false, when = "", showCites = true, latency = null) {
  const wrap = el("div", { class: `msg ${role}` });
  // who 行按非空段拼装（旧写法把自带 " · " 前缀的 time 再拼一次分隔符，
  // 渲染成 "Mikasa ·  · 09-10 22:30" 两个点）。耗时只加在 assistant 侧。
  const whoText = (name) => {
    const parts = [name];
    if (when) parts.push(fmtTime(when));
    const lat = role === "assistant" ? fmtLatency(latency) : "";
    if (lat) parts.push(lat);
    return parts.join(" · ");
  };
  const body = role === "user" ? el("p", { html: esc(content) }) : null;
  if (role === "assistant") {
    wrap.append(el("div", { class: "who" }, whoText("Mikasa")));
    const card = el("div", { class: "bubble" });
    card.innerHTML = refused
      ? `<p class="cite bad" style="display:inline-block">无据拒答</p><p>${esc(content)}</p>`
      : renderAnswer(content, citations, showCites);
    wrap.append(card);
    if (citations.length) { // 无引用（free/空拒答轮）不产生空 shelf
      const shelf = el("div");
      shelf.innerHTML = renderCitations(citations); // 内部已全部 esc
      wrap.append(shelf);
    }
  } else {
    // 署名取聊天设置里的昵称（settings.js nickname()，账号上线前为空回退"我"）
    wrap.append(el("div", { class: "who" }, whoText(nickname() || "我")), el("div", { class: "bubble" }, body));
  }
  attachBubbleActions(wrap);
  return wrap;
}

/**
 * 消息气泡的点击委托（挂在整条消息容器上，不逐元素加监听）：
 * - .btn-copy 代码复制钮：整块代码进剪贴板，按钮闪"✓ 已复制"；
 * - .cite 引用 chip：点亮 chip 并滚动到对应引用卡（bad chip 无
 *   data-marker，不响应）。
 * 代码块与 chip 都只在答案渲染后出现，委托在气泡生成时一次性挂上即可
 * （流式 done 帧与历史回放走同一入口）。
 */
function attachBubbleActions(root) {
  // 图片加载失败（生成图被清理 / 服务重启换了数据目录）：给容器挂个标记，
  // 由 CSS 显示一行说明。error 事件**不冒泡**，所以必须用捕获阶段。
  root.addEventListener(
    "error",
    (ev) => {
      const fig = ev.target instanceof HTMLImageElement ? ev.target.closest(".md-figure") : null;
      if (fig) fig.classList.add("broken");
    },
    true
  );
  root.addEventListener("click", async (ev) => {
    const btn = ev.target.closest(".btn-copy");
    if (btn) {
      const code = btn.closest(".code-block")?.querySelector("pre code");
      if (code && (await copyText(code.textContent))) {
        btn.classList.add("ok");
        btn.textContent = "✓ 已复制";
        clearTimeout(btn._t);
        btn._t = setTimeout(() => {
          btn.classList.remove("ok");
          btn.textContent = "复制";
        }, 1600);
      }
      return;
    }
    // 点整张引用卡 = 点它的 [n] 角标：阅读面板里打开并定位到该块（2026-09-11，
    // 用户要求：不要跳新标签，要跟角标一样的面板内打开）
    // 变量名不可用 card：下面角标分支已在同一作用域声明了 const card
    // （2026-09-11 曾因重名 SyntaxError 导致整页 JS 静默失效）
    const hitCard = ev.target.closest(".cite-card");
    if (hitCard) {
      const marker = hitCard.id.replace("cite-", "");
      const chipOf = root.querySelector(`.cite[data-marker="${marker}"]`);
      if (chipOf) chipOf.classList.add("lit");
      if (hitCard.dataset.chunkId) void openChunk(Number(hitCard.dataset.chunkId));
      return;
    }
    const chip = ev.target.closest(".cite:not(.bad)");
    if (!chip) return;
    const card = root.querySelector(`#cite-${chip.dataset.marker}`);
    root.querySelectorAll(".cite.lit").forEach((c) => c.classList.remove("lit"));
    if (card) {
      chip.classList.add("lit");
      // center：引用卡滚到消息区中部（nearest 会贴底，长文下看不到全卡）
      card.scrollIntoView({ behavior: "smooth", block: "center" });
    }
    // 再开阅读面板并定位到该引用的原文块（2026-09-10）。两处高亮互不干扰：
    // 上面是消息内的引用卡，面板里是原文正文里的那一块。
    if (chip.dataset.chunkId) void openChunk(Number(chip.dataset.chunkId));
  });
}

/**
 * 复制文本到剪贴板：先走 Clipboard API（127.0.0.1 属安全上下文，点击
 * 手势下可用），失败（权限拒绝/老引擎）降级隐藏 textarea + execCommand。
 */
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // 走降级路径
  }
  const ghost = el("textarea", { class: "clip-ghost", "aria-hidden": "true" });
  ghost.value = text;
  document.body.append(ghost);
  ghost.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  ghost.remove();
  return ok;
}

function scrollBottom() {
  messagesBox.scrollTop = messagesBox.scrollHeight;
}

/* ---------------- 发送与流式消费 ---------------- */

/** 发送提问：新会话 / 续问分别走对应 SSE 端点；body 携带当前 mode。 */
async function sendQuestion() {
  const question = questionInput.value.trim();
  const mode = currentMode;
  if (!question || busy) return;
  busy = true;
  sendBtn.disabled = true;
  questionInput.disabled = true;
  modeKbBtn.disabled = true; // 流式中锁模式：一条消息一个开关态
  modeFreeBtn.disabled = true;

  // 首轮判定取"发送时"状态：meta 帧每轮都发（续问也有），只有首问才
  // 该触发标题提炼——提炼材料取首问+首答，续问时结果不变还白烧一次 LLM
  const firstRound = activeSession === null;
  let gotMeta = false; // meta 帧到达 = 会话确认（后端把 id 送回）
  const path = activeSession === null
    ? "/api/ask/stream"
    : `/api/sessions/${activeSession}/chat/stream`;

  // 用户气泡立即上屏，答案区开一个"流式占位"
  messagesBox.querySelector(".empty")?.remove();
  messagesBox.append(bubble(question, "user"));
  const answerWrap = el("div", { class: "msg assistant streaming" });
  const card = el("div", { class: "bubble" });
  const streamBody = el("span", { class: "caret" });
  card.append(streamBody);
  const whoText = mode === "free" ? "正在自由作答…" : "正在检索知识库并作答…";
  // 等待计时（2026-09-10 用户实测）：检索、重排、译查询、生成全在首个
  // delta 之前，本地 qwen3 下可达数十秒——此前 who 行是静态文案，界面
  // 毫无变化，分不清"在思考"和"卡死了"。每 100ms 刷新已等时长。
  const startedAt = performance.now();
  const whoLine = el("div", { class: "who" });
  const tickWait = () => {
    const sec = (performance.now() - startedAt) / 1000;
    whoLine.textContent = `Mikasa · ${whoText} ${fmtSeconds(sec)}`;
  };
  tickWait(); // 立即上一次，不留首个 100ms 空窗
  const waitTimer = setInterval(tickWait, 100);
  answerWrap.append(whoLine, card);
  messagesBox.append(answerWrap);
  scrollBottom();

  let buffer = ""; // delta 累积（显示层只做纯文本追加，收尾再整块渲染）

  const finish = () => {
    busy = false;
    sendBtn.disabled = false;
    questionInput.disabled = false;
    modeKbBtn.disabled = false;
    modeFreeBtn.disabled = !freeAllowed; // 恢复 availability 置灰态
    answerWrap.classList.remove("streaming");
    questionInput.focus();
    refreshSessions();
  };

  try {
    await ssePost(path, { question, mode }, (type, data) => {
      if (type === "meta") {
        // 新会话在此确认：此后输入框指向该会话（列表已含它）
        activeSession = data.session_id;
        gotMeta = true;
        // 必须同步给树：否则左侧不高亮，且 qa-tree 仍认为"无活动会话"——
        // 用户删掉这个会话时 onActiveCleared 不触发，activeSession 继续指向
        // 已删 id，下一问打到 /api/sessions/{已删}/chat/stream 得 404
        setActiveSession(data.session_id);
      } else if (type === "delta") {
        buffer += data.text;
        streamBody.textContent = buffer; // textContent：流式内容绝不进 innerHTML
        scrollBottom();
      } else if (type === "done") {
        const answer = data.answer;
        clearInterval(waitTimer);
        if (answer) {
          // 收尾把等待文案换成真实耗时分段：总时长 + 各阶段去向
          whoLine.textContent =
            `Mikasa · ${fmtLatency(answer.latency_ms, (performance.now() - startedAt) / 1000)}`;
          card.innerHTML = answer.refused
            ? `<p class="cite bad" style="display:inline-block">无据拒答</p><p>${esc(buffer || answer.text)}</p>`
            : renderAnswer(buffer || answer.text, answer.citations, mode === "kb");
          if (answer.citations.length) {
            const shelf = el("div");
            shelf.innerHTML = renderCitations(answer.citations); // 内部已全部 esc
            card.after(shelf);
          }
          attachBubbleActions(answerWrap); // 委托：复制钮 + 引用 chip（code 块出现与否都无妨）
        }
        buffer = "";
      } else if (type === "error") {
        clearInterval(waitTimer);
        card.innerHTML = `<p style="color:var(--danger)">✗ ${esc(data.message)}</p>`;
      }
    });
  } catch (err) {
    card.innerHTML = `<p style="color:var(--danger)">✗ 发送失败：${esc(err.message)}</p>`;
  } finally {
    clearInterval(waitTimer); // 兜底：done/error 已清，异常中断也不会留下空转
    finish();
    if (firstRound && gotMeta) fireTitleSuggest(); // 首问：done 后补跑 LLM/截断提炼
  }
  questionInput.value = "";
  scrollBottom();
}

/**
 * 首轮新会话的标题提炼（非阻塞，不 disable 输入）。
 * 自动截断标题在 _record 已落库（发送后 finish 的刷新可见）；这里再走
 * suggest 是"截断 → LLM 提炼"的可选升级——api/local 档落回 ≤16 字
 * 提炼名后需要再刷一次侧栏；offline/mock 幂等同值，多刷一次无妨。
 */
async function fireTitleSuggest() {
  try {
    await apiFetch(`/api/sessions/${activeSession}/title/suggest`, {
      method: "POST",
      body: "{}",
    });
    refreshSessions();
  } catch {
    // 提炼失败（无消息/上游出错）不影响对话，静默——截断标题已在库
  }
}

/* ---------------- 启动 ---------------- */

questionInput.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey) {
    ev.preventDefault();
    sendQuestion();
  }
});
sendBtn.addEventListener("click", sendQuestion);
modeKbBtn.addEventListener("click", () => setMode("kb"));
modeFreeBtn.addEventListener("click", () => setMode("free"));
$("#new-session").addEventListener("click", newSession);
// 会话树接线：qa-tree 只认这三个回拨（busy 守卫 / 打开会话 / 删当前会话清场）
initSidebar({
  isBusy: () => busy,
  onOpenSession: openSession,
  onActiveCleared: () => {
    activeSession = null;
    messagesBox.innerHTML = welcomeHTML;
    questionInput.focus();
    setActiveSession(null);
  },
});
// 自动增高输入框（多行问题）
questionInput.addEventListener("input", () => {
  questionInput.style.height = "auto";
  questionInput.style.height = `${Math.min(questionInput.scrollHeight, 150)}px`;
});

initTopbar("qa").then((health) => {
  applyModeAvailability(health); // health 到达后置灰 offline 的 free
  void initOnboard(health); // 空库 + 首次打开 → 欢迎面板
  initUpdate(health); // 版本号回填 + 启动静默查更新（失败不打扰）
});
initSettings(); // 聊天设置面板（昵称/字号/对话框背景，本地持久化）
// 文生图：生成的图作为一条助手消息进对话（后端连图一起落库，刷新仍在）
initImageGen({
  button: $("#gen-image"),
  getSessionId: () => activeSession,
  onGenerated: (res) => {
    // 后端在"没有会话"时会新建一个并回传——先接管它，否则下一问会丢
    if (activeSession === null && res.session_id) {
      activeSession = res.session_id;
      setActiveSession(res.session_id);
      refreshSessions();
    }
    messagesBox.append(bubble(res.content, "assistant", [], false, new Date().toISOString(), false));
    messagesBox.scrollTo({ top: messagesBox.scrollHeight, behavior: "smooth" });
  },
});
initReader(); // 阅读面板：点引用角标 → 打开并定位到原文块（js/reader.js）
refreshSessions();
questionInput.focus();

/* ---------------- 回到底部浮钮（消息区滚离底部才出现） ---------------- */

const toBottomBtn = el("button", { type: "button", class: "to-bottom" }, "↓ 回到底部");
toBottomBtn.style.display = "none"; // 初始隐藏：视口已在底部时无意义
document.body.append(toBottomBtn);

// 距底部阈值（px）：滚离超过才认为"需要回到底部"（含流式增行抖动容忍）
const NEAR_BOTTOM_PX = 160;
messagesBox.addEventListener("scroll", () => {
  const farFromBottom =
    messagesBox.scrollHeight - messagesBox.scrollTop - messagesBox.clientHeight > NEAR_BOTTOM_PX;
  toBottomBtn.style.display = farFromBottom ? "" : "none";
});
toBottomBtn.addEventListener("click", () => {
  messagesBox.scrollTo({ top: messagesBox.scrollHeight, behavior: "smooth" });
});
