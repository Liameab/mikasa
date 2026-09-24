# Architecture

> Mikasa: a personal study knowledge-base Q&A system (LLM + RAG) with citation
> tracing and automated evaluation. This document answers "**how the system is
> organized and how data flows**"; for "why it is built this way" see
> [`design-decisions.md`](design-decisions.md) (ADRs), and for evaluation methodology see
> [`evaluation.md`](evaluation.md). All source-code comments are in Chinese.

## 1. Orientation: the data flow of a single question

```
                    ┌─────────── Ingest path (offline / web upload) ───────────┐
  Corpus (md/txt/pdf/docx) → loader → cleaning (text.py) → structural chunking (chunker)
                    → tokens to disk + embedding → SQLite(documents/chunks/embeddings)
                    └──────────────────────────────────────────────────────────┘
                                   ↓ meta.json fingerprint snapshot (for doctor / eval)

                    ┌─────────── Q&A path (CLI / web, three modes) ────────────┐
  Question ─┬→ kb knowledge base: dual-path recall ─→ RRF fusion ─→ rerank (optional) ─→ top_n chunks
            │        injected into the prompt (numbered 1..N) ─→ LLM generation ─→ [n] parse + validate (L1)
            │        ─→ answer + clickable citation tracing (refusal when there is no evidence)
            │        (the reader's "this document" scope is a narrow variant of the same
            │         kernel: both recall paths are filtered by an allow-list *before*
            │         fusion, and nothing is saved to a session; ADR-0034)
            └→ free mode: skips retrieval and injection ─→ direct LLM answer
                     (no citation/refusal protocol; requires an api/local model; see ADR-0013)

                    ┌─────────── Eval path (CLI eval / web background) ────────┐
  golden_set (hand-written / machine-generated) ─→ per-item check ─→ three stages: A retrieval / B protocol (mock) / C LLM-as-judge
                    ─→ eval_runs persisted + markdown report (same orchestration for CLI and web)
                    └──────────────────────────────────────────────────────────┘
```

All three paths share the same `Settings` (one profile switch covers all three
forms) and the same SQLite database. That is what makes this one system rather
than three scripts bolted together.

## 2. Layers and directories

| Layer | Directory | Responsibility |
| --- | --- | --- |
| Entry | `cli/` `web/` `__main__.py` | typer + rich commands; FastAPI + vanilla frontend |
| Application | `pipeline/` | ask orchestration: retrieve → inject → generate → validate |
| Domain services | `ingest/` `index/` `eval/` `papers/` `update/` | ingest, index, evaluation, online paper search (the pluggable algorithm zone), in-app updates (ADR-0022/0024) |
| Providers | `providers/` | LLM (OpenAI-compatible surface + Ollama native) / embedding / reranker / **vision** (ADR-0027) / **image generation** (ADR-0031): Protocol + implementations (ADR-0003) |
| Storage | `storage/` | SQLite connection/repositories + meta.json snapshot |
| Foundation | `config/` `models/` `utils/` `errors.py` | configuration, pydantic models, text/logging |

Dependencies point strictly downward (entry → application → domain →
storage/providers), and `providers` and `storage` have no dependency between
them — the offline and evaluation forms are achieved by "swapping configuration,
not code" (ADR-0001/0003). The system deliberately pulls in no third-party RAG
framework: retrieval and chunking are implemented in-house (ADR-0009–0012), and
the framework layer holds nothing but glue such as FastAPI/typer/rich.

## 3. Ingest path

Source file → cleaning → structural chunking → SQLite → tokenization/embedding,
all in one ingest pass. Paragraph structure (Markdown heading tree, PDF text
layer, DOCX paragraphs) is normalized by each loader into a `Para` intermediate
representation before it reaches the chunker — **the chunker accepts exactly one
input structure**, so a new format only ever needs a new loader.

**Idempotency and incremental rules** (`ingest/service.py`, a design point worth
discussing in a technical review):
- **Content unchanged**: if a document's `file_sha256` already exists → skip.
  Re-runs and duplicate imports have zero side effects (counted as skipped in the
  CLI summary);
