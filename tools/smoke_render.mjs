#!/usr/bin/env node
/* =========================================================================
   答案渲染纯函数冒烟（2026-09-09 块级 markdown 化后补齐；不进 CI）。

   用法：node tools/smoke_render.mjs   （无需任何依赖，node ≥ 18）

   仓库无 package.json → static/js/common.js 对 node 是 CJS 而源码用 ESM
   export 语法。因此这里不走 import 相对路径，而是读源码原文、以
   data: URL 形式按 ESM 求值——断言跑的是浏览器同款字节，无第二份拷贝。
   common.js 顶层无 DOM 引用（工具函数都在函数体内），node 可直接求值。
   ========================================================================= */

import fs from "node:fs";

const source = fs.readFileSync(
  new URL("../src/mikasa/web/static/js/common.js", import.meta.url),
  "utf8"
);
const mod = await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));
const { renderAnswer, renderCitations, renderMarkdown, fmtLatency, fmtSeconds, errorMessage } = mod;

let passed = 0;
const assert = (cond, msg) => {
  if (!cond) {
    console.error(`✗ FAIL: ${msg}`);
    process.exit(1);
  }
  passed += 1;
};
const kb = (text, citations = []) => renderAnswer(text, citations, true); // kb：chip 开
const free = (text, citations = []) => renderAnswer(text, citations, false); // free：chip 关

