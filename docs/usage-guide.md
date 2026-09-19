# Usage Guide

> A complete guide that takes you from zero to productive use: the three pages, the paper
> research workflow, detailed AI Q&A usage, corpus management, settings and data privacy.
> Companion piece to docs/ideas-and-backlog.md (the idea ledger).

---

## 1. What Mikasa Is

A local-first personal document RAG workspace:

- **Knowledge-base Q&A (kb)**: upload your material (md/txt/pdf/docx) first, then ask questions — answers **quote chunks of the original corpus** (`[n]` markers; click one to highlight the source), and when retrieval finds nothing the system refuses explicitly (an anti-hallucination feature, not a matching failure);
- **Free-form Q&A (free)**: no knowledge-base lookup, goes straight to the model for a detailed answer — good for "follow-up questions the material doesn't cover";
- **All data stays on this machine**: SQLite (data/mikasa.db) + uploaded copies (data/uploads) + indexes, with no third-party cloud in the path; the offline profile works with the network disconnected.

The typical loop (a user scenario): you hit an unfamiliar concept while working through problems → kb gives you a precise, citation-traced answer from your notes → free expands on the underlying principles and related ideas → you write the new understanding back into your notes and ingest them. Knowledge accumulates; that is the loop.

---

## 2. Quick Start

