# Automated Evaluation (M2): Methodology and Usage Guide

> Companion code: `src/mikasa/eval/`, `tools/build_golden.py`, `evals/questions.yaml`,
> `evals/golden_set.json`; commands: `mikasa eval run | list`.

Evaluation is not meant to answer "is the model any good." It answers three
questions that can be assessed separately: **were the sources we needed actually
retrieved (retrieval)**, **did the answer follow the citation rules and hold the
refusal line (generation protocol)**, and **is the answer correct and complete
(semantics)**. Each of the three costs more and depends on more than the last, so
evaluation is split into three stages, each run under its own configuration, so
that no conclusion masks another.

## 1. Three-stage methodology

| Stage | What it measures | Configuration | Metrics | Runs offline |
| --- | --- | --- | --- | --- |
| A Retrieval | Whether fusion recall brings back the material the question needs | Reranker off (fusion window independently anchored at 10, same as the current product value) | recall@5/8/10, MRR, nDCG@k (stratified by difficulty) | ✅ |
| B Generation | Citation discipline + refusal discipline | Product configuration as-is (reranker on, top5, window 10) | citation gold ratio, out-of-range citation rate, false refusals on answerable questions, refusal accuracy | ✅ (mock LLM) |
| C Semantic judge | Answer content quality | LLM-as-Judge (two rounds, positions swapped) | correctness 1-5, grade A-D, faithfulness, two-round agreement | ❌ needs an API key / local judge |

Why stage A turns the reranker off: the reranker can push the material a question
needs out of the top ranks — that is a reranker failure, not a recall failure.
Retrieval evaluation has to decouple recall from reranking and answer only "is the
material in the candidate set"; stage B returns to the product configuration
(reranker on, top5) to measure the real end-to-end path. The fusion window is 10
for both A and B (validated by the real 63-question run: a window of 8 misses the
second gold chunk on hard synthesis questions — q036/q037 put gold chunks at ranks
9-10, and at @10 47 questions reach recall@10=1.000) — A's window is anchored
independently by a runner constant, so shrinking it in the product later cannot
move A's methodology. Both configurations are disclosed in the same report.

## 2. The golden set: two guards against questions that no longer match the corpus

Golden questions are **hand-written** (`evals/questions.yaml`, 63 of them: 47
answerable graded easy/medium/hard + 16 unanswerable classified as unrelated /
insufficient / hallucination_bait). They are not auto-generated — auto-generation
would only learn the corpus's biases all over again.

- **Unique anchor hit**: every answerable question carries anchors copied verbatim
  from the corpus text. When `tools/build_golden.py` resolves anchors into real
  chunk_ids, both zero hits (question and corpus disagree) and multiple hits
  (ambiguity from chunk overlap) **refuse to freeze**, reporting every problem in a
  single pass;
- **Corpus fingerprint freeze**: the golden-set JSON is written to disk together
  with a corpus_sha256, which is checked again before evaluation — any addition,
  removal, edit, **or rebuild** of the corpus makes the fingerprint mismatch, and
  `mikasa eval run` fails immediately with a prompt to re-run
  `tools/build_golden.py`. That rules out the silent mismatch of "stale question
  set, fresh corpus."

  The fingerprint has a subtlety: the hashed object is the (chunk_id,
  content_sha256) pair, and **the id must be part of it**. Lesson (pitfall hit
  during the first real acceptance round, 2026-09-09): the chunk table uses
  AUTOINCREMENT, so after a `--reindex` rebuild ids shift wholesale and are never
  reused (old database 1-49 → new database 50-98). Hash only the content sequence
  and a database where "the content is unchanged but every id moved" keeps the same
  fingerprint, while the golden set's gold_chunk_ids are all misaligned — all 63
  questions score recall 0 and the fingerprint check still passes. Once ids are
  mixed in, any rebuild triggers an explicit mismatch, forcing a re-run of
  build_golden to re-resolve the anchors onto the new ids (idempotent, takes
  seconds).

One pitfall to note: the chunker keeps an **overlap window** between adjacent
chunks (a sentence at the end of one chunk reappears in the next), so such
sentences cannot be used as anchors; and long PDF lines get hard-wrapped by the
loader, so an anchor must fall inside a single line.

```bash
mikasa ingest sample-corpus      # 1. ingest the corpus
python tools/build_golden.py     # 2. freeze the golden set (idempotent; re-run when the corpus changes)
mikasa eval run --profile offline   # 3. run the evaluation (zero API keys); api/local enable the semantic judge
mikasa eval list                 #    browse past runs
```

## 3. Metric definitions (all hand-written, no evaluation library)

- recall@k = |G ∩ R[:k]| / |G| — G is the set of gold chunks for the question, R
  the retrieval result. With multiple gold chunks, each contributes its own hit
  ratio;
- MRR = reciprocal rank of the first gold chunk (0 if none is found); nDCG@k
  scores the overall ranking quality across multiple gold chunks;