- **Same name, changed content**: the old document row is deleted (foreign-key
  cascade clears chunks/embeddings) and then rebuilt, which gives "edit a note,
  re-import it, and it updates in place" its intuitive semantics;
- **Embedding failure**: the whole document is rolled back and marked failed —
  this keeps the vector matrix and the chunk table **always consistent**.
  IndexManager raises on any inconsistency (a half-written index would poison the
  entire database; roll back rather than leave residue);
- Vectors are appended only for new or changed documents (INSERT OR REPLACE),
  embedding incrementally with the same model instead of re-embedding the whole
  database; `mikasa ingest --reindex` provides a full rebuild path (after a
  rebuild, chunk ids shift, so items citing the old chunks are **skipped one by
  one** and named in the report — see evaluation.md §2).

The web upload endpoint, the online-paper import endpoint and the note editor all
share one ingest tail (`documents.ingest_web_file`): all three hand it a file inside
an exclusive `web-tmp` subdirectory plus its sha256, and all three get back the same
byte-identical 201/200/409 response (ADR-0019). Notes pass three extra arguments:
`force=True` (bypass content dedup — otherwise a second note with the same body is
silently skipped), `title=` (the title has to be in place *before* chunking, or the
index text keeps the old name), and `folder_id` (the "save into" choice must happen
inside the same lock). A note is not a new type — it is an ordinary document marked
`source_ref = "note:<key>"`, so retrieval, citations, the folder tree and the reader
need zero special cases (ADR-0021).

## 4. Chunking strategy

(The trade-offs are recorded in design-decisions.md ADR-0012; what follows is the
implementation detail.)

- **Boundary priority**: a heading change always forces a split; long text inside
  a chunk is split progressively by "paragraph → sentence-ending punctuation →
  comma → hard cut", so that meaning is not interrupted mid-phrase;
- **Overlap**: adjacent chunks under the same heading share up to `overlap`
  trailing characters (nothing is shared across headings), which mitigates recall
  misses caused by an answer being cut in half. When capacity runs short the
  overlap tail is **truncated rather than over-emitted**, and a test guards the
  length invariant "chunk ≤ size" (`test_overlap_respects_size`);
- **Heading prefixing**: at ingest time the heading path is prepended to the chunk
  (`# A > ## B\nbody`), which improves recall on both the sparse and the dense
  path. Because generation and display do not repeat the heading, body and heading
  are carried separately in `ChunkSpec` (heading_path is stored as citation
  metadata; page works the same way);
- **The inverse operation of overlap**: the reader view has to reconstruct a
  continuous document from a chunk sequence, which means trimming the overlap
  between chunks — `ingest/stitch.py` (decisions and validation data in ADR-0016).
  It is **derived from the same rules** as this section: nothing is shared across
  headings → never trim across headings; the tail may be truncated → hence a
  near-tail rule in addition to the suffix rule. **When you change the overlap
  rules, look at both places.**
- The tuning knobs (size/overlap/title_prefix) all live in configuration and can
  be ablated (see the evaluation methodology).

## 5. Retrieval path

```
  query ─→ tokenize ─┬→ BM25 recall top_k (tokens pre-tokenized on disk; only the query is tokenized per search)
                     └→ embed the query vector ─→ dense cosine recall top_k
       ─→ RRF fusion (1/(k+rank)) ─→ rerank (reranker, optional) ─→ top_n injected into generation
```

- The sparse and dense paths are complementary: BM25 catches literal matches,
  vectors catch semantic neighbors; fusion depends only on ranks (ADR-0011), and
  scores are never calibrated across paths;
- The dense path uses exact numpy cosine (ADR-0010), with no approximation error;
- Performance (measured on a personal library of a few thousand chunks): a single
  BM25 query <10ms, exact vector search <500ms (linear with scale up to 100k
  chunks). Retrieval was never the bottleneck — which is also the scale premise
  under which a hand-rolled index is viable;
- The reranker has three settings (api/local/none) and can be turned off: stage A
  of the evaluation must run recall with reranking off (decoupled methodology, see
  evaluation.md §1), while the product form enables reranking.

