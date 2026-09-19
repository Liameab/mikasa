---
version: alpha
name: Mikasa-design-system
description: A dense dark-canvas product UI for a local RAG workbench. One green accent carries brand, selection and success; cyan carries information and citations; amber and red carry warning and danger. Compact system-sans type (11–22px, no web fonts), one 4px-based spacing scale, a five-rung radius grammar, and a fixed z-index ladder for overlays. Built for a desktop window first, with exactly one responsive breakpoint. Every color, radius and overlay level is a CSS custom property in style.css :root — that block is the source of truth this document describes.

colors:
  bg: "#0f1117"            # var(--bg)       page canvas
  bg-panel: "#161b22"      # var(--bg-panel) cards, panels, bubbles, menus
  bg-inset: "#0d1117"      # var(--bg-inset) inputs, code, chip track, row hover
  border: "#30363d"        # var(--border)   1px hairlines and control outlines
  text: "#e6edf3"          # var(--text)     primary text
  muted: "#8b949e"         # var(--muted)    secondary text, icons, hints
  green: "#3fb950"         # var(--green)    brand + success + selection
  cyan: "#39c5cf"          # var(--cyan)     information + citation
  red: "#f85149"           # var(--red)      danger + error
  yellow: "#d29922"        # var(--yellow)   warning + attention + highlight
  focus: "#2f81f7"         # var(--focus)    keyboard focus ring only
  chat-font: "14.5px"      # var(--chat-font) user-overridable message size
  chat-bg: "transparent"   # var(--chat-bg)   user-overridable chat canvas color
  chat-img: "none"         # var(--chat-img)  user-overridable chat background image

typography:
  fontFamily: '"Segoe UI", "Microsoft YaHei", "PingFang SC", system-ui, sans-serif'
  mono: 'Consolas, "Courier New", monospace'
  base: 15px               # body
  lineHeight: 1.65         # body; long-form reading goes to 1.75–1.8
  brand:
    fontSize: 18px
    fontWeight: 700
    letterSpacing: 1px
  page-title:
    fontSize: 16px
    fontWeight: 600
  card-title:
    fontSize: 16px
    fontWeight: 600
  section-label:           # .card h3 / panel group heading
    fontSize: 14px
    fontWeight: 500
  body:
    fontSize: 14px
    fontWeight: 400
  row:                     # tree rows, table cells, menu items
    fontSize: 13px
    fontWeight: 400
  control:                 # buttons, inputs
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
  micro:                   # badges, meta lines, code language labels
    fontSize: 11px
    fontWeight: 400
  stat-value:
    fontSize: 20px
    fontWeight: 700

rounded:
  control: 6px             # buttons, inputs, menu items, swatches
  row: 8px                 # tree rows, bubbles, cards inside cards, toast
  card: 10px               # .card, settings panel, dropzone
  modal: 12px              # confirm dialog, user bubble
  pill: 999px              # pills, chips, segmented controls, progress bars

spacing:                   # base unit 4px; structural steps in bold below
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
    rounded: "7px"          # drift — see Known Gaps
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

## Overview

Mikasa's UI is a **dense, dark, single-user workbench**: four pages (ask / library /
find-papers / evaluation) inside one window, driven with a mouse and a keyboard, largely offline.

- **Desktop window first**; the layout is built for a packaged desktop app and a browser
  tab on the same machine, not for phones. There is exactly **one** responsive rule in
  the whole stylesheet (`@media (max-width: 900px)` shrinks the reader panel).
- **Density is the point**: three font sizes (11/13/14) carry almost all UI text, rows are
  26–34px tall, and the tree / message list / table are meant to show a lot at once. This
  is the opposite intent from a marketing surface, which is why the references this file
  was modelled on (Vercel-style product pages) needed their scaling re-derived, not copied.
- **One accent, three meanings — deliberately overloaded**: green means *brand*, *selected*
  and *success* at once. That is a considered trade: in a single-user tool with no
  marketing chrome, "this is current / this is fine" is one idea, and a second accent color
  would be noise. Cyan, amber and red each carry exactly one semantic family (info +
  citation / warning + attention / danger).
