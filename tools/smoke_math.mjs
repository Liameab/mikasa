#!/usr/bin/env node
/* =========================================================================
   公式渲染冒烟：**用随包内置的那份真 KaTeX** 跑一遍（2026-09-20）。

   用法：node tools/smoke_math.mjs   （无需依赖，node ≥ 18）

   与 smoke_render.mjs 的分工：那边塞**假** KaTeX，只验"接线"（分隔符、代码块
   不误伤、缺席不吞内容）；这里用 vendor 目录里的**原版** katex.min.js，验"真能
   渲染"——KaTeX 换版、字体缺失、判据把真公式挡在门外，都会在这里现形。

   为什么不能用浏览器验：本项目无构建链、E2E 要拉 Chrome；而 KaTeX 的
   renderToString 是纯字符串函数（不碰 DOM），node 里就能跑出与浏览器同款的 HTML。
   ========================================================================= */

import fs from "node:fs";
import vm from "node:vm";

const root = new URL("../", import.meta.url);
const read = (rel) => fs.readFileSync(new URL(rel, root), "utf8");

// KaTeX 是 UMD 包：给它一个 CJS 环境（module/exports），它就会把 katex 挂在
// module.exports 上——这样 §"globalThis.katex" 拿到的就是浏览器里那个对象。
const katexContext = { module: { exports: {} }, exports: {} };
katexContext.exports = katexContext.module.exports;
vm.createContext(katexContext);
vm.runInContext(read("src/mikasa/web/static/vendor/katex/katex.min.js"), katexContext);
const katex = katexContext.module.exports;
if (!katex || typeof katex.renderToString !== "function") {
  console.error("✗ vendor/katex/katex.min.js 没导出 katex（文件损坏或被换掉了？）");
  process.exit(1);
}
globalThis.katex = katex;

const mod = await import(
  "data:text/javascript;base64," + Buffer.from(read("src/mikasa/web/static/js/common.js")).toString("base64")
);

let passed = 0;
const assert = (cond, msg) => {
  if (!cond) {
    console.error(`✗ FAIL: ${msg}`);
    process.exit(1);
  }
  passed += 1;
};

/** 渲染一段文本，返回 HTML 与"是否出现 KaTeX 的红色错误标记"。 */
const render = (text) => mod.renderAnswer(text, [], false);
const hasError = (html) => html.includes("katex-error") || html.includes('mathcolor="red"');

/* ---- 用户真实问过的那条回答里的公式（泰勒展开/收敛域/阶乘/余项）---- */
{
  const cases = [
    "$$e^x = 1 + x + \\frac{x^2}{2!} + \\cdots + \\frac{x^n}{n!} + o(x^n)$$",
    "$$\\ln(1+x) = x - \\frac{x^2}{2} + \\frac{x^3}{3} - \\cdots + (-1)^{n-1}\\frac{x^n}{n} + o(x^n)$$",
    "$$(1+x)^\\alpha = 1 + \\alpha x + \\frac{\\alpha(\\alpha-1)}{2!}x^2 + \\cdots$$",
    "$$\\frac{1}{\\sqrt{1+x}} = 1 - \\frac{x}{2} + \\frac{3x^2}{8} - \\frac{5x^3}{16} + o(x^3)$$",
    "收敛域 $(-1, 1]$，右端点收敛（交错调和级数）。",
    "分母是 $n!$ 或 $(2n+1)!$，不是 $2n+1$。",
    "由 $\\cfrac{1}{1+x}$ 换元 $x\\to x^2$ 得 $\\dfrac{1}{1+x^2}$。",
    "$$\\lim_{x\\to 0}\\frac{\\cos x - e^{-x^2/2}}{x^4} = -\\frac{1}{12}$$",
  ];
  for (const text of cases) {
    const html = render(text);
    assert(html.includes('class="katex"'), `未渲染成 KaTeX：${text.slice(0, 40)}…`);
    assert(!hasError(html), `KaTeX 报了渲染错误：${text.slice(0, 40)}…`);
  }
}

/* ---- 结构断言：块级/行内各归各位，定界符与命令不留残渣 ---- */
{
  const html = render("$$\\int_0^1 x^2\\,dx = \\frac{1}{3}$$ 而 $\\sqrt{2}$ 是无理数。");
  assert(html.includes('class="katex-display"'), "块级公式带 katex-display 容器");
  assert((html.match(/class="katex"/g) || []).length === 2, "一段里的两个公式各渲染一次");
  assert(!html.includes("$$"), "$$ 定界符不再裸显");
  // KaTeX 把原始 TeX 塞进 MathML 的 annotation（供读屏/复制），这是唯一该出现
  // 源码的地方——除此之外正文里不该再有 \frac 之类
  const outside = html.replace(/<annotation[\s\S]*?<\/annotation>/g, "");
  assert(!outside.includes("\\int_0^1") && !outside.includes("\\frac"), "annotation 之外没有残留的 LaTeX 命令");
}

/* ---- 代码块 / 行内代码里的 $ 一律不碰（与 smoke_render 的真 KaTeX 版复核）---- */
{
  const code = render("```sh\nawk '{print $1 + $2}'\n```");
  assert(!code.includes('class="katex"'), "围栏代码块里的 $ 不当公式");
  assert(code.includes("$1 + $2"), "代码内容原样保留");
}

/* ---- 中文与公式混排：不允许把中文当公式渲染（KaTeX 会红字报错）---- */
{
  const html = render("在 $x=0$ 处展开，得到 $\\text{对数函数}$ 的级数。");
  assert(!hasError(html), "\\text{} 里的中文不触发 KaTeX 错误");
  assert((html.match(/class="katex"/g) || []).length === 2, "混排时公式各自渲染");
}

console.log(`✓ smoke_math：${passed} 条断言全部通过（KaTeX ${katex.version}）`);
