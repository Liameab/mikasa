/* =========================================================================
   自定义确认框（2026-09-10）：替换浏览器原生 confirm()。

   为什么不用原生 confirm：它是浏览器 chrome 的样式（白底、系统字体、
   按钮文案本地化由浏览器决定），在三页统一的暗色 UI 里像另一个程序弹的；
   且无法分级——"删除文档"和"删除空文件夹"看起来一模一样。

   形态：居中卡片 + 半透明遮罩，标题问句 + 次要说明（后果）分两级，
   危险操作用红色实心确认钮。交互：
     - Esc / 点遮罩 / "取消" → false；"确认" / Enter → true
     - 键盘事件在**捕获阶段**拦截并 stopPropagation：确认框是模态，
     Esc 只该关它自己，不能顺带把底下的阅读面板/设置面板一起收掉
     （那两个面板的 Esc 监听注册在冒泡阶段）。

   安全：文案全走 el() 的文本子节点，本模块不出现 innerHTML。
   ========================================================================= */

import { el } from "./common.js";

let current = null; // 单例：同一时刻至多一个确认框

/**
 * 弹出确认框，返回 Promise<boolean>（确认 true / 取消 false）。
 *
 * @param {object} opts
 * @param {string} opts.title   问句主体，如「删除文档《X》？」
 * @param {string} [opts.detail] 后果说明（次要文字，"不可恢复"之类）
 * @param {string} [opts.okText] 确认按钮文案
 * @param {string} [opts.cancelText] 取消按钮文案
 */
export function confirmDialog({ title, detail = "", okText = "确认删除", cancelText = "取消" } = {}) {
  // 已有对话框在等：先前那个按"取消"结算，避免悬挂的 Promise
  if (current) current.settle(false);

  return new Promise((resolve) => {
    let done = false;
    const settle = (ok) => {
      if (done) return;
      done = true;
      document.removeEventListener("keydown", onKey, true);
      backdrop.remove();
      current = null;
      resolve(ok);
    };

    const onKey = (ev) => {
      if (ev.key === "Escape") {
        ev.preventDefault();
        ev.stopPropagation();
        settle(false);
      } else if (ev.key === "Enter") {
        ev.preventDefault();
        ev.stopPropagation();
        settle(true);
      }
    };

    const cancelBtn = el("button", { class: "btn ghost", type: "button" }, cancelText);
    const okBtn = el("button", { class: "btn danger solid", type: "button" }, okText);
    cancelBtn.addEventListener("click", () => settle(false));
    okBtn.addEventListener("click", () => settle(true));

    const children = [el("div", { class: "cf-title" }, title)];
    if (detail) children.push(el("div", { class: "cf-detail" }, detail));
    children.push(el("div", { class: "cf-actions" }, cancelBtn, okBtn));

    const box = el(
      "div",
      { class: "confirm-box", role: "alertdialog", "aria-modal": "true" },
      ...children
    );
    // 点遮罩取消；点卡片内部不关（判 target 而非 closest，避免卡片内空白也算外）
    const backdrop = el("div", { class: "confirm-backdrop" }, box);
    backdrop.addEventListener("click", (ev) => {
      if (ev.target === backdrop) settle(false);
    });

    document.body.append(backdrop);
    okBtn.focus(); // 与原生 confirm 一致：焦点落在确认钮，回车即确认（Esc 可退）
    document.addEventListener("keydown", onKey, true);
    current = { settle };
  });
}
