# Mikasa

> A personal document QA workspace — local-first RAG with citation tracing and automated evaluation.
> *[中文说明见 README.md](README.md) —— 本仓库以中文为主，这一页是英文版
> （Chinese is the primary language of this repository; this is the English edition）.*

Mikasa is a local document-QA workbench that **does not sit on a RAG framework**: BM25, dense
retrieval, RRF fusion, Chinese-aware structural chunking, the citation protocol and the three-stage
automated evaluation are all implemented in this repository — there is no LangChain / LlamaIndex
layer. That buys transparent algorithms and reproducible numbers, and costs us owning retrieval
quality ourselves: every trade-off, price and mistake is written up in the
[design decisions](docs/en/design-decisions.md) and the
[failure archive](docs/en/limitations-and-failures.md).

**Status**: M0 skeleton → M1 core pipeline → M2 automated evaluation → M3 web UI → M3.5 dual QA
modes → M4 local profile → M4.5 session management (including the first real schema migration v1→v2)
→ corpus folders v3 → table rendering → cross-lingual retrieval → original + translation for cited
English passages → document reader with citation jump → pre-M5 audit (23 fixes, 4 of them
data-safety) → packaged installer → model settings in the panel → the embedding model shipped inside the package (first ingest is offline, ADR-0030) → text-to-image (draw straight from the ask page, ADR-0031) → Claude/OpenAI presets and in-app model pulling (ADR-0032) → browser/LAN access with an access password (ADR-0033) → ask-while-reading in the reader (ADR-0034)
→ an MCP server (plug the knowledge base into AI agents, ADR-0036) → visible token usage (ADR-0038)
→ confidence intervals and paired comparisons in evaluation → direct connections for local/private hosts (no more hangs behind a system proxy) → paper tables inventoried (a reader tab listing every table, plus a "list all tables" phrasing that bypasses sampling and enumerates them all) → the 15 fixes from a full-repo audit (security and data-safety boundaries; see docs/en/limitations §7). Currently **1188 tests passing**,
ruff + mypy clean, ~92% coverage. Windows portable zip and installer builds are published
(v0.1.0 → v0.1.15) with in-app update checking — downloads resume across dropped connections, and
closing the dialog, switching pages or reloading no longer interrupts them (progress lives in a
top-bar pill; ADR-0024).

## Highlights

- **Citation tracing** — every factual claim carries an `[n]` marker that links back to the exact
  source chunk (heading path + page number). The model cannot invent markers: out-of-range numbers
  are counted as violations.
- **Three-layer anti-hallucination** — hard marker validation → citation-support check → refusal
  discipline. Each layer leaves its own trace in the evaluation report; when evidence is missing,
  the system emits a fixed refusal sentence instead of guessing.
- **Three-stage automated evaluation** — (A) retrieval: recall@k / MRR / nDCG; (B) generation
  protocol: citation-gold ratio, out-of-range rate, refusal accuracy; (C) semantic judging:
  LLM-as-judge with position-swap consistency. Real acceptance run: recall@10 = 1.000, zero
  out-of-range citations, 16/16 unanswerable questions cleanly refused. The bank can also be
  **generated from your own corpus** on the evaluation page, so the measurement is not tied to one
  sample corpus (ADR-0026).
- **Cross-lingual retrieval** — a Chinese question against English documents is automatically
  translated into a second query, and both retrieval paths are merged with RRF. Cited English
  passages are shown as *original + Chinese translation* side by side.
- **Document reader with citation jump** — click a citation marker and the source opens in a side
  panel, either as reconstructed text or as the rendered PDF page with the cited region highlighted.
  You can also **ask right there** while reading (scope defaults to "this document", selected text
  becomes context, every hit is listed and jumps back to the highlighted passage), and those
  questions are discarded rather than piling up in the session tree (ADR-0034).
- **Three runtime profiles** — cloud API / local inference / zero-key offline; switch with a single
  `--profile` flag, no code changes, no hardcoded endpoints.
