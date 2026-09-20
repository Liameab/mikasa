/* =========================================================================
   评测页主逻辑（eval.html 入口模块）+ 评测报告渲染（report.js）。

   任务状态机（与后端 EvalJobManager 单槽语义对偶）：
     idle      —— 无任务：可点"开始评测"
     polling   —— 有 running 任务：1s 轮询 jobs/current 推进进度条
     done      —— 收尾：刷新历史 + 自动打开报告；释放按钮
     error     —— 任务失败：红条展示 error 消息；释放按钮

   页面刷新恢复：进入时 GET jobs/current 若为 running → 回到 polling
   （后台任务在服务进程里仍在跑，与页面无关）。
   评测是长任务（真实 api 配置约 4-5 分钟）：按钮按住 + 进度条是
   唯一交互面，不阻塞页面其他操作。
   ========================================================================= */

import { $, apiFetch, el, fmtTime, initTopbar, toast } from "./common.js";
import { renderRunReport } from "./report.js";
import { initOnboard } from "./onboard.js"; // 首启引导（空库时弹一次）
import { initUpdateBadge } from "./update.js"; // 更新进度胶囊（观察到别处起的下载）

let pollTimer = null; // 轮询句柄（页面卸载/任务收尾时清除）
const startBtn = $("#start-eval");
const jobPanel = $("#job-panel");
const jobBar = $("#job-bar");
const jobLabel = $("#job-label");
const jobRatio = $("#job-ratio");
const jobMeta = $("#job-meta");
const runList = $("#run-list");
const reportBody = $("#report-body");
const reportHead = $("#report-head");
const banksBox = $("#golden-banks");
const synthBtn = $("#synth-bank");
const synthPanel = $("#synth-panel");
const synthBar = $("#synth-bar");
const synthLabel = $("#synth-label");
const synthRatio = $("#synth-ratio");
const synthMeta = $("#synth-meta");
const corpusHint = $("#corpus-hint");

let activeBank = "builtin"; // 当前选中的题库（后端取值：builtin / auto）
let banks = [];

/* ---------------- 题库清单与选择 ---------------- */

/**
 * 拉取可用题库并渲染选择器。
 *
 * 题库有两份：内置示例语料（人工出题，题目面向示例语料）与「我的资料」
 * （自动生成，见后端 /api/eval/synthesize）。只有一份可用时也要显示两份的
 * 状态——否则用户不知道自动题库这条路存在。
 */
async function loadBanks({ keepSelection = true } = {}) {
  try {
    const body = await apiFetch("/api/eval/goldens");
    banks = body.banks || [];
  } catch (err) {
    banksBox.textContent = "";
    corpusHint.textContent = `题库读取失败：${err.message}`;
    return;
  }
  if (!keepSelection || !banks.some((b) => b.id === activeBank && b.available)) {
    const firstAvailable = banks.find((b) => b.available);
    if (firstAvailable) activeBank = firstAvailable.id;
  }
  renderBanks();
}

function renderBanks() {
  banksBox.textContent = "";
  for (const bank of banks) {
    const label = bank.available
      ? `${bank.label}（${bank.items} 题）`
      : `${bank.label}（未生成）`;
    const btn = el(
      "button",
      {
        type: "button",
        class: bank.id === activeBank && bank.available ? "btn primary" : "btn ghost",
        title: bank.id === "auto"
          ? "用你自己库里的文档自动出题生成的标准答案题库"
          : "仓库内置的人工题库（题目面向示例语料）",
      },
      label
    );
    if (!bank.available) btn.disabled = true;
    btn.addEventListener("click", () => {
      activeBank = bank.id;
      renderBanks();
      updateHint();
    });
    banksBox.append(btn);
  }
  updateHint();
}

function updateHint() {
  const bank = banks.find((b) => b.id === activeBank);
  if (!bank || !bank.available) {
    corpusHint.textContent = "还没有可用的题库：先导入语料，再点右侧「为我的资料生成题库」";
    return;
  }
  const auto = bank.source === "synthesized";
  corpusHint.textContent = auto
    ? `${bank.items} 题由 ${bank.model || "模型"} 从你的资料自动生成（${bank.created}）；自动题比人工题简单，分数偏高，适合横向比较`
    : `${bank.items} 题人工题库（面向内置示例语料）；语料增删改只会跳过受影响的题，不再整体失效`;
}

