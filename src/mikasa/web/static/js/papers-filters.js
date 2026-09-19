/* =========================================================================
   找论文页的筛选栏：按**来源能力**渲染与置灰（ADR-0020 的核心纪律）。

   为什么要有这个模块：各来源支持哪些筛选/排序只有它自己知道（CORE 的
   `yearFrom` 参数就是活例子——返回 200 但静默忽略）。前端若一律摆出选项，
   用户点了一个不生效的条件却看不出来，比"不支持"更坏。所以：

     - 来源列表与能力全部来自 `GET /api/papers/sources`，前端不硬编码；
     - 某个排序/筛选若**没有任何所选来源**支持 → 整项禁用 + 说明；
     - 部分支持（如 arXiv+CORE 按年份、OpenAlex 也能）→ 可用，但把
       "哪些来源不支持"写进说明，与服务端下发的 notes 互为呼应；
     - "只看开放获取"在所有所选来源都天然全 OA 时显示为**已勾选且禁用**
       （语义是"已满足"而不是"不支持"——两种情况的文案必须分开）。

   发表时间只留**日期选择器**（2026-09-20 用户要求，原来还有"不限/近3年/
   近5年/近10年"四个快捷档）：原生 `<input type="date">` 点开是日历、
   也能直接手输，快捷档是重复的一条路，去掉更干净。
   ========================================================================= */

import { $, el } from "./common.js";

const SORTS = [
  { key: "relevance", label: "相关度" },
  { key: "cited", label: "被引最多" },
  { key: "recent", label: "最新" },
];
const LANGS = [
  { key: "", label: "不限" },
  { key: "zh", label: "中文" },
  { key: "en", label: "英文" },
];