- **Build-free web UI** — four plain HTML/JS pages (chat / library / papers / evaluation) with
  hand-written SSE streaming (no node_modules, no bundler).
- **Session tree** — arbitrarily nested folders for conversations, with auto-generated titles from
  the first question (manual renames are never overwritten).
- **Photos into notes** — point the note editor at a photo (button, drag-and-drop, or paste a
  screenshot): a vision model transcribes it, the text lands at the cursor for you to correct, and
  saving ingests it as a searchable note. The original image is kept alongside the note. Vision is
  its own settings group and its own config section, so generation can stay local while only
  recognition goes to the cloud (ADR-0027).
- **Formula typesetting** — LaTeX in answers is rendered by a bundled KaTeX (offline, no CDN);
  fenced and inline code are left untouched, and prices like `$5` are not mistaken for formulas
  (ADR-0028).
- **Text-to-image** — draw an image straight from the ask page and keep it with the conversation.
  The upstream image is written to disk immediately (its link expires in an hour), the frontend
  renders same-origin images only (a remote image is a tracking pixel), and both outbound legs pass
  the SSRF gate (ADR-0031).
- **Model settings in the UI** — pick a provider (local Ollama / DeepSeek / SiliconFlow / Claude /
  OpenAI / any OpenAI-compatible endpoint, six presets in all), paste a key, test the connection,
  save: the LLM switches immediately, no restart and no `.env` editing. Keys stay in the local data
  directory and are never sent back to the browser; picking local Ollama also lets you **pull the
  model from inside the app** with a progress bar, instead of going back to `ollama pull`
  (ADR-0018 / ADR-0032).
- **In-app updates** — on startup the app checks GitHub for a newer release (silent on failure, and
  switchable off); a one-click download verifies the published sha256 and launches the installer.
  The client never sees a download URL, and the installer must match the expected name inside the
  update directory (ADR-0022).

## Documentation

| Document | Contents |
| --- | --- |
| [Architecture](docs/en/architecture.md) | How the system is organized and how data flows |
| [Design decisions (ADR)](docs/en/design-decisions.md) | Every "why": choices, trade-offs, and decisions later overturned by data |
| [Evaluation](docs/en/evaluation.md) | Three-stage methodology, golden-set mismatch guards, judge-bias correction, acceptance baseline |
| [Limitations & failures](docs/en/limitations-and-failures.md) | Ecosystem pitfalls, heuristic boundaries, and a real bug archive from development |
| [Usage guide](docs/en/usage-guide.md) | End-to-end workflows (paper reading, follow-up questions, what to do when it refuses) |
| [Known issues](docs/en/known-issues.md) | Unfixed issues (with rulings) and unscheduled candidates |
| [Design system](DESIGN.en.md) | Web UI tokens, type scale, radius grammar, z-index ladder and component contracts |

> This page is the English edition of the Chinese-primary [`README.md`](README.md). The technical
> documentation is Chinese too ([`docs/`](docs/)), with an English mirror in [`docs/en/`](docs/en/) —
> the two trees correspond file by file and a guardrail test keeps them that way. Source comments
> are in Chinese, the working language of this project's design notes.

## Quick start

```bash
# 1) Install (development mode; no compilation needed on Windows)
pip install -e ".[dev]"

# 2) Health check (dependencies / keys / index consistency; offline needs no keys)
mikasa doctor --profile offline

# 3) Ingest a corpus (directory or single file; md / txt / pdf / docx)
mikasa ingest sample-corpus --profile offline

# 4) Ask a question (offline demo uses the built-in MockLLM)
mikasa ask --profile offline "What is backpropagation?" --show-sources
```

To use real models, copy `.env.example` to `.env` and fill in your keys (DeepSeek / SiliconFlow);
`mikasa init` generates the config file for the selected profile.

**Fully local inference (no API keys, no cost):**