- **No web fonts, no icon font, no build chain**: the type stack is the OS UI font
  (`Segoe UI` / `Microsoft YaHei` / `PingFang SC` / `system-ui`), glyphs are Unicode
  (`⋯ ✕ ▸ ▾ ＋ ◌ ▌`), and a full re-render is a page refresh.
- **Every token is a CSS custom property** in the `:root` block at the top of
  `src/mikasa/web/static/css/style.css`. That block — not this prose — is the source of
  truth; this document exists so a new panel is *derived* from it instead of inventing a
  fourth shade of gray.

**Key characteristics:**

- Dark canvas (`{colors.bg}` #0f1117) with three surface steps: page → panel → inset. Depth
  is expressed by *stepping down* into insets, not by shadows.
- Single green accent (`{colors.green}` #3fb950) for brand, selection and success; tints are
  the token's own RGB at a fixed alpha ladder.
- CJK-first typography: **never negative letter-spacing** (CJK glyphs sit on a square
  grid); the only two `letter-spacing` declarations in the codebase are positive and Latin-only.
- Compact scale — 11/12/13/14/15/16/20/22px, body at 15px, reading surfaces at 14px/1.8.
- Radius grammar in five rungs: 6 (control) / 8 (row) / 10 (card) / 12 (modal) / 999 (pill).
- Overlay order is a **fixed ladder**, not ad-hoc: topbar 10 → to-bottom 30 → settings 40 →
  reader 50 → ctx-menu 60 → confirm 90 → update dialog 95 → toast 100 → onboard 200.
- Interactive states are part of the language (hover / active / disabled / focus-visible /
  selected / drag-over / highlighted) — an in-app product UI, unlike a marketing page,
  is *defined* by them.

## Colors

### Surfaces

| Token | Hex | Role |
| --- | --- | --- |
| `{colors.bg}` | #0f1117 | Page canvas (body background). |
| `{colors.bg-panel}` | #161b22 | Cards, chat bubbles, menus, toast, topbar. One step above canvas. |
| `{colors.bg-inset}` | #0d1117 | Inputs, code blocks, chip tracks, table headers, row hover. One step *below* canvas — recessed, not raised. |
| `{colors.border}` | #30363d | The only hairline color: card outlines, control borders, dividers, scrollbar thumb. |

**Depth rule:** elevation is expressed by stepping the background (`panel` → `bg` → `inset`)
and by tint, never by a shadow on a panel. Shadows exist only for *floating* things
(menus, dialogs, toast, panels) — see Elevation.

### Accent & semantics

| Token | Hex | Means | Never means |
| --- | --- | --- | --- |
| `{colors.green}` | #3fb950 | Brand, selected/current, success, upload/positive action | Danger, plain information |
| `{colors.cyan}` | #39c5cf | Information, citations, references, "back to bottom" | Success, selection |
| `{colors.yellow}` | #d29922 | Warning, attention, in-text quote, "this is worth a look" | Error |
| `{colors.red}` | #f85149 | Error, danger, destructive confirm, out-of-range citation | Warning |
| `{colors.focus}` | #2f81f7 | Keyboard focus ring (`:focus-visible`, input focus border) | Brand, decoration |

**The focus blue is the one deliberate exception to "one accent".** It appears only where
the keyboard is talking to the app, and it is never used as a decorative or brand color.
That is why it is allowed to sit outside the green family.

### The alpha ladder (tints)

Tints are always `rgba(<token RGB>, α)` — never a new hand-picked hex. The RGB triples in
use: green `63, 185, 80` · cyan `57, 197, 207` · amber `210, 153, 34` · red `248, 81, 73` ·
focus `47, 129, 247` · border `48, 54, 61` · white `255, 255, 255` (for hover washes on
tables, at 0.02–0.03).

The intended ladder, and the six rungs that carry 90% of the UI:

| Rung | α | Use |
| --- | --- | --- |
| wash | 0.05–0.08 | Section tint on a large surface: folder row, dropzone hover, blockquote bg, pill bg |
| tint | 0.10–0.16 | Selected row, active nav link, primary button fill, chip-on |
| press | 0.25–0.30 | The hover/active step above `tint` (buttons, cite chips, danger-solid) |
| edge | 0.45–0.55 | Border of a tinted thing (pill border, cite chip border, active row border) |
| ring | 0.18–0.35 | Focus/selection outline glow (`outline` / `box-shadow`) |
| glow | 0.8 | The single status dot halo in the topbar |

## Typography

### Family

One stack, no downloads:

```
"Segoe UI", "Microsoft YaHei", "PingFang SC", system-ui, sans-serif   /* UI + body */
Consolas, "Courier New", monospace                                    /* code */
```

Latin falls to Segoe UI (Windows) / the system UI font; CJK falls to Microsoft YaHei or
PingFang SC. `font: inherit` is set on every control so form elements do not fall back to
the browser's default 13.3px Times.

### Hierarchy

| Token | Size | Weight | Use |
| --- | --- | --- | --- |
| `{typography.brand}` | 18px | 700 | The `Mikasa` wordmark only (with +1px tracking — Latin only). |
| `{typography.page-title}` | 20–22px | 600 | Page-level headings and stat values. |
| `{typography.card-title}` | 16px | 600 | `.card h2`, bubble `h3`, confirm-dialog title. |
| `{typography.body}` | 14px | 400 | Default body: tables, menus, markdown reports, reader text. |
| `.settings-panel .s-group` | 12px | 600 | Panel section labels, cyan, +0.04em tracking (Latin-ish label voice). |
| `{typography.section-label}` | 14px | 500 | `.card h3`, table headers — muted, never competing with the content. |
| `{typography.control}` | 13.5px | 400 | Inputs, toast text. |
| `{typography.row-strong}` | 13px | 600 | Tree row names, primary buttons, selected/current emphasis. |
| `{typography.row}` | 13px | 400 | Tree rows, table cells, menu items, citation cards. |
| `{typography.caption}` | 12px | 400 | `.small`, `.pill`, hints, meta lines. |
| chip / mode label | 12.5px | 400 | `.s-chip`, `.mode-btn`, `.to-bottom` — the half-step between caption and row. |
| `{typography.micro}` | 11px | 400 | Badges, `.s-meta`, code language labels, msg `who`. |
| `{typography.stat-value}` | 20px | 700 | Eval metric values (with `tabular-nums`). |

Body copy in the chat/reading surfaces is user-overridable through `--chat-font`
(default 14.5px) — a token the *user* owns at runtime, applied via the settings panel.

### Principles

1. **CJK first: no negative letter-spacing, ever.** The two `letter-spacing` declarations in
   the codebase are both positive (`+1px` on the Latin wordmark, `0.04em` on panel section
   labels). Copying a Latin display system's `-0.28px` headline tracking onto Chinese text
   squeezes glyphs that were designed on a square em.
2. **Four weights, each with a job**: 400 body · 500 labels and quiet headings · 600
   emphasis, titles, selected state · 700 only the wordmark and stat values.
3. **Numbers that are compared get `font-variant-numeric: tabular-nums`** (metric values,
   numeric table cells) so columns line up.
4. **Long-form reading opens up**: chat bubbles keep 1.65, but the reader's text view and
   bilingual blocks go to 1.75–1.8 — a reader complaint ("characters look gray and cramped")
   set those values, not taste.
5. **Metadata is muted, never smaller-than-legible**: meta lines are `--muted` at 11–12px;
   going below 11px is not part of the system.

## Layout & Density

### Spacing

Base unit **4px**, with 6/8/10/12/14/16/18/20 as the working steps. The scale is *empirical*:
it was written down from what the pages actually use, and the honest rule is:

- **Inside a control**: 4–8px vertical, 8–16px horizontal (buttons 7×16, inputs 5×9,
  chips 3×10, pills 4×9).
- **Between sibling controls**: 6–8px (`gap` is most often 6px, then 8px).
- **Inside a card**: 14–18px (`.card` 16px, `.msg-scroll` 18px, `.s-body` 10–12px).
- **Between cards / layout rows**: 14px (`main` gap, `.qa-layout` gap, `.kb-main` gap).
- **Overlay padding**: 20px around a modal backdrop, 16–20px inside a dialog.

If a value is not on this scale, prefer the nearest rung over a new number — the drift
list in Known Gaps is what happens when that rule is skipped.

### Layout

- **`main`** is a vertical flex column with 18px padding and 14px gaps; pages fill it.
- **Ask / Library**: a two-column grid, `290px` sidebar + content (`{spacing.xl}` 14px gap).
  Library reuses the same sidebar component as Ask (`kb-tree` and the session tree are the
  same row vocabulary on purpose).
- **Evaluation**: `300px` run list + report, same 14px gap.
- **Reading surfaces are pinned, content scrolls inside them**: `#reading-card` is capped at
  `calc(100vh - 90px)` and the text pane scrolls — the header (tabs, zoom) and the status
  footer stay visible. This was a real bug fix (2026-09-11): without the cap, a long PDF made
  the *page* scroll and both bars disappeared.

### Density philosophy

The app should feel like a **file manager for knowledge**, not a landing page: rows 26–34px,
one line of metadata under a title, `⋯` menus instead of visible toolbars, hover-revealed
affordances. Whitespace is used to *separate groups*, not to create drama; the `.empty`
state (34px of vertical padding, centered, muted, with a `◌` prefix) is the one place the
layout deliberately breathes.

## Elevation & Depth

| Level | Treatment | Use |
| --- | --- | --- |
| Flat | No shadow | Page, cards, rows, bubbles, tables — the entire in-flow UI |
| Overlay shade | `rgba(0, 0, 0, 0.55)` backdrop | Confirm dialog, update dialog, on-boarding mask |
| Menu / toast | `0 6px 18px rgba(0,0,0,0.4)` · `0 8px 28px rgba(0,0,0,0.5)` | `#ctx-menu`, toast, settings panel (`0 8px 30px`) |
| Dialog | `0 18px 48px rgba(0, 0, 0, 0.55)` | Confirm box (also `0 18px 60px` on the reader panel) |
| Status glow | `box-shadow: 0 0 6px rgba(63,185,80,0.8)` | The health dot, once |
| Highlight | `outline: 2px solid` / `box-shadow: 0 0 0 1px` | `.cite.lit`, `.rd-chunk.lit`, focus rings |

**Rule:** shadows belong to things that float *over* the page. A card, a row or a button
never gets a shadow to look "raised" — use the inset/panel surface steps instead.

The z-index ladder is part of this system and must not be improvised:

```
10  topbar (sticky)      40  settings panel     90  confirm backdrop
30  to-bottom button     50  reader panel       95  update dialog
                         60  ctx menu          100  toast
                         70  note editor       200  onboard mask (covers everything)
```

The note editor sits between the context menu and the confirm dialog on purpose: the
"discard unsaved changes?" confirmation has to cover the editor it is asking about.

The update dialog sits between the confirm dialog and the toast on purpose: it is a
startup modal (above everything the app opens by itself) but the toast that confirms
"this version won't be suggested again" has to stay visible on top of it.

Toast sits above the confirm dialog on purpose (an action's result should be visible after
the dialog closes); the on-boarding mask covers everything because it is the product's first
sentence. `#toast` itself has `pointer-events: none` with clickable children — so a toast
that lands on top of a panel header never blocks the ✕ underneath.

## Shapes

| Rung | Value | Use |
| --- | --- | --- |
| control | 6px | Buttons, inputs, menu items, swatches, `.icon-btn`, `.t-more` |
| row | 8px | Tree rows, bubbles, toast, stat cards, code blocks, citation cards, nav-ish containers |
| card | 10px | `.card`, settings panel, dropzone, composer's bottom corners |
| modal | 12px | Confirm dialog, user message bubble, floating reader panel |
| pill | 999px | Pills, chips, segmented controls, progress track/bar, mode buttons |
| circle | 50% | Folder caret ring, in-folder session ring, health dot |

Two shapes are *semantic*, not stylistic: the **green ring** (`border: 2px solid var(--green)`
+ `border-radius: 50%`) marks a folder's caret, and the **amber ring** marks a session that
lives inside a folder. They are the same "circle" language with different colors, and the
distinction is load-bearing — do not recolor either.

## States

| State | Treatment |
| --- | --- |
| hover (row / menu item) | Background → `{colors.bg-inset}` |
| hover (button, default) | Border → `{colors.muted}` |
| hover (primary button) | Fill tint 0.16 → 0.28 |
| hover (danger button) | Border + text red, fill red 0.08 |
| active / current | Green tint 0.10–0.16 + green border or green text (nav, session row, run item, tab, mode button) |
| disabled | `opacity: 0.45` (or 0.55 for inputs), `cursor: not-allowed`; a disabled *primary* button drops its green entirely and returns to the neutral control look |
| focus | `:focus-visible` → `outline: 2px solid var(--focus)`; inputs → `border-color: var(--focus)` |
| drag source | `opacity: 0.45` |
| drop target | Green tint 0.14 + green border |
| highlighted (citation / reader chunk) | Amber tint, 1px amber ring, or `outline: 2px solid var(--yellow)` |
| streaming | Bubble border → cyan; blinking `▌` caret in green |
| busy / loading | Text in `--muted` ("正在…"), progress bar green→cyan gradient |

## Components

Layout：`.qa-layout` · `.kb-main` · `.eval-layout` · `.session-pane` · `.chat-pane` ·
`.msg-scroll`（消息滚动区，用户可覆写底色/背景图）

Chrome：`.topbar` · `.brand` · `nav.main a`（`.active`）· `.health-pill`（`.dot`）

Cards：`.card`（`h2` 标题 / `h3` 节标签）· `.stat-card`（`.k` 标签 `.v` 数值）· `#reading-card`

Buttons：`.btn`（默认描边）· `.btn.primary`（绿系主行动）· `.btn.ghost`（透明底）·
`.btn.danger` / `.btn.danger.solid`（危险，实心用于确认框）· `.icon-btn`（方角图标钮）·
`.btn-copy`（代码块复制）· `.mode-btn`（胶囊模式切换）

Inputs：`.kb-search`（查找框）· `.tree-input`（行内改名/新建）· `.composer textarea`（提问框）·
`#s-nick` / `#s-base-url` / `#s-model` / `#s-api-key`（设置面板输入）· `.rd-page-jump input`（页码）

Selection：`.pill`（`.ok` / `.warn` / `.bad`）· `.s-chip`（`.on`）· `.rd-tabs`（胶囊分段）·
`.s-sw`（色板圆点，`.on` / `.clear`）

Tree：`.t-row` · `.t-folder` · `.t-caret`（绿圆环）· `.t-name` · `.t-more`（悬停显现的 ⋯）·
`.session-item`（`.active` / `.in-folder` 琥珀圈）· `.t-empty` · `.tree-edit-row`

Chat：`.msg.user` / `.msg.assistant` · `.bubble` · `.who` · `.cite`（`.lit` / `.bad`）·
`.ref-shelf .cite-card`（`.c-head` / `.c-title` / `.c-snippet`）· `.code-block`（`.code-head` /
`.code-lang`）· `.md-table` · `blockquote.bilingual`（原文+译文块）· `.composer`（`.mode-bar` /
`.composer-row` / `.hint`）

Overlays：`#ctx-menu`（`.ctx-item` / `.ctx-head` / `.ctx-sep`）· `.confirm-backdrop` /
`.confirm-box`（`.cf-title` / `.cf-detail` / `.cf-actions`）· `#toast` / `.toast-msg`（`.ok` /
`.warn` / `.error`）· `.settings-panel`（`.s-head` / `.s-body` / `.s-row` / `.s-name` /
`.s-hint` / `.s-group`）· `.onboard-mask`（`.onboard-card` / `.ob-step`）· `.upd-backdrop` /
`.upd-card`（`.upd-head` / `.upd-sub` / `.upd-notes` / `.upd-progress` / `.upd-bar` /
`.upd-progress-text` / `.upd-error` / `.upd-actions`）

Evaluation：`.run-list` / `.run-item`（`.active`）· `.progress-track` / `.progress-bar` ·
`.md`（报告渲染）· `.stat-grid`

Library：`.dropzone`（`.drag`）· `.doc-item`（`.meta-bad` / `.meta-busy`）· `.dd-grid` ·
`.dd-open`

Find-papers page (ADR-0020)：`.papers-layout`（grid 三栏）· sidebar `.p-filter` /
`.p-filter-label` / `.p-check` / `.p-year` / `.p-note` / `.p-sorts` · results
`.paper-form` / `.paper-bar` / `.paper-results` / `.paper-item`（`.on` = selected）/
`.paper-head` / `.paper-title` / `.paper-src` / `.paper-meta` / `.paper-cites` /
`.paper-snippet` / `.paper-inlib` / `.paper-actions` / `.paper-status` / `.paper-key-row` ·
detail `.pd-title` / `.pd-meta` / `.pd-body` / `.pd-actions` / `.pd-exits`

Note editor (M6 ①)：`.note-backdrop` · `.note-box` / `.note-head` / `.note-title` ·
`.note-split` / `.note-input` / `.note-preview` · `.note-foot` / `.note-folder-wrap` /
`.note-folder` / `.note-hint`

Reader：`.reader-panel` / `.reader-inline` · `.rd-head`（`.rd-title` / `.rd-tabs` / `.rd-zoom`）·
`.rd-body` / `.rd-text` / `.rd-orig` / `.rd-frame` · `.rd-chunk`（`.lit`）· `.rd-page-*`
（`.rd-page-view` / `.rd-page-img` / `.rd-page-layer` / `.rd-page-mark` / `.rd-page-jump`）·
`.rd-foot`

Misc：`.empty`（空态，`◌` 前缀）· `.muted` / `.small` / `.nowrap` / `.grow` / `.hidden` ·
`.drag-over` / `.dragging` · `.to-bottom` · `.clip-ghost` · `table.list`（`.num` / `.clickable`）

**Component contract:** a component is a class, not a utility soup. New UI reuses the
classes above and adds a new one *in the same vocabulary* (state suffixes: `.on`, `.active`,
`.lit`, `.ok` / `.warn` / `.bad`, `.drag-over`; variant modifiers: `.primary`, `.ghost`,
`.danger`, `.solid`).

## Do's and Don'ts

### Do

- Pull every color, radius and overlay level from `:root`. A new tint is
  `rgba(token-rgb, α)` on the ladder, not a new hex.
- Reach for `{colors.bg-inset}` for inputs, code, chips and hover; `{colors.bg-panel}` for
  cards, menus and bubbles. Depth is a surface step.
- Let green mean "current / chosen / worked". Selection uses green in every surface (nav,
  session, run, tab, mode, chip) — consistency is what makes the overload legible.
- Keep CJK text at default tracking. Positive tracking only for Latin wordmarks and
  section labels.
- Give every interactive element all four states (hover, active/selected, disabled,
  focus-visible). A control with only a hover state is incomplete here.
- Use the z-index ladder; if a new layer is needed, name it in that ladder (and in this
  file) rather than picking a number that looks big.
- Reuse `.s-chip` / `.pill` / `.rd-tabs` for "pick one of N" — the pill + green-fill
  vocabulary is the app's selection language.

### Don't

- Don't add a second brand accent. Cyan/amber/red are semantic; focus blue is for the
  keyboard only.
- Don't put shadows on cards, rows or buttons — shadows are for overlays.
- Don't introduce a radius outside the five rungs (the 3/4/5/7/14px values in the current
  stylesheet are drift, not a scale to extend).
- Don't set body text below 11px, or CJK text below 12px.
- Don't use `--muted` for text the user must read to act (button labels, values, error
  messages) — muted is for metadata.
