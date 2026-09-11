/* =========================================================================
   评测报告渲染（eval 页右侧面板）。

   run 数据来自 GET /api/eval/runs/{id}，含两段：
   - run.metrics   —— 指标快照（decode 后的对象）：检索 recall/MRR、
                      生成层纪律、裁判统计——顶部做成摘要卡；
   - run.report_md —— render_report 的 markdown 报告：整体渲染在下。

   指标卡规则（演示重点 = 三大维度）：
     recall@10   检索层召回（阶段 A 口径，关重排）
     MRR         检索首位命中
     citation gold ratio  引用命中题面素材比例（阶段 B）
     拒答准确率  不可答题的正确拒答占比（阶段 B）
     裁判 1-5 分 语义裁判一致样本均值（阶段 C；无裁判显示"未启用"）

   数值取均值（mean），保留 3 位；指标不存在/无样本时显示 "—"。
   所有数字先过本地校验再显示；颜色分级：≥0.9 绿、≥0.7 黄、否则红
   （1-5 分制用 ≥4 / ≥3 档位）。
   ========================================================================= */

import { el, fmtTime, renderMarkdown } from "./common.js";

/** 取聚合统计的均值；结构缺失或 n=0 → null（显示 —）。 */
function meanOf(stats) {
  if (!stats || typeof stats !== "object" || !stats.n) return null;
  return stats.mean;
}

/** 0..1 比率着色档位；分制（5 分）用另一组阈值。 */
function tone(value, scale5 = false) {
  if (value === null) return "";
  const good = scale5 ? value >= 4.0 : value >= 0.9;
  const mid = scale5 ? value >= 3.0 : value >= 0.7;
  return good ? "good" : mid ? "mid" : "";
}

/** 数值文案：3 位小数或分制 2 位；null → "—"。 */
function fmtNum(value, scale5 = false) {
  if (value === null) return "—";
  return scale5 ? value.toFixed(2) : value.toFixed(3);
}

/**
 * 主入口：渲染摘要卡（head）+ 报告 markdown（body）。
 * 未完成（report_md 空 / incomplete）→ 说明性占位，不渲染空报告。
 */
export function renderRunReport(head, body, run) {
  const metrics = run.metrics || {};
  head.innerHTML = "";
  body.innerHTML = "";

  head.append(
    el(
      "h2",
      null,
      `评测报告 · run #${run.id} · ${run.eval_set_name}`,
      el("span", { class: "pill muted", style: "margin-left:10px;font-weight:400" },
        `${fmtTime(run.created_at)} · 语料指纹 ${run.corpus_sha256}…`)
    )
  );

  if (!run.report_md) {
    body.append(
      el(
        "div",
        { class: "empty" },
        run.status === "done"
          ? "该评测报告为空（异常落库）"
          : "本场评测未完成——任务中断或仍在运行中，完成后报告将自动出现"
      )
    );
    return;
  }

  // ---- 摘要卡：只读不做 HTML 插值，数字全部 fmtNum 后进入 textContent ----
  const stats = [
    {
      k: "recall@10（阶段A）",
      v: fmtNum(meanOf(metrics?.retrieval?.recall_at?.["10"])),
      cls: tone(meanOf(metrics?.retrieval?.recall_at?.["10"])),
    },
    {
      k: "MRR（阶段A）",
      v: fmtNum(meanOf(metrics?.retrieval?.mrr)),
      cls: tone(meanOf(metrics?.retrieval?.mrr)),
    },
    {
      k: "引用命中题面素材（阶段B）",
      v: fmtNum(meanOf(metrics?.generation?.citation_gold_ratio)),
      cls: tone(meanOf(metrics?.generation?.citation_gold_ratio)),
    },
    {
      k: "拒答准确率（阶段B）",
      v: fmtNum(metrics?.generation?.refusal_accuracy),
      cls: tone(metrics?.generation?.refusal_accuracy),
    },
  ];
  const judge = metrics?.judge || {};
  if (judge.judge_model && judge.judge_model !== "none") {
    const score = meanOf(judge.correctness);
    stats.push({
      k: `裁判评分 1-5（阶段C · ${judge.judge_model}）`,
      v: fmtNum(score, true),
      cls: tone(score, true),
    });
  } else {
    stats.push({ k: "语义裁判（阶段C）", v: "未启用", cls: "" });
  }
  const grid = el("div", { class: "stat-grid" });
  for (const s of stats) {
    grid.append(
      el("div", { class: "stat-card" }, el("div", { class: "k" }, s.k), el("div", { class: `v ${s.cls}` }, s.v))
    );
  }
  head.append(grid);

  // ---- 报告 markdown（renderMarkdown 内部已 esc 全部输入） ----
  const mdBox = el("div", { class: "md" });
  mdBox.innerHTML = renderMarkdown(run.report_md);
  body.append(mdBox);
}
