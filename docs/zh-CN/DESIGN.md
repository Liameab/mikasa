---
version: alpha
name: Mikasa-design-system
description: 一个暖米画布、编辑气质的本机 RAG 工作台界面，改编自 Anthropic Claude 官网那套设计系统（2026-09-19）。暖米画布承载每一页；唯一珊瑚强调色承担品牌、主行动与选中；暖青承担信息与引用；成功绿刻意**不**做品牌色。深色面只出现在产品露出"机芯"的地方——代码块与终端。人文无衬线正文配衬线标题（无 web font）、一套 4px 基数间距、五档圆角语法、固定 z-index 阶梯。为桌面窗口优先设计，全站只有一个响应式断点。每个颜色、圆角与浮层层级都是 style.css :root 里的 CSS 自定义属性——那个变量块才是本文档描述的事实源。

colors:
  bg: "#faf9f5"            # var(--bg)        页面画布——暖米，永不纯白
  bg-panel: "#f5f0e8"      # var(--bg-panel)  卡片、面板、气泡、顶栏（往里走一档）
  bg-inset: "#efe9de"      # var(--bg-inset)  输入区、用户气泡、芯片凹槽、行悬停
  bg-raised: "#ffffff"     # var(--bg-raised) 仅浮层面：模态、菜单、toast
  border: "#e6dfd8"        # var(--border)    1px 细线与控件描边
  text: "#141413"          # var(--text)      主文本（暖黑）
  muted: "#6c6a64"         # var(--muted)     次级文本、图标、提示
  accent: "#cc785c"        # var(--accent)    品牌 + 主行动 + 选中（珊瑚）
  accent-active: "#a9583e" # var(--accent-active) 按下的珊瑚
  teal: "#5db8a6"          # var(--teal)      信息 + 引用
  success: "#5db872"       # var(--success)   成功 + 健康——永不做品牌色
  warning: "#d4a017"       # var(--warning)   警示 + 注意
  danger: "#c64545"        # var(--danger)    危险 + 错误
  focus: "#cc785c"         # var(--focus)     键盘焦点圈（同品牌色）
  dark: "#181715"          # var(--dark)      仅代码块与终端
  dark-soft: "#1f1e1b"     # var(--dark-soft) 深色块内的代码头
  on-dark: "#faf9f5"       # var(--on-dark)   深色面上的文字（取画布同源色）
  on-dark-soft: "#a09d96"  # var(--on-dark-soft) 深色面上的次级文字
  chat-font: "14.5px"      # var(--chat-font) 用户可覆写的消息字号
  chat-bg: "transparent"   # var(--chat-bg)   用户可覆写的对话区底色
  chat-img: "none"         # var(--chat-img)  用户可覆写的对话区背景图

typography:
  fontFamily: '"Inter", "Segoe UI", "Microsoft YaHei", "PingFang SC", system-ui, sans-serif'
  display: 'Georgia, "Times New Roman", "Songti SC", "SimSun", "Source Han Serif SC", serif'
  mono: '"JetBrains Mono", Consolas, "Courier New", monospace'
  base: 15px               # body
  lineHeight: 1.65         # body；长文阅读放宽到 1.75–1.8
  brand:                   # 字标：衬线，永不加粗
    fontSize: 19px
    fontWeight: 400
    letterSpacing: 0.02em  # 只作用于拉丁——中文永不加字距
    fontFamily: '{typography.display}'
  page-title:              # .card h2、欢迎面板标题、更新弹窗标题
    fontSize: 17px
    fontWeight: 400
    fontFamily: '{typography.display}'
  card-title:              # 保持"界面"身份的卡片/弹窗标题
    fontSize: 16px
    fontWeight: 600
  section-label:           # .card h3 / 面板分组标题
    fontSize: 14px
    fontWeight: 500
  body:
    fontSize: 14px
    fontWeight: 400
  row:                     # 树行、表格单元、菜单项
    fontSize: 13px
    fontWeight: 400
  control:                 # 按钮、输入框
    fontSize: 13.5px
    fontWeight: 400
  row-strong:
    fontSize: 13px
    fontWeight: 600
  caption:
    fontSize: 12px
    fontWeight: 400
  caption-strong:
    fontSize: 12px
    fontWeight: 600
  micro:                   # 徽标、元信息行、代码语言标签
    fontSize: 11px
    fontWeight: 400
  stat-value:              # 衬线数字：指标读起来像杂志里的表格
    fontSize: 22px
    fontWeight: 400
    fontFamily: '{typography.display}'

rounded:
  control: 6px             # 按钮、输入框、菜单项、色板圆点
  row: 8px                 # 树行、气泡、卡内卡、toast
  card: 10px               # .card、设置面板、拖放区
  modal: 12px              # 确认框、用户气泡
  pill: 999px              # 胶囊、芯片、分段控件、进度条

spacing:                   # 基数 4px，粗体为结构性档位
  xxs: 4px
  xs: 6px
  sm: 8px
  md: 10px
  lg: 12px
  xl: 14px
  xxl: 16px
  layout: 18px
  section: 20px

z-index:
  topbar: 10
  to-bottom: 30
  settings-panel: 40
  reader-panel: 50
  ctx-menu: 60
  note-editor: 70
  confirm: 90
  update-dialog: 95
  toast: 100
  onboard: 200

