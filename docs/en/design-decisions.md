# Design Decisions (ADR)

> Mikasa's "why" archive. Companion code: the `ADR-XXXX` references in the header
> comments of each `src/mikasa/` module point at entries in this file; this document
> and [`architecture.md`](architecture.md) (how things are organized) are
> complementary, and this one carries the main body: **why it was built this way and
> not that way**.
>
> This file was completed after M3 wrapped (2026-09-09); entry dates are the milestone
> each one belongs to. Exact decision dates were not recorded day by day at the time,
> and this document does not pretend otherwise.

## Reading Conventions

- Numbering: ADR numbers were assigned on the fly when the **first landed code file**
  was written, and recorded in its comments. They are therefore not strictly
  chronological; the code comments are authoritative. This document was written after
  the fact without renumbering, to avoid breaking existing references across the
  repository.
- **ADR-0005 is a gap**: a planning-phase draft that was skipped and never landed any
  decision, with zero references in code. The hole is kept (renumbering would orphan
  every existing comment, which is not worth it).
- Status: `Accepted` (current) / `Superseded` (replaced by a later decision, kept for
  its record value). Superseded decisions are not deleted — in technical review, how a
  decision was overturned by data is often worth more than the decision itself.

| ID | Topic | Status |
| --- | --- | --- |
| ADR-0001 | Three profile configurations (api / local / offline) | Accepted |
| ADR-0002 | Secrets via environment variables only, never in YAML | Accepted |
| ADR-0003 | Provider Protocol + OpenAI-compatible protocol unifying all three backends | Accepted |
| ADR-0004 | Minimal SQLite schema_version migration | Accepted (revised in M4.5, see entry) |
| ADR-0005 | (numbering gap from the planning phase, see Reading Conventions) | — |
| ADR-0006 | openai SDK version pinning and a TLS escape hatch | Accepted |
| ADR-0007 | Text-cleaning philosophy: faithful to the original, no punctuation conversion | Accepted (original version Superseded) |
| ADR-0008 | Chinese tokenization: two implementations + automatic fallback | Accepted |
| ADR-0009 | Hand-written BM25, no FTS5 / Elasticsearch | Accepted |
| ADR-0010 | Exact numpy vector search, no FAISS / hnswlib | Accepted |
| ADR-0011 | RRF fusion, no score weighting | Accepted |
| ADR-0012 | Structured chunker: heading boundaries + semantic split priority | Accepted |
| ADR-0013 | free Q&A bypass: decoupled from kb/eval, mode not persisted | Accepted |
| ADR-0014 | Landing the local profile: keyless placeholder + local reranking/judge disabled + embedding-dimension discipline | Accepted |
| ADR-0015 | Session management upgrade: folder tree + three-path title lock + suggest fallback semantics | Accepted |
| ADR-0016 | Reader view: backend seam removal + original-file allowlist + the boundary of exposing body text | Accepted |
| ADR-0017 | Merging the "Page" view + page-number alignment + the Ollama context window | Accepted |
| ADR-0018 | Configuring the LLM from the web settings panel: user config overlay + live re-apply | Accepted |
| ADR-0019 | Online paper search: two free sources, interleaved pagination, a defended downloader | Accepted (point 8 superseded by ADR-0020) |
| ADR-0020 | "Find papers" becomes a page of its own: three sources, declared capabilities, an honest paging contract | Accepted |
| ADR-0021 | Notes are ordinary documents: a `source_ref` marker, no schema change, force-ingest past content dedup | Accepted |
| ADR-0022 | In-app updates: the client never sees a URL, checksums ship with the package, failed checks stay silent | Accepted |
| ADR-0023 | A fourth source, DOAJ: key-free Chinese open-access journals, the licensing line, and no WAF bypassing | Accepted |
| ADR-0024 | Resumable downloads, adoptable jobs: the "observation window" and slot self-healing (amends ADR-0022 points 3 and 7) | Accepted |
| ADR-0025 | Relicensed to AGPL-3.0: the distributed build bundles AGPL PyMuPDF (change the license, not the dependency) | Accepted |
| ADR-0026 | Synthesized question banks plus per-item verification: evaluate your own corpus, not just the sample one | Accepted |
| ADR-0027 | Photos into notes: vision as its own config section, recognition as a draft, images anchored to the note key | Accepted |
| ADR-0028 | Formulas are typeset locally: KaTeX is vendored into the build | Accepted |
| ADR-0029 | The local profile uses Ollama's native API: think / num_ctx knobs plus an answer-shape contract | Accepted |

---

## ADR-0001 Three profile configurations (api / local / offline)

- Status: Accepted | M0 (2026-09)

**Problem**: One codebase has to run in three shapes — cloud API (real models), local
inference (Ollama + CPU embeddings), and zero-key offline (demo / test / CI). If
endpoints and model names were scattered through the code, switching shapes would mean
editing code, and secret management would have no single entry point.

**Decision**: Three small files, `config/profiles/{api,local,offline}.yaml`, plus
`config.example.yaml` as a fully commented field reference. `--profile` selects one at
runtime; a profile declares only its overrides, which are recursively merged with the
default configuration via `_deep_merge`. All model names, endpoints, and batch
parameters live in YAML, with **zero hardcoding in code** (when `deepseek-chat` was
announced for deprecation, swapping it took one line of configuration). Every config
model uses pydantic v2 `frozen=True`: construction is validation, and the type is the
documentation.

**Consequences**: Switching between the three shapes is one flag. The cost is a larger
configuration surface, backstopped by `config.example.yaml` as the field reference.
Evaluation also gets "same question set, multiple configurations" for free (see the
three evaluation stages in evaluation.md — stages A and B deliberately run different
configurations, both enabled by this mechanism).

**Code**: `src/mikasa/config/settings.py`; `config/profiles/*.yaml`.

## ADR-0002 Secrets via environment variables only, never in YAML

- Status: Accepted | M0 (2026-09)

**Problem**: Writing literal secrets into the YAML configuration is equivalent to
committing them alongside the code; and the three profiles still need to share a single
secret-injection mechanism.

**Decision**: Configuration strings support `${ENV_VAR}` and `${ENV_VAR:-default}`
placeholder expansion (`settings.py::expand_env_vars`, applied recursively over
dict/list/str). Secret fields carry only `api_key_env` (the name of the expected
environment variable); the actual value comes from `.env` (loaded by python-dotenv) or
the system environment. Model names may ship with defaults in YAML; secrets never do.

**Consequences**: `.env` and `.env.example` are separate, so the repository can be
published safely; `doctor` can report only which variable name is missing, without ever
touching the value.

**Code**: `src/mikasa/config/settings.py`; `.env.example` at the repository root.

## ADR-0003 Provider Protocol + OpenAI-compatible protocol unifying all three backends

- Status: Accepted | M0-M1 (2026-09)

**Problem**: The generation side has three classes — cloud API, local Ollama, offline
mock — and the same is true of the embedding and reranking sides. Writing a dedicated
call path for each would make providers proliferate, and it would fork
evaluation/offline from the real pipeline ("the mock passes but the real thing fails"
is the worst kind of fork).

**Decision**:
1. Define one `Protocol` per capability (`LLMProvider` / `EmbeddingProvider` /
   `RerankerProvider`); external code depends only on the protocol and never sees the
   implementation;
2. **api and local share a single implementation**: DeepSeek, SiliconFlow, and Ollama
   are all compatible with the OpenAI Chat Completions protocol (Ollama exposes a
   `/v1`-compatible endpoint), so one HTTP client covers all of them — local and api
   differ only in base_url and model;
3. The mock (`MockLLM`) is a complete implementation of the protocol: deterministic
   output, zero keys, exercising the entire retrieval stack and citation protocol —
   quality is guaranteed by real models, protocol correctness is guaranteed by the mock
   in CI every day;
4. `complete()` returns a `Completion` (text + token usage) — usage belongs in the
   protocol rather than in a side-channel callback, because both evaluation reports and
   Q&A cost accounting depend on it.

**Consequences**: The three backends behave isomorphically and differ only in
configuration; evaluation stage B uses the mock to regression-test the citation protocol
offline in CI, and only stage C brings in real models. The cost: the mock has no
semantics (it only demonstrates the protocol), hence the honest limitation that "an
offline run is a protocol self-check, not a measure of real quality" (evaluation.md §5).

**Code**: `src/mikasa/providers/{llm,embedding,reranker}.py`, `providers/__init__.py` (factory).

## ADR-0004 Minimal SQLite schema_version migration

- Status: Accepted | M1 (2026-09)

**Problem**: `mikasa.db` gains tables and columns as features evolve; after an upgrade,
an old database may silently break on missing columns or produce wrong results.
Bringing in a mature migration framework (alembic et al.) is overkill for a
single-person project.

**Decision**: A `schema_version` table records the current version; on startup a version
mismatch **fails loudly** ("delete the old database and re-run ingest", or use the CLI
migration command), and never silently plays compatible with `CREATE TABLE IF NOT
EXISTS`. The version constant is currently 1 — one shot, without pretending there were
multiple migrations.

**Consequences**: The upgrade path is explicit and user-visible; database-structure
consistency is backstopped by a hard check, and no "half-migrated" state exists. The
cost: during early iteration, version bumps require a rebuild (ingest is idempotent, so
the rebuild cost is bounded — see "ingestion pipeline" in architecture.md).

**Code**: `src/mikasa/storage/db.py`; the `schema_version` validation logic.

---

**Revised decision (M4.5, 2026-09-09)**: The session-management upgrade (title column /
folder table) was the first real migration — for the first time the cost of rebuilding
exceeded the cost of migrating (27 sessions / 88 messages of real user data), so the
decision was revised into a **dual-track** design, with the original spirit intact:
- **Older database → automatic step-by-step migration** (no more error-and-delete):
  `_MIGRATIONS = {target_version: migration_function}` runs along the chain — each step
  is internally idempotent (ALTER guarded by `PRAGMA table_info`, backfill resumed on the
  `title IS NULL` condition so a crash mid-way can be re-run from where it stopped), and
  each step commits independently once with a `logger.info` trace; the in-database
  version stays at the "last successful step", so there is no half-migrated window where
  the version row runs ahead;
- **Newer database → hard error, kept as-is**: an old binary never reads or writes a
  newer database (preventing new column semantics from being misread by old code, such
  as the v2 title_manual lock); a version gap (missing migration path) likewise
  hard-errors and stops in place, leaving data intact;
- **New constraints**: the final-state schema SQL, the historical version snapshot
  (`_SCHEMA_V1_SQL`, immutable), and the migration functions must land in the same PR;
  the final-state `_SCHEMA_SQL` runs **only on a brand-new database**, while an existing
  database goes through migration functions alone (the final-state SQL is all IF NOT
  EXISTS, making it a no-op on an old database and never adding columns to old tables —
  adding columns is ALTER's job);
- **Status**: the M4.5 entry remains Accepted (the original "delete and rebuild"
  paragraph is superseded by this section).

**Code**: `src/mikasa/storage/db.py` (the three branches in init_db + `_upgrade` +
`_migrate_v1_to_v2`); migration tests in `tests/unit/storage/test_db_migration.py`.

## ADR-0006 openai SDK version pinning and a TLS escape hatch

- Status: Accepted | M1 (2026-09)

**Problem**: Since 3.0 the openai SDK has swapped its underlying transport (httpx →
bundled httpx2): the API surface is unchanged but the behavior and the dependency surface
change completely. Some environments (corporate proxies, older TLS stacks) may fail to
connect.

**Decision**: Pin `openai>=3.3,<4` (the new transport is the main path), with a comment
in pyproject stating the escape hatch: if TLS problems appear, fall back to `openai<3`.
This is a **configuration-level** fallback; the code uses only the stable Chat
Completions API and is decoupled from the SDK's major version. One related conclusion:
the httpx2 bundled with 3.x and the classic `httpx` are **two coexisting packages**;
starlette's `TestClient` needs the classic httpx transport, so dev dependencies
explicitly declare `httpx>=0.27,<1` — the two package names coexist without conflict, so
do not remove it.

**Consequences**: The dependency strategy is "pin a known-good range + leave an escape
hatch for each range + use only cross-version-stable APIs in code". See ADR-0008 (jieba)
for the same strategy — the project uniformly distrusts the idea that pinning a single
version solves the problem.

**Code**: the version-pinning section of `pyproject.toml` (with comments).

## ADR-0007 Text-cleaning philosophy: faithful to the original, no punctuation conversion

- Status: Accepted (original version Superseded) | M1 (2026-09)

**Problem**: Chinese documents in a Windows environment come with three classic
pitfalls — legacy encodings such as GBK, leftover PDF headers and footers, and fused
duplicate lines (line-by-line text extracted from PDFs). Whether and how aggressively to
clean before ingestion directly determines downstream display quality.

**Original decision (superseded)**: Do **full-width → half-width normalization** (mapping
`０-９`, `Ａ-Ｚ`), on the assumption that "fewer punctuation marks on the retrieval side
is better". It was overturned in review before it landed:

**Revised decision (current)**: Cleaning performs only semantics-preserving format
normalization — trimming and collapsing whitespace, filtering control characters,
removing empty lines; **no full-width/half-width punctuation conversion at all**. The
rationale comes from evidence in downstream dependencies:
1. Citation tracing display and chunk sentence splitting both depend on the original
   typesetting (breaking at `。"` changes sentence boundaries);
2. Retrieval-side tokenizers (jieba / bigram) ignore punctuation anyway, so conversion
   yields zero recall benefit;
3. Conversion only serves "looking tidy", while what it damages is display fidelity and
   reproducibility.
Encoding detection is also consolidated here (legacy encodings such as GBK are converted
to UTF-8 at this point), while header/footer and duplicate-line problems are solved at
the loader layer (those are "line" semantics, not "character" semantics).

**Consequences**: There is exactly one cleaning site in the repository; Chinese
punctuation (""，。…) is stored exactly as it appears in the source. Lesson on file:
"cleaning aggressiveness must be justified layer by layer by its downstream consumers
(display, sentence splitting, tokenization), not by an intuition about tidiness" — a
favorite over-cleaning counterexample in technical review.

**Code**: `src/mikasa/utils/text.py`.

## ADR-0008 Chinese tokenization: two implementations + automatic fallback

- Status: Accepted | M1 (2026-09)

**Problem**: The project needs Chinese tokenization. jieba is the de facto standard, but
new setuptools releases since 2026 remove `pkg_resources`, so `import jieba` hard-fails
in a fresh environment (jieba#1043; the officially endorsed stopgap is pinning
`setuptools<82`). Depending on jieba directly means the assumption "if it installs, it
runs" can shatter on a user's machine at any time; pinning versions alone would stake the
project's fate on a single stopgap.

**Decision**: Three layers of defense instead of a single point of dependency —
1. `Tokenizer` is defined as a **function protocol** (`Callable[[str], list[str]]`);
   callers never see the implementation;
2. Two implementations: jieba (quality first) and a pure-Python character bigram (zero
   dependencies, deterministic); if the jieba import fails (ImportError — a real failure;
   a silenced deprecation UserWarning is not masked) it automatically falls back to
   bigram, with the probe result cached;
3. On versions, the official stopgap is still applied — `jieba==0.42.1 + setuptools<82` —
   but as a **performance optimization rather than a survival dependency**: if the
   stopgap breaks, the worst case is falling back to bigram, which is not fatal;
4. The impact of tokenization quality is quantified: the gap between bigram and jieba on
   retrieval metrics is measured and disclosed in the evaluation (evaluation.md).

**Consequences**: `doctor` can report plainly that "jieba is unavailable, degraded to
bigram" (see limitations-and-failures.md), rather than users quietly receiving degraded
quality without knowing it.

**Code**: `src/mikasa/index/tokenizer.py`; the jieba check in `cli doctor`.

## ADR-0009 Hand-written BM25, no FTS5 / Elasticsearch

- Status: Accepted | M1 (2026-09)

**Problem**: Selecting the sparse retrieval path. SQLite FTS5 is ready-made and
Elasticsearch is the industry standard, but neither fits: FTS5's default Chinese
tokenization is essentially unusable (attaching your own tokenizer amounts to a
half-implementation), while ES is a sledgehammer for a personal knowledge base (JVM +
operations).

**Decision**: Hand-write Okapi BM25 (~100 lines):
1. **Educational and technical-review value**: IDF, term-frequency saturation, and length
   normalization (k1/b) are information-retrieval classics, with formulas that are
   visible and tunable and that support ablations in the evaluation (this is what decides
   the trade-off — the project is a showcase project, and algorithmic transparency is an
   asset, not a liability);
2. **Scale fit**: a personal library holds a few thousand chunks, and full linear scoring
   measures under 10ms, so the complexity payoff of an inverted index is zero;
3. **Performance engineering**: the full text is **tokenized ahead of time and persisted**
   at ingest (`chunks.tokens`, JSON), so a query tokenizes only the query. Build is
   O(total terms) and query is O(number of documents × query terms); documents are
   indexed in fixed ascending chunk_id order and results come back as [row number,
   score]. The smoothed IDF formula is `ln(1 + (N-df+0.5)/(df+0.5))` to avoid negative
   scores.

**Consequences**: Gains a fully explainable IR module and a clean ablation surface; the
cost is giving up FTS5's mature edge features (synonyms and the like), which a personal
library does not need.

**Code**: `src/mikasa/index/bm25.py`.

## ADR-0010 Exact numpy vector search, no FAISS / hnswlib

- Status: Accepted | M1 (2026-09)

**Problem**: The dense vector path needs a search engine. FAISS / hnswlib are the
standard answer, but this project is constrained to "zero-compile installation on
Windows" — neither ships a prebuilt wheel on Windows (faiss-cpu is conda-only; hnswlib
is source-only), so pulling them in means asking users to install a compiler.