/* ---- 管道表 ---- */
{
  const t = kb("| 模型 | 参数量 |\n| --- | --- |\n| qwen3 | 8B |\n| deepseek | 671B |");
  assert(t.includes('<div class="md-table-wrap"><table class="md-table">'), "表格有外层滚动容器");
  assert(t.includes("<thead><tr><th>模型</th><th>参数量</th></tr></thead>"), "表头为 th");
  assert(t.includes("<tbody>") && t.includes("<td>qwen3</td>") && t.includes("<td>671B</td>"), "表体行渲染");
  assert(!t.includes("| --- |") && !t.includes("| 模型 |"), "管道残迹消失（不再裸显）");
}
{
  // :对齐: 列映射（:--: 居中、--: 右；:--- 左不加样式）
  const t = free("| a | b | c |\n| :--- | :--: | ---: |\n| 1 | 2 | 3 |");
  assert(t.includes('<th>a</th>'), "左对齐列无内联样式");
  assert(t.includes('<th style="text-align:center">b</th>'), "居中列映射正确");
  assert(t.includes('<th style="text-align:right">c</th>'), "右对齐列映射正确");
}
{
  // 无分隔行退化：整表当正文（LLM 只写数据行不写表头骨架时）
  const t = free("| 甲 | 乙 |\n| 1 | 2 |");
  assert(!t.includes("<thead>"), "无分隔行不产生表头");
  assert(t.includes("<td>甲</td>") && t.includes("<td>1</td>"), "整表按 tbody 渲染");
}
{
  // 短行补齐到表头宽度（多出的格子保留，内容不丢）
  const t = free("| a | b | c |\n| --- | --- | --- |\n| 1 | 2 |");
  assert(t.includes("<tr><td>1</td><td>2</td><td></td></tr>"), "短行补空单元格");
}
{
  // 单元格行内：粗体 + 行内码 + kb 引用 chip 照常
  const c = [{ marker: 1, chunk_id: 11, document_title: "论文X", section: "", snippet: "" }];
  const t = kb("| 项 | 值 |\n| --- | --- |\n| 要点 | **粗** 与 `码` 与 [1] |", c);
  assert(t.includes('<td><strong>粗</strong> 与 <code>码</code> 与 <span class="cite" data-marker="1" data-chunk-id="11" title="《论文X》">[1]</span></td>'), "单元格行内渲染完整");
}
/* ---- 围栏代码 ---- */
{
  const t = free('```python\nprint("<x>")\n```');
  assert(t.includes('<div class="code-block">'), "代码块独立区域");
  assert(t.includes('<span class="code-lang">python</span>'), "语言角标");
  assert(t.includes('<button type="button" class="btn-copy" title="复制代码">复制</button>'), "复制按钮（常量文本）");
  assert(t.includes("&lt;x&gt;"), "代码内容已转义");
  assert(!t.includes("<p>"), "代码不进段落");
}
{
  // 代码内不做行内替换：** 与 [n] 保持字面；kb 模式下 [n] 也不变 chip
  const c = [{ marker: 2, chunk_id: 22, document_title: "d", section: "", snippet: "" }];
  const t = kb('```\n**bold** literal [2]\n```', c);
  assert(t.includes("**bold** literal [2]"), "代码内粗体/编号原样");
  assert(!t.includes('data-marker="2"') && !t.includes("data-chunk-id"), "代码内 [n] 不渲染成 chip");
}
{
  // 未闭合围栏（LLM 漏写 ```）：收到底兜底不丢代码
  const t = free("```\ndef f():\n    return 1\n剩下没有闭围栏");
  assert(t.includes("def f():") && !t.includes("<p>"), "未闭合围栏兜底收容");
}
{
  // 代码内容首尾多余空行剥掉，内部空行保留
  const t = free("```js\n\nconst a = 1;\n\nconst b = 2;\n\n```");
  assert(t.includes("const a = 1;\n\nconst b = 2;"), "内部空行保留");
  assert(!t.includes("\n</code></pre>"), "尾部空行剥除");
}
/* ---- 标题（#~#### → h3~h6） ---- */
{
  assert(free("# A").includes("<h3>A</h3>"), "# → h3");
  assert(free("## B").includes("<h4>B</h4>"), "## → h4");
  assert(free("### C").includes("<h5>C</h5>"), "### → h5");
  assert(free("#### D").includes("<h6>D</h6>"), "#### → h6");
  assert(free("### **粗标题**").includes("<h5><strong>粗标题</strong></h5>"), "标题行内加粗可用");
}
/* ---- 列表 ---- */
{
  const t = free("- 甲\n- **乙**");
  assert(t === "<ul><li>甲</li><li><strong>乙</strong></li></ul>", "无序列表两项");
  const t2 = free("1. 一\n2. 二");
  assert(t2 === "<ol><li>一</li><li>二</li></ol>", "有序列表去编号");
  const t3 = free("1) 一\n2) 二");
  assert(t3 === "<ol><li>一</li><li>二</li></ol>", "1) 形式也认");
}
{
  // 空行打断列表 → 两个列表
  const t = free("- 甲\n\n- 丙");
  assert((t.match(/<ul>/g) || []).length === 2, "空行打断列表");
}
{
  // 惰性续行折回当前项（markdown 语义，长项折行不散架）
  const t = free("- 第一行内容\n第二行继续写\n- 下一项");
  assert(t === "<ul><li>第一行内容 第二行继续写</li><li>下一项</li></ul>", "列表续行折叠");
}
{
  // 换列表类型 → 收口旧列表另起新列表
  const t = free("- a\n- b\n1. c");
  assert(t === "<ul><li>a</li><li>b</li></ul><ol><li>c</li></ol>", "类型切换另起列表");
}
/* ---- 引用块 / 分割线 ---- */
{
  const t = free("> 提示：注意**易错点**");
  assert(t.includes("<blockquote><p>提示：注意<strong>易错点</strong></p></blockquote>"), "引用块渲染");
  const t2 = free("上\n\n---\n\n下");
  assert(t2 === "<p>上</p><hr><p>下</p>", "分割线渲染");
}
/* ---- 段落与转义保真（旧版行为不回归） ---- */
{
  assert(free('含 <script>alert(1)</script>').includes("&lt;script&gt;"), "段落转义");
  assert(free("`a<b` 与 **粗**").includes("<code>a&lt;b</code> 与 <strong>粗</strong>"), "行内码/粗体转义次序");
  assert(free("第一行\n第二行").includes("<p>第一行 第二行</p>"), "软换行并入一段（旧语义）");
  assert(free("").trim() === "", "空文本零输出");
}
/* ---- 引用 chip 门控 ---- */
{
  const c = [{ marker: 3, chunk_id: 33, document_title: "d", section: "", snippet: "" }];
  assert(kb("见 [3] 与越界的 [9]。", c).includes('<span class="cite" data-marker="3" data-chunk-id="33" title="《d》">[3]</span>'), "kb：有引用渲染 chip");
  const bad = kb("越界的 [9]。", c);
  assert(bad.includes('class="cite bad"'), "kb：越界编号红标");
  assert(!bad.includes("data-chunk-id"), "越界红标不带 data-chunk-id（无引用可跳转）");
  assert(!free("[3] 无编号协议。", c).includes("class=\"cite"), "free：正文 [n] 原样不渲染");

  // 引用卡同样带锚点（跳转入口不只在 chip 上）
  const shelf = renderCitations(c);
  assert(shelf.includes('class="cite-card" id="cite-3" data-chunk-id="33"'), "引用卡带 data-chunk-id");
  assert(
    shelf.includes('<span class="pill">[3]</span>') && shelf.includes('<span class="c-title">d</span>'),
    "引用卡内容照常（加锚点不改渲染）"
  );
}
/* ---- 双语对照块（#8）：marker 必须落在粗体外才可点击 ---- */
{
  const c = [{ marker: 1, chunk_id: 44, document_title: "Helical Anchor Notes", section: "Behaviour", snippet: "" }];
  const block =
    "\n\n> **原文与译文对照**\n> \n> [1] **原文**（《Helical Anchor Notes》）\n" +
    "> Under cyclic loading the capacity degrades with load cycles.\n> \n" +
    "> [1] **中文翻译**\n> 循环荷载下螺旋锚的承载力随加载循环次数退化。";
  const t = kb("承载力逐渐下降 [1]。" + block, c);
  // 双语块挂专属类：整段是长文，需要长文可读样式（正文色 + 宽行高），
  // 不能沿用引用块的 --muted 弱化色（用户实测"字看不清"）
  assert(t.includes('<blockquote class="bilingual">'), "双语块渲染为 .bilingual 引用块");
  assert(
    free("> 提示：注意**易错点**").includes("<blockquote><p>"),
    "普通短引用不挂 .bilingual（两种语义分开）"
  );
  assert(t.includes("<strong>原文</strong>") && t.includes("<strong>中文翻译</strong>"), "双语块标签加粗");
  // 替换次序是 code → bold → 角标（common.js:206-209）：[n] 若写在 **…** 内
  // 会被 bold 那步 stash 掉变死文字——此行锁死"marker 在粗体外"的格式约定
  assert(t.includes('<span class="cite" data-marker="1" data-chunk-id="44"'), "双语块内角标可点击且可跳转");
  assert(!t.includes("cite bad"), "双语块内不产生越界红标（marker 取自真实引用）");
}
/* ---- 耗时摘要（2026-09-10 实测：等三四十秒要看得见时间） ---- */
{
  // 后端 generate 口径 = "检索起全程"（ask.py _finalize_latency 的 t0 在
  // 检索之前），检索/重排/译查询都落在它里——不减出来会把生成时间报大好几倍
  const lat = { retrieve: 628, rerank: 407, translate: 13826, generate: 16592, translate_answer: 1616 };
  const s = fmtLatency(lat, 33.1);
  assert(s.startsWith("用时 33.1s（"), "耗时摘要带前端墙钟总时长");
  assert(s.includes("生成 1.7s"), "生成时间 = generate 减去前置分段");
  assert(s.includes("译查询 13.8s") && s.includes("译对照 1.6s"), "两个翻译分段各自列出");
  assert(s.includes("检索 628ms") && s.includes("重排 407ms"), "检索/重排分段列出（<1s 用毫秒）");
  // free 轮只有 generate 一段（不减任何东西）
  assert(fmtLatency({ generate: 3200 }, 3.4) === "用时 3.4s（生成 3.2s）", "free 轮只显示生成");
  assert(fmtLatency(null) === "", "无延迟数据返回空串（回放旧消息不炸）");
  assert(fmtLatency({ generate: 500 }) === "生成 500ms", "无墙钟时不带'用时'前缀");
  // 50ms 下限：mock 下生成段只有几十毫秒，不该显示成"生成 0.0s"（真实页实测）
  assert(fmtLatency({ retrieve: 640, generate: 660 }, 0.7) === "用时 700ms（检索 640ms）", "过短分段不列");
  assert(fmtLatency({ generate: 30 }, 0.1) === "用时 100ms", "全部过短时只剩总时长");
  assert(
    fmtSeconds(0.66) === "660ms" && fmtSeconds(12.34) === "12.3s" && fmtSeconds(83) === "1分23秒",
    "时长格式（<1s 毫秒 / 秒 / ≥60s 转分）"
  );
}
/* ---- 表格与后续块的衔接 ---- */
{
  const t = free("| a | b |\n| --- | --- |\n| c | d\n- 列表项");
  assert(t.includes("<ul><li>列表项</li></ul>"), "表格后跟列表不被折行吞噬");
  assert(!t.includes("d - 列表项"), "列表标记行不折进表行");
}
/* ---- 错误文案（FastAPI 422 的 detail 是数组）---- */
{
  // 2026-09-11 修复：直接 String 数组会渲染成 "[object Object]"，
  // 用户看到"发送失败：[object Object]"完全不知道是长度超限
  const tooLong = [
    { loc: ["body", "question"], msg: "String should have at most 2000 characters", type: "x" },
  ];
  assert(
    errorMessage({ detail: tooLong }, '{"detail":[…] }', 422) ===
      "String should have at most 2000 characters",
    "422 数组 detail 取 msg"
  );
  assert(errorMessage({ detail: "会话不存在" }, null, 404) === "会话不存在", "字符串 detail 原样");
  assert(errorMessage({ error: { message: "上游超时" } }, null, 502) === "上游超时", "error.message 兜底");
  // 响应体不是 JSON（如 500 的 HTML 错误页）：只说状态，不把 HTML 糊给用户
  assert(errorMessage(null, "<html>…</html>", 500) === "请求失败（HTTP 500）", "非 JSON 不显示原文");
  assert(errorMessage({}, "纯文本错误", 500) === "纯文本错误", "JSON 无 detail 时回退原文");
  assert(errorMessage(null, null, 503) === "请求失败（HTTP 503）", "最终兜底带状态码");
  assert(errorMessage({ detail: [] }, null, 422) === "请求失败（HTTP 422）", "空数组不产出空文案");
}