components:
  topbar:
    backgroundColor: "{colors.bg-panel}"
    textColor: "{colors.text}"
    height: 54px
    borderBottom: "1px solid {colors.border}"
    z: "{z-index.topbar}"
  nav-link:
    backgroundColor: transparent
    textColor: "{colors.muted}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: 6px 14px
  nav-link-active:
    backgroundColor: "rgba(63, 185, 80, 0.12)"
    textColor: "{colors.green}"
    rounded: "{rounded.control}"
  card:
    backgroundColor: "{colors.bg-panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.card}"
    padding: 16px
    border: "1px solid {colors.border}"
  button:
    backgroundColor: "{colors.bg-inset}"
    textColor: "{colors.text}"
    typography: "{typography.control}"
    rounded: "7px"          # 漂移值——见文末「已知缺口」
    padding: 7px 16px
  button-primary:
    backgroundColor: "rgba(63, 185, 80, 0.16)"
    textColor: "{colors.green}"
    typography: "{typography.row-strong}"
    rounded: "7px"
    padding: 7px 16px
  button-primary-active:
    backgroundColor: "rgba(63, 185, 80, 0.28)"
    textColor: "{colors.green}"
    rounded: "7px"
  button-primary-disabled:
    backgroundColor: "{colors.bg-inset}"
    textColor: "{colors.muted}"
    rounded: "7px"
  button-danger:
    backgroundColor: transparent
    textColor: "{colors.red}"
    rounded: "7px"
  button-danger-solid:
    backgroundColor: "rgba(248, 81, 73, 0.16)"
    textColor: "{colors.red}"
    typography: "{typography.row-strong}"
    rounded: "7px"
  button-ghost:
    backgroundColor: transparent
    textColor: "{colors.text}"
    rounded: "7px"
  button-icon:
    backgroundColor: transparent
    textColor: "{colors.muted}"
    rounded: "{rounded.control}"
    padding: 4px 8px
    size: 15px
  text-input:
    backgroundColor: "{colors.bg-inset}"
    textColor: "{colors.text}"
    typography: "{typography.control}"
    rounded: "{rounded.control}"
    padding: 5px 9px
  text-input-focused:
    backgroundColor: "{colors.bg-inset}"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    border: "1px solid {colors.focus}"
  search-input:
    backgroundColor: "{colors.bg-inset}"
    textColor: "{colors.text}"
    typography: "{typography.row}"
    rounded: "7px"
    padding: 6px 10px
  pill:
    backgroundColor: transparent
    textColor: "{colors.muted}"
    typography: "{typography.caption}"
    rounded: "{rounded.pill}"
    padding: 4px 9px
    border: "1px solid {colors.border}"
  pill-ok:
    backgroundColor: "rgba(63, 185, 80, 0.08)"
    textColor: "{colors.green}"
    border: "1px solid rgba(63, 185, 80, 0.5)"
    rounded: "{rounded.pill}"
  pill-warn:
    backgroundColor: "rgba(210, 153, 34, 0.08)"
    textColor: "{colors.yellow}"
    border: "1px solid rgba(210, 153, 34, 0.5)"
    rounded: "{rounded.pill}"
  pill-bad:
    backgroundColor: "rgba(248, 81, 73, 0.08)"
    textColor: "{colors.red}"
    border: "1px solid rgba(248, 81, 73, 0.5)"
    rounded: "{rounded.pill}"
  chip:
    backgroundColor: "{colors.bg-inset}"
    textColor: "{colors.muted}"
    typography: "{typography.caption}"
    rounded: "{rounded.pill}"
    padding: 3px 10px
  chip-selected:
    backgroundColor: "rgba(63, 185, 80, 0.16)"
    textColor: "{colors.green}"
    typography: "{typography.caption-strong}"
    rounded: "{rounded.pill}"
  segmented-control:
    backgroundColor: "{colors.bg-inset}"
    textColor: "{colors.muted}"
    rounded: "{rounded.pill}"
    padding: 3px
    border: "1px solid {colors.border}"
  tree-row:
    backgroundColor: transparent
    textColor: "{colors.text}"
    typography: "{typography.row}"
    rounded: "{rounded.row}"
    padding: 6px 8px
  tree-row-selected:
    backgroundColor: "rgba(63, 185, 80, 0.1)"
    textColor: "{colors.text}"
    rounded: "{rounded.row}"
    border: "1px solid rgba(63, 185, 80, 0.4)"
  folder-row:
    backgroundColor: "rgba(63, 185, 80, 0.07)"
    textColor: "{colors.text}"
    typography: "{typography.row-strong}"
    rounded: "{rounded.row}"
  user-bubble:
    backgroundColor: "rgba(47, 129, 247, 0.18)"
    textColor: "{colors.text}"
    rounded: "{rounded.modal}"
    padding: 11px 14px
    border: "1px solid rgba(47, 129, 247, 0.35)"
  assistant-bubble:
    backgroundColor: "{colors.bg-panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.modal}"
    padding: 11px 14px
    border: "1px solid {colors.border}"
  cite-chip:
    backgroundColor: "rgba(57, 197, 207, 0.1)"
    textColor: "{colors.cyan}"
    typography: "{typography.micro}"
    rounded: "5px"
    padding: "1.5px 5px"
    border: "1px solid rgba(57, 197, 207, 0.55)"
  cite-card:
    backgroundColor: "{colors.bg-inset}"
    textColor: "{colors.text}"
    typography: "{typography.row}"
    rounded: "{rounded.row}"
    padding: 8px 12px
    borderLeft: "3px solid {colors.cyan}"
  code-block:
    backgroundColor: "{colors.bg-inset}"
    textColor: "{colors.text}"
    typography: "{typography.row}"
    rounded: "{rounded.row}"
    border: "1px solid {colors.border}"
  toast:
    backgroundColor: "{colors.bg-panel}"
    textColor: "{colors.muted}"
    typography: "13.5px"
    rounded: "{rounded.row}"
    padding: 10px 14px
    z: "{z-index.toast}"
  ctx-menu:
    backgroundColor: "{colors.bg-panel}"
    textColor: "{colors.text}"
    typography: "{typography.row}"
    rounded: "{rounded.row}"
    padding: 4px
    z: "{z-index.ctx-menu}"
  confirm-dialog:
    backgroundColor: "{colors.bg-panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.modal}"
    padding: 18px 20px 16px
    z: "{z-index.confirm}"
  settings-panel:
    backgroundColor: "{colors.bg-panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.card}"
    padding: 0
    width: 440px
    z: "{z-index.settings-panel}"
  empty-state:
    backgroundColor: transparent
    textColor: "{colors.muted}"
    typography: "{typography.body}"
    padding: 34px 10px
  dropzone:
    backgroundColor: transparent
    textColor: "{colors.muted}"
    rounded: "{rounded.card}"
    padding: 22px
    border: "1.5px dashed {colors.border}"
  stat-card:
    backgroundColor: "{colors.bg-inset}"
    textColor: "{colors.text}"
    rounded: "{rounded.row}"
    padding: 10px 12px
    border: "1px solid {colors.border}"
  progress-bar:
    backgroundColor: "{colors.bg-inset}"
    rounded: "{rounded.pill}"
    height: 8px