```bash
# 1) Install local extras (fastembed for CPU embeddings; Ollama itself: https://ollama.com)
pip install -e ".[local]"

# 2) Pull the generation model (~4.9 GB)
ollama pull qwen3:8b

# 3) Should be all green (missing deps / Ollama down / model absent are reported with fixes)
mikasa doctor --profile local

# 4) Build the index (first run downloads bge-small-zh-v1.5, ~100 MB; the packaged
#    build ships that model, so its first ingest is offline - see ADR-0030)
mikasa ingest --reindex --profile local

# 5) Same commands, different profile
mikasa ask --profile local "How does L2 regularization prevent overfitting?" --show-sources
```

## Runtime profiles

| profile | Generation | Embedding / rerank | Use case |
| --- | --- | --- | --- |
| `api` | DeepSeek (OpenAI-compatible) | SiliconFlow bge-m3 / bge-reranker | Real QA and acceptance runs |
| `local` | Ollama qwen3 (native `/api/chat`, **no API key**; the `think`/`num_ctx` knobs exist only there, ADR-0029) | fastembed bge-small-zh (512-d, **shipped inside the package**, ADR-0030) / no local reranker yet | Fully offline inference (see ADR-0014) |
| `offline` | Built-in MockLLM | none (BM25 only) | Zero-key demos, tests, CI |

All configuration lives in `config/profiles/*.yaml` with a fully commented field reference in
`config.example.yaml`.

## Web UI

```bash
mikasa serve                      # api profile (real models)
mikasa serve --profile offline    # zero-key demo
# Options: --host 0.0.0.0 (LAN access) / --port 9000 / --reload (dev)

# Use it from a phone, tablet or another computer (v0.1.11, ADR-0033):
mikasa auth set-password          # set the access password first
mikasa serve --host 0.0.0.0       # bind a non-loopback address
# Without a password that command **refuses to start** and tells you which
# one to run first (fail closed). Once the service is on the network, anyone
# on it could read your documents and spend your model credits - so it would
# rather not open the door than open it unlocked. Loopback (your own browser,
# the desktop app) never needs a password, and your data still stays on your
# own machine.
```

Open http://127.0.0.1:8000/ — four pages:

- **Chat** — SSE streaming, clickable citation markers, multi-turn sessions, and a KB / free-chat
  mode switch (KB mode is strict RAG with citations and refusals; free-chat mode talks to the model
  directly without retrieval or citations).
- **Library** — upload and delete documents in the browser; a folder tree organizes the corpus;
  newly ingested documents are searchable immediately. You can also **write Markdown notes right
  here** — type on the left, watch it render on the right, save, and the note is citable by the next
  question (and editable later). Notes can also start as a **photo**: the editor turns an image into
  text for you to correct before saving, and keeps the original image with the note.
- **Find papers** — search arXiv, OpenAlex, CORE and DOAJ at once (three of the four need no key;
  DOAJ is what brings in Chinese open-access journals) with date-range/language/open-access filters
  and citation- or date-sorting, page through the results with page numbers and jump-to-page (the
  total is summed from the sources' own hit counts and capped by the upstreams' own depth limits), and **click a row to open
  the paper in your browser** — 「详情」 in the row is where the full abstract, import button, and
  the three ways to keep going live: related papers, cited by, and references. Importing pulls the
  open-access PDF straight into the corpus (import, then ask about it right away); recent queries
  are one click away. Results you have
  already imported are marked "already in your library". Each source declares what it supports, so
  options it cannot honour are disabled with a reason rather than silently ignored — a CNKI-like
  experience, not CNKI's data: paywalled full text stays out.
- **Evaluation** — run the golden-set evaluation in the background, poll progress, and read the
  generated report.

API docs (Swagger) at http://127.0.0.1:8000/docs.

## Handing the library to an agent: the MCP server (ADR-0036)

The library is not only a web app — it can be attached to agents like Claude Code or Codex,
so they can search your material:

```bash
# One command to register it (Claude Code; everything after -- starts the server)
claude mcp add mikasa -- python -m mikasa mcp --profile local
claude mcp list          # should report mikasa ✔ Connected
```

There are exactly three tools and **all of them are read-only** (no ingest / delete /
settings, so an agent using your library has no blast radius): `search` returns snippets
with provenance → `read` fetches a chunk and its two neighbours by `chunk_id` → `ask`
returns an answer with `[n]` markers plus a citation list (document / section / page).
Agent questions **leave no trace** in your Q&A history, and on the offline (mock) profile
`ask` says plainly that only search / read are available instead of breaking the session.

## CLI

| Command | Description |
| --- | --- |
| `init` | Initialize the data directory and generate a config file |
| `doctor` | Environment health check (versions, deps, keys, index consistency) |
| `ingest <path>` | Ingest and index; `--reindex` rebuilds everything |
| `list` / `index stats` | Ingested documents / index status |
| `ask "<question>"` | Single question with refusal when evidence is missing (`--show-sources`) |
| `chat` | Multi-turn conversation (kb mode with history summary, free mode raw history) |
| `eval run/list` | Run the evaluation / browse past runs (`--profile offline` needs no keys) |
| `auth set-password` / `clear-password` | Set / clear the access password — required before `serve --host 0.0.0.0` (ADR-0033) |
| `mcp` | Expose the library to agents (MCP server over stdio): three read-only tools `search` / `read` / `ask` (ADR-0036) |
| `usage` | Local cumulative token usage: Q&A (by profile) plus evaluation (by session) and a grand total (tokens only, never converted to money — ADR-0038) |

## Milestones