**Decision**: Exact (brute-force) search with numpy: row-normalized matrices plus matrix
multiplication — the dot product is the cosine.
1. **Scale argument**: a personal library is under 100k chunks, and numpy matrix
   multiplication measures under 500ms; ANN's speedup payoff does not hold at this scale;
2. **A bonus of exact search**: no approximation error, so recall in the evaluation is
   *true* recall and the experimental conclusions are clean (an approximate index would
   fold index error into the ablation conclusions);
3. Memory budget: n × dim × 4 bytes — 10k chunks × 512 dimensions ≈ 20MB, no pressure
   for a personal library;
4. The interface is defined as a `VectorStore` Protocol (runtime_checkable), preserving
   an extension point for swapping in an ANN backend later.

**Consequences**: Trading away the "sophistication" of a library choice for zero-compile
installation and an exact, reproducible evaluation surface. Structurally identical to
ADR-0009: **implement it yourself after a scale argument, rather than reaching for a
library by default**.

**Code**: `src/mikasa/index/vector_store.py`.

## ADR-0011 RRF fusion, no score weighting

- Status: Accepted | M1 (2026-09)

**Problem**: How should the results of the two paths (BM25 + dense) be merged? Linear
weighting is the most intuitive approach, but BM25 scores and cosine similarities have
entirely different units and distributions, so the weights would have to be tuned per
corpus and would be brittle; normalization in turn introduces its own calibration
problems.

**Decision**: RRF (Reciprocal Rank Fusion): `score = Σ 1/(k + rank)`, with k=60 (the
classic value).
1. It depends on ranks only and requires **no cross-path calibration** whatsoever;
2. It is a stable and effective fusion baseline in Kaggle and search competitions, with
   a track record;
3. It can be computed by hand and walked through in a demo (a plus for a showcase
   project).
Known properties are disclosed as they are: a document must be ranked in both paths to
earn scores from both, and a document unique to one path but ranked very high can still
win — the evaluation shows evidence that fusion beats a single path, rather than only the
good news.

**Consequences**: The fusion implementation is 20 lines; RRF's effect is evidenced by
stage A metrics.

**Code**: `src/mikasa/index/hybrid.py`, `pipeline/retriever.py`.

## ADR-0012 Structured chunker: heading boundaries + semantic split priority

- Status: Accepted | M1 (2026-09)

**Problem**: The first gate on RAG retrieval quality is chunking. Fixed-length splitting
(a hard cut at 400 characters, say) is the simplest to implement, but it cuts sentences
and paragraphs in half, damaging both word meaning and citation granularity — retrieval
quality and the tracing experience suffer together.

**Decision** (full strategy in "chunking strategy" in architecture.md; the trade-offs are
recorded here):
1. **Structure first**: a heading change is a natural chunk boundary (the corpus consists
   of original notes with Markdown structure, and headings are the strongest semantic
   separator);
2. **Within a block, split long text by "paragraph → sentence-final punctuation → comma →
   hard cut" priority**, avoiding broken word meaning — better a slightly shorter chunk
   than a cut meaning;
3. **Overlap window**: adjacent chunks share up to `overlap` characters at the tail,
   mitigating recall loss from "an answer sliced in half"; overlap continues only under
   the same heading and never crosses headings;
4. **Heading prefixing**: at ingest the heading path is prepended to the chunk (for
   retrieval), while body and heading are carried separately (so display and generation do
   not duplicate the heading) — which makes an ablation possible;
5. **Length invariant (chunk ≤ size)** as a hard constraint: paragraphs are joined with
   `\n\n` and the window budget counts separators; the overlap tail is **truncated rather
   than over-issued** to fit remaining capacity — this is where a real out-of-bounds bug
   occurred, now guarded by a test (`test_overlap_respects_size`).

**Consequences**: The chunker becomes a core component that is unit-testable and
ablatable; every knob of the chunking strategy (size/overlap/title_prefix) lives in
configuration and feeds the evaluation.

**Code**: `src/mikasa/ingest/chunker.py`, `ingest/service.py`.

## ADR-0013 free Q&A bypass: decoupled from kb/eval, mode not persisted

- Status: Accepted | M3.5 (2026-09-09, feature release driven by user-reported friction)

**Problem**: The system only did strict RAG — any question outside the knowledge base was
refused with a uniform refusal template (an M2 acceptance feature that must not change).
But once the user connected a real LLM API (DeepSeek), knowledge outside the library that
came up while writing practice questions had no outlet, and the learning experience was
locked shut.

**Decision**: Split Q&A into two modes, with the default kb keeping its behavior unchanged
word for word:
1. **The bypass point is in the AskService layer** (the `mode="free"` branch), not in the
   Generator: the Generator's `build_answer` depends on resolving citations from hits (no
   hits, no citations), and adding a "no-citation mode" to the Generator would stuff RAG
   semantics into a component that should not know about them. free talks to the LLM
   directly and assembles `Answer(citations=[], refused=False, ...)` by hand — the bypass
   is deliberate design, and the three defense layers (L1 hard validation / L2 support /
   L3 refusal discipline) constrain only kb;
2. **mode is not persisted**: `qa_messages` has no mode column, avoiding a schema
   migration (the `schema_version=1` hard check from ADR-0004 stays untouched). Replay and
   rendering discriminate using existing columns: kb's `latency_ms` always contains a
   `retrieve` key, free has only `generate` — the history endpoint decides [n] chip
   rendering from key presence (a `[1]` in a free turn's body text is model output, not an
   out-of-bounds red flag); (M4.5 note: the session-management upgrade broke
   `schema_version=1` for the first time and moved to v2, adding title/folder_id columns,
   and the mode-not-persisted discrimination design survived its first migration unshaken
   — the `qa_messages` structure is unchanged, see ADR-0015);
3. **The guard keys off `settings.llm.backend`, not the profile string**: profiles are
   decoupled from backends (YAML can override), and the mock has no semantics → free
   always raises ConfigError;
4. **free is decoupled from evaluation**: `eval/` hardcodes Retriever + Generator and does
   not go through AskService, so protocol metrics are naturally unpolluted by free mode;
   multi-turn free injects the most recent ≤5 turns of **raw messages** (no kb-style
   summary compression, preserving referential continuity), and the FREE system prompt
   suppresses [n]/refusal-template contamination from earlier turns; for turns without
   citations (free turns) the kb summary switches to the honest wording "已作答（未引用知识库）"
   ("answered without knowledge-base citations"), no longer falsely claiming that citations
   were given.

**Contract**: CLI `--mode` (typer Literal choices) → ConfigError at the service layer (red
text, exit 1); the Web request body's `mode` is validated by a pydantic Literal (invalid
values → 422) → AskService guard (free+mock → 400 error envelope); in SSE streaming the
guard throws **before the generator's first yield** → the try in `_to_frames` produces
**exactly one error frame, zero meta/done**; the frontend greys the button out when
`/api/health.profile == "offline"` (a progressive-enhancement signal; the backend guard
remains the final authority).

**Consequences**: Three entry points (CLI / Web non-streaming / Web streaming) each need
mode pass-through and guard tests; the UI's "switch at any time within a session"
semantics means every question carries the switch state in effect at the time, and
messages can be replayed and classified.

**Code**: `pipeline/{ask,prompts}.py`, `cli/__init__.py`, `web/routers/qa.py`, `web/static/`.

## ADR-0014 Landing the local profile: keyless placeholder / local reranking and judge disabled / embedding-dimension discipline

- Status: Accepted | M4 (2026-09-09)

**Problem**: M1 already stubbed out all the local-profile provider code (shared
OpenAICompatLLM, LocalFastEmbed, factory dispatch, the `[local]` extra), but three hard
blockers kept `--profile local` from actually running: (1) the Ollama-compatible endpoint
needs no key, while `_get_client` raises ConfigError for any keyless configuration;
(2) the local reranker / judge depend on fastembed cross-encoders and a two-vendor judge
setup, and enabling them directly would drag "fully local" back into a calibration
quagmire; (3) api and local have different embedding dimensions (bge-m3 at 1024-d vs
bge-small-zh-v1.5 at 512-d), so switching profiles without rebuilding the index would
silently degrade or even crash the dense path. This entry records five landing points:

**① Keyless placeholder key — the pass-through point is the provider layer, not the settings layer**
Ollama's `/v1` does not validate `Authorization` (it only requires a non-empty value), yet
the openai SDK enforces a non-empty api_key at construction time. The local profile sets
`api_key_env: ""` → `api_key` is always None, so the provider layer injects the module
constant placeholder `_LOCAL_API_KEY = "ollama"`. The pass-through point is deliberately
not in the settings layer: if the config layer permitted "no key", the api profile would
also be silently let through whenever a key is missing (the worst failure mode). Putting
it in the provider layer, branching on `config.backend`, leaves api's fail-early semantics
(ConfigError: "please put your key in .env") untouched word for word. A future judge going
through the same OpenAICompatLLM inherits the pass-through automatically.

**② Local reranking (reranker backend) stays none**
LocalReranker's code and the `[local]` dependencies are both present, but M4 does not
enable it: RRF fusion retrieval is already evidenced by the evaluation (recall@10 = 1.000),
while the local reranker's benefit has not been validated on real data — enabling an
unproven component would only fork the metrics (two configurations answering the same
questions would no longer be comparable). **A fact added during M4 implementation:
fastembed 0.8.0 has removed every reranking API** (no top-level TextCrossEncoder /
TextReranker, and the rerank submodule does not even exist) — LocalReranker is a starting
point kept in the shape of an earlier API, so switching to `local` is not a one-line
configuration change: it requires a version/implementation choice first (fall back to
0.7.x or switch to a standalone reranking package; see the class docstring in reranker.py
and limitations-and-failures.md §4), and the trade-off flips with the evidence.

**③ Local judge stays disabled**
Stage C's semantic judge relies on "two-vendor hedging" to prevent self-preference (Qwen
judging DeepSeek, and vice versa). Locally there is only one Ollama model, so there is
nothing to hedge with; self-scoring by the same model has no quality calibration, and
mixing it into the acceptance baseline would contaminate how the metrics are interpreted.
Local evaluation therefore runs only retrieval-layer and protocol-layer metrics
(recall/MRR/nDCG, out-of-bounds rate, refusal rate); these layers do not involve the LLM,
and the metrics are isomorphic to api/offline. Evaluate what a system can be evaluated on
first — disclosed honestly.

**④ Embedding-dimension migration discipline (switching profiles requires a reindex, three layers of defense)**
`chunk_id` is the primary key of the embeddings table (exactly one vector per chunk) →
single database, **single-model semantics**. api (bge-m3, 1024-d) and local
(bge-small-zh-v1.5, 512-d) vectors cannot coexist in the same table or be queried
together. The discipline: switching the embedding model requires `mikasa ingest --reindex`
(ingest is idempotent, so the rebuild cost is bounded), with three layers of defense:
1. **doctor's three-way index consistency check** (`_check_index_consistency`): snapshot
   `meta.json` model/dimensions × in-database vector row counts (grouped by model, total
   chunk count, the set of dimensions) × current configuration; any mismatch prints a red
   line with reindex guidance — both "the whole database is on the wrong model" (snapshot
   is bge-m3 while the config is bge-small) and "partial migration" (two models left in the
   database) are caught, the latter via GROUP BY row counts rather than taking the latest
   row (`LIMIT 1` would mask a majority of stale vectors);
