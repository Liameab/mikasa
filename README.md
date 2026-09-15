# Mikasa

> A personal document QA workspace — local-first RAG with citation tracing and automated evaluation.
> *[中文说明见 README.zh-CN.md](README.zh-CN.md)*

Mikasa is a **from-scratch** RAG application: retrieval (self-implemented BM25 + dense vectors + RRF
fusion), Chinese-aware structural chunking, citation tracing, and a three-stage automated evaluation
pipeline — all hand-written, **no RAG framework**. The code is transparent and the evaluation numbers
are reproducible.

**Status**: 495 tests passing, ruff + mypy clean. Actively developed; M5 (public release) in progress.

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
  out-of-range citations, 16/16 unanswerable questions cleanly refused.
- **Cross-lingual retrieval** — a Chinese question against English documents is automatically
  translated into a second query, and both retrieval paths are merged with RRF. Cited English
  passages are shown as *original + Chinese translation* side by side.
- **Document reader with citation jump** — click a citation marker and the source opens in a side
  panel, either as reconstructed text or as the rendered PDF page with the cited region highlighted.
- **Three runtime profiles** — cloud API / local inference / zero-key offline; switch with a single
  `--profile` flag, no code changes, no hardcoded endpoints.
- **Build-free web UI** — three plain HTML/JS pages with hand-written SSE streaming (no node_modules,
  no bundler).
- **Session tree** — arbitrarily nested folders for conversations, with auto-generated titles from
  the first question (manual renames are never overwritten).
- **Model settings in the UI** — pick a provider (local Ollama / DeepSeek / SiliconFlow / any
  OpenAI-compatible endpoint), paste a key, test the connection, save: the LLM switches
  immediately, no restart and no `.env` editing. Keys stay in the local data directory and are
  never sent back to the browser.

## Documentation

| Document | Contents |
| --- | --- |
| [Architecture](docs/architecture.md) | How the system is organized and how data flows |
| [Design decisions (ADR)](docs/design-decisions.md) | Every "why": choices, trade-offs, and decisions later overturned by data |
| [Evaluation](docs/evaluation.md) | Three-stage methodology, golden-set mismatch guards, judge-bias correction, acceptance baseline |
| [Limitations & failures](docs/limitations-and-failures.md) | Ecosystem pitfalls, heuristic boundaries, and a real bug archive from development |
| [Usage guide](docs/usage-guide.md) | End-to-end workflows (paper reading, follow-up questions, what to do when it refuses) |
| [Known issues](docs/known-issues.md) | Unfixed issues (with rulings) and unscheduled candidates |

> Documentation is written in English; the Chinese originals live in [`docs/zh-CN/`](docs/zh-CN/).
> Source comments are in Chinese, the working language of this project's design notes.

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

# 4) Build the index (first run downloads bge-small-zh-v1.5, ~100 MB)
mikasa ingest --reindex --profile local

# 5) Same commands, different profile
mikasa ask --profile local "How does L2 regularization prevent overfitting?" --show-sources
```

## Runtime profiles

| profile | Generation | Embedding / rerank | Use case |
| --- | --- | --- | --- |
| `api` | DeepSeek (OpenAI-compatible) | SiliconFlow bge-m3 / bge-reranker | Real QA and acceptance runs |
| `local` | Ollama qwen3 (**no API key**) | fastembed bge-small-zh (512-d) | Fully offline inference (see ADR-0014) |
| `offline` | Built-in MockLLM | none (BM25 only) | Zero-key demos, tests, CI |

All configuration lives in `config/profiles/*.yaml` with a fully commented field reference in
`config.example.yaml`.

## Web UI

```bash
mikasa serve                      # api profile (real models)
# WARNING: --host 0.0.0.0 exposes the service to your whole network.
# There is no authentication: anyone on that network can read every
# document you ingested, delete them, upload files, and run evaluations
# (which spends your API credits). Only use it on a network you trust.
mikasa serve --profile offline    # zero-key demo
# Options: --host 0.0.0.0 (LAN access) / --port 9000 / --reload (dev)
```

Open http://127.0.0.1:8000/ — three pages:

- **Chat** — SSE streaming, clickable citation markers, multi-turn sessions, and a KB / free-chat
  mode switch (KB mode is strict RAG with citations and refusals; free-chat mode talks to the model
  directly without retrieval or citations).
- **Library** — upload and delete documents in the browser; a folder tree organizes the corpus;
  newly ingested documents are searchable immediately.
- **Evaluation** — run the golden-set evaluation in the background, poll progress, and read the
  generated report.

API docs (Swagger) at http://127.0.0.1:8000/docs.

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

## Engineering notes

- Python ≥ 3.11 (3.13 recommended); optional NVIDIA GPU for local inference;
- Source comments and internal docs are written in **Chinese**; baseline gates are
  `ruff format`, `ruff check`, `mypy`, and `pytest`;
- Current suite: **430 tests**, coverage ~93% (see the regression gate in `docs/evaluation.md`);
- Zero-compilation install on Windows + CPython 3.13 (all dependencies ship prebuilt wheels;
  see `pyproject.toml` and ADR-0006/0008 for the version-pinning rationale).

## Project layout

```
src/mikasa/
├── cli/          # typer + rich commands
├── web/          # FastAPI + plain HTML/JS (chat / library / evaluation pages)
├── pipeline/     # retrieve → inject → generate → verify orchestration (citation protocol)
├── ingest/       # four-format loaders + Chinese structural chunking
├── index/        # self-implemented BM25 / numpy exact vector search / RRF fusion
├── eval/         # golden-set evaluation: three stages + judge
├── providers/    # LLM / embedding / rerank: Protocol + cloud & mock implementations
├── storage/      # SQLite + meta.json snapshots
└── config/       # pydantic settings (three profiles merged)
sample-corpus/    # original sample corpus (MD / TXT / DOCX / PDF)
evals/            # golden-set source (questions.yaml → golden_set.json)
config/           # profiles/*.yaml + commented example
```

## License

See [LICENSE](LICENSE).