---

## 总览

Mikasa 的界面是一个**紧凑、暗色、单用户的桌面工作台**：四页（问答 / 知识库 / 找论文 / 评测）装在
一个窗口里，用鼠标与键盘操作，大量时间离线运行。

- **桌面窗口优先**：整套样式是按"打包成桌面应用"和"本机浏览器标签页"来写的，不是给手机的。
  全表**只有一条**响应式规则（`@media (max-width: 900px)` 收窄阅读面板）。
- **密度就是目的**：三个字号（11/13/14）承载了几乎全部界面文本，行高 26–34px，语料树 /
  消息列表 / 表格都以"一屏看到更多"为目标。这与营销页的意图正好相反——所以本文档的格式
  参考（Vercel 系产品页的分析）里那套尺度是**重推**的，不是照抄的。
- **一个强调色、三种含义——刻意如此**：绿色同时表示*品牌*、*选中*与*成功*。这是一次权衡：
  单用户工具没有营销装饰，"这就是当前的 / 这是对的"本就是同一个意思，再加第二个强调色
  只会变成噪声。青色、琥珀、红各自只承担一个语义族（信息+引用 / 警示+注意 / 危险）。
- **没有 web font、没有图标字体、没有构建链**：字体栈就是系统 UI 字体（`Segoe UI` /
  `Microsoft YaHei` / `PingFang SC` / `system-ui`），图标是 Unicode 字符
  （`⋯ ✕ ▸ ▾ ＋ ◌ ▌`），一次完整重渲染就是刷新页面。
- **每个 token 都是 CSS 自定义属性**，写在 `src/mikasa/web/static/css/style.css` 顶部的
  `:root` 块里。**那个块——而不是本文——才是事实源**；本文档存在的意义是让新面板从它
  *推导*出来，而不是自己发明第四种灰。

**关键特征：**

- 暖米画布（`{colors.bg}` #faf9f5）配三级表面：画布 → 面板 → 凹槽，外加只给浮层用的纯白。
  层次靠换底色表达，不靠阴影。**这块米色就是品牌**——换纯白就成了任何一个 AI 工具
  （2026-09-19：用户拿着 Anthropic 自己的设计系统来要这个气质）。
- 单一珊瑚强调色（`{colors.accent}` #cc785c）承担品牌、主行动与选中；着色一律是该 token 自身
  RGB 走固定 alpha 阶梯。珊瑚只"铺开"一次——主按钮填充；其余位置一律是洗色。
- `{colors.success}`（#5db872）与品牌色**分开**：绿只说"健康/完成"（顶栏状态点、`.pill.ok`），
  从不说"这是 Mikasa"。
- 中文优先的排版：**永不用负字距**（汉字按方块字格设计）；全仓仅有的两处 `letter-spacing`
  都是正值，且只作用于拉丁字标。
- 紧凑字阶：11/12/13/14/15/16/20/22px；正文 15px，阅读面 14px/1.8。
- 五档圆角：6（控件）/ 8（行）/ 10（卡片）/ 12（模态）/ 999（胶囊）。
- 浮层顺序是**固定阶梯**，不是随手写数字：顶栏 10 → 回底 30 → 设置面板 40 → 阅读器 50 →
  菜单 60 → 确认框 90 → 更新弹窗 95 → toast 100 → 首启引导 200。