2. **Retrieval-side dimension defense**: `ExactVectorStore.search` raises an explicit
   `StorageError` on a dimension mismatch (pointing at reindex) instead of numpy's bare
   ValueError — the CLI only catches StorageError/ConfigError, so a bare error would leak
   out as a traceback;
3. **Evaluation fingerprint against misconfiguration** (pre-existing; `corpus_digest` mixes
   in chunk_id): after a reindex the golden-set fingerprint necessarily mismatches, forcing
   a re-run of `tools/build_golden.py`; there is no silent misconfiguration where "an old
   golden set tests a new index".
The runtime BM25 fallback warning (in the manager) stays as it is: the fallback semantics
for a single-point incident remain, with up-front interception handled by doctor.

**⑤ doctor evolves toward backend gating**
Health checks move from "generic items" to backend gating: the local profile gains three
lines for "Ollama service reachable / model pulled / fastembed present", with failure
messages carrying an actionable next step (`ollama pull` / `pip install -e ".[local]"` /
the Windows `OLLAMA_BASE_URL` escape hatch). Probe functions are module-level
(monkeypatchable; unit tests do not actually connect). **doctor never triggers a model
download** (constructing TextEmbedding pulls ~100MB; downloading belongs to the runtime of
the first ingest or question). doctor is still a hard gate: any failing item is summarized
in a red line and exits 1 — the "warning" semantics are carried by the failure text, not
by "not exiting".

**Code**: `src/mikasa/providers/llm.py` (_LOCAL_API_KEY), `cli/__init__.py` (probe functions
+ doctor gating), `storage/repo.py` (embedding_models_in_db), `index/vector_store.py`
(dimension defense).

## ADR-0015 Session management upgrade: folder tree + three-path title lock + suggest fallback semantics

- Status: Accepted | M4.5 (2026-09-09, management release driven by observed user friction)
- Related: ADR-0004 (first real migration, see its revision), ADR-0013 (mode not persisted,
  which survived its first migration unshaken)

**Problem**: The sidebar session list offered only hardcoded "session #id" titles and a
single stream; once sessions multiplied they could not be found or grouped. The user asked
for the shape of a compiler's left-hand file tree — multi-level nested folders, batch
management of sessions by folder, titles auto-distilled from the core of the conversation,
and rename/delete.

**Decision** (trade-offs, item by item):

1. **Title strategy = three-path lock (title_manual column)**. There are three write paths
   for automatic distillation, and the lock semantics are unified in the repo layer's WHERE
   conditions (the comments are the contract):
   - `auto_title_if_untitled` (the _record hook after the first Q&A round): writes only to
     sessions with `title_manual=0 AND title IS NULL` — **only the first round**, with no
     drift across rounds (a title should not change face every round);
   - `apply_suggested_title` (the suggest entry point): may upgrade a session that was
     **already auto-named** (a truncation fallback lands first, and LLM distillation
     overwrites it), but the `title_manual=0` guard is unchanged;
   - `set_session_title` (user rename): non-empty → persist and lock `title_manual=1` —
     **a manual title is never overwritten automatically** (LLM distillation only returns a
     suggestion, not a write; applied=false semantics); explicit clearing (null/empty) →
     unlock back to untitled, and the next Q&A round names it again automatically (closing
     the loop: clearing a title ≠ running bare forever after).
   - `list_sessions` deliberately omits title_manual from its SELECT (to prevent misuse);
     lock state is exposed only through `get_session` and suggest's applied flag.
2. **Title material and fallback = truncation fallback + one LLM pass**. The material is
   uniformly the first question + first answer from the database (`first_user_message` is
   the single source of truth; the CLI/Web/streaming entry points share the _record hook →
   the CLI benefits with zero wiring). offline/mock has no LLM semantics → **the truncation
   fallback is a legitimate fallback, not an error** (first question folded to 20
   characters, millisecond-scale), and suggest reports applied=true; api/local go through
   the LLM (system prompt: ≤16 Chinese characters, no quotes/numbering/line breaks;
   temperature from configuration, max_tokens=24), and **any failure or empty output →
   truncation fallback** (traced in the logger; a failed distillation never takes down the
   Q&A stream). The CLI never calls the LLM.
3. **Folders = a multi-level nested tree, but stored flat and assembled in the frontend**.
   `qa_folders` is a single self-referencing table with parent_id (NULL = root); the list
   is flat in ascending id order, and `buildTree` nests by parent_id in the frontend (the
   foreign key guarantees parent id < child id, so the order is natural). Trade-off: the
   backend does not serialize recursively — tree operations (move/delete) go straight by
   id, session-to-folder membership stores a single folder_id column, and no closure table
   is needed. Cycle prevention (new parent ∈ self ∪ descendants) is one `parent_id in
   folder_descendant_ids(...)` (WITH RECURSIVE), and the frontend's move menu greys out
   targets with the same predicate (subtreeIds).
4. **Two kinds of delete semantics**: deleting a session = cascading message cleanup (the
   existing `qa_messages` ON DELETE CASCADE removes the contents along with the container —
   a session and its messages are a composition); deleting a folder = **only empty folders**
   (409 with child-folder/session counts, requiring the user to move things out first) — a
   folder is a container, and a container never cascades onto its contents (the `qa_folders`
   foreign keys are NO ACTION; should the application-layer 409 be bypassed, the constraint
   backstops it, and user sessions are never silently cascade-deleted).
5. **PATCH overwrite semantics = model_fields_set determines field presence**: an explicit
   null is a legitimate override value (title null = clear and unlock, folder_id null = move
   back to root), and an absent field means that attribute is untouched — a single-field
   rename does not damage the other attribute (renaming does not reset folder membership,
   and so on).
6. **Web menu interaction, no drag-and-drop** (user decision: move via menu): a ⋯ menu with
   a "move to…" submenu (path list with the root pinned at the top, self + descendants
   greyed out, current location greyed out, ← back to the main menu); inline rename and
   new-folder input (Enter to submit, Esc to cancel, click-outside to cancel); deletion
   always behind a confirm() second confirmation.
7. **The first-round auto-distillation loop closes in the Web layer**: no active session at
   send time = first question → POST /title/suggest after the done frame (input not
   disabled, failures silent) → refresh the sidebar. The discriminator is
   "activeSession===null at send time" rather than the meta frame — meta is sent every round
   (follow-ups included), and a follow-up should not re-run distillation (the material is
   first question + first answer, so re-running only burns an LLM call for nothing).
8. **Expansion state is not persisted** (a refresh returns everything to collapsed); titles
   do not update with rounds; session search/archive/pinning are not done — see the feature
   backlog in known-issues.md; nothing is pre-built before its value has been proven.

**Consequences**: The first real schema migration (v1→v2) brought the ADR-0004 revision with
it (automatic step-by-step migration + idempotent re-entry); the live database of 27
sessions / 88 messages upgraded smoothly, and the rehearsal is on record (one 1→2 step in
the migration log, title backfill = each session's first question truncated, counts
unchanged, zero migrations on the second start). The cost: CLI chat does not display titles
(the list endpoint enhancement does not affect it), and after qa_sessions gains columns any
"SELECT *" consumer automatically gets extra keys (a loose superset that breaks no existing
assertion).

**Code**: `storage/{db,repo}.py`, `utils/text.py` (fold_title),
`pipeline/{ask,prompts}.py`, `web/{schemas.py,routers/sessions.py}`,
`web/static/js/{tree,qa-tree,qa}.js`; tests `tests/unit/storage/
test_db_migration.py`, `test_repo_sessions.py`, `tests/unit/web/
test_sessions_api.py`; smoke `tools/smoke_tree.mjs`.

---

## ADR-0016 Reader view: seam removal on the backend + original-file allowlist + the boundary of exposing body text

- Status: Accepted | Paper reader phase b (2026-09-10, user explicitly asked to see the source text behind citations)
- Related: ADR-0012 (the structured chunker's overlap rules — this ADR is their
  **inverse operation**), ADR-0014 (single-model semantics for a given profile, in the same
  data-misconfiguration territory as the "original-file resolution" here)

**Problem**: The `[n]` superscript could previously jump only to the citation card inside
the message, with no way to see where the citation sits in the original document, and
imported documents could not be opened at all. Building "reader view + citation jump" first
requires answering two questions: **what to render** and **how to locate a citation**.

**Decision** (trade-offs, item by item):

1. **Text view = concatenate chunks by seq, with seam removal on the backend**
   (`ingest/stitch.py`). The database **does not store the parsed full text** —
   `Para`/`LoadedDocument` live only inside ingest (ingest/types.py) — and chunks are the
   only data natively aligned with `citation.chunk_id` and available for 23/23 documents.
   Why the inverse operation must share its source with the chunker:
   - Overlap is produced only when adjacent chunks have the **same** `heading_path`
     (chunker.py:110-114 clears the tail at a heading change) → the guard "never trim across
     a heading" costs **nothing** on real data (measured: across 941 adjacent chunk pairs in
     the library, "cross-heading with overlap" = **0 cases**);
   - An overlap tail is **truncated** to fit remaining capacity (the `take = min(...)` at
     chunker.py:94), and once truncated it is no longer a suffix of the previous chunk →
     besides a suffix rule, a **near-tail rule** is needed (a match must start within the
     last `seam_window` characters of the previous chunk). Measured on PDF-like corpora: the
     suffix rule covers 68.8%, the near-tail rule adds another 14.3%, for 83.1% total;
   - **Validation is not "it looks reasonable"**: the stitched character count matches the
     `documents.char_count` recorded by the loader at ingest to within 0.2%–2% for every
     document — doc 71 (866 chunks, 281441 characters) stitches to 248767 characters while
     the loader originally counted 248274, a difference of only 493 characters (which should
     be the `【表格】` ("table") marker the loader prepends to table chunks). Library-wide,
     309499 → 275034 characters (11.1% trimmed).
   Stitching **necessarily loses** Markdown `# heading` lines, code fences, blockquote
   markers, and PDF headers/footers (they never entered a chunk anyway) — subheadings are
   filled in from `heading_path`, and the original-file view is the outlet for the complete
   text: **no fake reconstruction**.

2. **Original-file view = the browser's built-in viewer, embedded** (`<iframe src="/api/documents/
   {id}/file#page=N">`), with zero new dependencies (no pdf.js in the frontend; without
   Range support a PDF cannot seek). Two hard constraints verified on the spot:
   - Starlette's `FileResponse` **defaults to `content_disposition_type="attachment"`**
     (measured on 1.6.0 locally) — without explicitly passing `"inline"`, Chrome downloads
     the file instead of rendering it inline, and the entire PDF view breaks;
   - Range is supported by `FileResponse` itself (206/416/multipart/`accept-ranges`,
     verified in the source), so a 28MB PDF can load progressively and seek.

3. **`/file` path resolution is allowlist-based, because `file_path` is not trustworthy**.
   Measured: **21 of 23 rows point at a discarded old project path**
   (`D:\Code\MyProject1\data\uploads\…`). Two hops, each with a containment check
   (`resolve().is_relative_to(uploads_dir)`): (1) by name (must use `PureWindowsPath` — the
   dirty data is in backslash form, and POSIX `Path` would treat the whole string as a
   single filename); (2) fall back to `stem == title` and verify identity with `sha256_file`
   against the ingest hash. **No fuzzy matching**: serving the wrong file is worse than a
   404. Media types go through an explicit allowlist that **excludes `text/html` and
   `image/svg+xml`** — the service runs same-origin on 127.0.0.1, and those two parsed as
   documents by the browser are the only route to stored XSS.

4. **Exposing body text is a deliberate exception to redaction, with the boundary hardcoded
   in code**. The Q&A pipeline already echoed body excerpts through `Citation.snippet`; the
   new `/content` and `/file` exist so that citations can be verified and documents read
   through. The boundary: `file_path`/`file_sha256` **never enter a response body**
   (`_public_document` unchanged, asserted in tests); both endpoints read the database by
   doc_id only and accept no path parameters. Widening this opening in the future starts by
   changing this rule.

5. **Panel shape = a right-hand drawer created dynamically in JS, shared by two pages**. The
   three HTML files each hardcode their nav, so a new page would mean editing three files and
   losing the contextual continuity between "tree ↔ source text"; and the wording in §5.A was
   originally "the **sidebar** can open a selected document". Having `reader.js` create the
   DOM means zero markup duplication across the two pages. Two interaction pitfalls already
   hit are written into the comments: (1) the click-outside whitelist **must include the
   opening source** (`.cite`/`.cite-card`/`.doc-item`), otherwise the chip's handler runs
   first, bubbles to document, is judged "outside", and closes the panel the instant it opens;
   (2) Esc also closes the settings panel (its listener is registered earlier and
   unconditionally), and **"Esc dismisses all overlays" is accepted** rather than
   intercepting by priority. The knowledge-base page's `onOpen` callback is **deliberately
   independent of `onSelect`**: the latter is reused by `notifyActive` and runs on every
   repaint, so putting it there would make "dragging a document into a folder pop the panel
   open".

5b. **One renderer, two presentations** (added after user-reported feedback on 2026-09-10).
   The first version was overlay-only: clicking a document row on the knowledge-base page
   opened the right-hand drawer, but the right column itself was still a few metadata fields,
   and the user reported it as "ugly — just a few lines of text sitting there". The fix makes
   `initReader(host)` distinguish by the host argument: no argument = overlay (clicking a
   superscript on the Q&A page, without interrupting the Q&A context); with an argument =
   embedded in the right column (selecting on the knowledge-base page reads immediately, body
   text fills the space, metadata is compressed into a single status line at the bottom, and
   the upload card yields). Rendering, positioning, and dual-view switching are all reused from
   the same code, with zero markup duplication across the two pages. Two accompanying fixes
   for things "a shape change is bound to hit": (1) **moving a document into a folder must move
   the status line with it** — the old detail card got this for free from `onSelect`
   re-rendering, but the embedded status line is built only when it opens, so `updateLocation()`
   was added and is pushed by `onSelect`; (2) the `onOpen` callback now passes `location`
   (position is computed from the tree; the reader should not look up the folder tree itself).

**Rejected options**: (1) adding a `/documents/{id}` reader page — would require editing three
navs plus `_PAGES`, and loses the sidebar context; (2) storing the parsed full text at ingest
(schema v4 + a full re-ingest) — the highest cost, and the reconstruction accuracy of stitching
chunks is already proven sufficient by `char_count` reconciliation; (3) pdf.js in the frontend
— pulls in a new dependency and conflicts with the "no build chain" orientation, while the
browser's built-in viewer already suffices.