export function createFilters({ onChange }) {
  let catalog = [];
  let selected = new Set();
  let sort = "relevance";
  let lang = "";
  let ready = false;

  function selectedCaps() {
    return catalog.filter((s) => selected.has(s.name)).map((s) => s.caps);
  }

  /** 某项能力在"所选来源"里的支持情况：any / all / none，以及不支持的名单。 */
  function coverage(pred) {
    const caps = selectedCaps();
    const okList = catalog.filter((s) => selected.has(s.name) && pred(s.caps));
    const missing = catalog
      .filter((s) => selected.has(s.name) && !pred(s.caps))
      .map((s) => s.label);
    return { any: okList.length > 0, all: caps.length > 0 && missing.length === 0, missing };
  }

  function renderSources() {
    const box = $("#p-sources");
    box.replaceChildren(
      ...catalog.map((src) =>
        el(
          "label",
          { class: "p-check" },
          el("input", {
            type: "checkbox",
            checked: selected.has(src.name) ? "checked" : null,
            "data-source": src.name,
          }),
          el("span", null, src.label)
        )
      )
    );
  }

  function renderSorts() {
    // 只有"按被引排序"存在能力门槛（相关度/时间各源都有）
    const citedCov = coverage((c) => c.cited_sort);
    const box = $("#p-sorts");
    box.replaceChildren(
      ...SORTS.map((item) =>
        el(
          "button",
          {
            class: `btn ghost${sort === item.key ? " active" : ""}`,
            type: "button",
            "data-sort": item.key,
            disabled: item.key === "cited" && !citedCov.any ? "disabled" : null,
          },
          item.label
        )
      )
    );
    $("#p-sort-note").textContent = citedCov.any
      ? citedCov.missing.length
        ? `按被引排序：${citedCov.missing.join("、")} 不提供被引数据，其结果为相关度排序`
        : ""
      : "当前所选来源都不提供被引数据（换个来源或改用其他排序）";
  }

  function renderLangs() {
    const cov = coverage((c) => c.language);
    const box = $("#p-langs");
    box.replaceChildren(
      ...LANGS.map((item) =>
        el(
          "button",
          {
            class: `s-chip${lang === item.key ? " on" : ""}`,
            type: "button",
            "data-lang": item.key,
            disabled: !cov.any ? "disabled" : null,
          },
          item.label
        )
      )
    );
    $("#p-lang-note").textContent = cov.any
      ? cov.missing.length
        ? `${cov.missing.join("、")} 不支持语言过滤，其结果未按语言筛选`
        : ""
      : "当前所选来源都不支持语言过滤";
  }

  function renderYearAndOa() {
    const yearCov = coverage((c) => c.year);
    for (const id of ["#p-date-from", "#p-date-to"]) {
      $(id).disabled = !yearCov.any;
    }
    $("#p-year-note").textContent = yearCov.any
      ? yearCov.missing.length
        ? `${yearCov.missing.join("、")} 只能按年过滤，其结果为整年范围`
        : ""
      : "当前所选来源都不支持按时间过滤";

    const oaBox = $("#p-oa-only");
    const caps = selectedCaps();
    const allAlways = caps.length > 0 && caps.every((c) => c.oa === "always");
    const anyFilterable = caps.some((c) => c.oa === "filterable");
    oaBox.disabled = !anyFilterable;
    // "全都天然是 OA" = 已满足：勾上并禁用，文案说清是"已满足"不是"不支持"
    oaBox.checked = allAlways || (!anyFilterable ? false : oaBox.checked);
    $("#p-oa-note").textContent = allAlways
      ? "所选来源全部为开放获取，此条件已自动满足"
      : anyFilterable
        ? ""
        : "当前所选来源没有可过滤的开放获取标记";
  }

  function renderAll() {
    renderSorts();
    renderLangs();
    renderYearAndOa();
  }

  /** 组装请求体的 filters 片段：**只放用户真的设过的条件**。 */
  function readFilters() {
    const from = $("#p-date-from").value || null;
    const to = $("#p-date-to").value || null;
    const oa = $("#p-oa-only").checked && !$("#p-oa-only").disabled;
    const filters = { sort };
    if (from) filters.date_from = from;
    if (to) filters.date_to = to;
    if (oa) filters.oa_only = true;
    if (lang) filters.language = lang;
    return filters;
  }

  function readSources() {
    return selected.size === catalog.length ? null : [...selected];
  }

  function bind() {
    $("#p-sources").addEventListener("change", (ev) => {
      const box = ev.target.closest("input[data-source]");
      if (!box) return;
      if (box.checked) selected.add(box.dataset.source);
      else selected.delete(box.dataset.source);
      if (!selected.size) {
        // 一个来源都不选 = 搜不出东西：拒绝并回勾，别让用户对着空列表猜
        box.checked = true;
        selected.add(box.dataset.source);
        return;
      }
      renderAll();
      onChange({ reSearch: true });
    });
    $("#p-sorts").addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-sort]");
      if (!btn || btn.disabled) return;
      sort = btn.dataset.sort;
      renderAll();
      onChange({ reSearch: true });
    });
    $("#p-langs").addEventListener("click", (ev) => {
      const chip = ev.target.closest("button[data-lang]");
      if (!chip || chip.disabled) return;
      lang = chip.dataset.lang;
      renderAll();
      onChange({ reSearch: true });
    });
    // 日期框：改完（选日历或手输）就重查——原生 date 输入的日历弹层
    // 在选中/失焦时才派发 change，所以"点日历"和"打字"共用这一条路径
    for (const id of ["#p-date-from", "#p-date-to"]) {
      $(id).addEventListener("change", () => {
        renderAll();
        onChange({ reSearch: true });
      });
    }
    $("#p-oa-only").addEventListener("change", () => {
      renderYearAndOa();
      onChange({ reSearch: true });
    });
  }

  /** 装配：拉目录 → 默认全选 → 渲染（目录没拿到时禁用整个筛选栏）。 */
  async function init(loadCatalog) {
    try {
      catalog = await loadCatalog();
    } catch {
      $("#p-sources-note").textContent = "来源目录读取失败，筛选不可用";
      return;
    }
    selected = new Set(catalog.map((s) => s.name));
    renderSources();
    renderAll();
    bind();
    ready = true;
    onChange({ reSearch: false });
  }

  return {
    init,
    readFilters,
    readSources,
    isReady: () => ready,
    /** 当前选中的来源个数（翻页深度上限按它算，见 papers-page.js 的 pageCap） */
    sourceCount: () => selected.size,
    /** 排序是否可用（无被引数据时按被引排序整项禁用） */
    sortAvailable: () => coverage((c) => c.cited_sort).any,
  };
}
