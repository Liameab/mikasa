/* =========================================================================
   找论文页的右侧详情面板：看完整摘要与元数据，再决定导不导。

   为什么不用阅读器（js/reader.js）预览：阅读器走
   `/api/documents/{id}/content`，**只能读已入库的文档**；未导入的论文
   没有对应的文档行，所以这里自己渲染搜索响应里的字段。

   交互纪律：所有来自上游的字符串（标题/作者/摘要/期刊名）一律走 el()
   文本节点，不拼 HTML——它们是外部 API 的输入，是天然的注入入口。
   ========================================================================= */

import { el } from "./common.js";
import { fetchRelated } from "./papers-api.js";

const SOURCE_LABEL = { arxiv: "arXiv", openalex: "OpenAlex", core: "CORE", doaj: "DOAJ" };

/** 被引数：0 与"不知道"是两回事，文案必须分开。 */
export function citesText(paper) {
  if (paper.cited_by === null || paper.cited_by === undefined) return "被引数据：该来源不提供";
  return `被引 ${paper.cited_by} 次`;
}

/** 空态：没选中任何结果时的提示（点行开浏览器、详情走按钮，见 papers-page.js）。 */
export function renderEmpty(detail) {
  detail.replaceChildren(
    el("h2", null, "论文详情"),
    el(
      "div",
      { class: "empty" },
      "点结果行会在浏览器打开论文原页；点行里的「详情」，这里显示完整摘要与导入"
    ),
    el(
      "div",
      { class: "muted small", style: "text-align:center" },
      "导入的论文会直接进入知识库，可立刻对它提问"
    )
  );
}

/**
 * 引证关系区块（v0.1.4）：相关论文 / 引用了它。
 *
 * **懒加载**：点开才发请求——详情面板是"点一下就看"的东西，不该为每个
 * 结果预先付一次网络往返（这些免费源的往返本来就不便宜）。
 *
 * 诚实降级：桥不到 OpenAlex（没有 DOI / 中文刊 DOI 覆盖不全）时服务端回
 * 200 + note，这里显示一行灰字，**不编"相关论文"**。
 */
function relatedSection(paper, { onPick, onImport }) {
  const box = el("div", { class: "pd-related" });
  const list = el("div", { class: "pd-rel-list" });
  const status = el("div", { class: "pd-rel-status muted small" });
  const cache = {}; // kind → {results, note, total}
  let current = "";

  const render = (kind, data) => {
    list.replaceChildren();
    if (!data.results.length) {
      // 一条都没有：note 就是全部信息（"没有 DOI"/"OpenAlex 里没有这篇"…）
      status.textContent =
        data.note ||
        (kind === "cited" ? "暂时没有查到引用它的论文" : "暂时没有相关论文");
      return;
    }
    // 有结果时 note 是**来源说明**（如"同一主题：岩土工程与地下结构"），
    // 与"显示 N 篇"并存——用户该知道这批结果是怎么来的
    const count =
      kind === "cited" && data.total !== null
        ? `共 ${data.total.toLocaleString("zh-CN")} 篇引用，按被引排序显示前 ${data.results.length} 篇`
        : `显示 ${data.results.length} 篇`;
    status.textContent = data.note ? `${data.note} · ${count}` : count;
    for (const item of data.results) {
      const title = el("div", { class: "pd-rel-title" }, item.title);
      const meta = el(
        "div",
        { class: "pd-rel-meta muted small" },
        [
          item.authors && item.authors.length ? item.authors[0] + (item.authors.length > 1 ? " 等" : "") : "",
          item.year ? String(item.year) : "",
          item.venue || "",
          item.cited_by === null || item.cited_by === undefined ? "" : `被引 ${item.cited_by}`,
          item.in_library ? "已在库中" : "",
        ]
          .filter(Boolean)
          .join(" · ")
      );
      const row = el("div", { class: "pd-rel-item" }, title, meta);
      row.addEventListener("click", () => onPick(item));
      // 行内直接导入（能导入的才给按钮，与详情面板同一判据）
      if (!item.in_library && item.pdf_url) {
        const btn = el("button", { class: "btn ghost pd-rel-import", type: "button" }, "导入");
        btn.addEventListener("click", async (ev) => {
          ev.stopPropagation(); // 别顺带把整行点成"选中"
          btn.disabled = true;
          btn.textContent = "导入中…";
          await onImport(item, btn);
          btn.textContent = item.in_library ? "已导入" : "导入";
          btn.disabled = item.in_library;
        });
        row.append(btn);
      }
      list.append(row);
    }
  };

  const load = async (kind, button) => {
    current = kind;
    for (const b of box.querySelectorAll(".pd-rel-tab")) b.classList.toggle("on", b === button);
    if (cache[kind]) {
      render(kind, cache[kind]);
      return;
    }
    status.textContent = "正在查询…";
    list.replaceChildren();
    try {
      const data = await fetchRelated(paper.source, paper.id, kind);
      if (current !== kind) return; // 连点两个页签：只认最后一次
      cache[kind] = data;
      render(kind, data);
    } catch (err) {
      if (current === kind) status.textContent = `查询失败：${err.message}`;
    }
  };

  const tabRelated = el("button", { class: "pd-rel-tab", type: "button" }, "相关论文");
  const tabCited = el("button", { class: "pd-rel-tab", type: "button" }, "引用了它");
  const tabRefs = el("button", { class: "pd-rel-tab", type: "button" }, "参考文献");
  tabRelated.addEventListener("click", () => void load("related", tabRelated));
  tabCited.addEventListener("click", () => void load("cited", tabCited));
  tabRefs.addEventListener("click", () => void load("references", tabRefs));
  box.append(
    el("div", { class: "pd-rel-head" }, el("span", null, "更多"), tabRelated, tabCited, tabRefs),
    status,
    list
  );
  return box;
}

/**
 * 渲染一条结果的详情。
 * `onImport` 返回 Promise；导入成功后由调用方负责再调一次 renderDetail
 * 并把 paper.in_library 置真（详情面板只负责画，不持有状态）。
 */
export function renderDetail(detail, paper, { onImport, onPick }) {
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

  const children = [el("h2", null, "论文详情"), title, meta, abstract, actions];
  // 引证关系只在"这条不在库里"或"在库里也值得看更多"时都给——它本来就是
  // 用来继续找论文的，与导入状态无关
  if (onPick) children.push(relatedSection(paper, { onPick, onImport }));
  detail.replaceChildren(...children);
}