**Code**: `ingest/stitch.py` (new), `web/routers/documents.py` (three endpoints +
`_resolve_upload_file`), `web/static/js/{reader,reader-view}.js` (new), `common.js` (chip and
citation card gain `data-chunk-id`), `kb-tree.js` (`onOpen`), `documents.js`/`qa.js` (wiring);
tests `tests/unit/ingest/test_stitch.py`, `tests/unit/web/test_documents_api.py`; smoke
`tools/smoke_reader.mjs`, E2E `tools/chrome_reader.py`.

---

## ADR-0017 Merging the "Page" view + page-number alignment + the Ollama context window

- Status: Accepted | Paper reader phase b, continued (2026-09-11, driven by user-reported issues)
- Related: ADR-0016 (the foundation of the reader view), ADR-0012 (the chunker)

**Problem**: The text view had "messy characters, broken tables, no images"; the user asked
to **merge "original file" and "highlight annotations" into a single view**. The previously
planned solution was "restore table column structure at ingest + extract images into storage".

**Decision**:

1. **The merged view = rendered page images + coordinate highlights**, not a parser change.
   Each PDF page is rendered to PNG on demand (`GET /api/documents/{id}/page/{n}.png`;
   PyMuPDF renders a page in milliseconds, nothing hits disk, and the deterministic result is
   served from a private cache), and citation chunks are then drawn as an overlay highlight by
   coordinate (`GET /api/documents/{id}/locate/{chunk_id}` returns rectangles **normalized to
   0–1** and grouped by line). Tables, images, and formulas are **automatically correct** —
   they were in the page render all along. The entire chain of "extract table columns + image
   asset directory + schema change + full re-ingest" therefore becomes unnecessary (schema
   impact drops to zero). The cost: text in a page image cannot be selected or found with
   Ctrl+F — hence the "↗ 原文件" (open original file) link in the header, falling back to
   Chrome's built-in viewer.
2. **Location uses token-sequence matching, not `page.search_for`**. Chunk text was cleaned at
   ingest and is not character-identical to the PDF source — `search_for` matched only **55%**
   in practice. Normalizing both sides with NFKC and stripping punctuation before comparing
   tokens matches **98%** (pure functions in `ingest/pagelocate.py`). When location fails, empty
   rects are returned and the frontend shows the page without a highlight — graceful
   degradation.
3. **Page-number alignment: the same class of bug exists in two places, and both needed
   fixing**. `enumerate(cleaned, start=1)` numbers by list position, and "one page per element"
   is a hard invariant. But `_drop_page_furniture` and `_cut_reference_tail` each filtered out
   empty pages with `if kept:` / `[t for t in rest if t.strip()]` — with enough blank pages,
   subsequent page numbers shifted systematically (measured +3 to +5 positions for doc 71; the
   two combined truncated 301 pages to 189, with the front section misaligned). The fix: both
   **always append, keeping an empty string as a placeholder** (the downstream
   `if not page_text.strip(): continue` skips them as usual). Each is locked down by its own
   regression test.
4. **Ollama's runtime context window is only 2048 while the model itself is 40960** (measured
   2026-09-11: a 6000+ token prompt was truncated to 2050, and truncated **from the beginning**
   — the citation/refusal rules in the system prompt may never have reached the model; a
   passphrase test returned empty replies before the fix and matched `紫罗兰七号` (Violet No. 7)
   exactly after). Mikasa.bat now sets `OLLAMA_CONTEXT_LENGTH=16384` (a comfortable margin for
   qwen3:8b). This was also most of the root cause behind "retrieval hands over too little
   content" — injecting 10 chunks ≈3500 characters already overflowed the window. In step, local
   `fusion_top_k` went 10→14 and api `top_n` 5→8 (changing top_n shifts the evaluation baseline,
   so watch out when comparing across baselines).
5. **Citation cards gain an explicit "↗ open original file" button**: the `[n]` superscript is a
   clickable hidden interaction that new users never discover — every citation card now carries
   an explicit button (opening the original in a new tab) that does not depend on discovering
   the superscript first.
6. **Font-size adjustment lives only in the reading area**: the `--reader-font` variable hangs on
   the reader root node (overlay or embedded alike) rather than `documentElement`, and
   `mikasa.ui.readerFont` persists it locally; the A-/A+ controls sit in the reader header
   (`documents.html` has no settings panel, and putting them in the settings panel would leave
   the knowledge-base page out of reach).

**Rejected options**: restoring table columns at ingest (changing the join in
`_reflow_by_geometry` to add ` | `; the column-gap data is available before the join) plus
extracting images into storage (a `data_dir/assets` directory + a `【图:...】` marker or a
`document_assets` table) — the merged view makes both unnecessary, and the cost would far
outweigh the benefit (three join changes + asset lifecycle cleanup in three places + orphan
sweeping on reindex + rewriting three existing regression assertions).

**Code**: `ingest/pagelocate.py` (new), `ingest/loaders.py` (the two page-number fixes),
`web/routers/documents.py` (/page/{n}.png, /locate/{chunk_id}), `web/static/js/reader.js` (page
view + font size + explicit button), `common.js`/`qa.js` (citation-card button), `Mikasa.bat`
(OLLAMA_CONTEXT_LENGTH), `config/profiles/{local,api}.yaml` (fusion_top_k/top_n); tests
`tests/unit/ingest/test_pagelocate.py`, `test_loaders.py` (the two page-number regressions),
`tests/unit/web/test_documents_api.py`; E2E `tools/chrome_reader.py` (PDF page-view assertions).

## ADR-0018 Configuring the LLM from the web settings panel

- Status: Accepted | Post-M5 hardening (2026-09-15, user request)
- Related: ADR-0001 (profiles), ADR-0002 (secrets via environment variables only),
  ADR-0014 (embedding-dimension discipline), ADR-0017 (the Ollama context window)

**Problem**: the onboarding panel promised "you can also change this in the settings panel",
but the panel only ever had appearance settings — pointing the app at a different model or
provider meant hand-editing `.env`, and in the packaged build even that file sits in the
read-only unpack directory. The user asked for a CC Switch–style panel: pick a provider, paste
a key, test the connection, done.

**Decision**:

1. **Persistence is a user-writable overlay, not a rewritten profile.** The panel writes
   `user_data_root()/config.yaml` (source mode: `data/config.yaml`; packaged:
   `%LOCALAPPDATA%\Mikasa\config.yaml` — the packaged `resource_root()` is read-only). It is
   deep-merged **only when the base config is a profile file**; an explicit `--config` or a
   `config/config.yaml` keeps its full-replacement semantics (tools and the migration drill
   rely on that). The overlay's `profile:` key is ignored outright: the profile decides the
   embedding/retrieval set, and letting the panel flip it would pair a profile's embedding
   config with another profile's index (ADR-0014).
2. **The panel only writes the `llm:` section.** Embedding changes alter vector dimensions and
   require a full re-index, so they stay a CLI decision; reranker and judge still follow the
   profile. The panel writes only the fields it owns (not temperature/max_tokens/timeout), so
   future profile tuning is not frozen by an old snapshot.
3. **Keys go to `user_data_root()/.env`** (python-dotenv `set_key`, `quote_mode="always"`,
   created with a UTF-8 header when missing), never into the overlay. Reads resolve the key
   through `api_key_env` at request time, exactly as ADR-0002 prescribes. `GET
   /api/settings/model` returns `has_api_key` and never the key; the connection probe passes a
   throwaway `MIKASA_SETTINGS_TEST_KEY` environment variable that is removed in a `finally`.
   Clearing a key removes the line and pops it from the process environment — and only the one
   variable the request names (the api profile shares `SILICONFLOW_API_KEY` between embedding,
   reranker and judge; clearing it by accident would silently break retrieval).
4. **Saving re-applies live.** Write files → set the process environment for that one key
   (`load_dotenv` does not override existing variables) → `load_settings()` again → swap
   `services.settings` **before** calling `rebuild_ask()` (it rebuilds from `self.settings`) →
   swap `app.state.settings`, which `/api/health` reads. An `_APPLY_LOCK` serialises saves;
   in-flight questions keep their old objects and finish normally; the new `AskService` starts
   with a cold index cache (acceptable for a rare, explicit action).
5. **Every existing `.env` in the chain is loaded.** The old loader stopped at the first file
   that existed. With the panel writing keys into the data-dir `.env`, that would have masked
   the other keys in the repo-root `.env` (in the api profile that silently breaks embeddings,
   reranker and judge). Files now load in chain order; for duplicate keys the first file wins,
   which is the same precedence as before.
6. **The connection probe never touches live state**: a one-off client with `max_retries=0`
   (3 retries would turn a 20 s timeout into a 60 s wait), `max_tokens=8`, always HTTP 200 with
   `{ok, latency_ms, error?}` — "unreachable" is a probe result, not a server error, and the UI
   renders it as one pill.

**Rejected options**: dumping the full effective settings into the overlay as a snapshot — it
would freeze `${VAR}` expansion into literals, mask every future profile-default change
behind a stale copy, and one wrong `profile:` key would trigger a full re-index. Writing into
the bundle's `resource_root()` — impossible in the packaged build (read-only). Letting the
panel switch profiles — same re-index hazard as above.

**Limitations**: the overlay applies to every profile, including `offline` (a deliberate user
action; the "zero calls" promise of that profile ends the moment a model is configured). When
the configuration comes from `--config`/`config.yaml`, the panel is read-only (the endpoint
answers 400 with the file path). Judge and reranker are not configurable from the panel yet.

**Code**: `config/settings.py` (overlay load/merge, `user_config_path`, `user_env_path`,
`write_llm_overlay`, `write_api_key`, `clear_api_key`, chain-wide `.env` loading),
`providers/ollama.py` (moved out of the CLI), `providers/llm.py` (`max_retries`),
`web/routers/settings.py` (new), `web/schemas.py`, `web/static/js/model-settings.js` (new),
`static/index.html`, `css/style.css`, `static/js/settings.js`, `static/js/onboard.js`; tests
`tests/unit/config/test_user_config.py`, `tests/unit/web/test_settings_api.py`,
`tests/conftest.py` (global `MIKASA_DATA_DIR` isolation); E2E `tools/chrome_model_settings.py`.

## ADR-0019 Online paper search: two free sources, interleaved pagination, a defended downloader

- Status: Accepted | M7 (2026-09-15/16, user request: "search related papers in the knowledge base, like CNKI")
  — **superseded in part by ADR-0020** (M8, 2026-09-16): point 8's "the panel lives on the knowledge
  base page" no longer holds; the feature is a page of its own (`/papers`) with three sources. Points 1-7
  and 9-10 (sources, interleaved paging, degradation, the defended downloader, the import tail, the key
  discipline) still stand.
- Related: ADR-0002 (secrets live in the environment), ADR-0016 (reader view, original-file allowlist),
  ADR-0018 (the settings panel's key discipline this panel copies), ADR-0020 (its successor)

**Problem**: the user wanted CNKI-style literature search inside the knowledge base — find papers
beyond the ones already uploaded, pull them in, and ask questions about them right away. The
honest constraint, established by a feasibility probe on 2026-09-11: CNKI/Wanfang/VIP hold their
full text behind paywalls with no public API and active anti-scraping; scraping them is a legal
problem, not an engineering one. **The target is a CNKI-like experience, not CNKI's data** — and
the UI says so.

**Decision**:

1. **Two sources with public APIs, both free: arXiv (Atom XML) and OpenAlex (works JSON).**
   arXiv carries CS/physics preprints, all open access; its API needs https (the http port was
   blocked on this machine in testing) and sits behind a ~1 request/3 s politeness limit, honoured
   by a module-level throttle that sleeps only *after* a successful call. OpenAlex covers
   DOI-carrying core journals including Chinese ones (a Chinese query like "水库坝" returns
   thousands of hits) but has required a free API key since 2026-02 (100k credits/day; a list
   query costs 10) and stores abstracts as inverted indices, reconstructed without punctuation or
   case. Both return wildly different field shapes, normalised into one `PaperResult`.
2. **Interleaved pagination instead of score merging.** The two sources' relevance scores are not
   commensurable, so a merged ordering would be a fiction. Global positions are assigned by
   parity — even to arXiv, odd to OpenAlex — so every page shows both sources and Chinese results
   (OpenAlex) can never be permanently buried under English ones (arXiv). `has_more` replaces
   `total`: arXiv's relevance count drifts and OpenAlex's `meta.count` is an approximation, so
   "did we fill the page" is the only honest paging signal.
3. **Per-source degradation.** One source raising puts a Chinese message into an `errors` map and
   the response still returns 200 with the other source's results; only both failing together is
   worth a 502. The frontend renders the map as a "部分来源暂时不可用" line above the results.
4. **Import never trusts the client.** The request carries `{source, id}` — not a title, not a PDF
   URL. The server re-fetches the metadata from the source API and derives the PDF URL from its
   own record; ids are regex-validated twice (schema parse + before entering a URL), which closes
   the SSRF line at the entry point rather than at the socket. A paper with no open-access full
   text is a 409 carrying a landing-page (DOI) hint, not a failure.
5. **A defended downloader** (`papers/download.py`): https only to hosts that resolve to public
   addresses; http only to loopback (the E2E fake-source escape hatch — loopback cannot reach the
   intranet, so the SSRF guarantee is unchanged); every redirect hop re-validated, max 3;
   `Content-Type: application/pdf` plus a `%PDF-` magic-byte sniff; a 50 MB hard cap; half-written
   files deleted on every failure path. Accepted residual: validation and the actual connection do
   two independent DNS lookups, so a theoretical DNS-rebinding window remains; closing it means
   writing our own connection layer, which is not worth it for a local personal app.
6. **Import reuses the upload tail.** The ingest tail of the upload endpoint was extracted into
   `documents.ingest_web_file`, so an import returns a **byte-identical** 201/200/409 response —
   the tree refresh, the toasts and the duplicate handling are the same code path, not a copy.
   The filename is `title[:80] (source id).pdf`: ingest's same-name-replacement semantics would
   let two identically titled papers clobber each other, and the id suffix keeps them apart, while
   genuinely identical bytes still dedupe by sha256 (200, "已跳过重复导入"). The title is
   truncated *before* the suffix is appended, because `sanitize_filename` keeps the head and cuts
   the tail.
7. **The OpenAlex key lives in the panel, with ADR-0018's three-state semantics** (`null` =
   leave alone, `""` = clear, value = write) and its discipline: written to the data-dir `.env`
   plus the process environment for hot effect, `GET` answering a boolean and never the value.
   The key row is collapsed by default — the key is optional (anonymous access has a small trial
   quota) and should not compete with the search box for attention.
