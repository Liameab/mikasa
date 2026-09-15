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

                    ┌─────────── Q&A path (CLI / web, two modes) ──────────────┐
  Question ─┬→ kb knowledge base: dual-path recall ─→ RRF fusion ─→ rerank (optional) ─→ top_n chunks
            │        injected into the prompt (numbered 1..N) ─→ LLM generation ─→ [n] parse + validate (L1)
            │        ─→ answer + clickable citation tracing (refusal when there is no evidence)
            └→ free mode: skips retrieval and injection ─→ direct LLM answer
                     (no citation/refusal protocol; requires an api/local model; see ADR-0013)

                    ┌─────────── Eval path (CLI eval / web background) ────────┐
  golden_set (hand-written anchors + fingerprint) ─→ three stages: A retrieval / B protocol (mock) / C LLM-as-judge
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
| Domain services | `ingest/` `index/` `eval/` | ingest, index, evaluation (the pluggable algorithm zone) |
| Providers | `providers/` | LLM / embedding / reranker: Protocol + implementations (ADR-0003) |
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
  rebuild, chunk ids shift, so golden-set fingerprints will mismatch explicitly —
  that is a feature; see the lesson in evaluation.md §2).

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
retrieval instruction prefix); the rerank backend is none — RRF fusion passes
through in rank order straight to generation (the implementation is retained; for
enabling it see ADR-0014). Generation goes through Ollama (qwen3:8b) on its
OpenAI-compatible `/v1` endpoint, **no API key required** (a placeholder key is
injected; the argument for allowing it is in ADR-0014 ①). Dimension-migration
discipline is a matter of survival for the local profile: the embeddings table
holds one model per database (api bge-m3 at 1024 dimensions ≠ local at 512), so
switching profiles requires `ingest --reindex`, and three safeguards enforce it —
the doctor's three-way consistency check, the dimension guard in
`ExactVectorStore.search`, and an explicit warning when the golden-set fingerprint
mismatches (ADR-0014 ④).

## 6. Generation and the citation protocol (three lines of defense against hallucination)

**Prompt protocol** (`pipeline/prompts.py`, the single source of truth — MockLLM
and real LLMs share the same markers, so the two can never drift):
- It uses **plain-text section markers** (【资料片段】 "source excerpts" / 【问题】
  "question" / 【历史对话摘要】 "conversation summary") rather than structured JSON
  output: more stable for small local models such as Ollama, friendlier to
  streaming, and parse failures stay observable (the format-parse failure rate is a
  must-watch item in every evaluation regression);
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
them to render clickable source cards.

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
│    ├── qa_folders                       # conversation folder tree (self-referencing parent_id)
│    ├── qa_sessions / qa_messages        # sessions (title/title_manual/folder_id) and history
│    └── eval_runs                        # eval rows (report_md column = the full report)
├── uploads/           # copies of ingested documents (the single home for web uploads; deleted with the document)
├── indexes/meta.json  # derived snapshot: corpus fingerprint / chunk count — the source for the
│                      #   doctor consistency check and for evaluation fingerprints (rebuildable from the DB, never authoritative)
└── eval-reports/      # {run_id}-{name}.md evaluation reports (same origin as the eval_runs table)
```

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
ReadableStream (no sse-starlette).

**Single-process constraint** (a hard design decision for `serve`): the in-memory
index snapshot and the evaluation EvalJobManager (a single-slot state machine
guarded by a threading.Lock) both live inside the process, so `serve` cannot run
multiple workers (uvicorn --workers is unsupported and reports an error), and
startup validates consistency against the existing database before loading the
snapshot. In other words: **the web form is a single-user interactive application,
not a horizontally scaled service** — the scale boundary is stated honestly in the
README/FAQ.

Four layers of upload safety (`web/routers/documents.py`): sanitize_filename
(Chinese characters preserved) → extension allowlist, 415 → `read(max+1)`
over-limit, 413 → temp file unlinked in a finally block; at the DB layer
`file_path` is redacted, closing off path probes.

## 9. Eval orchestration (one implementation for CLI and web)

`eval/service.py::run_and_persist` is the shared "run it and persist it"
orchestration used by both the CLI and web background tasks (moved down out of the
CLI in M3): the golden set is loaded by each caller (the CLI prints a banner; the
web POST validates the fingerprint synchronously and returns 400 immediately on
mismatch, rather than failing inside the task). Persistence order: insert a
placeholder row to obtain a run_id → run → render the report and write it back
(the web polls for "row exists but report_md is empty", which naturally aligns
with the frontend's "running / already failed"). Failure semantics: structural
failures (fingerprint mismatch, empty database) raise and are surfaced by the
caller; an individual question failing is a normal part of evaluation and is
recorded per item (record.error) without affecting how the run finishes.

Three-stage methodology, hand-written metric definitions, and judge-bias
corrections are in evaluation.md. At the architecture level there is only one
rule: **evaluation and the product share the same retrieval/generation code
path** (swap configuration, not code). Offline mocks in CI guarantee protocol
correctness; real quality comes from actually running stage C on api/local.

## 10. Runtime profiles

| Profile | Command | Notes |
| --- | --- | --- |
| CLI | `mikasa init/doctor/ingest/list/index/ask/chat/eval` | Full capability, rich output |
| Web | `mikasa serve [--profile/--host/--port/--reload]` | Three pages (Q&A / documents / evaluation), SSE |
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