| | What | State |
| --- | --- | --- |
| M0 | Project skeleton (config system, layout, doc plan) | ✅ |
| M1 | Ingest pipeline + hybrid retrieval + citation protocol + CLI | ✅ |
| M2 | Golden set + three-stage automated evaluation (acceptance run: recall@10 = 1.000) | ✅ |
| M3 | Three-page web UI + SSE streaming + background evaluation jobs | ✅ 242 tests |
| M3.5 | Dual QA modes: knowledge base (kb) / free chat, switchable in the UI | ✅ 259 tests |
| M4 | local profile: Ollama (qwen3:8b) + fastembed CPU embeddings (bge-small-zh-v1.5); rerank/judge off (ADR-0014) | ✅ 282 tests |
| M4.5 | Session management: nested folders, auto-generated titles, rename/delete, first real schema migration v1→v2 (ADR-0004/0015) | ✅ 332 tests |
| M4.5+ | Corpus folders v3 → table rendering → cross-lingual retrieval → original+translation for cited English passages → document reader with citation jump (PDF page rendering + highlight) | ✅ 430 tests |
| M5 | Documentation, sample corpus, git, GitHub release, CI | ✅ (README is Chinese-first since 2026-09-20; this is the English edition) |
| M7 | Online paper search: arXiv + OpenAlex (interleaved paging, per-source degradation) + guarded PDF downloader (ADR-0019) | ✅ 578 tests |
| M8 | Find-papers page of its own: four sources, declared capabilities, filters/sorting, detail panel, "already in library" (ADR-0020) | ✅ 679 tests |
| M6 ① | Markdown notes in the library page: write/render/save, citable immediately, edited in place (a note is an ordinary document tagged `note:`, ADR-0021) | ✅ 679 tests |
| v0.1.1 | In-app updates: silent check → dialog → one-click download → sha256 verify → install (ADR-0022) | ✅ 738 tests |
| v0.1.4 | Resumable downloads, adoptable jobs (ADR-0024); same batch added related papers / cited-by / references and search history | ✅ 815 tests |
| v0.1.5 | A round of fixes only: re-uploading no longer duplicates a document, reindex verifies every copy first, note optimistic locking, cross-process migration lock, deeper paging (OpenAlex/DOAJ to item 10,000), code blocks indexed, relicensed to AGPL-3.0 (ADR-0025) | ✅ 859 tests |
| v0.1.6 | Photos into notes (vision as its own section, recognition as a draft, images kept, ADR-0027) + question banks synthesized from your own corpus (ADR-0026) + **formula typesetting** (bundled KaTeX, ADR-0028) + "local model times out" fixed (`localhost` normalisation) + reconnecting downloads on a bad network | ✅ 918 tests |
| v0.1.7 | The local profile moves to **Ollama's native API**: "thinking mode / context length" knobs (thinking off, context 16384 by default — fixing a silent cut that processed only 2050 tokens of every prompt, ADR-0029) + an output-shape contract (sections, tables, formulas, diagrams) + **v0.1.6 double-click fixed** (leftover websockets stub; release sentinel `smoke_frozen` added) + four-lane audit, 17 fixes (cross-site writes 403, panel host allow-list, interrupted ingests self-heal, reindex mapping persisted…) + formula pre-pass misses (multiline `$$`, spaced `$ x $`) | ✅ 943 tests |
| v0.1.8 | **The embedding model ships with the app**: the first upload no longer needs a network download (the 91 MB bge-small-zh-v1.5 rides inside the installer; a pinned revision fetched at build time, seeded into the cache at runtime, with an offline-ingest release sentinel and a payload assertion, ADR-0030) | ✅ 951 tests |
| v0.1.9 | **Text-to-image**: one click on the ask page, and the picture stays in the conversation. Generation is its own section (off by default), upstream images are saved immediately (their URLs live one hour), the frontend renders same-origin images only (remote images are tracking pixels), and both network hops pass an SSRF gate (ADR-0031). The same release fixes a payload that carried the model twice (with Xet the blobs land at the **cache root** as a second copy, doubling the zip — caught by the release sentinel, located with a CI probe, ADR-0030 patch) | ✅ 977 tests |
| v0.1.10 | Model sources grow: **Claude** (through Anthropic's OpenAI-compatible endpoint) and **OpenAI** presets, six in total, with the compatibility layer's boundaries and 'a subscription is not an API' stated in the UI; **the local model pulls from inside the app** (native /api/pull streamed progress, cancellable, single slot - installing Ollama itself is still manual, no 1.5 GB installer fetched on your behalf); fixes a race where clicking a preset right after opening the panel was overwritten by the async fill-in (ADR-0032) | ✅ 995 tests |
| v0.1.11 | **Browser / LAN access with an access password** (`mikasa auth set-password` plus `serve --host 0.0.0.0`: loopback stays exempt, a non-loopback bind refuses to start without a password, and data still lives on each person's own machine - ADR-0033); **in-app updates fixed** (the expected hash now comes from the GitHub API's `asset.digest` - fetching that small file from github.com could fail on a reset handshake and kill the whole update at 0%); **"recognise this page"** in the PDF reader (one-click OCR of the current page, built in the Codex lane and merged after browser verification) | ✅ 1018 tests |
| v0.1.13 | **MCP server** (`mikasa mcp`: the knowledge base plugged into agents like Claude Code / Codex — three read-only tools search/read/ask, agent questions never saved, ADR-0036) + **visible token usage** (ask-page cost line, evaluation reports, `mikasa usage`; tokens only, no price arithmetic, ADR-0038) + **system proxies handled** (requests to a local Ollama go direct again instead of being swallowed by a system proxy and hanging) + local generation timeout 60s→180s (a long answer no longer drops the whole question) + three reader fixes (fit-to-window default / the ask box grows as you type / the 1000× wait timer) + prompt examples turned into placeholders (small models can no longer copy them, ADR-0040); on the evaluation side: confidence intervals and paired comparisons, claim-level faithfulness (ADR-0042), the RAGAS control group (ADR-0043) | ✅ 1163 tests |
| v0.1.14 | **Paper tables inventoried** (a user report: "list every table in this paper" could not be answered — a global aggregation that top-k sampling cannot fit): a new reader tab **Tables** listing every table chunk of the document (page + preview, click to highlight it in the page view; pure frontend, zero new endpoints), and an inventory intent in chat that **skips sampling and feeds every table chunk in order**, answering as a one-row-per-table list with clickable markers; because small models copy prompt placeholders and marker formats verbatim, the server normalizes the echoed markers deterministically (docs/en/limitations §13) | ✅ 1170 tests |
| v0.1.15 | **The 15 fixes from a full-repo audit** (security: a page route could read any file / the login page reflected input and allowed an open redirect / `--reload --host 0.0.0.0` dropped the auth gate / a key slot name could inject a `.env` line / the save path skipped the SSRF guard. Data: a failed ingest now leaves a `failed` row instead of vanishing, same-named files in one batch overwrote each other, deleting a row on a case-insensitive filesystem could remove another row's only copy, a failed reindex still dropped the property map, a Setext heading swallowed the next line. Crashes: mixed vector dimensions now degrade instead of 500ing, a malformed reranker response is validated and empty results fall back to the fused order, reader offsets are UTF-16, an empty completion raises, and history pairs by role) — every fix landed behind a regression test that failed first; case table in docs/en/limitations §7 | ✅ 1188 tests |
| v0.1.12 | **Ask-while-reading in the reader** (a persistent ask bar at the bottom of the reader: scope defaults to "this document" with one-click "whole library", selected text becomes context, every retrieved hit is listed and clicking one jumps back to the highlighted passage, and questions are never saved to a session - ADR-0034); **"the API key had to be re-pasted on every switch" fixed** (the panel's "saved" hint described the currently active slot only - it now follows the slot of the source you picked; the key was never lost); **local-model question generation no longer times out** (thinking forced off locally, per-call timeout raised to 120s, and the error text now lists three things to try) | ✅ 1048 tests |

## Engineering notes

- Python ≥ 3.11 (3.13 recommended); optional NVIDIA GPU for local inference;
- Source comments and the README are written in **Chinese** (the project's working language; this
  page is the English edition); baseline gates are `ruff format`, `ruff check`, `mypy`, and
  `pytest`;
- Current suite: **1170 tests**, coverage ~92% (see the regression gate in `docs/en/evaluation.md`);
- Zero-compilation install on Windows + CPython 3.13 (all dependencies ship prebuilt wheels;
  see `pyproject.toml` and ADR-0006/0008 for the version-pinning rationale).

## Project layout

```
src/mikasa/
├── cli/          # typer + rich commands
├── web/          # FastAPI + plain HTML/JS (chat / library / papers / evaluation + login)
├── pipeline/     # retrieve → inject → generate → verify orchestration (citation protocol)
├── ingest/       # four-format loaders + Chinese structural chunking
├── index/        # self-implemented BM25 / numpy exact vector search / RRF fusion
├── eval/         # golden-set evaluation: three stages + judge + question generation
├── papers/       # online paper search: four sources + a defended PDF downloader
├── update/       # in-app updates: check / resumable download / verify / launch installer
├── providers/    # LLM (compatible surface + Ollama native) / embedding / rerank / vision / image
├── storage/      # SQLite + meta.json snapshots
└── config/       # pydantic settings (three profiles merged)
sample-corpus/    # original sample corpus (MD / TXT / DOCX / PDF)
evals/            # golden-set source (questions.yaml → golden_set.json)
config/           # profiles/*.yaml + commented example
```

## License

**AGPL-3.0-or-later** — see [LICENSE](LICENSE).

This is a strong-copyleft license, and it is deliberate: the app bundles
[PyMuPDF](https://pymupdf.readthedocs.io/) (AGPL-3.0 or commercial) for PDF parsing and page
rendering, so the distributed builds cannot be MIT. You may use, study, modify and redistribute
Mikasa freely under the same terms; if you run it as a network service for other people, §13
requires you to offer them the corresponding source. Bundled third-party components and their
licenses are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) (regenerate with
`python tools/make_third_party_notices.py`).