- 交互状态是语言的一部分（hover / active / disabled / focus-visible / selected / drag-over /
  高亮）——应用内界面与营销页不同，**状态本身就是设计**。

## 配色

### 表面

| Token | 十六进制 | 用途 |
| --- | --- | --- |
| `{colors.bg}` | #faf9f5 | 页面画布（body 底色）。暖米，是这套系统的签名。 |
| `{colors.bg-panel}` | #f5f0e8 | 卡片、对话气泡、顶栏、侧栏。往表面里走一档。 |
| `{colors.bg-inset}` | #efe9de | 输入框、用户气泡、芯片凹槽、行悬停。往里走两档——凹进去的。 |
| `{colors.bg-raised}` | #ffffff | 只给浮层：模态、菜单、toast、阅读面板。 |
| `{colors.border}` | #e6dfd8 | 唯一的细线色：卡片轮廓、控件描边、分隔线、滚动条滑块。 |
| `{colors.dark}` | #181715 | 代码块与终端——产品的"机芯"，永不做页面底色。 |

**层次规则**：层次靠换底色（画布 `bg` → 凹进 `bg-panel` → 更凹 `bg-inset`）、着色，以及
（真正浮起来的东西）白色 `bg-raised`。面板**永不**用阴影假装抬高——阴影只属于*浮在页面上方*
的东西（菜单、确认框、toast、面板），见「高度与层次」。

### 强调色与语义色

| Token | 十六进制 | 表示 | 永不表示 |
| --- | --- | --- | --- |
| `{colors.accent}` | #cc785c | 品牌、选中/当前、主行动 | 成功、危险、纯信息 |
| `{colors.teal}` | #5db8a6 | 信息、引用、参考文献、"回到底部" | 成功、选中 |
| `{colors.success}` | #5db872 | 成功、健康、"已在库中" | 品牌、选中 |
| `{colors.warning}` | #d4a017 | 警示、注意、正文引用块、页面高亮 | 错误 |
| `{colors.danger}` | #c64545 | 错误、危险、破坏性确认、越界引用 | 警示 |
| `{colors.focus}` | #cc785c | 键盘焦点圈（`:focus-visible`、输入框聚焦描边） | 装饰 |

**焦点就是品牌色本身**——原来的焦点蓝 #2f81f7 在 2026-09-19 并进了珊瑚，于是旧暗色系统里
那个"必须解释的例外"不再需要存在。

### alpha 阶梯（着色）

所有着色一律 `rgba(<token RGB>, α)`，绝不新挑一个十六进制。在用到的 RGB 三元组：
珊瑚 `204, 120, 92` · 暖青 `93, 184, 166` · 成功 `93, 184, 114` · 警示 `212, 160, 23` ·
高亮琥珀 `232, 165, 90` · 危险 `198, 69, 69` · 墨 `20, 20, 19`（表格与代码头的悬停洗色，
0.02–0.03）· 遮罩 `24, 23, 21`。

预期的阶梯，以及承载 90% 界面的六档：

| 档 | α | 用途 |
| --- | --- | --- |
| wash 洗色 | 0.05–0.08 | 大面积的段落底色：文件夹行、拖放区悬停、引用块底、徽标底 |
| tint 着色 | 0.10–0.16 | 选中行、导航当前项、主按钮填充、芯片选中 |
| press 按下 | 0.25–0.30 | 在 tint 之上的悬停/按下档（按钮、引用芯片、危险实心钮） |
| edge 描边 | 0.45–0.55 | 着色物的边框（徽标边、引用芯片边、选中行边） |
| ring 光圈 | 0.18–0.35 | 焦点/选中的轮廓光（`outline` / `box-shadow`） |
| glow 辉光 | 0.8 | 顶栏那个唯一的状态点光晕 |

## 排版

### 字族

一套字体栈，零下载：

```
"Inter", "Segoe UI", "Microsoft YaHei", "PingFang SC", system-ui, sans-serif      /* 界面 + 正文 */
Georgia, "Times New Roman", "Songti SC", "SimSun", "Source Han Serif SC", serif  /* 标题 */
"JetBrains Mono", Consolas, "Courier New", monospace                             /* 代码 */
```

衬线/无衬线的分工就是这套系统的编辑口吻：**衬线给字标、页面标题、欢迎与弹窗标题、指标数字；
无衬线给一切"用户要动手操作"的东西**。拉丁衬线落到 Georgia，中文衬线落到宋体族
（Songti SC / SimSun）——那是 Windows 机器上唯一稳定可用的衬线。正文拉丁落到 Inter 再到
Segoe UI，中文落到微软雅黑或苹方。所有控件都写了 `font: inherit`，避免表单元素回退到
浏览器默认的 13.3px Times。

### 层级