/* ---------------- 开始评测 ---------------- */

async function startEval() {
  startBtn.disabled = true;
  try {
    const body = await apiFetch(`/api/eval/runs?golden=${encodeURIComponent(activeBank)}`, {
      method: "POST",
    });
    toast(body.message || "评测已开始", "ok");
    showJob(body.job); // 202 响应里带着 running 初始快照
    poll(); // 立即开始轮询（首个 tick 就到新状态）
  } catch (err) {
    if (err.message.includes("运行中")) {
      // 409：别人（或上个页面）启动的任务还在跑——转成跟随轮询
      toast("已有评测在后台运行，页面已转为跟随进度", "warn");
      showJob(null);
      poll();
    } else {
      toast(`无法开始评测：${err.message}`, "error");
      startBtn.disabled = false;
    }
  }
}

/* ---------------- 为我的资料生成题库 ---------------- */

let synthTimer = null;

async function startSynth() {
  synthBtn.disabled = true;
  try {
    const body = await apiFetch("/api/eval/synthesize", { method: "POST" });
    toast(body.message || "出题已开始", "ok");
    synthPanel.hidden = false;
    showSynth(body.job);
    pollSynth();
  } catch (err) {
    toast(`无法生成题库：${err.message}`, "error");
    synthBtn.disabled = false;
  }
}

function stopSynthPolling() {
  if (synthTimer !== null) {
    clearTimeout(synthTimer);
    synthTimer = null;
  }
}

function showSynth(job) {
  if (!job) return;
  const total = job.total || 0;
  const done = job.done || 0;
  synthPanel.hidden = false;
  synthLabel.textContent = `${job.stage || "准备中"}…`;
  synthRatio.textContent = `${done} / ${total}`;
  synthBar.style.width = total ? `${Math.min(100, Math.floor((done / total) * 100))}%` : "0%";
  synthMeta.textContent = `已生成 ${job.answerable || 0} 道可答题、${job.unanswerable || 0} 道不可答题`;
}

/** 一次出题轮询：done 后刷新题库清单并把它切成当前题库。 */
async function pollSynth() {
  stopSynthPolling();
  let job;
  try {
    ({ job } = await apiFetch("/api/eval/synthesize/status"));
  } catch (err) {
    synthLabel.textContent = `进度查询失败：${err.message}`;
    synthBtn.disabled = false;
    return;
  }
  if (!job || job.status === "done" || job.status === "error") {
    synthBtn.disabled = false;
    if (job && job.status === "error") {
      synthLabel.textContent = "出题失败";
      synthMeta.textContent = job.error || "";
      toast(`生成题库失败：${job.error || "未知错误"}`, "error");
      return;
    }
    if (job && job.status === "done") {
      // 进度条封顶：轮询间隔 1s，最后一份 running 快照可能停在中途，不补满
      // 就会出现"条停在 12/32、文案说已生成 32 道"的自相矛盾（E2E 抓到）
      synthLabel.textContent = "完成";
      synthBar.style.width = "100%";
      synthRatio.textContent = `${job.total} / ${job.total}`;
      synthMeta.textContent = `已生成 ${job.answerable} 道可答题、${job.unanswerable} 道不可答题`;
      toast(`题库已生成（${job.answerable + job.unanswerable} 题），已切换为「我的资料」`, "ok");
      activeBank = "auto";
      await loadBanks();
    }
    return;
  }
  showSynth(job);
  synthTimer = setTimeout(pollSynth, 1000);
}

/* ---------------- 任务轮询 ---------------- */

/** 一次轮询：拉任务状态并更新 UI；done/error 收尾并停止。 */
async function poll() {
  try {
    const { job } = await apiFetch("/api/eval/jobs/current");
    if (job === null) {
      stopPolling();
      jobPanel.hidden = true;
      startBtn.disabled = false; // 服务端无任务（可能被新评测覆盖）→ 释放
      return;
    }
    showJob(job);
    if (job.status === "done") {
      stopPolling();
      startBtn.disabled = false;
      toast(`评测完成：run #${job.run_id}`, "ok");
      await refreshRuns(job.run_id); // 刷新列表并打开最新报告
      jobPanel.hidden = true;
    } else if (job.status === "error") {
      stopPolling();
      startBtn.disabled = false;
      toast(`评测失败：${job.error}`, "error");
      jobPanel.hidden = true;
      await refreshRuns();
    } else {
      scheduleNextPoll(); // running → 1s 后再看
    }
  } catch (err) {
    toast(`进度查询失败：${err.message}`, "error");
    stopPolling();
    startBtn.disabled = false;
  }
}