**The local form in practice (M4)**: embeddings run through fastembed (onnxruntime,
CPU-capable) with bge-small-zh-v1.5 (**512 dimensions**; queries get the bge
retrieval instruction prefix), and **the published build ships that model inside
the package** (pinned revision + seeded into the cache at runtime +
`local_files_only`, so the first ingest never touches the network — ADR-0030;
the source install still downloads it once); the rerank backend is none — RRF fusion passes
through in rank order straight to generation (the implementation is retained; for
enabling it see ADR-0014). Generation goes through Ollama's (qwen3:8b) **native
`/api/chat`** (`OllamaNativeLLM`, **no API key required** — the native surface
sends no auth header at all; the api profile is unaffected and still uses the
OpenAI-compatible surface with a placeholder key, argued in ADR-0014 ①). The
native surface is a hard requirement, not a preference: `think` and `num_ctx`
are understood **only** there, and the compatible surface silently ignores them
(ADR-0029). Those are the two local-only knobs: `llm.think` (default `false` —
otherwise reasoning eats the whole output budget) and `llm.num_ctx` (default
`16384` — measured: without it the same prompt is truncated to 2050 tokens,
losing most of the material). Dimension-migration
discipline is a matter of survival for the local profile: the embeddings table
holds one model per database (api bge-m3 at 1024 dimensions ≠ local at 512), so
switching profiles requires `ingest --reindex`, and three safeguards enforce it —
the doctor's three-way consistency check, the dimension guard in
`ExactVectorStore.search`, and the per-item golden check before a run (old chunk
ids are gone → those items are skipped; with nothing left the run is refused;
ADR-0014 ④ / ADR-0026).

## 6. Generation and the citation protocol (three lines of defense against hallucination)