**Double-click `Mikasa.bat`** (or the "Mikasa" desktop shortcut) → it brings up Ollama (if it isn't already running) plus the service → the browser opens http://127.0.0.1:8787/ automatically. To stop it, close the minimized "Mikasa service" window in the taskbar.

Manual start (developers):
```bash
.venv\Scripts\mikasa.exe serve --profile local --port 8787
```
Three profiles: `api` (DeepSeek generation + SiliconFlow embeddings/reranking, best quality, needs keys in .env) / `local` (Ollama qwen3:8b + fastembed, free and offline) / `offline` (MockLLM, zero-key demos and evaluation; the free-chat toggle is greyed out). Default data directory = `data/` at the repository root.

---

## 3. Page Tour

Three pages in the top bar: **Chat** (home) / **Library** / **Evaluation**; the status pill in the top right shows the current model and the live connection state.

| Page | Left | Right |
|---|---|---|
| Chat | Session tree | Chat area (with the free-chat toggle) |
| Library | Corpus folder tree + search box | Upload area + details of the selected document |
| Evaluation | — | Golden-set evaluation runs + past reports |

---

## 4. Knowledge-Base Q&A (Including the Paper Research Workflow)

**The mode switch** sits above the input box: `KB` / `Free chat` (locked while an answer is streaming; the toggle state belongs to the individual message).

### 4.1 Researching a Paper or Looking Up Material in Five Steps
1. Switch to the **Library** page and drag in or browse to your papers/notes (md/.txt/pdf/docx, max 500 MB per file; duplicate content is skipped automatically; re-uploading an updated file under the same name replaces it in place, keeping both its title and its folder placement);
2. Optionally **create folders to organize** the tree on the left (drag a document row into a folder, or use the ⋯ at the end of a row → Move to…);
3. Go back to the **Chat** page and confirm the mode is KB;
4. Ask a question — preferably one **the corpus can actually answer** (paraphrases land just as reliably: recall@10 = 1.000 in the acceptance run):
   - ✅ "Why does L2 regularization prevent overfitting?", "Compare the update rules of Adam and SGD", "Why is dk square-rooted in the attention mechanism?"
   - ⚠️ Material that genuinely isn't there triggers a **refusal with an explanation** — that is the anti-hallucination design (see 4.3 for follow-ups);
5. The answer body carries `[n]` citation markers — **clicking one locates and highlights the matching source chunk in the message area**, so every claim can be traced back; points with insufficient support are called out explicitly;
6. **When English-language sources are hit, the answer automatically appends an "original vs. translation" section** (api/local profiles, `answer.bilingual`) — each cited English chunk is listed as a pair: `[n] original` + `[n] Chinese translation`, and the `[n]` markers inside those blocks are clickable too. Chinese questions work exactly as before, just with an extra bilingual section; the offline/mock profile makes no extra calls and produces no such block.
7. **A live timer runs while you wait, and a per-stage breakdown appears once the answer lands**: after you ask, the line above the message ticks up as `Retrieving from the knowledge base… 12.3s`, then becomes `Took 33.1s (generate 25.3s · translate query 4.2s · …)` when it finishes — local qwen3 often needs tens of seconds per turn, and this tells you whether it is thinking or stuck. The breakdown is replayed along with past sessions (the total "took" time is not, since that needs the wall clock of the original turn).
8. **Clicking a `[n]` marker opens the reader panel on the right, positioned at that chunk** (since 2026-09-10) — the cited passage is highlighted in amber, with its position and surrounding context readable above and below. The reader has two tabs:

   - **Text**: the continuous body text reassembled from the ingested chunks; you can jump to and highlight passages. Note that markdown `#` heading lines and code fences are not visible here (they never entered the chunks), so subheadings are re-laid out from `heading_path`;
   - **Page** (PDF only, since 2026-09-11): renders the PDF **exactly as it is** into page images and **highlights the chunk you asked about by its coordinates** — **use this when you want the real layout with highlights drawn on it**. Tables, figures, and formulas are all there, untouched. You can page forward and back and jump to a page; to select text or use Ctrl+F, click "↗ Original file" in the top right (the browser's built-in viewer, opened in a new tab). For md/txt this tab is called **Original file**: it shows the text as imported; for docx it offers a download link.

   **The page-number box at the top works in both views, but it jumps differently**: in **Original file** it turns to that page of the PDF; in **Text** it **scrolls to that page's body text and highlights the chunk** (reference sections and the like are stripped during parsing, and a page with no body text says so explicitly). The upper bound is the **original file's** real page count, not the number of pages left after parsing.

   **The same reader panel appears in two forms**:

   | Where | Form | How to open | How to close |
   |---|---|---|---|
   | Chat page | Floating panel on the right | Click a `[n]` marker | ✕ / `Esc` / click outside the panel |
   | **Library page** | **Embedded in the right column** | Click any document row on the left | ✕ to collapse (back to the upload view) |

   After you select a document on the Library page, the right column *is* the **document text itself** (no longer a handful of metadata fields), with a persistent footer line showing `location · type · chunks · characters · status`. The upload area only holds that space when nothing is selected — select something and the space goes to the text.

   **Reader font size**: `A-` / `A+` in the header scale only the reader's body text; the choice is stored locally and remembered next time. **Every citation card has an "↗ Open original file" button** — if you never guessed that the markers were clickable, this is the explicit entry point.

### 4.2 Follow-Up Techniques
- Chain follow-ups within one turn (the context stays coherent): "What about vanishing gradients?", "What if we switch to L1?";
- Push for depth with compare / example / boundary questions: "Give me an example", "When does it not apply?";
- Locating things in a long document is what the citation markers are for — there is no need to paste the whole thing into your question.

### 4.3 When the Knowledge Base Can't Answer
- Switch to **free chat** and ask the same question there (it unfolds the principles and gives examples);
- **Write the new understanding into your notes and upload them again** (re-uploading the same file updates it in place) → the KB can answer from then on — knowledge accumulates; that is the loop closing.

---

## 5. Free-Form Q&A in Detail

- No upload, no retrieval, no refusal. The answer style is tuned: **conclusion first → supporting argument → one or two concrete examples → common mistakes and likely test points for study-oriented questions**; length scales with complexity (a few lines when simple, 200–500 characters when moderate, unconstrained when complex but never padded) — capped at 4096 tokens on the `api` profile and 2048 on `local`.
- **Rich answer rendering**: tables (horizontally scrollable), code blocks (their own dark region with one-click copy in the top right), headings, lists, block quotes, and horizontal rules are all rendered as blocks, as tidy as the Doubao/DeepSeek clients.
- Suggested ways to ask (treat the model as a sparring partner):
  - Extend: "Why doesn't the Transformer use a recurrent structure?" (a deep follow-up beyond your notes)
  - Correct / discriminate: "L2 and Dropout both guard against overfitting — what's the difference, and which do I tune first?"
  - Self-test: "Give me 3 short-answer questions on this chapter, no answers yet" … "I've answered; grade me"
  - Summarize: "Turn my questions above into a knowledge framework"
- When an answer is worth keeping → copy the key points into your notes → upload them to the knowledge base (see 4.3).

---

## 6. Library Page · Managing the Corpus Folder Tree

**Search box**: live filtering at the top — matches folder names and document titles, auto-expanding the branches that match; shows a placeholder hint when nothing matches; ✕ clears it and restores the whole tree in one click.

**Folders** (freely nestable, the same shape as the session tree):
- Create: the `＋ New folder` button (at the root) or a folder's ⋯ menu → New subfolder;
- Clicking a row expands/collapses it (arrow ▸/▾);
- ⋯ menu: expand/collapse / new subfolder / **rename** (inline input; Enter commits, Esc cancels) / **move to…** (opens a path picker, with the folder itself and its descendants greyed out to prevent cycles) / delete (**empty folders only**: a folder containing subfolders or documents returns 409 with a count — a container never takes its documents down with it, so move them out first, then delete).

**Document rows**:
- Click = select → the right column switches to the **reading view** for that document (body text or original file, with a status bar carrying path / type / chunk count / character count / ingest status);
- **Drag** = drop onto a folder row to move it in (collapsed folders expand automatically), drop onto a document row to move it into that document's folder, drop on empty space to move it back to the root; the source row goes semi-transparent and the target row is outlined in green;
- ⋯ menu: rename / move to… / delete (deleting opens an **in-app confirmation dialog** — not a native browser popup; it states plainly that "the chunks and the in-library copy are cleared together, and this cannot be undone", and confirms with a red button. `Esc` or a click outside cancels, so nothing is deleted by accident. Session deletion works the same way).

**Semantics worth remembering**: renaming changes only the display name inside the library (the file in uploads is untouched); deleting a document removes its retrieval chunks and its uploaded copy together; re-uploading identical content is skipped, while re-uploading the same file with new content updates it (organizational placement preserved); and the full rebuild after switching embedding models (`reindex`) likewise preserves your organization.

## 6c. Writing Notes · Markdown straight into the library (M6 ①)

**＋ New note** (left column) opens a modal editor: write Markdown on the left, see it rendered on the
right, pick a folder, save. The note becomes an ordinary library document — it shows up in the tree,
drags into folders like anything else, and is searchable/citable by the very next question. Notes are
labelled **笔记** instead of `md` in the tree so they are distinguishable from files you uploaded, and
their ⋯ menu gains **编辑笔记**.

- **Editing** re-ingests automatically: the note is replaced in place (same row in the tree, same file in
  uploads), so nothing is duplicated and your folder placement is kept. Pressing save without changing
  anything is a no-op.
- **Unsaved changes are protected**: `Esc`, 取消 or a click on the backdrop asks for confirmation before
  discarding, and a failed save keeps the window and your text open so you can fix and retry.
- **What gets searched**: the note's prose, headings, tables and — since 2026-09-20 — **the contents of
  fenced code blocks**. A block is indexed as plain text, so a `#` inside it is not mistaken for a
  heading, and a note that is *only* code can now be saved and found. One exception remains: a note with
  **nothing but headings** produces no indexable paragraph and is still rejected with 400.
- **The note file is the only copy.** Unlike an uploaded paper — where you still have your original —
  a note exists only inside the library (`data/uploads/`). Back up `data/` before switching embedding
  models or moving machines.

## 6b. Find-Papers Page · Searching the literature and importing what you find

The **找论文** page is its own tab (ADR-0020). Four free sources are queried at once — arXiv (preprints, all open access), OpenAlex (journal metadata including Chinese journals), CORE (an open-access aggregator with full-text links) and **DOAJ** (the open-access journal directory, **no key required**, which is where Chinese open-access journals such as 工业水处理 live) — and a result can be imported straight into your corpus, where it becomes askable like any other document. **Three of the four need no registration at all** (only OpenAlex prefers a free key, see below).

- **Left column — filters.** Year range, sort (relevance / most cited / newest), which sources to search, language, and "only open access". Options a selected source cannot honour are greyed out with an explanation: arXiv has no citation counts, so "most cited" is only offered while a source that has them is selected; CORE ignores year *parameters* upstream, so its year filtering runs through the query instead. When every selected source is entirely open access, "only open access" shows up checked and disabled — that condition is *already satisfied*, which is not the same as unsupported.
- **Search history**: recent queries appear as small chips under the search box (kept in this browser, at most 8) — click one to search again, and the input's native suggestions come from the same list. **Suggestions come only from your own history**, never from an upstream endpoint (the upstream type-ahead measured over ten seconds, which on a keystroke just looks like a hang).
- **Middle column — results.** Type a query in Chinese or English and search. The four sources are queried **in parallel** and their results rotate, so a page always shows a mix rather than one source burying the others; a page is 50 rows (raised from 20 on 2026-09-19). Each row carries the title, source badge, authors, year, journal, citation count (or "该来源不提供" when the source has none — 0 citations and "unknown" are different things), and a two-line abstract preview.
- **Clicking a row opens the paper's own page in a new browser tab** (2026-09-20: the first thing you do with a paper is go read it). To see the full abstract, import it into the corpus, or walk its citation graph, click the **「详情」** button inside the row — the right-hand pane is where that lives now.
- **Paging** (2026-09-20, replacing the old "load more" button): the strip under the list has previous/next, page numbers (1 … current±2 … last) and a "jump to page N" box. The **total page count** is computed from the hit counts the sources report (summed, and the status line shows where that number comes from, e.g. "上游合计 72,600 条"), **capped by what the upstreams actually allow**: the page-numbered sources (OpenAlex, DOAJ) stop at their 10,000th result, and with n sources sharing each 50-row page the cap is 200 x n (800 pages with all four selected). Fewer sources means a shallower cap — that is the upstream's reality, not a number we picked. A page past the end of the data says so plainly rather than inventing a last-page number.
- **Publication date is two date boxes** (from / to): click for a calendar, or type `2026-09-20`. The old "不限 / last 3 / 5 / 10 years" shortcut chips were removed on 2026-09-20 — they were a second path to the same thing the calendar already does.
- **The status line reports each source's upstream hit count** (e.g. "OpenAlex 命中 591,095 条"). That is the number the source itself claims, shown so you can feel how large the corpus is; it is unrelated to how many rows this page holds, and CORE's is especially inflated (it counts broad matches).
- **A page waits at most 30 seconds.** The links to these free services intermittently hang (25–40 seconds measured for a single source). At the deadline the app delivers what has arrived and says so plainly — the missing source is listed as "响应太慢，本次已跳过", and searching again usually works. **For steady results, register a free OpenAlex key** (bottom of the left column): anonymous access gets only a small trial quota, and once it is spent OpenAlex answers 429 and the source gets skipped; a key raises it to 100k credits per day.
- **Full text comes in three states**, and the detail pane says which one you are looking at: ① a direct PDF link →「导入知识库」works; ② **open access, but the source only offers a landing page** (true for most Chinese journals indexed by DOAJ) → the import button is disabled and tells you to open the original page; ③ not open access → the import button is disabled with that reason.
- **Right column — details.** Click **「详情」** inside a result row to read the full abstract and metadata, then **导入知识库** to download the PDF and ingest it. Below it sit **three ways to keep going**: *related papers* (a fresh cross-source search using this paper's title keywords — measured to beat the upstream recommendation, which for one Chinese gas-hydrate paper suggested cardiac surgery and a choir concert), *cited by* (who cites it, sorted by citation count, with the total), and *references* (what it cites). The latter two come from OpenAlex's citation graph; papers from other sources are bridged by **DOI**, and a paper without one (an arXiv preprint, say) says so plainly instead of inventing results. The row and the detail pane both switch to **已在库中** and offer **去提问** / **去知识库**; importing the same paper twice is skipped by content hash. Papers without an open-access full text have their import button disabled up front.
- **Coverage, stated honestly**: this is a CNKI-like experience, not CNKI's data. CNKI/Wanfang/VIP exclusive full text is paywalled with no public API, so Chinese social-science coverage is near zero, while DOI-carrying Chinese science/engineering journals are covered. Some CORE records point at `http://` repository links, which the downloader's security policy refuses — those records cannot be imported.
- **OpenAlex 密钥** (bottom of the left column) stores an optional [OpenAlex API key](https://openalex.org) — free registration, 100k credits/day, anonymous access only gets a small trial quota. It is written to the data directory's `.env` and takes effect immediately; the saved value is never displayed, only whether one exists (empty the box and save to clear it).

---

---

## 7. Session Management (Left Side of the Chat Page)

- `＋ New session` / `＋ Folder` (nestable);
- Sessions are titled automatically by **distilling the first question** (≤16 characters), and can be renamed via ⋯ → Rename (a manual name is never overwritten by the auto title; once you clear it, the auto title takes over again);
- Drag a session row into a folder / ⋯ → Move to… (path picker, cycle prevention greyed out); deleting a session cascades to its messages;
- Finding old sessions: folder organization plus memorable titles (session tree visuals: folders carry a green ring, sessions inside folders an amber ring, plain root-level conversations no decoration).

---

## 8. Settings (⚙ in the Top Right of the Chat Page)

The panel has two groups with different homes: **Model** is stored server-side (in the data
directory), **Chat appearance** lives in the browser (localStorage, this browser only).

### 8.1 Model — pick a provider, paste a key, done

- **Source presets**: Ollama (local, free, keyless), DeepSeek (official API),
  SiliconFlow (has free models — the cheapest way to try a cloud model), or Custom (any
  OpenAI-compatible endpoint: a `/v1` URL + model name + key).
- **Connection test**: sends one tiny request and reports "connected · 123ms" or the real
  error — it never changes your saved configuration.
- **Save and apply**: takes effect immediately, no restart (the answer pill in the top bar
  picks up the new model within ~20 s; open questions finish on the old model).
- **Where it is stored**: `%LOCALAPPDATA%\Mikasa\config.yaml` (the model choice) and
  `%LOCALAPPDATA%\Mikasa\.env` (the key, plain text, this machine only) — or `data/…` when you
  run from source. The key is never sent back to the browser and never leaves your machine
  except to the provider you configured.
- **To change the key later**: type a new one and save; to delete it, clear the field and save.
- **Scope**: this only changes the answering model. The retrieval embedding model still comes
  from the startup profile (changing it requires re-indexing the whole corpus, so it stays a
  CLI decision: `mikasa ingest --reindex`).
- If the configuration came from an explicit `--config` or a `config.yaml`, the panel is
  read-only and tells you which file to edit instead.

### 8.2 Chat appearance (local to this browser)

- **My nickname**: re-signs past message bubbles instantly (a local display-layer feature, and the stand-in until accounts exist);
- **Message font size**: slider, 12–20;
- **Chat background color**: 6 presets + a custom color picker;
- **Background image**: pick a local image → it is compressed automatically (JPEG, long edge 1600) and used as the chat background; removable at any time. The background applies only to the message area.

### 8.3 About · version and updates (v0.1.1; download behaviour since v0.1.4)

- On startup the app **checks once for a newer version** (silently — a failure never nags you); if one exists, a dialog lists what changed;
- **"Download and install"**: the app fetches the installer into `updates/` inside your data directory (with a progress bar), verifies its sha256 against the `SHA256SUMS.txt` published with the release, and then starts the wizard — the wizard closes Mikasa first and reopens the new version when it is done. Your data (library, uploads, settings) is untouched;
- **The download runs in the background; the UI is just a window onto it** (v0.1.4): the dialog can be closed ("Keep downloading in the background"), and progress moves to a small topbar capsule — **visible on every feature page**, and clicking it reopens the dialog. Switching pages, reloading, even closing the app never restarts the download from zero;
- **A dropped connection resumes** (v0.1.4): what has been downloaded stays on disk and the next attempt continues from that offset (the dialog says so honestly: "connection dropped, retrying (attempt N)"). If it ultimately fails, "Open release page" is still there as the browser fallback — and browsers resume too, so both paths behave the same now;
- **It will not press install for you**: if the download finishes while you are on another page, the capsule becomes "update ready · click to install" and you decide when — the installer closes Mikasa first, so it does not act while you are elsewhere;
- **"Skip this version"** silences that version; to bring it back, press "Check for updates" in settings (a manual check ignores the skip marker);
- **"Check for updates on startup"** can be turned off entirely — the app then makes no automatic check at all (the manual button still works);
- The update dialog can only appear **in v0.1.1 and later**: older builds do not contain the code and cannot know a new release exists. The same holds for **the v0.1.4 resume/reattach behaviour** — you need the new build first, so this upgrade is still best done in a browser.

---

## 9. Evaluation Page

"Golden-set evaluation" uses the built-in question set (golden set, derived from sample-corpus) to verify retrieval and anti-hallucination metrics: recall@k / MRR / refusal rate / out-of-range and invalid citations / false refusals. Click Run (a single task slot) → the past-reports area expands to show per-question details and a metrics summary. A real run consumes actual LLM calls (roughly 4–5 minutes per round on `api`, 25–35 minutes on `local`) — for a fast, deterministic offline demo use `--profile offline`.

---

## 10. Data and Privacy

- **Everything is local**: data/mikasa.db (SQLite) + data/uploads (ingested copies) + data/indexes (index files) + data/logs; model configuration lives in `config.yaml` and `.env` inside the data directory (written by the settings panel); chat appearance lives in browser localStorage.
- **Backup** = copy the whole `data/` directory (safest with the service stopped first); the database schema upgrades automatically (the v1→v2→v3 migration path has been rehearsed on a real database), so keep whole-directory backups to allow rollback.
- API keys live only in `.env` (the data directory's `.env` when set from the panel; the repo-root `.env` in the api profile), never in the database and never uploaded; the key is never echoed back to the browser (the API answers with a yes/no flag only). The GitHub repository contains neither data/ nor .env.
- **The one automatic outbound request is the startup update check** — it asks GitHub for the latest version number and nothing else: no local data, no credentials, and it can be switched off in settings (§8.3). Asking, retrieval and ingest all stay on this machine — unless you point the model provider at a cloud API yourself.
- For known boundaries and the failure archive, see docs/known-issues.md and docs/limitations-and-failures.md.

---

## 11. Troubleshooting Quick Reference

| Symptom | Cause / Fix |
|---|---|
| The top bar is stuck on "connecting to the service…" | The service isn't running: double-click Mikasa.bat, or start it manually with `mikasa serve --profile local --port 8787` |
| Chat shows a red "LLM call failed (qwen3:8b…)" | Ollama isn't running (Mikasa.bat starts it automatically; manually, run `ollama.exe serve`); on the api profile it means a missing key or a network problem |
| Upload reports "unsupported file type" / "size limit" | Extension whitelist (md/txt/pdf/docx) / max 500 MB per file |
| Deleting a folder returns 409 with a count | The folder contains subfolders or documents; move them out first (see §6) |
| The KB answers "that isn't in these materials…" | The anti-hallucination refusal feature, not a bug; paraphrase hit rates are verified by evaluation (see §4.1) |
| The free-chat button is greyed out | A limitation of the offline (zero-key demo) profile; available on api/local |
| Session titles don't match what you expected | Auto-distilled to ≤16 characters; rename to lock in your own (see §7) |
| You want a different answering model | Open ⚙ → **Model** in the top right, pick a source, paste the key, Save (see §8.1) — no restart, no `.env` editing |
| The ⚙ panel's model fields are greyed out | The service was started with `--config`/`config.yaml`; the panel reports the file to edit instead |
| You want different retrieval quality | Start with a different profile (api is strongest / local is free); changing the embedding model means re-indexing (`mikasa ingest --reindex`) |