| Token | 字号 | 字重 | 用途 |
| --- | --- | --- | --- |
| `{typography.brand}` | 19px | 400 | 仅 `Mikasa` 字标（衬线；+0.02em 字距，只作用于拉丁）。 |
| `{typography.page-title}` | 17px | 400 | 页面级标题（衬线）、欢迎面板与更新弹窗标题。 |
| `{typography.card-title}` | 16px | 600 | `.card h2`、气泡 `h3`、确认框标题。 |
| `{typography.body}` | 14px | 400 | 默认正文：表格、菜单、评测报告、阅读正文。 |
| `.settings-panel .s-group` | 12px | 600 | 设置面板分组标签，青色，+0.04em 字距（拉丁式标签口吻）。 |
| `{typography.section-label}` | 14px | 500 | `.card h3`、表头——弱化，不与内容抢焦点。 |
| `{typography.control}` | 13.5px | 400 | 输入框、toast 文本。 |
| `{typography.row-strong}` | 13px | 600 | 树行名、主按钮、选中/当前强调。 |
| `{typography.row}` | 13px | 400 | 树行、表格单元、菜单项、引用卡。 |
| `{typography.caption}` | 12px | 400 | `.small`、`.pill`、提示、元信息行。 |
| chip / 模式标签 | 12.5px | 400 | `.s-chip`、`.mode-btn`、`.to-bottom`——处于 caption 与 row 之间的半档。 |
| `{typography.micro}` | 11px | 400 | 徽标、`.s-meta`、代码语言标签、消息署名。 |
| `{typography.stat-value}` | 22px | 400 | 评测指标数值（衬线 + `tabular-nums`）。 |

对话区与阅读面的正文字号可由用户覆写（`--chat-font`，默认 14.5px）——这是一个**用户在运行期
拥有**的 token，由设置面板写入。

### 原则

1. **中文优先：永不使用负字距。** Claude 那套系统的衬线标题带负字距（-0.3 至 -1.5px），
   这一条**刻意没有搬过来**：我们的标题多是中文，而挤压按方块字格设计的字形是拉丁习惯。
   全仓的 `letter-spacing` 保持小而正（拉丁字标 `0.02em`、面板分组标签 `0.04em`）。
2. **四个字重，各有各的活**：400 正文 · 500 标签与安静的标题 · 600 强调、标题、选中态 ·
   700 只给字标与指标数值。
3. **需要对比的数字一律 `font-variant-numeric: tabular-nums`**（指标数值、数值列），让列对得齐。
4. **长文阅读要放开**：气泡保持 1.65，但阅读视图正文与双语块放宽到 1.75–1.8——这两个值是
   用户反馈（"字发灰、看着挤"）定下来的，不是审美偏好。
5. **元信息弱化，但不小于可读下限**：元信息行是 `--muted` 的 11–12px；低于 11px 不属于本系统。

## 布局与密度

### 间距

基数 **4px**，工作档位为 6/8/10/12/14/16/18/20。这套尺度是**经验值**——从页面实际用法里抄录
下来的，诚实的规则是：

- **控件内部**：纵向 4–8px、横向 8–16px（按钮 7×16、输入框 5×9、芯片 3×10、徽标 4×9）。
- **同级控件之间**：6–8px（`gap` 最常用 6px，其次 8px）。
- **卡片内部**：14–18px（`.card` 16px、`.msg-scroll` 18px、`.s-body` 10–12px）。
- **卡片/布局行之间**：14px（`main` gap、`.qa-layout` gap、`.kb-main` gap）。
- **浮层内边距**：遮罩四周 20px，弹窗内部 16–20px。

如果某个值不在这套尺度上，优先靠到最近的档位，而不是加一个新数字——文末「已知缺口」里的
漂移清单就是跳过这条规则的后果。

### 布局

- **`main`** 是纵向 flex 列，18px 内边距、14px 间距；三页都填进它。
- **问答 / 知识库**：两栏网格，`290px` 侧栏 + 内容区（14px 间距）。知识库页复用问答页同一套
  侧栏组件（`kb-tree` 与会话树刻意共用一套行语言）。
- **评测**：`300px` 运行列表 + 报告，同样 14px 间距。
- **阅读面钉住高度、内容在内部滚动**：`#reading-card` 上限 `calc(100vh - 90px)`，正文面板
  自己滚——顶部（标签、缩放）与底部状态栏因此常驻。这是一次真实 bug 修复（2026-09-11）：
  不钉住的话，长 PDF 会让**整个页面**滚，头尾两条都消失。

### 密度哲学

界面应该像一个**知识文件管理器**，而不是落地页：行高 26–34px，标题下只留一行元信息，
用 `⋯` 菜单代替常驻工具条，能悬停显现的就不常驻。留白用来**分组**，不用来制造戏剧性；
`.empty` 空态（纵向 34px、居中、弱化、带 `◌` 前缀）是全站唯一刻意"喘口气"的地方。

## 高度与层次

| 层级 | 处理 | 用途 |
| --- | --- | --- |
| 平铺 | 无阴影 | 页面、卡片、行、气泡、表格——全部在流内的界面 |
| 浮层遮罩 | `rgba(0, 0, 0, 0.55)` | 确认框、更新弹窗、首启引导 |
| 菜单 / toast | `0 6px 18px rgba(0,0,0,0.4)` · `0 8px 28px rgba(0,0,0,0.5)` | `#ctx-menu`、toast、设置面板（`0 8px 30px`） |
| 对话框 | `0 18px 48px rgba(0, 0, 0, 0.55)` | 确认框（阅读面板用 `0 18px 60px`） |
| 状态辉光 | `box-shadow: 0 0 6px rgba(63,185,80,0.8)` | 顶栏状态点，全站仅此一处 |
| 高亮 | `outline: 2px solid` / `box-shadow: 0 0 0 1px` | `.cite.lit`、`.rd-chunk.lit`、焦点圈 |

