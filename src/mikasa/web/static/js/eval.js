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

/* ---------------- 开始评测 ---------------- */

async function startEval() {
  startBtn.disabled = true;
  try {
    const body = await apiFetch("/api/eval/runs", { method: "POST" });
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
initTopbar("eval");
refreshRuns();
// 恢复态：服务进程里可能已有 running 任务（页面刷新/409 跟随）
apiFetch("/api/eval/jobs/current")
  .then(({ job }) => {
    if (job && job.status === "running") {
      startBtn.disabled = true;
      poll();
    }
  })
  .catch(() => {});