/** 更新进度条展示（running 快照；idle 空态可复用同一函数）。 */
function showJob(job) {
  if (!job) {
    jobPanel.hidden = false;
    jobBar.style.width = "0%";
    jobLabel.textContent = "等待任务占位…";
    jobRatio.textContent = "";
    return;
  }
  jobPanel.hidden = job.status !== "running";
  if (job.status !== "running") return;
  const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
  jobBar.style.width = `${pct}%`;
  jobLabel.textContent = job.status === "running" ? `正在作答 ${job.current}` : job.current;
  jobRatio.textContent = `${job.done} / ${job.total}`;
  jobMeta.textContent = `开始于 ${fmtTime(job.started_at)}`;
}

function scheduleNextPoll() {
  stopPolling();
  pollTimer = setTimeout(poll, 1000);
}
function stopPolling() {
  if (pollTimer !== null) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

/* ---------------- 历史列表与报告 ---------------- */

/** 刷新历史列表；highlightId 存在时同时加载对应报告。 */
async function refreshRuns(highlightId = null) {
  try {
    const { runs } = await apiFetch("/api/eval/runs");
    runList.innerHTML = "";
    if (!runs.length) {
      runList.append(el("div", { class: "empty" }, "还没有评测记录"));
      if (!highlightId) reportBody.innerHTML = "";
      return;
    }
    for (const run of runs) {
      const status = run.status === "done" ? "ok" : "warn";
      const label = run.status === "done" ? "完成" : "中断/进行中";
      const item = el(
        "div",
        {
          class: `run-item${run.id === highlightId ? " active" : ""}`,
          "data-run-id": String(run.id), // 高亮定位用精确 id：文本 includes("run #1") 会命中 run #12
        },
        el(
          "div",
          { class: "r-meta" },
          el("span", { class: "r-name" }, `run #${run.id} · ${run.eval_set_name}`),
          el("span", { class: `pill ${status}` }, label)
        ),
        el("div", { class: "r-time" }, fmtTime(run.created_at))
      );
      item.addEventListener("click", () => openRun(run.id));
      runList.append(item);
    }
    if (highlightId !== null) await openRun(highlightId);
  } catch (err) {
    toast(`历史加载失败：${err.message}`, "error");
  }
}

/** 打开一次评测的详情：指标摘要卡 + 报告 markdown 渲染。 */
async function openRun(runId) {
  try {
    const { run } = await apiFetch(`/api/eval/runs/${runId}`);
    // 列表高亮同步
    for (const item of runList.children) item.classList.remove("active");
    // 精确匹配 data-run-id：文本子串匹配会让 run #1 命中 run #12（列表按 id 降序，
    // 高亮会落在错误的行上）
    const target = [...runList.children].find((item) => item.dataset.runId === String(runId));
    target?.classList.add("active");
    renderRunReport(reportHead, reportBody, run);
  } catch (err) {
    toast(`报告加载失败：${err.message}`, "error");
  }
}

/* ---------------- 启动 ---------------- */

startBtn.addEventListener("click", startEval);
synthBtn.addEventListener("click", startSynth);
initTopbar("eval").then((health) => void initOnboard(health));
initUpdateBadge(); // 应用更新进度（本页不打 check，只看本地状态）
refreshRuns();
loadBanks({ keepSelection: false });
// 恢复态：服务进程里可能已有 running 任务（页面刷新/409 跟随）
apiFetch("/api/eval/jobs/current")
  .then(({ job }) => {
    if (job && job.status === "running") {
      startBtn.disabled = true;
      poll();
    }
  })
  .catch(() => {});
// 出题任务同理：页面刷新后回到跟随进度（出题要跑几十次 LLM，最容易撞上刷新）
apiFetch("/api/eval/synthesize/status")
  .then(({ job }) => {
    if (job && job.status === "running") {
      synthBtn.disabled = true;
      showSynth(job);
      pollSynth();
    }
  })
  .catch(() => {});