- Don't animate beyond what exists: 0.15s on border/background, 0.2s slide-in for toast,
  0.4s for the progress bar, a blinking caret. No bounces, no parallax.
- Don't hide functionality behind the hover-revealed `⋯` unless the row itself is the
  primary target — a destructive action must also be reachable from a visible surface
  (the confirm dialog and the reader's status bar are the precedents).

## Responsive Behavior

One breakpoint, by intent:

| Range | Behavior |
| --- | --- |
| ≥ 901px | Full layout. The target: a desktop window (packaged app) or a laptop browser tab. |
| ≤ 900px | The floating reader panel narrows to `92vw`; everything else keeps its desktop grid and scrolls. |

The library's panels (`#paper-form` search row, the key row) wrap rather than shrink;
`overflow-wrap: anywhere` on table cells and `min-width: 0` on flex children are the
standard guards against long tokens and CJK strings blowing out a card.

Touch targets are desktop-sized (28–34px tall controls). This is a deliberate scope
boundary: the app is not designed for phones, and pretending otherwise with 44px targets
would cost the density that makes the three-pane layout work.

## Iteration Guide

1. **One component at a time.** Reference its class, then check the nearest existing
   sibling in the same family (a new panel is a `.card`; a new control is a `.btn` variant).
2. **New token → `:root` first.** If a value is needed in more than one place, it belongs in
   the variable block, and in this file.
3. **Selection looks like selection.** Green fill + green border (or green text) at the
   `tint` rung, no exceptions.
4. **Metadata is muted and small; actions are bright and labeled.** If a user cannot tell
   what is clickable, the fix is contrast, not a border.
5. **Comments in the stylesheet are Chinese** and explain *why* a value is what it is —
   especially the values that came from a user report ("太狭窄", "字看不清"). Keep that
   habit: a number without its reason gets "cleaned up" by the next contributor.
6. **Update this file in the same change** that adds a component or a token, and run the
   guardrail test (below).

## Verification

`tests/unit/web/test_design_md.py` keeps this file honest with zero browser and no new
dependencies (PyYAML is already a runtime dependency):

- the YAML front-matter parses, and every `colors:` value matches the `:root` variable of
  the same name in `style.css` (a stale hex fails CI);
- every CSS class named in the Components section exists in `style.css`;
- the radius/z-index tables' values appear in the stylesheet.

## Known Gaps

- **Radius drift**: alongside the five rungs, the stylesheet still contains 3/4/5/7/14px
  (bubble/user radius 12 + inner corner 4, `.btn` 7px, `.cite` 5px, `.s-sw`/`.t-caret` 6px,
  `.clip-ghost` 0). The rungs above are the *intended* scale; converging the strays is a
  cleanup, and this file states the target rather than pretending the drift is a system.
- **Alpha drift**: green alone appears at 15 distinct alphas (0.05→0.8). The ladder names
  the six that matter; the rest are historical.
- **Spacing is empirical, not derived**: the 4px base with 6/8/10/12/14/16/18/20 steps was
  written down from the existing CSS. A strict 4/8/12/16/20 scale would be cleaner and is
  the direction, but it has not been applied wholesale (every panel would need re-checking).
- **No light theme.** The palette is dark-only; the "chat background color/image" settings
  are a user-level escape hatch, not a theme system.
- **No icon system.** Glyphs are Unicode characters, so their weight and optical size vary
  by platform font; there is no equivalent of an icon token.
- **Motion is undocumented beyond the few transitions above** (no timing tokens).
- **Contrast is not measured**: the muted-on-panel combinations were chosen by eye. If
  accessibility work starts here, `{colors.muted}` on `{colors.bg-panel}` is the first pair
  to audit.
- **The evaluation report's markdown renderer** (`.md`) is a lighter subset of the chat
  renderer (`.bubble` + markdown classes) and shares tokens but not all spacing.