- **citation gold ratio**: the share of cited chunks in an answer that hit G
  (computed only over samples that have citations). This is the recall-side proxy
  when the semantic judge is absent, and is deliberately not conflated with true
  citation precision;
- **refusal accuracy** = clean refusals / unanswerable questions. Clean refusal =
  total − wrong answers − refusals that still carry citation markers (L3
  violations, theoretically unreachable, so any occurrence is a protocol bug);
- out-of-range / fabricated citation rate: among all `[n]` markers in the answer
  body, the share whose number falls outside the injected 1..N. A model inventing
  numbers is the core signal of citation-format discipline, and maps to "citation
  parse failure" in production RAG systems.

Per-item values accumulate in a `_Mean` container, and `summarize` reports mean /
p50 / p95 / min / max / n at report time. Difficulty strata and the overall
figures are computed from the same raw per-item data, keeping the methodology
transparent.

## 4. The semantic judge (stage C) and its three bias corrections

An LLM judge has three known biases — self-preference, position sensitivity, and
scale drift — each addressed explicitly:

1. **Position swap**: the same judgement is asked twice with the 【参考答案要点】
   (reference answer points) and 【候选回答】 (candidate answer) positions reversed.
   Only rounds that agree are admitted into the score statistics; disagreements
   (including format-parse failures) are disclosed separately as inconsistent —
   disagreement is itself a signal of "unstable generation or judge sensitivity",
   so it is listed in the anomaly detail for human review rather than smoothed
   away;
2. **Two scales**: each round asks for both a 1-5 score and an A-D grade. Grade and
   score are judged independently, and the report discloses the share of A/B grades
   among agreeing samples;
3. **Cross-vendor**: the judge and the generator default to different vendors
   (DeepSeek for generation / SiliconFlow Qwen for judging, which the `api` profile
   enforces; the `local` profile uses Ollama for both). This is enforced at the
   configuration layer, and the report discloses the configuration in effect.

Parsing relies on fixed-format lines (`正确性评分: N/5` "correctness score" /
`档位: X` "grade" / `忠实性: 是/否` "faithfulness"), never on JSON mode — small
Ollama models are unreliable at it, and parse failures are recorded raw for the
audit trail. The judge scores samples that are answerable and not refused; refusals
and empty answers short-circuit without judging (correctness is the refusal
bucket's job). Judge material = the gold chunks' raw text concatenated (capped at
3000 characters, to control cost and keep per-question cost fair).

**NoJudge (protocol layer only)**: when the judge is not enabled or the api key is
missing, the system degrades automatically to NoJudge and the report states
explicitly that it covers "protocol-layer metrics only." Offline/CI runs have no
semantic score, and that is the intended default — the three profiles behave
identically, differing only in configuration.

## 5. Known boundaries and limitations (an honest list)

- **The offline mock LLM has no semantics**: it only demonstrates the citation
  protocol (it answers with a citation when the question and the corpus share a
  ≥4-character contiguous overlap, and refuses otherwise), so citation gold ratio
  and refusal accuracy in offline runs are a "protocol-layer self-check", not real
  quality. In the real 63-question run, 7 unanswerable questions were answered
  wrongly by the mock (4 of 5 bait, 3 of 5 insufficient, the rest cleanly refused) —
  those wrong answers are exactly the evidence of the mock's limits, and they show
  that real semantics require stage C;
- A note on the mock's built-in noise: offline refusal accuracy ≠ the product's
  refusal capability; do not report it externally;
- **Real api-profile baseline (2026-09-09 acceptance run, DeepSeek generation +
  Qwen judge)**: stage A recall@10=1.000 / MRR=0.979 / nDCG≈0.98 (hard, 12
  questions: recall@5=0.972); out-of-range citation rate 0.000, citation gold ratio
  mean 0.848 (p50=1.000, 44-question sample); 16/16 refusals on unanswerable
  questions (the real model holds the line; the mock's 7 wrong answers drop to zero
  here); 3/47 false refusals on answerable questions — q036 was missing its second
  gold chunk in retrieval (genuinely missing material, so refusing was reasonable),
  while q021/q031 were conservatively refused despite complete supporting material
  (the boundary of prompt rule 4, "answer when partially covered"; granting more
  latitude would threaten unanswerable-question discipline, so it is accepted as a
  known boundary); judge two-round agreement 36/44 (of the 8 disagreements, most
  are 4/B vs 5/A boundary cases and a few are faithfulness reversals; when judge
  noise exceeds 10%, suspect the judge model before the generator); correctness
  mean 4.83, 100% A/B grades.
- **Real local-profile comparison (2026-09-09 run #5, qwen3:8b generation, judge
  off → protocol-layer metrics only)**: stage A recall@5=0.975 / recall@10=0.982 /
  MRR=0.940 — easy/medium are all 1.000 and the entire gap sits in the 12 hard
  questions (recall@10=0.931), i.e. the loss of 512-dimension bge-small against the
  api profile's 1024-dimension bge-m3 is concentrated in the hardest stratum; stage
  B out-of-range citation rate 0.000, L3 violations 0, false refusals 0/47 (the
  questions the api profile refused conservatively, q036 among them, are all
  answered locally — semantic quality unconfirmed by a judge, protocol compliance
  only); citation gold ratio 0.798 (n=47); **refusals 13/16**: the three wrong
  answers on unanswerable questions, q057/q058/q059, are all of the "external
  knowledge the model remembers from pre-training" type (including the deliberately
  planted hallucination_bait q059 — the sentinel caught the small model as
  intended), i.e. small local models tend to answer rather than refuse, the mirror
  image of DeepSeek's conservative refusals; trading 3/16 of refusal discipline for
  zero cost, with the trade-off recorded in ADR-0014 ③/⑤;
