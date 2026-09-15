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