/* ---- 评测报告 markdown（renderMarkdown，只服务 report.js）---- */
{
  const html = renderMarkdown(
    [
      "## 阶段A 检索层",
      "",
      "| 项目 | 值 |",
      "| --- | --- |",
      "| 黄金集 | mikasa-auto（题量 32），**自动生成**（fake-eval-1） |",
    ].join("\n")
  );
  assert(html.includes("<h4>阶段A 检索层</h4>"), "报告二级标题映射 h4");
  assert(html.includes("<thead><tr><th>项目</th><th>值</th></tr></thead>"), "报告表头为 th");
  // 2026-09-20 修：字符类写成 [\s:-|] 会被解析成 `:` 到 `|` 的范围，`-` 不在
  // 其中 → "| --- | --- |" 匹配不上，每张报告表都多渲染一行分隔符（E2E 截图实拍）
  assert(!html.includes("<td>---</td>"), "分隔行不当数据行渲染");
  assert(html.includes("<strong>自动生成</strong>"), "单元格里的 **粗体** 生效");
  assert(
    html.includes("<td>mikasa-auto（题量 32），<strong>自动生成</strong>（fake-eval-1）</td>"),
    "含粗体的单元格内容完整"
  );
}

/* ---- 公式渲染接线（KaTeX，2026-09-20）----
   node 里没有 KaTeX（那是浏览器脚本），所以这里塞一个假 KaTeX 只验**接线**：
   分隔符认不认、块级/行内分得清不清、代码里的 $ 会不会被误当公式、KaTeX
   缺席时会不会吞内容。真渲染由 KaTeX 自己负责（vendor 目录里的原版文件）。 */
{
  const fake = {
    calls: [],
    renderToString(tex, opts) {
      fake.calls.push({ tex, display: opts.displayMode });
      return `<katex-fake data-display="${opts.displayMode}">${tex}</katex-fake>`;
    },
  };
  globalThis.katex = fake;

  const block = free("$$e^x = 1 + \\frac{x^2}{2!} + o(x^n)$$");
  assert(
    block.includes('<katex-fake data-display="true">e^x = 1 + \\frac{x^2}{2!} + o(x^n)</katex-fake>'),
    "块级 $$…$$ 交给 KaTeX（displayMode=true）"
  );
  assert(fake.calls[0].tex === "e^x = 1 + \\frac{x^2}{2!} + o(x^n)", "块级公式内容原样传入（未转义）");

  const inline = free("收敛域是 $x=0$ 处展开。");
  assert(
    inline.includes('<katex-fake data-display="false">x=0</katex-fake>'),
    "行内 $…$ 交给 KaTeX（displayMode=false）"
  );
  assert(inline.startsWith("<p>收敛域是 ") && inline.endsWith("处展开。</p>"), "公式之外的文字保持段落");

  const brackets = free("\\[\\int_0^1 f(x)\\,dx\\] 与 \\(\\alpha\\) 两种写法");
  assert(brackets.includes("\\int_0^1 f(x)\\,dx"), "\\[…\\] 认作块级公式");
  assert(brackets.includes('<katex-fake data-display="false">\\alpha</katex-fake>'), "\\(…\\) 认作行内公式");

  // 正文里的 "<" 必须在抽公式**之前**（否则会以 &lt; 进 KaTeX，渲染出乱码）
  const lt = free("$$x < y$$");
  assert(fake.calls.at(-1).tex === "x < y", "公式里的 < 不被转义后再交给 KaTeX");

  const code = free("```sh\necho $HOME $1 + $2\n```");
  assert(!code.includes("katex-fake"), "围栏代码块里的 $ 不当公式");
  assert(code.includes("$HOME"), "代码内容原样保留");
  const incode = free("行内 `a $x$ b` 不是公式");
  assert(!incode.includes("katex-fake") && incode.includes("<code>a $x$ b</code>"), "行内代码里的 $ 不当公式");

  const money = free("价格 $5 到 $10 之间");
  assert(!money.includes("katex-fake"), "货币写法（$5 到 $10）不误渲染成公式");

  // 判据不能太严：第一版把区间/阶乘/小 o 全挡在门外，用户那条泰勒展开一次漏 8 处
  const intervals = free("收敛域 $(-1, 1]$、$[-1, 1]$、$(-\\infty, +\\infty)$");
  assert((intervals.match(/<katex-fake /g) || []).length === 3, "区间写法（含右闭端点）全部渲染");
  const factorials = free("分母是 $n!$ 或 $(2n+1)!$，不是 $2n+1$");
  assert((factorials.match(/<katex-fake /g) || []).length === 3, "阶乘与 2n+1 这类简单式也渲染");
  const smallO = free("余项写成 $o(x)$ 或 $o(x^n)$");
  assert((smallO.match(/<katex-fake /g) || []).length === 2, "小 o 余项渲染");
  // 中文夹钱数（**没有空格**，最像公式的那种写法）必须挡住
  const tightMoney = free("价格$5到$10之间");
  assert(!tightMoney.includes("katex-fake"), "无空格的中文钱数（$5到$10）不误渲染");
  const plainNumber = free("单价 $1000$ 元");
  assert(!plainNumber.includes("katex-fake"), "孤立金额（纯数字）不误渲染");

  const inList = free("- 指数函数 $$e^x$$ 收敛域 $(-\\infty, +\\infty)$");
  assert(inList.includes("<li>指数函数 ") && inList.includes('<katex-fake data-display="true">e^x</katex-fake>'), "列表项里的块级公式");
  assert(inList.includes("(-\\infty, +\\infty)"), "列表项里的行内公式");

  const inTable = free("| 式 | 收敛域 |\n| --- | --- |\n| $\\ln(1+x)$ | $(-1, 1]$ |");
  assert(inTable.includes('<katex-fake data-display="false">\\ln(1+x)</katex-fake>'), "表格单元格里的公式");
  assert(inTable.includes("<th>式</th>"), "含公式的表格结构不受影响");

  // kb 模式下公式与引用角标共存：公式先抽走，[n] 才不会被公式内容干扰
  const withCite = kb("由 $e^x$ 得 [1]。", [{ marker: 1, chunk_id: 9, document_title: "笔记", section: "" }]);
  assert(withCite.includes('<katex-fake data-display="false">e^x</katex-fake>'), "kb 模式公式渲染");
  assert(withCite.includes('data-marker="1"'), "公式与引用 chip 共存");

  delete globalThis.katex; // 模拟脚本缺失
  const noKatex = free("公式 $\\frac{a}{b}$ 原样");
  assert(!noKatex.includes("katex-fake") && noKatex.includes("\\frac{a}{b}"), "KaTeX 缺席时不吞内容（原样显示 LaTeX）");
}

console.log(`✓ smoke_render：${passed} 条断言全部通过`);
