/* =========================================================================
   找论文页的右侧详情面板：看完整摘要与元数据，再决定导不导。

   为什么不用阅读器（js/reader.js）预览：阅读器走
   `/api/documents/{id}/content`，**只能读已入库的文档**；未导入的论文
   没有对应的文档行，所以这里自己渲染搜索响应里的字段。

   交互纪律：所有来自上游的字符串（标题/作者/摘要/期刊名）一律走 el()
   文本节点，不拼 HTML——它们是外部 API 的输入，是天然的注入入口。
   ========================================================================= */

import { el } from "./common.js";

const SOURCE_LABEL = { arxiv: "arXiv", openalex: "OpenAlex", core: "CORE", doaj: "DOAJ" };

/** 被引数：0 与"不知道"是两回事，文案必须分开。 */
export function citesText(paper) {
  if (paper.cited_by === null || paper.cited_by === undefined) return "被引数据：该来源不提供";
  return `被引 ${paper.cited_by} 次`;
}

/** 空态：没选中任何结果时的提示。 */
export function renderEmpty(detail) {
  detail.replaceChildren(
    el("h2", null, "论文详情"),
    el("div", { class: "empty" }, "点左侧任意一条结果，这里显示完整摘要与元数据"),
    el(
      "div",
      { class: "muted small", style: "text-align:center" },
      "导入的论文会直接进入知识库，可立刻对它提问"
    )
  );
}

/**
 * 渲染一条结果的详情。
 * `onImport` 返回 Promise；导入成功后由调用方负责再调一次 renderDetail
 * 并把 paper.in_library 置真（详情面板只负责画，不持有状态）。
 */
export function renderDetail(detail, paper, { onImport }) {
  const title = paper.landing_url
    ? el(
        "a",
        {
          class: "pd-title",
          href: paper.landing_url,
          target: "_blank",
          rel: "noopener noreferrer",
        },
        paper.title
      )
    : el("div", { class: "pd-title" }, paper.title);

  const metaBits = [
    paper.authors && paper.authors.length ? paper.authors.join("、") : "作者未提供",
    paper.year ? String(paper.year) : "年份未知",
    paper.venue || "",
    paper.doi ? `DOI: ${paper.doi}` : "",
  ].filter(Boolean);

  const meta = el("div", { class: "pd-meta" });
  meta.append(...metaBits.map((text) => el("div", null, text)));
  meta.append(
    el(
      "div",
      null,
      el("span", { class: "paper-cites" }, citesText(paper)),
      " · ",
      SOURCE_LABEL[paper.source] || paper.source,
      // 三种情况要分开说：有直链 PDF / 开放获取但只有落地页（DOAJ 常态）/
      // 压根不是开放获取。混成一句会把"能自己去出版商页面拿"说成"没有"。
      paper.pdf_url ? " · 开放获取" : paper.oa ? " · 开放获取（该来源未提供直链 PDF）" : " · 无开放获取全文"
    )
  );

  const abstract = el(
    "div",
    { class: "pd-body" },
    paper.abstract || "（该来源没有提供摘要）"
  );

  const actions = el("div", { class: "pd-actions" });
  if (paper.in_library) {
    // 已在库：给出口而不是导入按钮（"导入"在这种情况下没有意义）
    actions.append(
      el("span", { class: "pill paper-inlib" }, "已在库中"),
      el("a", { class: "btn", href: "/" }, "去提问"),
      el("a", { class: "btn", href: "/documents" }, "去知识库")
    );
  } else {
    const btn = el("button", { class: "btn primary", type: "button" }, "导入知识库");
    if (!paper.pdf_url) {
      // 提前禁用：点了才吃 409 是更差的体验（ADR-0019 点 8 的先例）。
      // 禁用原因分两种——开放获取但只有落地页（DOAJ 收录的多数中文刊）、
      // 与根本不是开放获取。文案不同，用户才知道下一步该点哪儿。
      btn.disabled = true;
      btn.title = paper.oa
        ? "这篇是开放获取，但来源只给了文献页链接：点「打开原页」去出版商页面阅读或下载"
        : "该论文没有开放获取全文";
    }
    btn.addEventListener("click", () => void onImport(paper, btn));
    actions.append(btn);
    if (paper.landing_url) {
      actions.append(
        el(
          "a",
          { class: "btn ghost", href: paper.landing_url, target: "_blank", rel: "noopener noreferrer" },
          "打开原页"
        )
      );
    }
  }

  detail.replaceChildren(el("h2", null, "论文详情"), title, meta, abstract, actions);
}