- **M4.1 investigation (2026-09-09, full reproduction on the same fingerprint and
  configuration, plus ablations; conclusion: all accepted as local-profile
  boundaries)**: ① hard-stratum misses pinpointed to q036's second gold chunk 155
  (the gradient descent chunk) and q041's third gold chunk 126 (the scaled
  dot-product attention chunk) — fusion ranks 14/16/24; ablating fusion_top_k
  10→20 and the bm25/dense sub-window 20→40 produced **zero change** in recall@10
  (a wider window does not change the top-10 order; enlarging the sub-window only
  moved 155 from 24 to 16) — the gap is fundamentally the "answer spans chunks"
  problem of multi-gold-chunk questions: gold labels mark 2-3 chunks by answer
  logic, while dual-path retrieval ranks on a single relevance peak (in the api
  profile's bge-m3 era the second gold chunk also sat at the 9-10 boundary, see the
  note in runner.py), so the tuning route is empirically ineffective; ②
  investigation of the three refusal failures: q057 fabricated citations to
  support an answer (leaning on a KNN chunk), q058 answered bare with no citations
  (knew it had nothing, answered anyway), q059 hallucinated with citations attached
  to the wrong sources (a paper chunk used to support an author count) — prompt
  rule 3, "even if you know the answer", and its worked example (the "Earth's
  radius" analogue) already cover all of these, and DeepSeek scores 16/16 under the
  same rules, which shows the rules work and qwen3:8b simply complies less
  reliably; hardening the prompt has low marginal return, and changing the shared
  protocol would require a full-profile regression; ③ the citation gold ratio of
  0.798 is likewise a generation-model citation-precision issue (p50=1.000, a few
  questions partially miss). Options on record: switch to qwen3:14b (tight at 8 GB
  of VRAM, markedly slower generation, no guarantee of reaching zero); enabling the
  local reranker requires picking a fastembed version (ADR-0014 ②) — neither is a
  low-cost win;
- Judge cost: two rounds × 63 questions; a full api-profile run makes roughly 126
  judging calls, linear in the number of questions;
- The online effect of the reranker and dense vectors in stage B depends on local
  models or API keys; CI covers only the protocol layer.

## 6. Reading the report

`data/eval-reports/{run_id:04d}-{name}.md` comes from the same origin as the
`eval_runs` table (the report_md column). Fixed sections:

- **Stage A table**: check first whether recall@10 is ≥0.95 — if not, look at
  chunking and retrieval before touching the model;
- **Stage B table**: false refusals > 0 → check whether "the refusal threshold is
  too tight"; answers with no citations that were not refusals > 0 → check the
  citation prompt; refusal accuracy < 1 → go to the anomaly detail and read the
  per-question entries (which reason category the wrong answers cluster in tells
  you whether to fix the prompt or the corpus);
- **Stage C table**: the two-round disagreement rate is a health signal; above 10%,
  suspect the judge temperature/model first, then unstable generation;
- **Anomaly detail**: pipeline failures, false refusals, wrong answers, L3
  violations, and judge disagreements listed one by one — this is the entry point
  for human review; for normal items, inspect the metrics_json items for the
  per-item audit trail.

Regression gate (CI): full `pytest -q` (including 58 eval unit tests) + `ruff` +
`mypy` + `mikasa eval run --profile offline` as a smoke test. **Coverage baseline:
93%** (3469 statements, full re-measurement after the M4.5 conversation-management
work landed on 2026-09-09; `--cov=mikasa --cov-report=term`, 332 test cases in
~17s; the previous baseline was 91% = 3143 statements, re-measured after M3.5
landed). Real quality acceptance (run manually before release): `--profile api`
(DeepSeek generation + Qwen judge, stage C scoring); with the `local` profile the
judge is off (ADR-0014 ③) → protocol-layer metrics only (recall / citation
discipline / refusal), with the api profile standing in as the semantic-quality
reference.