**规则**：阴影属于浮在页面上方的物件。卡片、行、按钮**永不**靠阴影假装抬高——要用就用
inset/panel 的底色档位。

z-index 阶梯是这套系统的一部分，不许临时发明：

```
10  顶栏（sticky）        40  设置面板        90  确认框遮罩
30  回到底部按钮         50  阅读面板         95  更新弹窗
                         60  ⋯ 菜单         100  toast
                         70  笔记编辑器     200  首启引导遮罩（盖住一切）
```

笔记编辑器刻意夹在 ⋯ 菜单与确认框之间：「放弃未保存的修改？」必须能盖住它正在问的
那个编辑器。

更新弹窗刻意夹在确认框与 toast 之间：它是启动时的模态（要盖住应用自己打开的一切），
但「这个版本不再提示」的那颗 toast 必须能浮在它上面。

toast 刻意压在确认框之上（动作结果要在弹窗关掉后仍看得见）；首启引导盖住一切，因为它
是产品的第一句话。`#toast` 容器自身 `pointer-events: none`、子元素可点——落在面板头部上方
也不会挡住下面的 ✕。

## 形状

| 档 | 值 | 用途 |
| --- | --- | --- |
| control | 6px | 按钮、输入框、菜单项、色板圆点、`.icon-btn`、`.t-more` |
| row | 8px | 树行、气泡、toast、指标卡、代码块、引用卡、导航式容器 |
| card | 10px | `.card`、设置面板、拖放区、提问条的底部两角 |
| modal | 12px | 确认框、用户消息气泡、浮动阅读面板 |
| pill | 999px | 徽标、芯片、分段控件、进度槽/条、模式按钮 |
| circle | 50% | 文件夹开合环、夹内会话环、状态点 |

有两个形状是**语义性**的，不是风格：**绿环**（`border: 2px solid var(--green)` +
`border-radius: 50%`）标文件夹的开合箭头，**琥珀环**标"住在文件夹里"的会话。它们是同一套
"圈"语言、两种颜色，区分是有承载的——不要改这两个颜色。

## 状态

| 状态 | 处理 |
| --- | --- |
| hover（行 / 菜单项） | 底色 → `{colors.bg-inset}` |
| hover（普通按钮） | 描边 → `{colors.muted}` |
| hover（主按钮） | 填充着色 0.16 → 0.28 |
| hover（危险按钮） | 描边与文字变红，填充红 0.08 |
| active / 当前 | 绿色着色 0.10–0.16 + 绿描边或绿字（导航、会话行、运行项、标签、模式钮） |
| disabled | `opacity: 0.45`（输入框 0.55）、`cursor: not-allowed`；禁用的*主*按钮**完全褪掉绿色**，回到中性控件外观 |
| focus | `:focus-visible` → `outline: 2px solid var(--focus)`；输入框 → `border-color: var(--focus)` |
| 拖拽源 | `opacity: 0.45` |
| 拖放目标 | 绿色着色 0.14 + 绿描边 |
| 高亮（引用 / 阅读块） | 琥珀着色、1px 琥珀光圈，或 `outline: 2px solid var(--yellow)` |
| 流式中 | 气泡描边 → 青色；绿色 `▌` 光标闪烁 |
| 忙碌 / 加载中 | 文本用 `--muted`（"正在…"），进度条绿→青渐变 |

## 组件

布局：`.qa-layout` · `.kb-main` · `.eval-layout` · `.session-pane` · `.chat-pane` ·
`.msg-scroll`（消息滚动区，用户可覆写底色/背景图）

顶栏：`.topbar` · `.brand` · `nav.main a`（`.active`）· `.health-pill`（`.dot`）·
`.upd-pill`（顶栏的更新进度胶囊，`.ok` / `.warn`）

卡片：`.card`（`h2` 标题 / `h3` 节标签）· `.stat-card`（`.k` 标签 `.v` 数值）· `#reading-card`

按钮：`.btn`（默认描边）· `.btn.primary`（绿系主行动）· `.btn.ghost`（透明底）·
`.btn.danger` / `.btn.danger.solid`（危险，实心用于确认框）· `.icon-btn`（方角图标钮）·
`.btn-copy`（代码块复制）· `.mode-btn`（胶囊模式切换）

输入：`.kb-search`（查找框）· `.tree-input`（行内改名/新建）· `.composer textarea`（提问框）·
`#s-nick` / `#s-base-url` / `#s-model` / `#s-api-key`（设置面板输入）· `.rd-page-jump input`（页码）

选择：`.pill`（`.ok` / `.warn` / `.bad`）· `.s-chip`（`.on`）· `.rd-tabs`（胶囊分段）·
`.s-sw`（色板圆点，`.on` / `.clear`）