**Prompt protocol** (`pipeline/prompts.py`, the single source of truth — MockLLM
and real LLMs share the same markers, so the two can never drift):
- It uses **plain-text section markers** (【资料片段】 "source excerpts" / 【问题】
  "question" / 【历史对话摘要】 "conversation summary" / 【正在阅读的段落】 "the passage
  being read") rather than structured JSON
  output: more stable for small local models such as Ollama, friendlier to
  streaming, and parse failures stay observable (the format-parse failure rate is a
  must-watch item in every evaluation regression). Section **order is a hard
  constraint**: 【正在阅读的段落】 must come *before* 【资料片段】, or MockLLM's parser
  swallows it into the last excerpt and offline answers silently change meaning
  (ADR-0034);
- Both system prompts append the same **output-format contract**
  (`OUTPUT_FORMAT_CONTRACT`, ADR-0029): lead with the conclusion, use `## `
  sections, use a table when a table is what the material calls for, LaTeX for
  formulas, completeness over brevity — small models do not "remember" these
  forms on their own, they follow them once they are written down;
- Citation numbers `[n]` map one-to-one onto the injected chunk numbers, and **the
  model has no license to invent numbers** — injection order is the numbering, and
  anything out of range is a protocol violation;
- Refusals use a single sentence constant, `REFUSAL_TEXT`: both the evaluation and
  the UI detect a refusal by exact string match, rather than trusting the model to
  comply.

**Three lines of defense against hallucination** (validation escalates layer by
layer, following the "the earlier the check, the cheaper" principle):

| Layer | Location | Check | On violation |
| --- | --- | --- | --- |
| L1 hard validation | Generator, `pipeline/generator.py` | Legality of `[n]` references (within 1..N, well-formed) | Out-of-range or malformed markers are dropped and counted, feeding the evaluation metrics |
| L2 semantic support | Evaluation layer (judge criteria) | Whether the citation actually supports the claim it is attached to | Disclosed through metrics such as citation gold ratio |
| L3 refusal discipline | Evaluation layer | When a refusal sentence appears, the text must carry no `[n]` at all | Separate metric (L3 violations are theoretically unreachable, so any occurrence is a protocol bug) |

The generation output `Answer` (text + the parsed Citation list + a refusal flag)
and `Completion` (including token usage, for cost accounting and evaluation) are
returned separately. The citation-tracing loop closes end to end: a chunk's
heading_path/page travel all the way into the Citation, and the web Q&A page uses
them to render clickable source cards. The reader has one extra model,
`ReaderSource`: **every hit plus whether it was cited**, carried only on that
endpoint's SSE frames — `Answer` and `/api/ask/stream` gain not a single field,
and neither the persisted payload nor the evaluation methodology moves (ADR-0034).

**free mode is a deliberate bypass** (ADR-0013): on the same AskService facade,
`mode="free"` skips retrieval, injection, citation parsing, and refusal detection
to call the LLM directly — defenses L1–L3 constrain only the kb path. The bypass
point is the **service layer**, not the Generator (the Generator's `build_answer`
depends on citation parsing from hits, and free mode has no hits, so it is not
reused); the evaluation harness (`eval/`) hard-codes Retriever + Generator and
never goes through AskService, so free mode is decoupled from evaluation by
construction — protocol metrics are never "contaminated" by free mode, and both
sides stay clean.

## 7. Storage layout

```
data/
├── mikasa.db          # SQLite: WAL mode (readers and writers do not block each other), FK cascades
│    ├── documents / chunks / embeddings  # body text, pre-tokenized tokens, float32 vectors
│    ├── kb_folders                       # corpus folder tree (library page, self-referencing parent_id)
│    ├── qa_folders                       # conversation folder tree (self-referencing parent_id)
│    ├── qa_sessions / qa_messages        # sessions (title/title_manual/folder_id) and history
│    ├── eval_runs                        # eval rows (report_md column = the full report)
│    └── doc_links                        # groundwork for the M6 ③ knowledge chain: table and
│                                         #   repository layer only, no writer yet — it stays empty
├── uploads/           # copies of ingested documents (the single home for web uploads; deleted with the document)
├── note-media/        # note images (one subdirectory per note key, ADR-0027)
├── generated/         # text-to-image output (ADR-0031) — kept outside uploads for the same
│                      #   reason as the two above: otherwise a reindex sweeps images up as documents
├── indexes/meta.json  # derived snapshot: corpus fingerprint / chunk count — the source for the
│                      #   doctor consistency check and for the report's "which corpus this bank was written for" (rebuildable from the DB, never authoritative)
├── eval-reports/      # {run_id}-{name}.md evaluation reports (same origin as the eval_runs table)
├── eval/golden-auto.json  # the machine-generated question bank (ADR-0026)
├── web-tmp/           # staging subdirectories for uploads/imports (cleared in a finally block either way)
├── updates/           # update packages and their .part partials (ADR-0024 resumable download)
├── logs/              # rotating logs
├── auth.json          # PBKDF2 hash + salt for the access password (no plaintext, ADR-0033)
├── config.yaml        # user-writable configuration overlay (written by the settings panel, ADR-0018)
├── .env               # API keys (written by the panel; local only, never echoed back)
└── models/            # runtime target for the bundled embedding model (fastembed cache shape, ADR-0030)
```

**The schema end state is v5.**

- **Version management**: schema_version routing (ADR-0004 revision): older
  databases migrate automatically, step by step, along `_MIGRATIONS` (each step
  idempotent, committed separately, and logged); newer databases fail hard — an old
  program never reads or writes a newer database. The schema end state, historical
  version snapshots (immutable), and migration functions land in the same PR;
- **Conversation tree**: qa_sessions gains three columns, title/title_manual/
  folder_id (v1→v2), and title_manual locks in "automatic summarization never
  overwrites a manual name" (ADR-0015); qa_folders is stored flat with the frontend
  assembling the tree; cycle prevention and empty-folder deletion semantics are in
  ADR-0015;
- **Consistency principle**: the DB is authoritative and meta.json is only a
  snapshot; every deletion of a "document / chunk / vector" goes through a
  foreign-key cascade with no hand-written cleanup path — partial states do not
  exist. One exception is deliberate: session deletion cascades, but **a folder can
  only be deleted when empty** (a NO ACTION foreign key backstops this; a container
  never takes its contents down with it).

## 8. Web service and process model (M3)

`create_app(settings)` is a factory that injects settings explicitly (tests can
pass offline settings pointing at an isolated data_dir); uvicorn `--reload` uses
the `serve_app_factory()` import string. The frontend has zero build chain: plain
HTML + ES Modules, with every dynamic string escaped before it reaches innerHTML
(LLM output is untrusted), and SSE frames parsed by hand from fetch +
ReadableStream (no sse-starlette). That rule has **two controlled exceptions**:
formulas are pulled out *before* `esc()` and handed to KaTeX (doing it after
would render `x < y` wrongly, ADR-0028), and images are allowed only from
**same-origin paths starting with a single `/`** (a remote image is a tracking
pixel, ADR-0031) — both are narrow allow-list openings, not "we forgot to escape
that one".

**Single-process constraint** (a hard design decision for `serve`): the in-memory
index snapshot and a set of **single-slot state machines** (the evaluation
EvalJobManager, the question-bank SynthJobManager, the Ollama PullJobManager and
the update UpdateManager, each behind a threading.Lock) all live inside the
process, so `serve` cannot run
multiple workers (uvicorn --workers is unsupported and reports an error), and
startup validates consistency against the existing database before loading the
snapshot. In other words: **the web form is a single-user interactive application,
not a horizontally scaled service** — the scale boundary is stated honestly in the
README/FAQ.

Four layers of upload safety (`web/routers/documents.py`): sanitize_filename
(Chinese characters preserved, 415 on failure) → extension allowlist, 415 →
`seek()` to measure the real size and reject over-limit with 413 (the file is
never read into memory; a request-body limit middleware backstops it, and an
empty file is a 400) → temp file unlinked in a finally block; at the DB layer
`file_path` is redacted, closing off path probes.

**Online paper search** (M7/M8/ADR-0023) sits in `papers/`: a four-source search
service — arXiv (Atom) and OpenAlex/CORE/DOAJ (JSON) each get a source module,
all **normalised into one `PaperResult`**; sources are fetched in parallel behind
a 30-second page deadline (a source that hangs is skipped and reported). DOAJ is
the fourth source, added 2026-09-19: a key-free open-access journal directory, and
the landing place for Chinese OA journals (decisions and licensing boundaries in
ADR-0023). The rest is unchanged: rotating-interleaved pagination with per-source
degradation, and
a defended PDF downloader (public-address check per redirect hop, PDF sniffing,
50 MB cap). Each source declares what it can do (`SourceCaps`: year filter,
citation sort, recency sort, language filter, open-access handling), and the
filters the source cannot honour come back as `notes` — degradation is reported,
never silently ignored (CORE's year *parameters* are accepted and dropped by the
upstream, so that source filters by query syntax instead). `web/routers/papers.py`
exposes six endpoints — `POST /api/papers/search`, `POST /api/papers/import`,
`GET /api/papers/sources`, `GET/PUT /api/papers/settings`, and
`GET /api/papers/related` (citation relations: related work / cited by /
references — only OpenAlex has the data natively; other sources bridge via DOI)
— and the import path
re-derives the PDF URL from the source API rather than trusting the client, so
the only user input that reaches a URL is a regex-validated `{source, id}` pair.
The import also records `documents.source_ref` (`"arxiv:2401.12345"`), which is
what lets a search result say "already in your library"; the v4 migration adds
that column, and the two re-index keep-lists carry it across a full rebuild.

Paging needs no new endpoint: page N is the same search request with
`offset=(N-1)×50`, because the service's window formula spreads that global
window across the sources — "jump to page N" is just a different offset. The
page *count* is derived on the frontend from the hit totals the sources report
(summed, capped by the upstreams' 10,000-result page-paging limit x source count), and the status line states where that number came
from rather than presenting it as a precise fact.

**In-app updates** (v0.1.1, ADR-0022) live in `update/` and are exposed by
`web/routers/update.py`: `GET /api/update/check`, `POST /api/update/download`,
`GET /api/update/download/status`, `POST /api/update/install`. The server asks GitHub's
`/releases/latest` itself and picks the installer asset out of the API response — **the client
never receives a URL** — then downloads it behind a host allowlist (`github.com` /
`*.githubusercontent.com`; http+loopback is the E2E escape hatch), verifies the installer's sha256 —
**preferring the GitHub API's own `asset.digest`** (the change that followed a 2026-09-21 user
report: previously it always fetched the few-hundred-byte `SHA256SUMS.txt` from github.com, and a
reset TLS handshake in China killed the whole update with the progress bar stuck at 0); only when
the digest is missing (older API, fake source in tests) does it fall back to downloading the
checksum file, with no check dropped — and launches the wizard with double-click semantics
(`os.startfile`). A failed check stays silent (log + status endpoint only), results are cached for
10 minutes, and the frontend offers "skip this version" plus a startup-check toggle in settings.

Since ADR-0024 the download (and the task slot) is resumable and adoptable: the partial lives in
`updates/<asset>.part`, later requests carry `Range: bytes=N-`, transport failures retry with backoff
and continue from that offset, and the slot records worker liveness so a dead thread can be taken
over — `POST /api/update/download` is therefore idempotent (202 with `adopted: true`) instead of a
409. On the frontend one polling loop drives both the dialog and a topbar capsule, so closing the
dialog, switching pages or reloading never interrupts anything, and the installer is only launched
automatically when the dialog is still open (otherwise the capsule waits for a click).

**Formula typesetting** (v0.1.6, ADR-0028): KaTeX 0.18.7 is bundled into
`web/static/vendor/katex/` (including 20 woff2 fonts; all four pages link it, with the vendor
stylesheet placed *before* `style.css` so the project's font sizes still win). `renderAnswer`
typesets in **one pass up front**: extract LaTeX from the raw text → placeholders → the existing
(esc → controlled substitution) pipeline → restore KaTeX's HTML. It **must** happen before `esc()`,
or KaTeX receives `x &lt; y`. Two gates first strip fenced and inline code (`echo $HOME` is not a
formula), then exclude "Chinese text wrapping money" and bare numbers (`价格$5到$10之间`, `$1000$`),
letting everything else through — the strictness here was learned the hard way: the first version
was too tight and silently missed 8 real formulas in one user answer. When KaTeX is absent it shows
the LaTeX source as-is and **never swallows content**.

**Text-to-image** (v0.1.9, ADR-0031): image generation is its own config section (`image:
none|api`, default `none`, kept separate from vision — one takes images in, one puts them out, and
they do not even share request parameters). `POST /api/images/generate` returns **bytes, not a
URL** (upstream image links expire in an hour, so writing one into an answer means a broken image
an hour later) → written to `data_dir/generated/`, served by
`GET /api/images/generated/{name}` (the filename is assembled server-side behind a regex
allow-list, so path traversal has no entry point). **Both outbound legs pass the SSRF gate**: the
base_url the user configured and the image URL the upstream returns (the latter is semi-trusted
input — unchecked, it is a ready-made internal-network probe). The frontend renders same-origin
images only (see above). Generated images **do not enter the knowledge base**.

**Model sources** (v0.1.10, ADR-0032): six presets — Ollama (local) / DeepSeek / SiliconFlow /
Claude / OpenAI / custom. Claude goes through Anthropic's official OpenAI-compatible endpoint (the
docs say plainly that this is a compatibility layer: no prompt caching, unknown fields silently
ignored); the OpenAI preset states that "a ChatGPT/Codex subscription is not an API". The second
step of setting up a local model moved into the app: `POST /api/settings/ollama/pull` (202 +
polling, per-frame progress, cancellable, single-slot state machine) — but Mikasa does **not**
download the Ollama installer itself (a 1.5 GB third-party binary redistribution with version
drift, to save one trip to the website).

**Browser access and the access password** (v0.1.11, ADR-0033): Mikasa was always a web app (the
desktop build is just a pywebview shell), so browser/LAN access is a first-class entry point
(`serve --host 0.0.0.0`). Exposing it to the network installs a **fail-closed gate**: binding to a
non-loopback address **requires a password to be set first** — otherwise the server refuses to
start and prints the exact command to run (`mikasa auth set-password`). The password is
PBKDF2-HMAC-SHA256 with a random salt in `<data dir>/auth.json`; a session is an
**HMAC-signed expiry timestamp** (no server-side state, no database table — changing the password
rotates the key, which instantly invalidates every old session); **loopback sources need no
password** (anyone who can reach 127.0.0.1 is already sitting at your machine). The gate has
exactly one criterion — is the source address loopback — and that criterion cost us a real
"empty gate" bug in this very code (the command line bound `0.0.0.0` while the gate read the
config and still believed it was local, so a LAN address got a 200): **when one criterion has a
second consumer, both must read it from the same place**. There is no account system; multi-user
and cloud hosting are a separate milestone.

**Ask-while-reading in the reader** (v0.1.12, ADR-0034): a persistent ask bar at the bottom of the
reader panel, scope defaulting to "this document" with one click to "whole library", and selected
text becomes context automatically; questions are **asked and discarded** (no session, nothing
persisted). Scoping to a document goes through a keyword-only `document_id` on
`Retriever.retrieve` (allow-list filtering that happens **before `rrf_fuse`** — filtering after
fusion lets the large library flatten the ranks first, which shows up as silent quality loss). The
streaming kernel was extracted into `_kb_stream`, leaving the ask page and the reader to decide
individually whether to persist.

## 9. Eval orchestration (one implementation for CLI and web)

`eval/service.py::run_and_persist` is the shared "run it and persist it"
orchestration used by both the CLI and web background tasks (moved down out of the
CLI in M3): the golden set is loaded by each caller (the CLI prints a banner; the
web POST runs the per-item check synchronously and returns 400 immediately when
no answerable item survives, rather than failing inside the task). Persistence
order: insert a
placeholder row to obtain a run_id → run → render the report and write it back
(the web polls for "row exists but report_md is empty", which naturally aligns
with the frontend's "running / already failed"). Failure semantics: structural
failures (no answerable item left, empty database) raise and are surfaced by the
caller; an individual question failing is a normal part of evaluation and is
recorded per item (record.error) without affecting how the run finishes.

Three-stage methodology, hand-written metric definitions, and judge-bias
corrections are in evaluation.md. At the architecture level there is only one
rule: **evaluation and the product share the same retrieval/generation code
path** (swap configuration, not code). Offline mocks in CI guarantee protocol
correctness; real quality comes from actually running stage C on api/local.

**Question-bank generation** (v0.1.6, ADR-0026) is another job of the same shape:
`POST /api/eval/synthesize` (202 + `GET /api/eval/synthesize/status` for polling), with
`SynthJobManager` following the evaluation job's single-slot + `threading.Lock` semantics; the
bank lands in `<data dir>/eval/golden-auto.json`. The offline profile has no model to generate
questions with, so that endpoint rejects with a 400 outright rather than queueing a doomed job.

## 10. Runtime profiles

| Profile | Command | Notes |
| --- | --- | --- |
| CLI | `mikasa init/doctor/ingest/list/index/ask/chat/eval/auth` | Full capability, rich output; `auth` manages the access password (required before exposing to a LAN, ADR-0033) |
| Web | `mikasa serve [--config/--profile/--host/--port/--reload]` | Four pages (Q&A / library / papers / evaluation) plus a login page, SSE |
| Health check | `mikasa doctor` | Dependencies / API keys / **local inference dependencies (the local profile gates on Ollama/fastembed)** / three-way index consistency; used as the CI smoke test (methodology in limitations-and-failures.md) |
| Real eval run | `mikasa eval run --profile api/local` | 63 questions × three stages; the api profile takes 4-5 minutes per round |

All three profiles share one configuration (switched by profile), one SQLite
database, and one evaluation methodology — an offline session runs the entire flow
with zero API keys (mock/none), which is exactly the default form for CI and demos.

**Configuration lookup order**: an explicit `--config` → `config/config.yaml` (working
directory first, source mode only, then the repo root) → `config/profiles/<profile>.yaml`.
On top of the profile file, a user-writable overlay `<data dir>/config.yaml` (written by the
web settings panel, ADR-0018) is deep-merged — it never applies when `--config`/`config.yaml`
supplied the base, and its `profile:` key is ignored. Secrets never live in any of these: the
panel writes the key to `<data dir>/.env`, and every `.env` in the lookup chain
(`MIKASA_ENV_FILE` → data dir → resource root → next to the exe) is loaded, with the first
file winning for duplicate names.