8. **The panel lives on the knowledge base page** (`js/papers.js`), because that is where imported
   documents land and where the corpus tree gives immediate feedback. The settings panel only
   exists on the QA page, so the key row is the panel's own. Result rows render title/authors/
   year/source with an expandable abstract; the import button is disabled up front for papers
   without open access, so the 409 is a safety net rather than the UX.
   *(M8 note: reversed by ADR-0020 — the panel was too cramped to carry filters and a detail pane,
   so it became the `/papers` page. The "already in the library" feedback it traded away is restored
   by `documents.source_ref`.)*

**Rejected options**: scraping CNKI/Wanfang/VIP (paywall + anti-bot + no API = legal problem, and
the docs say so); Semantic Scholar as a third source (it works, but rate-limits at 429 — not worth
a third parser right now); CORE/ChinaXiv as Chinese OA supplements (kept in the backlog);
accepting a PDF URL from the client (that is the SSRF hole the design closes); a separate "papers"
page (the knowledge base is where documents live — import must land where the user already is);
merging both sources by score (see 2); letting the client send the title for the filename (the
server would be writing a name the user could have forged).

**Limitations**: CSSCI/social-science Chinese coverage is ≈ 0, and CNKI-exclusive full text is not
reachable — the UI and the docs say so plainly and offer a DOI jump instead. arXiv's 3 s throttle
makes back-to-back searches feel slow (one search can be two upstream calls). No "jump to page N":
the interleaved window advances by results received, so paging is forward-only. The abstract
reconstructed from OpenAlex loses punctuation and case. Import = download: there is no in-app
preview for a paper that has not been imported, and the 50 MB cap rejects oversized PDFs.

**Code**: `papers/sources.py` (the `PaperResult` model and `PaperSource` protocol), `papers/arxiv.py`,
`papers/openalex.py`, `papers/download.py`, `papers/service.py` (interleaving + degradation),
`papers/errors.py`; `web/routers/papers.py` (new), `web/schemas.py`, `web/routers/documents.py`
(`ingest_web_file` extraction), `web/static/js/papers.js` (new), `static/documents.html`,
`static/js/documents.js`, `css/style.css`; tests `tests/unit/papers/` (sources, service, download)
and `tests/unit/web/test_papers_api.py`; E2E `tools/chrome_papers.py`.

## ADR-0020 "Find papers" becomes a page of its own: three sources, declared capabilities, an honest paging contract

- Status: Accepted | M8 (2026-09-16, user request after using M7: "the search is too thin — make it its own page")
- Related: ADR-0019 (superseded in part — its point 8 placed the panel on the library page),
  ADR-0004 (schema migration discipline), ADR-0016 (the reader can only show ingested documents)

**Problem**: M7 shipped online paper search as a panel in the library page's right column, and the
user's verdict after using it was that it was too cramped to be useful: one search box, one flat list,
no filters. The ask was to promote it to a **page of its own** (alongside 问答 / 知识库 / 评测) and to
make it richer in four directions: more metadata per result, filtering and sorting, more sources, and a
reading/status experience (pick a result, read the full abstract, see what is already in the library).

**Decision**:

1. **A fourth page (`/papers`), not a bigger panel.** The three-column layout (filters 300px / results /
   details 340-440px) needs horizontal room the library page does not have — its right column is the
   upload + corpus area. The nav is hardcoded in four HTML files, so this cost one line each plus a
   `_PAGES` entry; the panel was removed from the library page in the same change so the two cannot drift.
2. **A third source: CORE, chosen by measurement.** CORE's v3 works API answered anonymously (no key),
   tolerated a handful of quick requests, and its `downloadUrl` served a real PDF (`%PDF-1.5` verified).
   Semantic Scholar was rejected for now (HTTP 429 on the anonymous pool, twice) and ChinaXiv was
   rejected outright: its `/api/search` exists but the contract is unguessable — GET and two POST
   encodings all answer "请求方法应为POST", and `/oai` refuses anonymous access. Scraping it would be the
   only way in, and that is out (ADR-0019's reasoning about paywalled Chinese full text still stands).
3. **Sources declare their capabilities; the UI disables what they cannot do.** `SourceCaps(year,
   cited_sort, recent_sort, language, oa)` is part of the `PaperSource` protocol, and
   `GET /api/papers/sources` hands the catalogue to the frontend, which greys out unsupported options and
   says why. This exists because of a measured "false support": CORE's `yearFrom`/`yearTo` **parameters**
   return HTTP 200 while being silently ignored (asked for 2020+, got 2012/2018/2010 papers) — its year
   filter only works through query syntax (`... AND yearPublished>=2020`), which is what the source now
   emits. Serving an option that quietly does nothing is worse than not offering it.
4. **`notes` and `errors` are different things.** `errors` is failure (the source raised), `notes` is
   degradation (the source answered but could not honour a filter: arXiv has no citation data, so "sort
   by cited" falls back to relevance). Both surface in the UI, in different words. One subtlety worth
   writing down: `oa: "always"` is **satisfied**, not degraded — a source that is entirely open access
   already fulfils "only open access", so it produces no note, and the frontend renders the checkbox as
   checked-and-disabled ("已自动满足"), deliberately distinct from "not supported".
5. **Rotating interleave generalised from two sources to N.** Global position `p` belongs to source
   `p % n` and is that source's result number `p // n`; the window math is `k ∈ [max(0,
   ceil((offset-i)/n)), floor((offset+limit-1-i)/n)]`. Substituting `n=2` reproduces the old parity
   arithmetic digit for digit (a unit test pins that equivalence). The merge fills by global position and
   **leaves holes rather than packing**: the old `zip + extend` tail-packing made the second page repeat
   results when a source ran out early (page 1 took `[a0, o0, o1, o2]`, the client advanced by the four
   results it received, and `o2` came back on page 2).
6. **The paging contract is by window, not by results received.** With holes in a page, "advance offset
   by the number of results" is wrong; the client advances by `limit`. Both halves are regression-locked,
   because either one alone still duplicates.
7. **OpenAlex's window alignment needed its own fix.** OpenAlex has only `page`/`per-page`, and page
   boundaries sit on multiples of `per-page` — there is no way to ask for an arbitrary offset. The old
   `page = start // count + 1` was accidentally correct only while `start` was a multiple of `count`
   (true for two sources splitting a 20-item page, false the moment N sources rotate). A first fix
   (align to `count`, fetch by `per_page`) was still wrong — the two multiples differ. The shipped
   approach fetches **page 1 with `per-page = start + count` and slices** `[start : start+count]`: one
   request, no boundary arithmetic, and OpenAlex bills per query (10 credits) regardless of page size, so
   the extra bytes are free. Windows deeper than the 200-record page cap come back short, so `has_more`
   turns false and that source's paging stops honestly instead of pretending.
8. **"Already in the library" needs a column, not a title suffix.** `documents` gained
   `source_ref TEXT` (`"arxiv:2401.12345"`, `"core:72543"`), written by the import path and read by the
   search endpoint in one `IS NOT NULL` sweep. Reverse-parsing the filename suffix `(arxiv 2401.12345)`
   was rejected: users rename documents (`PATCH /api/documents/{id}` touches only `title`), a rename is
   inherited across re-ingests so it never heals, titles are truncated at 80 characters before the suffix
   is appended, and the only stable carrier (the uploads copy name) is reachable solely through
   `file_path`, a known-dirty legacy field. The column follows the v3 `folder_id` precedent exactly —
   including the two "keep" lists that preserve it across a re-index, whose omission is precisely how
   `folder_id` was lost once.
9. **The sha256 skip path backfills the source.** A user who dragged the PDF in by hand and later clicks
   "import" on the same paper hits the content-hash skip; without a backfill that row keeps
   `source_ref = NULL` forever and the search page never shows "already in the library" for a document
   that is demonstrably in the library. The skip branch fills it in when (and only when) the existing row
   has no source.
10. **The source reference is public metadata.** It is exposed through `_public_document`, a deliberate
    widening of the redaction boundary: `file_path` and `file_sha256` stay hidden (local filesystem
    facts), while an arXiv id or DOI is already printed in every search result.