树：`.t-row` · `.t-folder` · `.t-caret`（绿圆环）· `.t-name` · `.t-more`（悬停显现的 ⋯）·
`.session-item`（`.active` / `.in-folder` 琥珀圈）· `.t-empty` · `.tree-edit-row`

对话：`.msg.user` / `.msg.assistant` · `.bubble` · `.who` · `.cite`（`.lit` / `.bad`）·
`.ref-shelf .cite-card`（`.c-head` / `.c-title` / `.c-snippet`）· `.code-block`（`.code-head` /
`.code-lang`）· `.md-table` · `blockquote.bilingual`（原文+译文块）· `.composer`（`.mode-bar` /
`.composer-row` / `.hint`）

浮层：`#ctx-menu`（`.ctx-item` / `.ctx-head` / `.ctx-sep`）· `.confirm-backdrop` /
`.confirm-box`（`.cf-title` / `.cf-detail` / `.cf-actions`）· `#toast` / `.toast-msg`（`.ok` /
`.warn` / `.error`）· `.settings-panel`（`.s-head` / `.s-body` / `.s-row` / `.s-name` /
`.s-hint` / `.s-group`）· `.onboard-mask`（`.onboard-card` / `.ob-step`）· `.upd-backdrop` /
`.upd-card`（`.upd-head` / `.upd-sub` / `.upd-notes` / `.upd-progress` / `.upd-bar` /
`.upd-progress-text` / `.upd-error` / `.upd-actions`）

评测：`.run-list` / `.run-item`（`.active`）· `.progress-track` / `.progress-bar` ·
`.md`（报告渲染）· `.stat-grid`

知识库：`.dropzone`（`.drag`）· `.doc-item`（`.meta-bad` / `.meta-busy`）· `.dd-grid` ·
`.dd-open`

找论文页（ADR-0020）：`.papers-layout`（三栏网格）· 筛选栏 `.p-filter` / `.p-filter-label` /
`.p-check` / `.p-year` / `.p-note` / `.p-sorts` · 结果 `.paper-form` / `.paper-bar` /
`.paper-results` / `.paper-item`（`.on` = 选中）/ `.paper-head` / `.paper-title` / `.paper-src` /
`.paper-meta` / `.paper-cites` / `.paper-snippet` / `.paper-inlib` ·
`.paper-actions` / `.paper-status` / `.paper-key-row` · 详情
`.pd-title` / `.pd-meta` / `.pd-body` / `.pd-actions` / `.pd-exits`

找论文页补充（v0.1.4）：`.paper-history`（`.ph-chip` / `.ph-clear`）· `.pd-related` / `.pd-rel-head` / `.pd-rel-tab`（`.on`）/ `.pd-rel-status` / `.pd-rel-list` / `.pd-rel-item` / `.pd-rel-title` / `.pd-rel-meta` / `.pd-rel-import`
找论文页翻页（2026-09-20）：`.paper-pager` / `.pg-nums` / `.pg-num`（`.on` = 当前页）/ `.pg-gap` / `.pg-jump` · `.paper-detail-btn`（行内「详情」按钮；点整行本身是在浏览器打开论文原页）

笔记编辑器（M6 ①）：`.note-backdrop` · `.note-box` / `.note-head` / `.note-title` ·
`.note-split` / `.note-input` / `.note-preview` · `.note-foot` / `.note-folder-wrap` /
`.note-folder` / `.note-hint`

阅读器：`.reader-panel` / `.reader-inline` · `.rd-head`（`.rd-title` / `.rd-tabs` / `.rd-zoom`）·
`.rd-body` / `.rd-text` / `.rd-orig` / `.rd-frame` · `.rd-chunk`（`.lit`）· `.rd-page-*`
（`.rd-page-view` / `.rd-page-img` / `.rd-page-layer` / `.rd-page-mark` / `.rd-page-jump`）·
`.rd-foot`

杂项：`.empty`（空态，`◌` 前缀）· `.muted` / `.small` / `.nowrap` / `.grow` / `.hidden` ·
`.drag-over` / `.dragging` · `.to-bottom` · `.clip-ghost` · `table.list`（`.num` / `.clickable`）

**组件契约**：组件是**一个类**，不是工具类的拼盘。新界面复用上表的类，新类要长在**同一套
词汇**里（状态后缀：`.on`、`.active`、`.lit`、`.ok` / `.warn` / `.bad`、`.drag-over`；
变体修饰：`.primary`、`.ghost`、`.danger`、`.solid`）。

## 该做与不该做

### 该做

- 每个颜色、圆角、浮层层级都从 `:root` 取。新着色 = `rgba(token-rgb, α)` 走上阶梯，不是新十六进制。
- 输入框、代码、芯片、悬停用 `{colors.bg-inset}`；卡片、菜单、气泡用 `{colors.bg-panel}`。层次 = 换底色档。
- 让珊瑚表示"当前的 / 选中的 / 被作用的"。所有界面里的选中都用珊瑚（导航、会话、运行、标签、模式、芯片）——一致性是它读得懂的前提；绿留给"它成功了"（`--success`）。
- 中文保持默认字距。正字距只给拉丁字标与分节标签。
- 每个可交互元素都给全四个状态（hover、active/选中、disabled、focus-visible）。只有一个 hover 态的控件在这里算没做完。
- 用 z-index 阶梯；确实需要新层就先在阶梯里给它起名（并写进本文），而不是随手挑个大数字。
- "多选一"一律复用 `.s-chip` / `.pill` / `.rd-tabs`——胶囊 + 强调色填充就是本应用的选中语言。