11. **Public `http://` full texts are allowed again** (measured, user-approved on 2026-09-16). ADR-0019
    had restricted http to loopback. That turned out to block the feature's own main use case: sampling
    three queries showed **5/7 and 3/6 of the Chinese open-access links are plain `http://`** (domestic
    journals and repositories frequently never moved to https), while the English sample had 0/15 —
    "Chinese papers are findable but not importable". Since the URL always comes from a source API
    record (never user input — ADR-0019's entry-point design) and the payload is a public paper fetched
    without credentials, the transport-security argument for https-only is weak here. Every SSRF check
    stays: public addresses only, plus a loopback exception for http (the E2E fake source, and local
    services), with private/reserved/`169.254.0.0/16` ranges rejected exactly as before. Verification
    after the change: a Chinese `http://` full text imported cleanly (138 chunks), a second host
    answered 403 regardless of browser-like headers (upstream blocking, reported as a 502, out of our
    hands). The residual risk is integrity — a network attacker could substitute a different PDF in
    transit — and it is accepted for a local single-user tool.

**Rejected options**: keeping the panel and enlarging it (no horizontal room; the corpus tree is the
library page's job); a fourth source for its own sake (S2/ChinaXiv fail on evidence, not taste); letting
the client send an arbitrary PDF URL (ADR-0019 point 4 — the SSRF line closes at `{source, id}`); packing
holes in the interleave (see 5); a separate `document_sources` table (a second FK surface for a
one-to-one fact); per-source filter flags hardcoded in the frontend (capabilities belong to the source,
and hardcoding them is how "options that do nothing" get shipped).

**Limitations**: CORE has no citation sort (`sort=citationCount` answers HTTP 500) and rate-limits
aggressively under bursts, so it carries its own 2-second throttle; a meaningful share of its
`downloadUrl` values are `http://` repository addresses, which the downloader's security policy rejects
(public https or loopback http only), so those records cannot be imported; `yearPublished` arrives dirty
(observed `710300`, `202022`), so years are parsed as the first four digits and range-checked; arXiv has
no citation data at all, so "sort by cited" is offered only when a selected source can honour it;
OpenAlex's deep paging is capped at its 200-record page; and the reader still cannot preview a paper
before it is imported (the detail pane renders the search response instead).

**Code**: `papers/sources.py` (`SourceCaps` / `PaperFilters` / `PaperResult.cited_by`), `papers/core.py`
(new), `papers/arxiv.py` and `papers/openalex.py` (filter translation, capability declarations, the
window fix), `papers/service.py` (N-source rotation, hole-leaving merge, `attempted` / `notes`,
`source_catalog`), `storage/db.py` + `models/document.py` + `storage/repo.py` + `ingest/service.py`
(`source_ref`, v4 migration, the re-index keep lists), `web/routers/papers.py` (+ `GET
/api/papers/sources`, `in_library`), `web/routers/documents.py` (`ingest_web_file(source_ref=...)`, the
skip-path backfill), `web/app.py` (`/papers`); frontend `static/papers.html`, `static/js/papers-page.js`,
`papers-api.js`, `papers-filters.js`, `papers-detail.js`, `css/style.css`, and the four nav bars; tests
`tests/unit/papers/test_core.py`, `test_papers_service.py`, `test_sources.py`,
`tests/unit/storage/test_db_migration.py`, `tests/unit/ingest/test_service.py`,
`tests/unit/web/test_papers_api.py`; E2E `tools/chrome_papers.py` (three fake sources).

---

## ADR-0021 Notes are ordinary documents: a `source_ref` marker, no schema change, and a force-ingest escape from content dedup

- Status: Accepted | M6 phase 1 (2026-09-16, user picked "build a new feature" and confirmed two product
  calls: notes must be **editable** — saving re-ingests automatically — and the editor must have a
  **live Markdown preview**)
- Related: ADR-0020 (whose `source_ref` semantics this entry broadens), ADR-0004 (schema migration
  discipline — the path deliberately not taken), ADR-0012 (the chunker that notes reuse unchanged)

**Problem**: the product's stated shape is "notes and AI Q&A, two wings", but the only way into the
library was to bring a file from outside. Writing a note meant leaving the app, and whatever you learned
while asking questions had nowhere to land. M6's phase 1 is the smallest step that closes the loop: write
Markdown on the library page, save, and it is immediately retrievable and citable.

**Decision**:

1. **A note is an ordinary document with a marker, not a new kind of entity.** The marker is
   `source_ref = "note:<key>"`. Everything else — the folder tree, drag-and-drop, rename, delete,
   retrieval, citations, the reader — already works on documents and now works on notes for free. Why not
   a new column or table: `_KeptProps` already carries `source_ref` through both re-index and same-name
   replacement, so the marker survives a full rebuild without a single new line; `_public_document`
   already exposes the field; and the find-papers page matches `arxiv:` / `core:` **exactly**, so a
   `note:` value cannot be mistaken for a paper already in the library. The price is that the column's
   meaning widens from "which online record this came from" to "this document's origin" — the four
   comments that claimed otherwise are updated in the same change, because that is exactly the kind of
   comment that otherwise becomes a quiet lie.
2. **The note is a real `.md` file in `uploads/`**, named `<sanitized title> (note <key12>).md`. This
   falls out of the ingest contract: the uploads copy's basename *is* the document's identity, so the
   name is generated once, server-side, and **never changes** — renaming a note renames the display
   title only, which is what `set_document_title` already promises for uploaded documents. The body is
   stored byte-exact (binary write, LF-normalised) so an editor round-trip is lossless. The `<key>` is 48
   random bits, checked against both existing paths and existing markers before use: a collision would
   not raise, it would *replace someone else's row*.
3. **Notes ingest with `force=True`, and that is not an optimisation.** `_ingest_one`'s byte-level
   content dedup is right for a corpus (a backup copy of the same bytes should not pollute retrieval)
   but silently wrong for notes: a second note with the same body would return success and never be
   written, and editing one note to match another would make the response point at *the other row*.
   `force` bypasses the dedup at both checks while still taking the same-name-replacement path, so
   editing stays an in-place update. Identical chunk bodies across two notes are fine — `chunks` is only
   unique on `(document_id, seq)`.
4. **The title is handed to ingest, not patched afterwards.** `ingest_one(title=...)` with precedence
   explicit title > inherited title > loader-derived title. Two independent reasons: the chunker builds
   each chunk's index text as `《title》｜heading_path`, so fixing the title after ingest would leave the
   old name inside the very representation BM25 and the vectors search — rename a note and you cannot
   find it by its new name; and same-name replacement inherits the *previous* row's title, so without an
   explicit override, editing the title would never take effect at all.
5. **Editing is same-name replacement, so `documents.id` changes on every save.** In-place update means
   delete the old row and insert a new one. The response is therefore built from a lookup **by uploads
   path**, never from the id in the request, and the page re-selects the row by the id it gets back
   (without that, the refresh sees "the selected row was deleted" and collapses the reading pane the user
   was in the middle of). A save whose body hash and title are both unchanged short-circuits to 200 and
   touches nothing, so "open the editor, press save" costs zero.
6. **The whole save holds the ingest lock** (`services.ingest.exclusive()`), the same discipline DELETE
   uses. Without it, a save racing a delete resurrects the deleted note as an **ordinary document with no
   marker**: ingest happily rebuilds the copy from the web-tmp file and inserts a fresh row.
7. **Read-back reads the uploads copy raw.** The editor must prefill with what the user actually typed,
   so `GET /api/notes/{id}` returns the file's bytes decoded. `/content` is not an option: it is the
   stitched-chunk reading text, and by construction it has already dropped `#` heading lines, code fences
   and blockquote markers — prefilling from it would silently rewrite the user's note on the next save.
8. **The endpoints refuse to touch anything that is not a note.** PUT and GET check the `note:` prefix
   and answer 404 otherwise (404, not 403 — no existence oracle), so an uploaded file or an imported
   paper can never be overwritten through the note API.
9. **Alternatives rejected**: a dedicated `is_note` column (a v5 migration plus keep-list plus repo and
   API plumbing, for a boolean already derivable from a field that survives rebuilds); a separate
   `notes` table (would forfeit the folder tree, retrieval, citations, reader and delete paths, all of
   which would then need re-implementing and re-testing); and reusing `PATCH /api/documents` with
   `/content` for editing (see point 7).

**Consequences**: fenced code blocks **do not enter retrieval** — the Markdown loader skips them so that
a `#` inside code is not read as a heading — which means a note consisting only of code is rejected with
a 400 whose wording has to explain that; the uploads copy is the note's *only* authoritative copy (there
is no "user's original" to fall back on), so `data/` must be backed up before any re-index; the parsed
text still passes through `strip_repeated_lines` (≥3 identical lines) and Setext `---` handling, so the
indexed representation can differ from the file (the file itself is untouched); two browser tabs editing
one note is last-write-wins (no optimistic locking — SQLite's `datetime('now')` is second-granular and
useless as a version); and every save rebuilds chunks, so citation chips in older answers point at
deleted chunk ids — the same consequence re-uploading a file already has. The pre-existing
same-name-replace file-lock hazard (an external program holding the uploads copy makes the rename step
fail *after* the old row was deleted) applies to notes too and is documented rather than fixed here,
because it lives on the ingest rollback path the corpus depends on.

**Code**: `web/routers/documents.py` (the `notes` section: `NOTE_REF_PREFIX`, `_note_stem`,
`_note_name`, `_note_payload`, `_get_note`, `_write_note_tmp`, `create_note` / `update_note` /
`get_note`; `ingest_web_file` grew `force` / `title` / `folder_id`), `web/schemas.py` (`NoteIn`,
`NoteUpdateIn`, `NOTE_BODY_MAX`), `ingest/service.py` (`ingest_one(title=...)` and the precedence rule),
`storage/db.py` + `storage/repo.py` (comment-level widening of `source_ref`); frontend
`static/js/note-editor.js` (new), `static/js/tree.js` (`isNote`, `folderOptions`), `static/js/kb-tree.js`
(`onEditNote`, `selectDocById`, the `笔记` meta label), `static/js/documents.js`, `static/documents.html`,
`css/style.css`, `DESIGN.md` (Chinese source of truth) + `DESIGN.en.md` (English mirror)
(the `note-editor: 70` rung); tests
`tests/unit/web/test_notes_api.py` (new), `tests/unit/ingest/test_service.py`; smoke
`tools/smoke_tree.mjs`; E2E `tools/chrome_notes.py` (new).

---

## ADR-0022 In-app updates: the client never sees a URL, checksums ship with the package, failed checks stay silent

- Status: Accepted | v0.1.1 (2026-09-19, after the user reported "the desktop shortcut is always the old
  version" and chose the one-click download-and-install flow)
- Related: ADR-0019 / ADR-0020 (the download defence chain and the loopback escape-hatch convention,
  reused here), ADR-0018 (the settings panel, where the "About" section lives), ADR-0002 (the update
  chain carries no credentials at all)

**Problem**: an installed copy is reached through a desktop shortcut that points into the install
directory, and that directory only changes when the installer runs. There was no update mechanism at
all: after every change I rebuilt the development bundle, and the user's copy stayed old (a recurring
frustration from 2026-09-11 onward, called out explicitly on 2026-09-19). The flow the user chose: on
startup, if a newer release exists, show a dialog → one click downloads it → the installer runs.

**Decision**:

1. **The check endpoint speaks for the client; the response contains no URL.** `GET /api/update/check`
   asks GitHub's `/releases/latest` server-side; download URLs never appear in the response — the
   frontend only ever says "download the latest". The client therefore cannot name any address, which
   narrows the SSRF surface to "the one chain the server picked out itself". The download entry point
   needs exactly one thing: a host allowlist (`github.com` / `*.githubusercontent.com`, https).
2. **Loopback http is an escape hatch, and only that.** Same trade-off as `papers/download.py`: a
   non-allowlisted host must be **http + loopback** (the E2E fake GitHub and fake asset host). Loopback
   cannot reach the intranet, so the SSRF guarantee is untouched; it also blocks "the API response was
   swapped for an arbitrary public host".
3. **Every download is checked against a sha256, and the checksum ships with the package.** The
   installer and the same release's `SHA256SUMS.txt` are fetched together and compared byte for byte
   before the installer may start; **without a checksum file there is no automatic update** (the user
   downloads manually instead). This defeats corrupted or in-flight-replaced downloads; a compromised
   release account is not defeated (checksum and package share a source) — an accepted residual,
   recorded in limitations. Any failure deletes the partial file: nothing that "looks installable" is
   ever left behind.
   (**Amended 2026-09-19, see ADR-0024**: a partial now exists only as a `.part` suffix and never
   takes part in launching; and it is **kept** on transport failures — resume needs it. Only a
   checksum mismatch, a size-cap breach or a security-policy rejection deletes it.)
4. **Launching the installer is double-click semantics** (`os.startfile`) behind three gates: the file
   exists, it sits inside the data directory's `updates/`, and its name matches
   `Mikasa-Setup-*-win64.exe`. The wizard `taskkill`s the running Mikasa itself, so after "launch" this
   process dies — the frontend expects no further response. E2E sets `MIKASA_UPDATE_SKIP_LAUNCH=1`,
   which turns this step into a log line (it only makes the code do *less*, so the switch points in the
   safe direction).
5. **A failed check is always silent**: an offline user should not be nagged by a network error; the
   error goes to the log and the status endpoint only. Only an explicit "Check for updates" click
   surfaces a failure. Check results are cached for 10 minutes (GitHub's anonymous quota is 60/hour/IP);
   the manual path passes `force=1`.
6. **"Skip this version" lives in localStorage and only silences the silent check**: a manual check
   clears the marker (asking means wanting to know now). The settings panel gained an "About" section:
   current version, the startup-check toggle, and a manual check button.
7. **The dialog cannot be closed while downloading** — otherwise the user sees "I clicked download, the
   window vanished, nothing happened". Esc and backdrop clicks are ignored mid-download; on failure the
   dialog explains itself and keeps "Open release page" within reach (manual download is the permanent
   fallback).
   (**Superseded 2026-09-19 by ADR-0024**: in practice that second half meant "switch pages and the
   whole thing disappears, and coming back does not reattach" — exactly the "it broke" the user
   reported. The dialog is now closable: closing it means "keep going in the background", and the
   progress lands in a topbar capsule that every page shows.)
8. **This code has to ship in v0.1.1**: the old build does not contain it and cannot know a new release
   exists — this one still has to be installed by hand, and only then does "it tells me on startup"
   hold.

**Consequences**: startup makes one read-only request to `api.github.com` (no credentials, no local
data), and it can be turned off — in a local-first product this is a **disclosed network behaviour**,
written into the usage guide; downloads from github.com are slow from mainland China (18 seconds
measured for a small file), so a large installer can take minutes — the progress bar and the
"fall back to the release page" path exist for that (**added 2026-09-19, see ADR-0024**: a dropped
connection now resumes with `Range` and retries with backoff — "slow" remains, "one drop costs you
minutes of re-downloading" does not); automatic install covers Windows only
(`os.startfile`); `updates/` keeps only the most recent file (older residue is cleaned when the next
download starts); and the release process gains one rule: **every release must ship
`SHA256SUMS.txt`**, or existing users cannot use the automatic update at all.

**Code**: `src/mikasa/update/` (`release.py` version compare / release parsing / asset picking,
`install.py` allowlisted download + verification + launch + single-slot job manager, `checker.py` TTL
cache, `errors.py`), `web/routers/update.py` (four endpoints), `web/services.py` (`updates` /
`update_jobs`), `web/app.py` (router); frontend `static/js/update.js` (new), `static/js/qa.js`
(startup wiring), `static/index.html` (settings "About" section), `css/style.css` (`.upd-*`,
z-index 95), `DESIGN.md` (Chinese source of truth) + `DESIGN.en.md` (English mirror)
(the new rung and components); tests
`tests/unit/update/test_release.py`, `tests/unit/update/test_install.py`,
`tests/unit/web/test_update_api.py`; E2E `tools/chrome_update.py` (new: a fake GitHub, a throttled fake
download, and no executable ever launched).

---

## ADR-0023 A fourth source, DOAJ: key-free Chinese open-access journals, and where the licensing line sits

- Status: Accepted | the v0.1.3 cycle (2026-09-19; the user asked for "someone installs it and it just
  works, without going off to register for anything")
- Related: ADR-0019 / ADR-0020 (source protocol, capability declarations, interleaved paging — this
  entry only appends a source to the registry, the protocol is untouched), ADR-0022 (the other half
  of the same product demand: updating should also just work)

**Problem**: each of the three existing sources has a gate — OpenAlex has required a free key since
2026 (anonymous access gets a small trial allowance and then answers 429), CORE's anonymous rate
limit is tight, and arXiv only carries English preprints. For "install it and it works", Chinese
journals were simply missing, and asking people to register for a key first is exactly the
experience the user rejected.

**Decision**:

1. **Integrate DOAJ**, the official directory of open-access journals: **no key**, an official REST
   API, metadata declared CC0. Measured working for Chinese ("充填体" → 185 hits, matching Chinese
   journals such as 工业水处理). Appended to the end of the registry — registry order is interleaving
   order, and moving earlier entries would shift the global position mapping of existing sources.
2. **Capabilities are declared from measurement, not from documentation**: the year range works
   through query syntax (measured 185 → 115); sorting by citations/recency and language filtering
   **timed out in testing**, so all three are declared unsupported — the UI greys them out with a
   reason. Better to offer three fewer options than to pretend a filter works.
3. **"Open access" and "one-click importable" are different things.** Every `link[]` we sampled was
   a publisher landing page (0 of 25 ended in `.pdf`), so `pdf_url` is only set when a `.pdf` link
   really appears. The UI therefore has three states rather than two: direct PDF (importable) /
   open access but landing page only (import disabled, "open the original page") / not open access.
4. **The licensing line is documented in code and docs** (the user asked directly): a source may be
   integrated only if it explicitly permits programmatic access — DOAJ (metadata CC0), OpenAlex
   (CC0), arXiv (official API terms, identifiable UA, polite rate limits), CORE (an open-access
   aggregator whose API terms allow it). **Not** CNKI/Wanfang/NCPSSD and the like: their records are
   commercially licensed or served through internal endpoints, and scraping them is a breach.
5. **Bypassing bot detection is explicitly out of scope.** The most tempting candidate was the
   National Center for Philosophy and Social Sciences Documentation (free Chinese journals, strong
   social-science coverage); its search endpoint is anonymous JSON — but it sits behind a
   **ChinaNetCenter WAF**: a real browser gets results while a plain HTTP client gets an empty shell
   (same URL, same parameters: 265 hits in headless Chrome, `total: 0` from urllib). Getting past
   that means solving the WAF's challenge cookie, which is working *against* the site's own bot
   protection rather than a technical hurdle — so it is not done. (Its predecessor, NSSD, suspended
   service on 2024-07-11 and folded into the current site.)
6. **Upstream metadata is untrusted input; strip markup before display.** DOAJ passes paper HTML
   straight through its abstract field (measured: the UI showed `1<sup>#</sup>`), hence
   `utils/text.strip_markup` — it removes tags, restores entities and collapses whitespace, and
   **does not render rich text** (rendering would import an XSS surface).

**Consequences**: Chinese literature finally has a zero-configuration source, at the cost that its
full texts mostly live on the publisher's page (point 3); the source count is now four, and the
30-second page deadline plus parallel fetching (papers/service.py) keep "one more source" from
meaning "one more wait"; the two hardcoded source lists in `schemas.py` are now pinned to the
registry by a guard test (adding DOAJ tripped exactly that: ticking it in the UI produced a 422).

**Code**: `papers/doaj.py` (new), `papers/service.py` (registry, parallel fetch, page deadline),
`papers/http.py` (shared UA + network-level retry, which retries only network errors and 406),
`utils/text.py` (`strip_markup`), `web/schemas.py` (both source lists), `web/routers/papers.py`
(`_ID_VALIDATORS`), frontend `papers-page.js` / `papers-detail.js` (labels and the three full-text
states); tests `tests/unit/papers/test_doaj.py` (new), `tests/unit/papers/test_papers_http.py` (new),
`tests/unit/web/test_papers_api.py` (the guard test); E2E `tools/chrome_papers.py` (a fourth fake
source).

---

## ADR-0024 Resumable downloads, adoptable jobs: the "observation window" and slot self-healing

- Status: Accepted | v0.1.4 cycle (2026-09-19, after the user reported "downloads are painfully slow,
  switching to another feature breaks it, and clicking again says 'the download has already started'")
- Related: ADR-0022 (**this amends its points 3 and 7**; every other defence stands), ADR-0019 /
  ADR-0020 (the download defences and the "loopback escape hatch" convention)

**Problem**: the user's v0.1.3 tried to upgrade and hit three different things — slow, "breaks when I
switch pages", and "can never download again":

1. **The server was never interrupted.** The download runs inside a `BackgroundTasks` sync call (an
   anyio worker thread), tied to neither the request nor the connection. Measured: the client read the
   202 and hard-closed the connection, then made zero requests for 12 seconds — the download finished
   anyway. What "broke" was the **UI**: the four feature pages are full page loads, so switching pages
   tears down all JS (polling and dialog included), and coming back to the chat page had no "there is
   still a job running" recovery path (the eval page has one; this code never got it).
2. **"Can never download again" was a 409.** With a running job in the slot, `POST
   /api/update/download` answered 409 and the frontend rendered it as "failed to start the download:
   the download has already started". The slot recorded state but not **whether the worker thread was
   still alive**, so a thread that vanished quietly left the slot stuck in `running` forever — with no
   self-healing path.
3. **Slow is real, and one drop costs everything.** Measured on this machine (through the system
   proxy): ~300 KB/s single-stream, so the 83.7 MB installer takes 4–5 minutes, with a peer reset
   caught in the middle. And a failed download deleted the partial file, had no `Range` resume and no
   retry — **every drop restarted from zero**, which was the biggest waste of all.

**Decisions**:

1. **Resume.** The download lands in `Mikasa-Setup-<version>-win64.exe.part` and later requests carry
   `Range: bytes=N-` (206 appends / 200 rewrites the whole file / 416 is treated as "already
   complete", left to the checksum to judge). Transport failures retry with backoff (3 attempts,
   2s/5s), and **both retries and later sessions resume from `.part`**. The partial is a **suffix
   only** — `Path.with_suffix` would eat the `.exe` and leave a name that "looks like the installer",
   so `launch_installer` gained an explicit gate refusing it: that invariant should not depend on a
   regex coincidence in `SETUP_ASSET_RE`.
2. **The checksum line does not move by a single byte.** It is still compared byte-for-byte against
   the same release's `SHA256SUMS.txt`, and the file is **renamed only after verification**
   (`os.replace`), so a bad file never gets the final name. A `.part` may be last round's stub, or the
   remote asset may have been re-uploaded — this module cannot tell, so the **only** arbiter is
   sha256: a mismatch deletes the whole partial and downloads once more (closing the "poison prefix"
   loop — otherwise every click hits the same wall), and a second mismatch fails honestly.
   Completeness is judged on the **final file size** (partial + this attempt's writes), not on "how
   many bytes we wrote this time" — without that, resume would be killed by our own completeness check.
3. **Slot self-healing.** Besides the state, `UpdateManager` now records **worker liveness**
   (`worker_started/finished`, paired by the task wrapper) and **when the state last changed**. A dead
   worker past the grace window (10s, covering the gap where BackgroundTasks starts the thread after
   the response) lets a new job **take over** the slot; `done` with the file still present is not
   taken over (that would waste 80 MB — the frontend should go install instead). Every slot carries a
   token and writes with a stale token are dropped — otherwise a superseded zombie thread would
   revert the new job's state, or even append into the same `.part`.
4. **The POST is idempotent.** The download endpoint no longer answers 409: a live job returns 202
   with `adopted: true` (the frontend just follows), and whether to re-download is decided by the
   takeover rules. The frontend plays along: "Download and install" asks for the status first and only
   follows when it is running/verifying/done, never POSTing first.
5. **"Observation window".** The download is a server-side background job and the UI is just a window
   onto it — the dialog **can be closed** (closing means "carry on in the background") and the topbar
   keeps a cross-page capsule (all four pages show progress; clicking it reopens the dialog). Switching
   pages, reloading and closing the dialog no longer interrupt anything, and returning to the chat page
   reattaches automatically (unless the user explicitly dismissed it — the capsule stays either way).
   The capsule is a **separate element**: it must not live inside `.health-pill`, which `initTopbar`
   rewrites with `innerHTML=""` every 20 seconds. The other three pages mount the capsule only and
   **never call `/api/update/check`**: with "check on startup" turned off, no page should quietly go
   online.
6. **No unattended install.** When the download finishes with the dialog open (the user is watching),
   the installer still launches automatically; otherwise the capsule just becomes "update ready ·
   click to install". Rationale: the installer's first act is to `taskkill` Mikasa (ADR-0022 point 4),
   and killing someone's app while they are elsewhere is rude.
7. **One lie removed along the way.** The frontend used to have a 30-minute deadline that declared
   "download timed out" — while the server was still downloading. Gone: terminal states come from the
   server, stalling is reported via the backend's `stalled`, and retries are shown honestly as
   "connection dropped, retrying (attempt N)".

**Explicitly not doing**:

- **Multi-connection parallel downloads**: measured on the same link, 4 connections ≈442 KB/s versus
  ~300 KB/s single-stream — only 1.4×, while the failure branches double (some CDN nodes answer
  `501 Unsupported client range`). Resume plus retry is what pays on this link.
- **Moving the download off `BackgroundTasks` into its own thread**: it demonstrably survives client
  disconnects, and changing it would only cost test determinism (TestClient runs background tasks
  synchronously).
- **A cancel button**: a wedged download is covered by "single-chunk read timeout 60s → retry → error",
  and slot self-healing keeps the button clickable.

**Consequences**: a dropped connection no longer costs a re-download — the partial stays in
`updates/<asset>.part` and the next attempt (even after a restart) resumes from it; the price is a
~90 MB partial left on disk after a failure, until the next download cleans it up (recorded in
limitations). Automatic install still covers Windows only. **This fix takes effect in the next
release**: the build the user has contains the old code, so this upgrade still needs a manual install
via the browser (browsers resume on their own).

**Code**: `update/install.py` (`_part_path` / `_open(range_start)` / the three response branches in
`_download_once` / retry and poison-prefix handling / `JobTicket` plus `UpdateManager` liveness and
takeover), `web/routers/update.py` (the `_run_job` wrapper and the idempotent POST); frontend
`static/js/update.js` (one tick loop driving both capsule and dialog, a closable dialog,
`initUpdateBadge`), `documents.js` / `papers-page.js` / `eval.js` (one line each), `css/style.css`
(`.upd-pill`); tests `tests/unit/update/test_install.py`, `tests/unit/web/test_update_api.py`; E2E
`tools/chrome_update.py` (the fake source gained `Range` plus a "cut once at 1 MiB" switch, and five
new assertion groups: idempotency / resume / cross-page capsule / dismissed dialog / install only on
click).

---

## ADR-0025 Relicensed to AGPL-3.0: the distributed build bundles AGPL PyMuPDF

- Status: Accepted | 2026-09-20 (after the user asked what to do about the MIT / AGPL-PyMuPDF clash)
- Related: ADR-0022 / ADR-0024 (in-app updates and the release process — **publishing binaries** is the
  act that triggers the obligation)

**Problem**: the project has always been MIT, while a core dependency in `pyproject.toml` is
**PyMuPDF** ("AGPL-3.0 or Artifex Commercial"). It handles PDF text extraction
(`ingest/loaders.py`) and the reader's page rendering and text layer (`web/routers/documents.py`) — a
core component, not an optional extra. Since v0.1.1 we have been **publishing built exe/zip files**,
and that is when AGPL sections 5-6 kick in: distributing a binary that includes AGPL code means the
whole distribution must be licensed under the AGPL with the corresponding source offered to
recipients. The MIT claim simply does not cover that part — and **nothing in local development ever
surfaces this**; the obligation only exists on the *distribute* side.

**Decision**: the project moves to **AGPL-3.0-or-later**.

1. `LICENSE` becomes the verbatim AGPL-3.0 text (from gnu.org, LF endings); `pyproject.toml` carries
   `license = "AGPL-3.0-or-later"` plus the matching classifier; both READMEs state the license, the
   section-13 network obligation, and where the third-party notices live.
2. **No commercial license, and no replacing PyMuPDF**: the reader's "select text on the page and hit
   the same box in the original" depends on its word-box coordinates, and table geometry
   reconstruction and Chinese text-layer quality lean on it heavily. Swapping in `pypdf` (text) plus
   `pypdfium2` (rendering) is a real migration measured in weeks whose main outcome would be lower
   quality. **Fit the license to the dependency, not the dependency to the license.**
3. Compliance reuses machinery that already exists rather than adding process:
   `tools/make_third_party_notices.py` has always singled PyMuPDF out in `THIRD_PARTY_NOTICES.md`
   (including the "copyleft components require the corresponding source" hint), and both
   `tools/make_release.py` and `packaging/Mikasa.iss` already ship `LICENSE` and that notice with the
   product. Section 13's source offer is satisfied by the public repository itself.

**Consequences**: Mikasa becomes a strong-copyleft project — free to use, study, modify and
redistribute, but **modifications must be AGPL too**, and running it as a network service for others
requires offering them the source. A few legally cautious companies will therefore avoid this code; for
a public personal project that is an acceptable price, and it beats claiming MIT while bundling AGPL.
**The already-published v0.1.1-v0.1.4 builds still carry the MIT notice** (that cannot be recalled);
from the next release onward the repository and the artifacts agree.

---

## ADR-0026 Synthesized question banks and per-item verification: evaluation stops being tied to one corpus

- Status: Accepted | 2026-09-19 (landed after the user asked to "evaluate my own material")
- Related: ADR-0003 / ADR-0018 (pluggable providers — synthesis reuses the same LLM client and the model
  configured through the settings panel)

**Problem**: two things were stuck together.

1. **A hand-written bank can only evaluate the corpus it was written for**: every answerable item in
   `evals/questions.yaml` carries an anchor quoted verbatim from the sample corpus — point it at a
   different corpus and the questions stop matching their gold answers. Yet "evaluate *my* material"
   is the form the user actually wants. That path can only come from generation: nobody has written
   gold answers for the user's own documents.
2. **The whole-corpus fingerprint guarded far more than it protected**: the pre-run check compared the
   entire (chunk_id, content hash) sequence, so adding any one document or renaming a title
   invalidated the whole bank — while the real risk only concerns "did the few chunks the questions
   cite change?".

**Decision**:

1. **Synthesized banks** (`eval/synth.py`): sample chunks from the current corpus, have the model read
   one and write a question "only this passage can answer", and **that chunk becomes the gold answer**
   (gold = chunk id + content_sha256). Three sampling rules: **round-robin across documents**
   (otherwise the bank piles up on the longest document and measures that document's retrieval
   quality), **a chunk-length window** (very short chunks make thin questions and are nearly
   impossible to *miss*), and a **fixed seed** (the same corpus regenerates the same bank, so two
   retrieval changes can be compared head-to-head).
2. **Both question kinds are mandatory**: alongside answerable items the bank must contain
   **unanswerable** ones (several chunks, one question none of them answers) — the refusal discipline
   is this system's core promise (see limitations) and cannot be measured without them. If either
   kind comes up empty the whole synthesis fails and nothing is written to disk.
3. **Mismatch protection becomes per-item verification** (`GoldenItem.gold_hashes` aligned
   one-to-one with `gold_chunk_ids`): before a run, every item is checked — "is this chunk still
   here, is its content unchanged?" — and items that fail are **skipped**, with the count stated in
   the report header and each item listed with its reason. **Skip, not let through**: silently
   scoring against a stale gold answer produces numbers that look fine and mean nothing, the worst
   failure mode an evaluation tool has. **Keep a visible record, not silence**: quietly shrinking the
   denominator makes scores look better. Only "every answerable item was skipped" is a hard error
   (the corpus was rebuilt from scratch). `corpus_sha256` is demoted to metadata (the report shows
   which corpus the bank was written for).
4. **The honest boundary goes into the report**: synthesized questions are written by a model that
   just read the passage, so they are **easier** than hand-written ones and score higher by
   construction — they support **relative comparisons only** (two retrieval changes against the same
   bank), never absolute comparison with the hand-written bank. The report header marks them with
   `source: synthesized` and an explicit "自动生成 / machine-generated" label.
5. **A real model is required**: synthesis is dozens of LLM calls, so the offline mock profile gets a
   clear 400 up front (rather than letting the user discover a run full of junk questions); the bank
   lands in the data directory at `eval/golden-auto.json` (user data, not shipped); one job slot plus
   1-second progress polling — **separate from the evaluation slot**, because the two progress
   meanings differ ("questions generated" vs "answering question N") and merging them into one state
   machine would confuse the polling clients.

**Explicitly not doing**:

- **No automatic regeneration**: a failed check points the user at rebuilding (CLI `tools/build_golden.py`,
  or the web button), it never silently re-runs an LLM pass.
- **No difficulty tiers**: model self-assessment is not trustworthy; synthesized items are pinned to medium.
- **Not a replacement for the hand-written bank**: the built-in set remains the only ruler covering
  easy/medium/hard and the three unanswerable categories — that takes a human picking corners and
  cross-passage inference, which generation cannot do.

**Consequences**: any corpus (including the user's own library) can now be evaluated; the price is
that synthesized absolute scores cannot be compared against the hand-written bank, and the tie between
bank and corpus is looser — held together by "skip per item + record it in the report" rather than by
rejecting the whole bank. Older banks without `gold_hashes` degrade to checking only that the chunk
ids still exist.

**Code**: `eval/synth.py` (sampling / question writing / unanswerables / persistence),
`eval/golden.py` (`gold_hashes` / `check_against_corpus` / `source` / `model`), `eval/runner.py`
(per-item verification replaces the fingerprint comparison; `skipped_items` joins `EvalResult`),
`eval/report.py` (skipped-items section + header label), `index/manager.py`
(`Corpus.content_hashes()`), `web/routers/eval.py` (`GET /api/eval/goldens`,
`POST /api/eval/synthesize` + status, `POST /api/eval/runs?golden=`), `web/services.py`
(`SynthJobManager`, single slot), `static/eval.html` + `static/js/eval.js` (bank selection,
synthesis panel, progress polling, a full bar on completion); tests `tests/unit/eval/test_synth.py`,
`test_golden.py`, `test_runner.py`, `tests/unit/web/test_eval_api.py`; E2E `tools/chrome_eval.py`
(a local fake OpenAI-compatible endpoint: synthesis → a full evaluation run → the report).

---

## ADR-0027 Photos into notes: vision as its own config section, recognition as a draft, images anchored to the note key

- Status: Accepted | 2026-09-20 (M6 ②, the user asked for "photos into note text")
- Related: ADR-0021 (notes are ordinary documents with a marker), ADR-0018 (model setup in the
  settings panel), ADR-0026 (the same honesty rule: generated content must say so)

**Problem**: whiteboard photos, textbook pages and handwritten outlines taken while revising just
sit there as images — they cannot be searched or asked about, and retyping them defeats the point of
a quick note. Four questions have to be answered before this joins the note pipeline:

1. **Where does vision live?** The local profile (Ollama qwen3) has no vision model and the offline
   profile is a mock — and local is the profile the user actually runs day to day.
2. **Can recognition go straight into the library?** No: OCR always has errors (handwriting
   especially), and storing them would freeze those errors into the knowledge base.
3. **Do we keep the image?** The user decided **yes** (to check against later), but an image is not
   a document and must not drift into the corpus.
4. **Where does it live?** Notes are "same-name replacement" (every save changes `documents.id`), so
   anything keyed on the id is orphaned by the first edit.

**Decision**:

1. **Vision gets its own config section** (`VisionConfig`: `backend: none | api | local`),
   **defaulting to none**. Not merged into `llm`: one takes text, the other takes images, and merging
   them leaves no place for the real combination "DeepSeek for generation + SiliconFlow for
   recognition" — which is exactly what a local-profile user with a cloud key wants. `api.yaml`
   defaults to SiliconFlow `Qwen2.5-VL-32B-Instruct` (sharing `SILICONFLOW_API_KEY` with the
   retrieval side); `local.yaml` / `offline.yaml` say none explicitly (offline keeps its zero-call
   promise).
2. **Vision is its own provider layer** (`providers/vision.py`), and **the LLM Protocol is left
   alone**: multimodal content arrays cannot pass `list[dict[str, str]]` typing, and widening it
   would drag in the ask pipeline and MockLLM's crash point on non-string content. Only
   `build_openai_client` is extracted for shared key handling (a second copy would drift into
   "recognition works, chat does not").
3. **Recognition is a draft, not an ingest**: "识别图片" in the editor inserts the text at the
   cursor, the user corrects it, and saving ingests it. Images **never enter the body text**: the
   body feeds the index, so an image marker is retrieval noise and would force a re-embed on every
   save.
4. **Images are anchored to the note key** (`data_dir/note-media/<key>/<nnn>-<sha8>.<ext>`):
   the key inside `source_ref = "note:<key>"` is fixed at creation and survives renames and edits
   (ADR-0021 already established that the copy's filename is never regenerated). Content-addressed
   for dedup, several per note, and deleting the document removes the directory (**failures are
   logged, never blocking** — the same policy as the uploads copy, and lower risk since note-media
   is not scanned by reindex).
5. **The panel gains a vision group, but its key is write-only**: `SILICONFLOW_API_KEY` is shared
   with embedding/reranker/judge, so clearing it here would break the whole retrieval chain while
   presenting as "search results got worse" — the hardest failure to trace back to a key. An empty
   string is rejected with 422 pointing at the model section, and the frontend never sends an empty
   key in the first place. The panel is **purely additive** (`vision-settings.js` + `#v-*` DOM); the
   existing model section's regression surface is untouched.
6. **The OCR endpoint** (`POST /api/notes/ocr`): magic-byte validation (never the extension or
   Content-Type), a 12 MB cap (**independent of BodySizeLimit**, which is baked at `create_app` and
   does not follow hot reloads), images **never written to disk** (memory → base64 data URL),
   400 with an actionable message when vision is not connected (pointing at the panel or
   `ollama pull qwen2.5vl:7b`), and 502 on upstream failure (the app-level handler).
7. **Three frontend entry points, one function** (button / drag-and-drop / pasted screenshot →
   `addImage()`); compression reuses `image-util.js`, extracted from settings.js (two copies would
   eventually drift into "one compresses to 1600 px, the other ships the full phone photo"). A failed
   recognition **keeps the image** (the user may have wanted it stored anyway).

**Explicitly not doing**:

- **Archival image quality**: what is stored is a compressed JPEG (1600 px long edge ≈ 150 dpi),
  enough to check against later, not a scan.
- **Orphan-directory sweeping**: `ingest --reindex` empties the rows and leaves note-media
  directories unclaimed (recorded in limitations for a later round).
- **Batch recognition**: the frontend runs one image at a time (each takes seconds; concurrency
  would only queue).
- **Real Ollama VLM testing**: this round only documents the `ollama pull qwen2.5vl:7b` path.

**Consequences**: any profile can set up recognition — api works out of the box, local can point at
the cloud with one click or pull a local VLM; images stay decoupled from the body, so retrieval
quality is unaffected. The price: the settings panel lives on the QA page only (a cross-page
reality, handled with a pointer in the copy), and apart from deleting a note there is no automatic
cleanup path for images (the editor can remove them one by one).

**Code**: `config/settings.py` (`VisionConfig`, `note_media_dir`, the read-modify-write
`write_section_overlay`), `providers/vision.py` (new: magic-byte sniffing and `NoVision`'s
actionable error), `providers/llm.py` (extracted `build_openai_client`),
`web/routers/documents.py` (`/api/notes/ocr` + the four media endpoints + delete cleanup),
`web/routers/settings.py` (three `/api/settings/vision` endpoints), `web/schemas.py`,
`web/services.py` (`services.vision` + `rebuild_vision`); frontend
`static/js/{image-util,vision-settings,note-editor,reader,settings}.js` + `index.html` +
`style.css`; tests `tests/unit/providers/test_vision.py`, `tests/unit/web/test_note_ocr_api.py`,
`test_note_media_api.py`, `test_vision_settings_api.py`, `tests/unit/config/test_user_config.py`;
E2E `tools/chrome_note_ocr.py` (a fake multimodal endpoint driving the real browser through the
whole flow) and `tools/chrome_model_settings.py` (extended to the vision section, including the
end-to-end red line "configuring vision must not wipe the model section").

## ADR-0028 Formulas are typeset locally: KaTeX is vendored into the build

**Context**: the user asked a calculus question in free mode and got a correct, well-structured
answer whose `$$…$$` block formulas were displayed as raw LaTeX source — "why is the layout such a
mess". The cause was not a regression: the project had never rendered math at all. `renderAnswer`
is a small hand-written Markdown-ish renderer (fenced code, inline code, bold, headings, lists,
tables, `---`, citation chips), and it passed `$…$` / `\frac{}` through as plain text. A grep for
`KaTeX|MathJax|LaTeX` across the repo returned zero hits.

**Decision**: vendor KaTeX 0.18.7 (`katex.min.js`, `katex.min.css`, 20 `.woff2` fonts, `LICENSE`)
into `web/static/vendor/katex/` and typeset math inside `renderAnswer` as a **pre-pass**:
extract LaTeX from the raw text, replace it with placeholders, run the existing pipeline
(escape → controlled replacement), then swap the KaTeX HTML back in.

**Why a pre-pass instead of a DOM post-pass**: KaTeX's own `renderToString` is a pure string
function, which keeps the "no build chain, no DOM dependency" property of this renderer (and its
smoke test can run it under plain Node). Extracting *before* `esc()` matters: a post-pass would
hand KaTeX `x &lt; y` and render nonsense.

**Guardrails**: code fences and inline code are masked first (`echo $HOME` is not a formula), and
paired `$` alone is not enough — a Chinese-character run without any LaTeX command, or a purely
numeric payload, is treated as prose/currency (`价格$5到$10之间`, `$1000$`). The first version of
that rule was too strict and silently skipped 8 real formulas in the user's own answer
(`(-1, 1]`, `n!`, `2n+1`, `o(x)`) — the rule is now "exclude, then accept by default", with the
miss locked by smoke assertions.

**Alternatives considered and rejected**: telling the model to avoid LaTeX (math becomes unreadable
in exactly the subject where it matters); MathJax (heavier, and its DOM-walking design does not fit
a string-returning renderer); a CDN loader (this app must work offline, and the desktop window has
no guaranteed internet path — the project already burned a day on GitHub connectivity).

**Consequences**: answers carry ~150 KB of KaTeX HTML for a formula-dense reply (the user's Taylor
expansions: 15 display + 42 inline formulas → 148 KB rendered, up from 6 KB of Markdown), and the
package grows by 545 KB. In exchange, math is readable, selectable and copyable, and it works with
the network unplugged. If KaTeX is missing (script absent, Node smoke), the raw LaTeX is shown
verbatim — never swallowed.

**Code**: `web/static/vendor/katex/**` (vendored, MIT, listed in `THIRD_PARTY_NOTICES.md` via
`tools/make_third_party_notices.py`), `web/static/js/common.js` (the "公式渲染" section),
the four pages (`vendor` CSS **before** `style.css`, so the project's font-size override wins),
`css/style.css` (`.katex` / `.katex-display`, long formulas scroll instead of bursting the bubble);
tests `tools/smoke_render.mjs` (fake KaTeX: wiring, code masking, currency rejection),
`tools/smoke_math.mjs` (the real vendored KaTeX, 24 assertions), and
`tests/unit/web/test_frontend_static.py` (files present + every page wired + CSS order).

## ADR-0029 The local profile uses Ollama's native API: think / num_ctx knobs plus an answer-shape contract

**Context**: on 2026-09-20 the user reported two things — "the local model answers far less
than DeepSeek" and "the layout is not great". They are not the same problem, but both trace
back to what the local channel could not do:

1. **Thinking mode ate the entire output budget.** qwen3:8b thinks by default, and Ollama's
   OpenAI-compatible surface (`/v1/chat/completions`) **ignores** the `think` parameter. So the
   model reasoned first every time: on the same question, **14.2 s, 617 characters of reasoning,
   0 characters of answer** (the 400-token output budget was consumed by thinking). With thinking
   off, the same question answers in **1.4 s**. What the user saw was "it spins for a long time
   and then there is nothing".
2. **The context was silently cut in half, twice.** Ollama's runtime default context on this
   machine is 4096, and a single request fits even less — **a prompt that should be parsed in
   full was processed at only 2050 tokens** when `num_ctx` was omitted. This profile's prompt is
   "system prompt + 14 retrieved chunks + history summary", easily above ten thousand tokens, so
   every question was truncated by more than half (and typically from the front, meaning the
   citation and refusal rules may never have reached the model). This is also most of the story
   behind "it gives me too little material".
3. **Answer shape was left entirely to the model's own judgement.** DeepSeek sections its
   answers, uses lists and tables; an 8B model just writes flat prose. That is not a hard
   capability gap — the large model *chose* those shapes, and a small one needs to be told.

**Decision**:

- The local profile's generation side **switches from the OpenAI-compatible surface to Ollama's
  native `/api/chat`** (new class `OllamaNativeLLM` in `providers/ollama.py`). The api profile is
  unaffected and still speaks OpenAI-compatible.
- Two new **local-only** knobs (ignored by the api profile, and shown in the settings panel only
  under the "this machine" source):
  - `llm.think`: tri-state (`None` = omit the parameter, `true` / `false`), **`local.yaml`
    defaults to `false`**;
  - `llm.num_ctx`: `None` or a token count, **`local.yaml` defaults to `16384`**.
  "Not configured" and "turned off" are different things: `None` omits the key from the request
  body and follows Ollama's default.
- Both system prompts (kb and free) gain the same **output-shape contract**
  (`OUTPUT_FORMAT_CONTRACT`): lead with the conclusion, section with `## `, use a Markdown table
  whenever data is being compared, typeset math in LaTeX, sketch structures with a fenced
  text diagram, prefer completeness over brevity. It does not conflict with rule 6 — "if the
  source is a table, present it as a table" remains a hard constraint; the contract only adds
  "when a shape is called for, do not fall back to prose".

**Why the channel has to change**: `think` and `options.num_ctx` are understood **only** by the
native API; the compatible surface **silently ignores** them (no error, no effect). As long as
requests went through `/v1/chat/completions`, both knobs were decorative and problems 1 and 2
had no fix.

**Why stdlib urllib instead of the openai SDK**: the native response shape is not something the
SDK parses — streaming is **NDJSON**, one JSON object per line, and reasoning text arrives in
`message.thinking` alongside `message.content`. A local server has no key and no proxy, so urllib
is enough, and it sidesteps the httpx address-selection trap (`normalize_base_url`, see
limitations §七).

**Measurements** (2026-09-20, Ollama 0.34.2 + qwen3:8b, RTX 4060 8 GB):

| Prompt | num_ctx | Tokens actually processed |
| --- | --- | --- |
| 5296 tokens | omitted (runtime default 4096) | **2050** |
| 5296 tokens | 4096 | 2050 |
| 5296 tokens | 8192 | 5296 (full) |
| 5296 / 8992 tokens | **16384** | 5296 / **8992 (full)** |
| 8992 tokens | 32768 | 8992 (full) |

16384 is where effect and VRAM balance: qwen3:8b's KV cache at 16384 costs roughly 2.4 GB, and
going further (32768 ≈ 4.7 GB) starts competing with the 5 GB of model weights. `Mikasa.bat`
sets `OLLAMA_CONTEXT_LENGTH=16384` for the source checkout — the same value — but that only
helps an Ollama started from that script; the packaged build (double-clicked exe, Ollama started
by the tray app) never sees it, so **the setting has to live in the config file**.

**Consequences and boundaries**:

- **Only Ollama takes this path.** LM Studio / vLLM and friends should use the api profile with
  a custom base URL (they have no concept of `think` or `num_ctx`).
- **Thinking defaults to off** as a trade-off: on hard derivation-style questions thinking does
  help, but it costs ~10x the time and can burn the whole budget. Off by default, one click away
  in the settings panel.
- **The panel refuses private-network addresses** (SSRF gate, see limitations §九): an Ollama on
  your LAN has to be pointed at from the config file.
- The shape contract is a prompt-level constraint and **does not guarantee obedience** from a
  small model; it turns "hope the model picks a shape" into "ask for the shape". In practice the
  8B model's use of tables and sections on the same question went up noticeably.

**Code**: `providers/ollama.py` (`OllamaNativeLLM`), `providers/__init__.py` (factory split),
`config/settings.py` (`LLMConfig.think` / `num_ctx`), `config/profiles/local.yaml` (defaults),
`pipeline/prompts.py` (`OUTPUT_FORMAT_CONTRACT`), `web/routers/settings.py` + `web/schemas.py`
(panel fields, and the probe travels the same channel as real questions),
`web/static/{index.html,js/model-settings.js}` (the two "this machine" dropdowns); tests
`tests/unit/providers/test_ollama_native.py`, `tests/unit/providers/test_factory.py`,
`tests/unit/web/test_settings_api.py`. The E2E fake server learned both protocols
(`tools/fake_llm.py`).