### 不该做

- 别加第二个品牌强调色。暖青/琥珀/红/绿都是语义色；焦点直接用品牌珊瑚，所以没有第三个色相可捡。
- 别把画布刷成纯白，也别拿深色面去铺代码以外的东西——米色到深色的对比是这套系统的节奏，任何一边铺开，节奏就散了。
- 别给卡片、行、按钮加阴影——阴影只属于浮层。
- 别引入五档之外的圆角（现有样式表里的 3/4/5/7/14px 是漂移，不是可以继续扩展的尺度）。
- 正文别小于 11px，中文别小于 12px。
- 别把用户必须读才能操作的信息放进 `--muted`（按钮文字、数值、错误信息）——弱化色是给元信息的。
- 别新增现有之外的动效：0.15s 描边/底色、toast 0.2s 滑入、进度条 0.4s、光标闪烁。不要弹跳、不要视差。
- 别把功能藏在悬停显现的 `⋯` 后面，除非那一行本身就是主要操作目标——破坏性操作必须另有一条可见入口（确认框与阅读器状态栏是先例）。

## 响应式行为

按设计只有一个断点：

| 区间 | 行为 |
| --- | --- |
| ≥ 901px | 完整布局。这是目标场景：桌面窗口（打包应用）或笔记本浏览器标签页。 |
| ≤ 900px | 浮动阅读面板收窄到 `92vw`；其余保持桌面网格并各自滚动。 |

知识库面板（`#paper-form` 搜索行、密钥行）是**换行**而不是缩小；表格单元的
`overflow-wrap: anywhere` 与 flex 子项的 `min-width: 0` 是防长 token 和中文串撑破卡片的标准闸门。

触摸目标是桌面尺寸（控件高 28–34px）。这是刻意的范围边界：本应用不为手机设计，
假装支持而把目标撑到 44px，会直接花掉撑起三栏布局的那份密度。

## 迭代指南

1. **一次只动一个组件。** 先定位它的类，再看同一族里最近的兄弟（新面板 = `.card`；新控件 = `.btn` 的变体）。
2. **新 token 先进 `:root`。** 一个值要在两处以上用到，它就该进变量块，并写进本文。
3. **选中就要长得像选中。** 珊瑚填充 + 珊瑚描边（或珊瑚字），落在 tint 档，没有例外。
4. **元信息弱化且小，操作醒目且有字。** 用户看不出哪里能点时，解法是对比度，不是加描边。
5. **样式表里的注释是中文**，并且解释*为什么*是这个值——尤其是那些来自用户反馈的值（"太狭窄"、"字看不清"）。保持这个习惯：一个没有理由的数字，会被下一个改动者"顺手清理"掉。
6. **新增组件或 token 时，在同一个改动里更新本文**，并跑一遍护栏测试（见下）。

## 校验

`tests/unit/web/test_design_md.py` 用零浏览器、零新依赖（PyYAML 本就是运行时依赖）的方式
让本文档保持诚实：

- front-matter 能解析，且 `colors:` 里每个 token 与 `style.css` `:root` 里的同名 CSS 变量
  逐字一致（过期色值会让 CI 变红）；
- 「组件」段点名的每个 CSS 类都真实存在于样式表；
- 圆角五档与 z-index 阶梯里的数值都能在样式表里找到。

**中英两份文档都会检查**：`DESIGN.md`（英文，事实源）与 `docs/zh-CN/DESIGN.md`（本文件）
的 token 值必须同时与样式表一致——镜像不得各自漂移。

## 已知缺口

- **圆角漂移**：除五档之外，样式表里仍有 3/4/5/7/14px（气泡 12 + 内角 4、`.btn` 7px、
  `.cite` 5px、`.s-sw`/`.t-caret` 6px）。上面的档位是**目标尺度**；收敛这些零头是一次清理，
  本文档写明目标，而不假装那些漂移就是系统。
- **alpha 漂移**：仅绿色就出现过 15 个不同的 α（0.05→0.8）。阶梯只点名了其中关键的六档，
  其余是历史值。
- **间距是经验值而非推导值**：4px 基数配 6/8/10/12/14/16/18/20 的档位是从现有 CSS 抄录的。
  严格的 4/8/12/16/20 会更干净，方向也是那边，但尚未整体执行（每个面板都要重新过一遍）。
- **没有浅色主题。** 色板只有暗色；"对话背景色/图"设置是用户级逃生门，不是主题系统。
- **没有图标系统。** 图标是 Unicode 字符，字重与视觉大小随平台字体而变；没有等价于
  icon token 的东西。
- **动效除上述几处过渡外未记录**（没有时长 token）。
- **对比度未经测量**：弱化色配面板底是按眼睛挑的。若要做无障碍，`{colors.muted}` 落在
  `{colors.bg-panel}` 上是第一对要审的。
- **评测报告的 markdown 渲染器**（`.md`）是对话渲染器（`.bubble` + markdown 类）的轻量
  子集，共用 token 但不是全部间距。
