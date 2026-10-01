# Automated Evaluation (M2): Methodology and Usage Guide

> Companion code: `src/mikasa/eval/` (orchestration plus `synth.py`, the question
> generator), `tools/build_golden.py`, `evals/questions.yaml`, `evals/golden_set.json`.
> Commands: `mikasa eval run | list | compare`; **the synthesized bank is generated from the web
> evaluation page**, not from the CLI.

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
| A Retrieval | Whether fusion recall brings back the material the question needs | Reranker off (fusion window independently anchored at 10, the same as the api/offline profile's current value; the local profile uses 14) | recall@5/8/10, MRR, nDCG@k (stratified by difficulty) | ✅ |
| B Generation | Citation discipline + refusal discipline | Product configuration as-is (reranker on, `top_n=8`, window 10) | citation gold ratio, out-of-range citation rate, false refusals on answerable questions, refusal accuracy | ✅ (mock LLM) |
| C Semantic judge | Answer content quality | LLM-as-Judge (two rounds, positions swapped) | correctness 1-5, grade A-D, faithfulness, two-round agreement | ❌ needs an API key / local judge |

Why stage A turns the reranker off: the reranker can push the material a question
needs out of the top ranks — that is a reranker failure, not a recall failure.
Retrieval evaluation has to decouple recall from reranking and answer only "is the
material in the candidate set"; stage B returns to the product configuration
(reranker on, `top_n=8`) to measure the real end-to-end path. The fusion window is
10 for A, and for B it is whatever the product says (10 on api/offline, 14 on
local) — validated by the real 63-question run: a window of 8 misses the
second gold chunk on hard synthesis questions — q036/q037 put gold chunks at ranks
9-10, and at @10 47 questions reach recall@10=1.000) — A's window is anchored
independently by a runner constant, so shrinking it in the product later cannot
move A's methodology. Both configurations are disclosed in the same report.

## 2. The golden set: two banks + guards against questions that no longer match the corpus

**The built-in bank is hand-written** (`evals/questions.yaml`, 63 of them: 47
answerable graded easy/medium/hard + 16 unanswerable classified as unrelated /
insufficient / hallucination_bait). Its value is that a human does not phrase
questions the way the corpus does — auto-generation would only learn the corpus's
biases all over again.

**The synthesized bank** (2026-09-19, `src/mikasa/eval/synth.py`; **its entry point
is the web evaluation page** — `POST /api/eval/synthesize`, with the bank landing in
`<data dir>/eval/golden-auto.json`; there is no CLI subcommand) covers the other
half: hand-written anchors are verbatim quotes from the sample corpus, so they die
with a corpus swap — and nobody has written gold answers for *your* documents. The
synthesizer samples chunks from your own corpus and asks the model for a question
only that chunk can answer; **that chunk becomes the gold answer**, so any corpus
can be evaluated. The cost is stated in the report: a synthesized question is
written by a model that has just read the source text, so it is easier than a
hand-written one and scores run high — use it for **relative** comparisons (two
retrieval changes on the same bank), never against hand-written absolute numbers.
The report header marks such banks as **auto-generated**.

Two guardrails (2026-09-19):

- **Unique anchor hit** (built-in bank only): every answerable question carries
  anchors copied verbatim from the corpus text. When `tools/build_golden.py`
  resolves anchors into real chunk_ids, both zero hits (question and corpus
  disagree) and multiple hits (ambiguity from chunk overlap) **refuse to freeze**,
  reporting every problem in a single pass;
- **Per-chunk freeze** (both banks): each answerable question is frozen together
  with the content_sha256 of its gold chunks. Before a run every question is
  checked — "is that chunk still there, is it still the same text?" — and the ones
  that fail are **skipped**, with the count in the report header and the ids and
  reasons listed in the body. Only a bank whose answerable questions are *all*
  stale is a hard error.

  **Changed from the old global fingerprint**: the guard used to hash the whole
  corpus as a (chunk_id, content_sha256) sequence, so adding any single document —
  or renaming one — invalidated the entire bank, while the actual risk only ever
  concerned the few dozen chunks a question points at. Chunk-level checking keeps
  what matters: a `--reindex` that shifts ids (old ids no longer resolve → skip)
  and an edited chunk (hash mismatch → skip). Skipping rather than proceeding is
  the point: scoring against misaligned gold answers produces numbers that look
  normal and mean nothing, which is the worst failure mode an evaluation tool has.
  Showing the skips is the other half: silently shrinking the denominator makes the
  score look better.

One pitfall to note: the chunker keeps an **overlap window** between adjacent
chunks (a sentence at the end of one chunk reappears in the next), so such
sentences cannot be used as anchors; and long PDF lines get hard-wrapped by the
loader, so an anchor must fall inside a single line.

```bash
mikasa ingest sample-corpus      # 1. ingest the corpus
python tools/build_golden.py     # 2. freeze the golden set (idempotent; re-run when the corpus changes)
mikasa eval run --profile offline   # 3. run the evaluation (zero API keys); only api enables
                                    #    the semantic judge (local has judge.enabled=false, ADR-0014 ③)
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

### 3b. Uncertainty: confidence intervals and paired comparisons (2026-09-25)

A single number over 63 questions carries **no uncertainty** — when a retrieval
change moves recall@10 from 0.98 to 0.99, those two numbers alone cannot answer
"is this better, or is it noise". Two additions:

- **A 95% confidence interval on the mean** (a new column in the stage A table):
  bootstrap resampling over the **question bank** (2000 draws, fixed seed). It
  reads as "if another bank were drawn from the same distribution, the mean would
  probably land here". **The seed is fixed on purpose**: rendering the same data
  twice must give the same interval, otherwise "0.82 last time, 0.79 this time"
  cannot be attributed to the model rather than to the resampling. It does *not*
  cover corpus changes or model-version drift — those belong to fingerprint
  checks and per-item validation, not to statistics.
- **`mikasa eval compare <old> <new>`: a paired, per-question comparison.** This
  is the stronger and more useful test: **the difference of two independent run
  means is drowned by between-question variance** (the normal wobble of the few
  hard questions is larger than the real effect of a retrieval change), while a
  paired test only looks at "how much did *this* question move" — unchanged
  questions cancel out. It prints the mean difference, its 95% interval, and a
  `significant` flag (the interval does not cross zero).

**Two boundaries, stated plainly**: (1) the interval holds for **this bank**;
the bank is itself a sample, and this is not a law of physics. (2) Pairing
requires per-question traces on both runs (only runs from 2026-09-25 onward have
them), and with fewer than five shared questions the command refuses to draw a
conclusion rather than computing one anyway. Strata with small samples (hard has
12 questions) get much wider intervals — check the interval before reading
anything into "hard dropped two points".

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
- ⚠ **Configuration footnote**: both 09-09 baselines below were run on older link
  parameters (api `reranker.top_n=5`, local `fusion_top_k=10`, and the local profile
  had neither `think` nor `num_ctx` yet). They are **not directly comparable** to a
  fresh run against today's defaults (ADR-0017 ④: changing `top_n` shifts the
  baseline). They remain evidence for the *magnitude* of real quality, not a
  regression reference.
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
  chunking and retrieval before touching the model. The **95% CI (mean)** column
  in the same table is that number's uncertainty (see §3b): when the interval
  straddles your decision line, enlarge the bank or use a paired comparison
  instead of reading the second decimal place;
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

Regression gate (CI): full `pytest -q` + `ruff` +
`mypy` + `mikasa eval run --profile offline` as a smoke test. **Coverage baseline:
92%** (8594 statements / 694 missed, full re-measurement on 2026-09-30:
**1136 passed + 2 skipped in 1m51s**; `--cov=mikasa --cov-report=term`. The previous
baseline was 92% = 8578 statements / 1126 passed on 2026-09-27 (the MCP round),
before that 92% = 8190 statements / 1085 passed on 2026-09-25, and before that
93% = 3469 statements after M4.5 on 2026-09-09 — the statement count
nearly doubled while the percentage dropped one point, which is what "every new
feature ships with its tests" looks like). Real quality acceptance (run manually before release): `--profile api`
(DeepSeek generation + Qwen judge, stage C scoring); with the `local` profile the
judge is off (ADR-0014 ③) → protocol-layer metrics only (recall / citation
discipline / refusal), with the api profile standing in as the semantic-quality
reference.

---

## 7. Structured output A/B: JSON carriers vs text markers (2026-09-27)

**Why**: the opening line of `pipeline/prompts.py` — "plain text markers rather than
JSON structured output" — decides how the `[n]` protocol is **carried**, and had never
been measured: it was an assumption living in a comment (ADR-0037 turned it into an
off-by-default switch plus this comparison).

**Protocol and paths**: numbering is still **injection order** and the model still may
not invent markers; the two prompts differ by an enumerable amount (the JSON contract
plus a `chunk_id` on each source line), so the only variable is the serialization layer.
Sessions: `mikasa eval run --profile api [--config overlay] --name api-marker|api-json`
(same 63 questions, same sample-corpus, same bge-m3 embedding profile).

> **Prompt-version note (added 2026-09-30)**: the generation-side numbers in this
> section (and §8) were produced with the **old demo examples**, which contained concrete
> entities and values. On 2026-09-30 a real prompt leak forced those demos to become
> placeholders, with a rule stating explicitly that the prompt is not source material
> (ADR-0040). Stage A retrieval metrics are unaffected; **quote the out-of-range rate,
> citation gold and refusal counts below only as "old prompt" figures.**

### 7.1 api profile (DeepSeek, `json_object`, **not** constrained decoding) — done

| Metric | api-marker | api-json | Reading |
| --- | --- | --- | --- |
| Format failures | — (no such event on this path) | **0 / 63** | all 63 were valid JSON *and* consistent with the prose |
| Out-of-range / invented markers | 0.000 | 0.000 | neither group overstepped |
| citation gold ratio | 0.729 | 0.706 | paired delta −0.023, 95% CI [−0.085, +0.040] — **not significant** |
| Answerable questions refused | 0 | 0 | — |
| Unanswerable questions answered | 3 | **1** | refusal accuracy 13/16 → **15/16** (81.2% → 93.8%) |
| Stage C correctness (1-5) | 4.925 (n=40) | 4.974 (n=38) | slightly up; not conclusive at this sample size |
| Stage C faithfulness | 1.000 | 1.000 | — |
| Input / output tokens | 144,211 / 15,724 | 161,917 / 18,294 | **+12.3% / +16.3%** (the price of the contract and chunk_ids) |
| Total wall time | 296.8s | 288.7s | no real difference (network jitter dominates) |

**Conclusion (api profile)**: on DeepSeek the JSON carrier does **not** improve citation
discipline (both achieve zero out-of-range, zero malformed markers); what it buys is
**better refusal discipline** (unanswerable mistakes 3 → 1) and a slightly higher
semantic score, at a cost of **roughly 12% more tokens**. Note that this is **not**
constrained decoding — only "valid JSON" is guaranteed; schema compliance rests on the
prompt plus client-side validation. All 63 happened to pass, which is a property of this
model, not a general law.

### 7.2 local profile (Ollama `format: <schema>`, constrained decoding) — done (2026-09-28)

```bash
mikasa eval run --profile local --name local-marker                      # run #2
mikasa eval run --profile local --config build/structured-local.yaml --name local-json   # run #3
```

Sessions: run #2 (marker, 1010.7s) and run #3 (json, 843.1s), same 63 questions, same
sample-corpus, same embedding (bge-small-zh-v1.5). The local profile has the judge off
(ADR-0014 ③), so **everything below is protocol-layer only — there is no semantic score**;
that is this profile's built-in boundary and it can only be patched by manual sampling.

| Metric | local-marker (#2) | local-json (#3) | Reading |
| --- | --- | --- | --- |
| Format failures | — (no such event on this path) | **9 / 62** | the constraint guarantees valid JSON, yet **not one failure was syntactic** (see below) |
| Out-of-range / invented markers | 0.000 | 0.007 | paired +0.007, CI [0.000, +0.022] — not significant |
| citation gold ratio | 0.817 | 0.810 | paired −0.007, CI [−0.081, +0.063] — **not significant** (see rerun noise) |
| Answerable questions refused | 0 | 0 | — |
| Refusal accuracy | 11/16 = 68.8% | 11/16 = 68.8% | identical (different questions fail: q061 ↔ q059) |
| Unanswerable questions answered | 5 | 5 | of which cited: 4 → 5 |
| Input tokens | 211,944 | 230,994 | **+9.0%** (the price of the contract and chunk_ids) |
| Output tokens | 10,677 | 5,926 | **−44.5%** |
| Answer length (mean, answerable) | 324 chars | 130 chars | **−60%** |
| Sections / bullets per answer | 1.196 / 2.674 | 0.087 / 0.130 | **the presentation almost disappears** |
| Total wall time | 1010.7s | 843.1s | shorter output, naturally faster |

> **What the two "paired" columns mean**: the two means come from each session's **own** report,
> whose denominator is "answerable questions not refused and carrying citations" (n=46); the
> paired columns compare the **common items** of both sessions (n=45, 2000 bootstrap resamples,
> fixed seed — the same function `eval compare` uses). The denominators differ, so a mean
> difference and a paired difference are not exactly the same quantity: the per-session means
> drop each session's own lost question (see below).

**The nine format failures** (taken one by one from the run log): 8 × `chunk_id` mismatch
and 1 × "prose disagrees with the declaration". The mismatches are telling —
`marker=5 declared 55, actual 54`, `marker=6 declared 38, actual 39`,
`marker=7 declared 3118, actual 56`: the model wrote **a neighbouring chunk id, or a
multi-digit number it made up**. Constrained decoding constrains the *shape*, not the
*content*: the schema guarantees an integer, not that the integer is right. That is the
single most valuable sentence of this round — `format: <schema>` is easy to read as
"format is solved"; what it actually solves is syntax, while the semantic problem stays
exactly where it was, now harder to notice because it arrives inside a well-formed shell.

**Conclusion (local profile)**: on an 8B model the JSON carrier leaves **nothing but
cost** — output tokens −44.5%, answer length −60%, sections and bullets all but gone (the
model spends its budget filling fields and the answer gets squeezed into short
sentences), while refusal discipline and citation-gold hit rate **do not move** (every
difference sits inside the rerun noise). Read this next to §7.1: **the same change has
opposite signs on the two models** — on DeepSeek the JSON carrier improved refusals
(mistakes 3 → 1) for +12% tokens; on the 8B it is pure cost. The sentence at the top of
`pipeline/prompts.py` ("plain text markers rather than JSON, more stable on small models")
turns from a hunch into a **measurement** — it is right, and now we know what it costs.

**Rerun noise (read this before any delta above)**: the same configuration run twice
overnight (#1 → #2) gave a per-session citation gold of **0.850 → 0.817** (a −0.033 difference);
paired over the 45 common items it is **0.846 → 0.835** (Δ−0.011, CI [−0.041, +0.007]). The two
differ because **each session lost one question** to a timeout — not the same question
(q038 / q033) — so the denominators are not the same set of items. So on this profile a mean
difference **within ±0.04 is not an effect**, and the −0.007 between marker and json sits inside
that band. The **counts** in the report (9/62 format failures, 11/16 refusals) are steadier than
the means, which is why they are the ones worth quoting.

**Side finding (fixed 2026-09-29, see known-issues E5)**: the local profile's
`llm.timeout_seconds` defaults to 60s, while `max_tokens=4096` answers occasionally run
past it (one lost question per session). Both groups ran the same configuration and lost
one question each, and the pairing uses the shared questions, so the conclusions above
stand. **The fix**: `config/profiles/local.yaml` now sets `timeout_seconds: 180` — at this
profile's measured ~16.4 tokens/s that leaves room for ~3000 tokens, inside the 4096 cap;
and neither session's log shows VRAM eviction, which rules out the "stuck on memory
pressure" explanation (corroboration in `known-issues.md` E5). **This change moves the
profile's baseline**: local sessions no longer lose questions, so compare future runs
against the 60s-configured historical sessions (#1/#2/#3) with the denominator in mind.

### 7.3 The experiment's other output: a real bug

After the first api-json run, its log held **63 "query translation failed" lines**
(api-marker had none): the structured switch was attached to the provider config, so
**every non-streaming call** (query translation, the bilingual compare block, session
title extraction) was switched to JSON mode, silently killing the cross-language second
retrieval path for the whole session. The fix is "the config only decides whether answer
generation is structured; the provider only honours an explicit argument"
(`complete(..., structured=True)`), with a regression test — and **all four sessions were
re-run**, so the table above comes from one single code revision. This is evaluation
earning its keep: the **scope** of a feature switch is easier to get wrong than its
default value.

## 8. LangChain control group: the framework's default chain vs this repo's protocol layer (2026-09-29)

**The question**: retrieval can be swapped for a framework — but what about the **generation /
citation layer**? What does a framework's default RAG chain do for you, and what does it leave
out? This section is a measurement. The control directory is `experiments/langchain_baseline/`
(a **control, not a foundation**: it does not enter `src/`, does not enter the main CI, and
brings 63 packages / 253 MB of dependencies); full per-question data and re-run commands are in
that directory's README.

**Method (stated up front, or the numbers mean nothing)**: both arms receive the **verbatim same**
14 chunks (in the order the production injection window produced them); the framework side does
**not** retrieve for itself, and neither does ours — the only difference is the generation layer.
Same model (qwen3:8b), same temperature. The framework side is what you get from any
"LangChain RAG in 10 minutes" post: `RetrievalQA.from_chain_type(chain_type="stuff")` — the
default prompt (sources pasted as one block, **unnumbered**) plus `StrOutputParser`
(**no validation**). Our side runs the production `Generator`.

| Metric (63 questions: 47 answerable / 16 unanswerable) | LangChain default chain | This repo's protocol layer |
| --- | --- | --- |
| Answers containing `[n]` markers (answerable) | **0.0%** | 100% |
| Markers per answer | 0.00 | 3.49 |
| Out-of-range marker rate | 0.0% (empty set) | 0.0% |
| Refusal rate (the 16 unanswerable) | **0 / 16** | **10 / 16 = 62.5%** |
| Mean characters (answerable) | 340 | 302 |
| Sections / bullets per answer | 0.00 / 0.70 | 1.15 / 1.91 |
| Median seconds per question | 14.8s | 11.7s |
| Input / output tokens | 151,882 / 11,955 | 212,181 / 9,940 |

**How to read it**:

1. **The framework's "0% out-of-range" is a zero over an empty set.** Across all 63 questions it
   emitted `[n]` **not once** (checked per question; `any(markers)` is false) — with no numbering
   system there is nothing to be out of range. That cell cannot be read as "the framework cites
   properly too".
2. **Refusal 0/16 is the hardest line in this section.** On all 16 questions whose answer is not
   in the corpus, the framework produced a substantive answer — **every single one** (both the
   strict and the loose measure are 0): the default chain never refuses. That is where
   hallucination comes from, and it is the direct evidence for why the protocol layer had to be
   written by hand: it is not a formatting preference, it is **a gate against hallucination**.
3. **The price is +39.7% input tokens** (212,181 vs 151,882): the numbering, the citation rules,
   the refusal rules and the examples all live in the prompt, and that is what they cost. Median
   latency is actually 21% *faster* (refusals are short) and output tokens are −16.9%.
4. **Presentation is a product of the protocol layer too**: 1.15 vs 0.00 sections and 1.91 vs 0.70
   bullets — the production prompt specifies how an answer is organised; the framework's default
   prompt has nothing to say about it.

**The same comparison at the retrieval layer (conclusions; table in the control README §3)**:
over the 47 answerable questions, same corpus and same embeddings, recall@5/8/10 is **identical
at 0.982** (paired difference zero) and MRR is 0.924 vs 0.911 (Δ−0.012, CI [−0.035, +0.000], not
significant). Per-path diagnosis localises the difference to one place: the **dense path's top-20
agrees question by question, 47/47, while the BM25 path agrees 0/47** — the difference is entirely
the BM25 IDF formula (the framework uses `ln((N−df+0.5)/(df+0.5))` with a negative floor; this repo
uses the always-positive `ln(1 + …)`), not "the framework's vector search is worse". Retrieval
costs single-digit milliseconds on both sides (1.2ms vs 0.4ms), while embedding the same queries
costs 40.8ms — the measured basis for **don't swap an implementation for performance**.

**Where this does not generalise**: ① the framework column is its **default chain**; a different
prompt would add citations and refusals — which is exactly the point: **the framework gives you
the plumbing, the protocol is yours to write**, and writing it inside the framework is still
writing it. ② This is the local profile; the api profile (DeepSeek + bge-m3) has not been compared
the same way, since both cost and rate are the provider's to decide. ③ The 10/16 refusal here
differs slightly from the 11/16 of the §7 evaluation run because this control uses the **frozen
production injection window** (no cross-language second path), not the evaluation retrieval chain
— two entry points measuring the same thing.

## 9. MCP tool-calling evaluation: after the library becomes an agent's tool (2026-09-30)

**Why measure this**: MCP (ADR-0036) turns the knowledge base into three read-only tools, but
"the tools exist" is not "the tools get used correctly" — tool selection, call counts and whether
citations survive the round trip all need measuring (proposal §4-B). This section measures **the
model's side**; "is the protocol right" is covered by `tests/unit/mcp/` and by the real Claude
Code trajectory (2026-09-27).

**How**: `tools/eval_mcp_tools.py` — the script is itself an MCP client (it really spawns
`mikasa mcp` and speaks stdio JSON-RPC) wrapped around a minimal agent loop: the tool list comes
from `tools/list`, is converted to Ollama's native `tools` format, and **qwen3:8b decides which
tool to call** (a scripted policy would not measure tool selection at all). The 10 questions are
selected by rule from the golden set (6 answerable: 2 each easy/medium/hard; 4 unanswerable:
2 unrelated, 1 insufficient, 1 hallucination bait), the corpus is the isolated sample-corpus
build, `temperature=0`, `think=false`. Three arms, **the same 10 questions**:

1. **Baseline**: the default driver prompt ("whenever knowledge-base content is involved you must
   call a tool, do not answer from memory");
2. **Hardened**: `--strict-tools`, adding one sentence — "**even if you think you know the
   answer**, you must still fetch it from the knowledge base first";
3. **ask probe**: `--probe-ask`, no model involved — it calls `ask` once per question and checks
   the citations directly.

| Metric (10 questions) | Baseline | Hardened | ask probe |
| --- | --- | --- | --- |
| Total tool calls (agent side) | 8 (search 7 / read 1) | 11 (search 10 / read 1) | 10 (all `ask`) |
| **Questions that used `ask`** | **0 / 10** | **0 / 10** | 10 / 10 |
| Answered from memory (no tool call at all) | **2** (q048 weather, q049 latte) | 0 | — |
| Answerable but "searched only" (prose, no citations) | 6 / 6 | 6 / 6 | 0 (ask answered all) |
| **False "the knowledge base has nothing"** | 1 (q017) | 1 (q017) | **0** |
| Citation verifiability | nothing to verify | nothing to verify | **12/12 chunk_ids read back, 0 out of range** |
| Handling of unanswerable questions | 2 from memory + 2 honest "not found" | 4/4 honest "not found" | 4/4 `refused=true` |
| Median / total per question | 10.4s / 153s | 13.9s / 161s | 9.4s / 106s |

**Three conclusions**:

1. **The 8B only ever uses `search`**: of 20 agent-side calls, 19 were search, 1 was read and
   **0 were ask**. Compare the real Claude Code trajectory of 2026-09-27 (**six different query
   wordings** before it read, then asked, and refused honestly when the library had no support)
   — the gap between "tools available" and "tools used well" sits entirely on the model side,
   and its two ends are exactly this repo's two most valuable mechanisms: **query reformulation**
   (retrieval strategy) and **`ask`** (the citation contract). In this run not once was a query
   reworded: the query text was the whole question.
2. **Hardening the prompt only fixes half of it**: adding "even if you know it, fetch it first"
   took answering-from-memory from **2 to 0**; the `ask` usage rate stayed at **0/10** — "don't
   answer from memory" and "pick the right tool" are different problems. This is directly useful
   for what we tell users by default (now recorded in `usage-guide.md` §2c).
3. **The tools themselves hold up, and the probe proves it**: across the 10 ask calls, all 6
   answerable questions came back with citations, **all 12 chunk_ids were readable via `read`**,
   zero out-of-range markers, and all 4 unanswerable questions returned `refused=true`.
   **q017 is the clearest case**: the agent arm retrieved 5 hits (including the relevant chunk 23)
   and still answered "no related content in the knowledge base", while the probe arm's `ask` on
   the same question returned a cited answer (`refused=false`) — **same library, same model; pick
   the right tool and it answers, pick the wrong one and it reports "nothing found"**.

**Failure modes** (per-question verbatim records live in `build/mcp-tool-eval*.json`):
① answering from memory; ② prose answers with zero citations (search returns snippets, and a
self-written answer has no `[n]` markers); ③ **false negatives** (retrieved, yet answered
"nothing found"); ④ single-shot retrieval, no rephrasing or follow-up; ⑤ `ask` never triggered.
**Zero protocol-level errors**: every argument matched the schema, no unknown tools, no tool
errors, no timeouts.

**Boundaries**: ① 10 questions is a sample; ② the driver prompt is hand-written and the numbers
move if it is reworded (both prompt texts are stored in the JSON); ③ only the local profile —
an api profile (DeepSeek-class) is expected to do clearly better and was not measured; ④ the ask
probe measures "can the tool honour its citation contract", not "does an agent honour it".

**Reproducing** (three arms, one command with different flags; run `ollama ps` first — on an 8 GB
card another resident model makes qwen3:8b get evicted over and over):

```bash
MIKASA_DATA_DIR=build/eval-local/data python tools/eval_mcp_tools.py --profile local   # baseline
MIKASA_DATA_DIR=... python tools/eval_mcp_tools.py --profile local --strict-tools      # hardened
MIKASA_DATA_DIR=... python tools/eval_mcp_tools.py --profile local --probe-ask         # ask probe
```

## 10. Evidence self-assessment plus one supplementary retrieval: a minimal Self-RAG closure A/B (2026-09-30)

**The question**: retrieval is a one-way street — a gold chunk outside the first-round fusion
window is something the generation stage never sees (the hard tier's recall@10=0.931 is exactly
the part of those 12 questions that fell outside the window). Let the model self-assess the
**first-round hits** once — "is this enough to answer?" — and when it judges them insufficient,
re-retrieve once with **the new query it hands back**: can that fish the missed evidence back
in? This is checklist item 5 (the Agentic minimal slice of the tech-roadmap negotiation notes
§4), implemented as `retrieval.sufficiency_retry` (**off by default**, experimental overlay
`build/sufficiency-local.yaml`).

**How it was measured**: two full local sessions over all 63 questions, the same corpus /
question bank / model (qwen3:8b, temperature 0.1, think=false, num_ctx 16384), **differing in
this one switch only**:

```bash
MIKASA_DATA_DIR=build/eval-local/data python -m mikasa eval run --profile local --name suff-off
MIKASA_DATA_DIR=build/eval-local/data python -m mikasa eval run --profile local \
    --config build/sufficiency-local.yaml --name suff-on
python -m mikasa eval compare <off_id> <on_id> --profile local   # per-question pairing + bootstrap interval
```

**The closure's terms** (implementation in `pipeline/sufficiency.py`): the self-assessment is
fed only the 200-char snippet of the first 8 chunks, and the protocol is a **one-line either/or**
(`足够` "sufficient" / `不足：<新查询>` "insufficient: <new query>"; anything unparseable
counts as sufficient); judged insufficient → the new query (translated first on the
cross-language profile) retrieves once more → the new chunks, deduplicated, are **appended at
the tail of the hits, at most 3 of them** (numbers `[n]` follow injection order, the main hits
must keep their stable slots, the same as `hop_expand`); a self-assessment call that throws or
an empty query always falls back to the baseline; the mock profile never triggers it (ADR-0014 ③).

### 10.1 Results

| Metric (63 questions: 47 answerable / 16 unanswerable) | #4 baseline | #5 supplementary retrieval | Difference | 95% CI (difference) | Significant |
| --- | --- | --- | --- | --- | --- |
| recall@5 / @8 / @10 (paired n=47) | 0.982 | 0.982 | +0.000 | [+0.000, +0.000] | No |
| MRR (paired n=47) | 0.924 | 0.924 | +0.000 | [+0.000, +0.000] | No |
| citation gold ratio (paired n=50) | 0.759 | 0.788 | **+0.030** | [−0.030, +0.098] | **No (crosses 0)** |
| Out-of-range / invented-marker rate (paired n=50) | 0.000 | 0.000 | +0.000 | [+0.000, +0.000] | No |
| Refusal accuracy (16 unanswerable) | 11/16 | 11/16 | 0 | — | — |
| Answerable refused / unanswerable answered | 0 / 5 | 0 / 5 | 0 | — | — |
| **Not refused yet zero citations** | 0 | **2** | +2 | — | — |
| Input / output tokens | 215,385 / 11,024 | 233,595 / 9,989 | **+8.5% / −9.4%** | — | — |
| Total wall time | 743.0s | 805.7s | **+8.4%** | — | — |

**Trigger statistics** (read from the log line "补检索：追加 N 块" — "supplementary retrieval: N
chunks appended"): of the 63 questions **26 were judged "sufficient" and 37 "insufficient"**;
35 really did append chunks (30 appended the full 3, 5 appended 2), and the other 2 were judged
insufficient but had nothing new after dedup. The self-assessment call itself is cheap: 0.9s for
one pre-check question (qwen3:8b think=false), so the +62.7s of wall clock is basically the
price of those 63 self-assessments.

**Per-question changes** (paired analysis, `metrics_json.items`):

- **The 4 questions whose citation surface was widened**: q021 3→6, q033 1→2, q036 3→4,
  q037 2→3. q036/q037 are exactly the two hard synthesis questions already known historically
  to "lose their second gold chunk at fusion window @8" — supplementary retrieval really did
  push them above the threshold.
- **Gold-chunk hits**: 2 questions better (q003 0→1, q035 0→1), 2 worse (q041 3→2, q045 1→0).
- **The 2 questions with zero citations** (q038 4→0, q045 1→0): re-run and checked on both
  arms — q045's **baseline arm re-run also returns 0 citations** (that question already sat on
  the model's jitter boundary); the q038 supplementary-retrieval arm's answer is not badly
  organised, it simply never writes `[n]` anywhere. That is, "zero citations" is a symptom of
  generation jitter, not a systematic degradation caused by the closure; but it is a real
  regression in a discipline metric, listed as the report shows it, unwashed.
- The −9.4% output tokens is the sum of small shortenings on most questions; the two biggest
  single drops are exactly q041 (−546) and q038 (−313) — the questions that lost citations got
  written shorter.

### 10.2 Conclusion (how to read it)

1. **Not significant**: citation gold +0.030, CI crosses 0. At a sample size of 63 questions,
   "supplementary retrieval is better than not" is a sentence we **cannot utter**; what can be
   said is "the direction is positive, and the positive cases cluster on multi-gold-chunk hard
   questions".
2. **The cost is certain**: wall clock +8.4%, input tokens +8.5% — the self-assessment call
   **happens on every question** (63/63), while the payoff lands on a few. That is the inherent
   structure of this kind of "review, then retrieve" closure: the review fee is fixed, the
   payoff is long-tailed.
3. **The trigger rate is on the high side**: 59% judged "insufficient" (16 of those being
   unanswerable questions that "should be judged insufficient anyway"). Of the remaining 47
   answerable questions, 21 (45%) were judged insufficient too — i.e. its verdict on "is this
   enough" leans conservative. A 10-question probe compared a variant that feeds **whole chunks
   (the full text of the first 6)**: answerable triggers 3/7 → 2/7, unanswerable 3/3 unchanged —
   **truncation is not the main cause**, and changing how chunks are fed will not pull the
   trigger rate back to "the ideal level". (Pushing the trigger rate down means changing the
   decision mechanism itself; see the next item.)
4. **Off by default stands** (`retrieval.sufficiency_retry`), the same discipline as
   `hop_expand` / `structured_output`: **a capability with no significant gain does not enter
   the baseline**, or every historical number would have to be recalibrated.
5. **A valuable by-product**: this table is also a single measurement of "evidence-surface
   width → citation-surface width" — supplementary retrieval raises the citation counts on
   multi-gold-chunk questions (q021 3→6, q036/q037 +1), while refusal discipline does not move
   at all (11/16, the 5 wrong answers identical on both arms). If the "refuse only when the
   evidence is insufficient" close-out is ever built (wiring the self-assessment result into
   the refusal path rather than into the retrieval path), the trigger rate and verdict
   distribution here are ready-made raw material.

### 10.3 Boundaries (an honest list)

1. **A single run + temperature 0.1**: re-running the same question on both arms can give
   different citation counts (q045, measured), and this table cannot separate how much of the
   +0.030 difference is jitter.
2. **Only the local profile was measured.** The api profile (DeepSeek-class) was not — the
   verdict quality and the cost structure of self-assessment will both differ there.
3. **The zero difference at the stage-A retrieval layer is by design**: the closure hangs off
   the generation path (`ask._retrieve` and evaluation stage B), while stage A is the fixed
   measure of "the retriever itself" and does not pass through the closure. Do not read it as
   anything beyond "the closure did not change retrieval".
4. **Supplementary retrieval appends at most 3 chunks and never re-ranks**: that is the
   trade-off that keeps citation numbering stable (the same as `hop_expand`), not the only
   answer to "what supplementary retrieval should look like"; allowing a re-rank would raise
   the ceiling on the gain, at the price of numbering no longer being stable.
5. **MCP `search` deliberately does not take the closure**: it is a pure retrieval tool, and
   "a search box that thinks" is not what it is (see the comment in ask.py).

## 11. Claim-level faithfulness (self-built L2) plus a RAGAS comparison (2026-09-30)

**The two questions to answer** (evaluation checklist item 6): ① what does whole-answer
faithfulness (the judge gives one 0/1) fail to see, and what more does breaking the ruler
down to the **claim** level see? ② putting RAGAS's four dimensions — the evaluation world's
de facto standard — next to this repo's hand-written measure: how many of them line up, and
where they do not, what is the difference?

### 11.1 Claim-level L2: the blind spot of a whole-answer verdict (measured evidence)

On 2026-09-30 both rulers ran over the same 20 questions (sampling in §11.3):

| Measure | Result |
| --- | --- |
| Whole-answer faithfulness (judge 0/1, same batch of answers) | **20/20 all green** (faithful=True) |
| Claim-level check (self-built, same batch of answers) | 167 claims: **161 supported / 3 unsupported by the cited material / 3 uncited / 0 undecided** |

**The 3 claims a whole-answer verdict cannot see** (falling in 2 questions: two in q033, one
in q036) — that is the entire reason for breaking the ruler down this round: when one
out-of-bounds claim hides inside three correct claims, the whole-answer verdict still says
"faithful".

The points of the measure (details in ADR-0042):

- **The evidence face = the chunks that claim itself cites** (`[n]` → the n-th injected
  passage), **not** the whole retrieval context. This repo's definition of L2 is "does the
  **citation** really support the statement it is attached to" — validating against the
  whole-corpus context would judge "the citation was attached to the wrong chunk, but the
  content happens to be in another hit" as supported, and what that loses is exactly the
  line the citation protocol is there to hold.
- **Uncited claims (`uncited`)**: a factual statement with no `[n]` at the head of the line —
  it can be counted neither as supported nor as refuted, so it is disclosed separately ("how
  many factual statements in an answer carry no citation at all" is a discipline signal in
  itself).
- **Undecided (`undecided`)**: a claim whose verdict cannot be parsed out. The same
  discipline as the judge's "not measured ≠ not faithful".
- **Off by default** (`judge.claims`): two extra judge calls per question, switched on only
  in the sessions that need the finer ruler.

**A pitfall on record**: the first version of the parser accepted only `1: 是`, while Qwen
wrote the per-claim verdicts as an ordered list (`1. 是`) → 16 of the 20 questions had all
their claims recorded as "undecided" (it looked like the model had not answered, but in fact
the parser had not read it). The fix: take `.、)）。` into the separator set, and add a
regression test. **When a metric reads 0, suspect the reader first** — the same origin as the
2026-09-27 "blind spot in the malformed-marker statistics".

### 11.2 The mapping table with RAGAS (the one page that is "externally comparable")

| This repo's measure | RAGAS's measure | Relationship |
| --- | --- | --- |
| **claim support rate** (a claim → does **the chunk it cites** support it) | **faithfulness** (a claim → does **the whole retrieval context** support it) | the same ruler (claim level), **a different evidence face**: this repo accepts only the citation closure, which is stricter; the difference between the two is exactly the class "the citation was attached to the wrong chunk" |
| whole-answer faithfulness (judge 0/1) | — (RAGAS has no whole-answer granularity) | unique to this repo; claim level is precisely its patch |
| **citation gold ratio** (the share of cited chunks that hit the question's `gold_chunk_id`) | **context precision** (the signal-to-noise of the retrieval context against the reference answer) | chunk level against gold ↔ sentence/passage level against the reference; the same direction, **a different ruler**, the numbers cannot be compared directly |
| recall@k (is the gold chunk inside the top k) | **context recall** (can every key point of the reference answer be found in the context) | as above: is the chunk there ↔ is the key point there |
| judge correctness 1-5 | answer relevancy (embedding similarity of answer and question) | **a different axis**: this repo measures "is it right", RAGAS measures "is it on topic" — the one dimension of the four with no counterpart |
| refusal discipline (false refusals on answerable / wrong answers on unanswerable / L3 violations) | — (RAGAS does not measure refusals) | unique to this repo's protocol layer; this is also the same thing as the framework side's 0/16 refusals in the LangChain control |

### 11.3 The RAGAS comparison (2026-09-30, the same 20 questions, the same answers, the same contexts)

**Protocol**: api profile (DeepSeek generation), corpus `build/eval-api/data` (57 chunks),
**a stratified sample of 20 questions** from the 47 answerable ones (seed `20260930` written
into the code); the answers and contexts are exported once by the product chain
(`experiments/ragas_baseline/export_answers.py`, verbatim the same track as evaluation stage
B), and the RAGAS side only reads, never re-runs — both sides eat the same thing. The judge
is **the same** Qwen2.5-72B as the main repo (SiliconFlow), the embeddings bge-m3 from the
same vendor. The control directory is `experiments/ragas_baseline/`, the per-question table in
`out/result.md`.

| Measure | This repo | RAGAS (the same 20 questions) |
| --- | --- | --- |
| Claim-level faithfulness | claim support rate **0.982** (161/164 claims; 3 uncited, 0 undecided) | faithfulness **0.973** |
| Whole-answer faithfulness | judge **1.000** (20/20 all green) | — |
| Citation / context quality | citation gold ratio **0.724**, out-of-range **0** | context precision **0.987** |
| Recall | see evaluation stage A (whether this question set's gold chunks enter the window) | context recall **0.925** |
| Answer vs question | judge correctness **4.80**/5 | answer relevancy **0.422** |

**Three conclusions**:

1. **The two faithfulness rulers line up, and the difference is explainable.** 0.982 vs 0.973
   are two numbers on the same ruler: this repo's evidence face accepts only **the chunk that
   claim itself cites** (stricter), RAGAS uses **the whole retrieval context**. Question by
   question: q033 is flagged by both sides (2 unsupported on our side / RAGAS 0.875); on q036
   our side flags 1 while RAGAS gives 1.000 — **exactly the class "the citation was attached
   to the wrong chunk but the content is in another hit"**, which only the citation-closure
   measure can see; q046 is the reverse (RAGAS 0.667, 0 unsupported on our side), caused by a
   different sentence-splitting and claim-splitting granularity. **Neither ruler is wrong;
   they measure different "evidence faces".**
2. **answer relevancy is systematically pushed down on a Chinese corpus by language, and
   cannot be compared across languages.** 0.422 looks alarming, but a reproduction probe
   pinned the cause down: that RAGAS prompt and its examples are **all in English**, so Qwen
   wrote the "reverse question" of a Chinese answer in English — measured on q004: the
   bge-m3 cosine between the original question (Chinese) and the reverse question (English)
   is only **0.584**, i.e. what is measured is **cross-language similarity**, whereas the
   0.8~0.9 seen on English papers is monolingual similarity. This is not "the answer is off
   topic": on the same batch of answers the judge's correctness is 4.80/5 and citation gold
   0.724. **This number must be reported together with its language**, otherwise it is
   measuring two rulers of different units against each other.
3. **The gap between context precision 0.987 and citation gold 0.724 is a gap in
   granularity.** RAGAS measures "the signal-to-noise of the whole retrieval window against
   the reference answer" (most of the 14 chunks really are relevant), this repo measures "do
   the **cited** chunks hit the question's gold". One is wide, one is narrow, both numbers are
   true, and putting them in one table to compare sizes is meaningless — this is the measured
   version of the negotiation notes' prediction that "the granularity is not comparable".

**Boundaries**: ① a 20-question sample, a single run; ② api profile only (all four RAGAS
dimensions need an LLM judge, the local profile cannot run it); ③ versions pinned in
`requirements.txt` (ragas 0.3.1 + langchain 0.3.x; 0.4.3 with langchain-community 0.4.x is a
broken combination); ④ **a control environment** with 99 packages / 591 MB of dependencies —
it does not enter `src/`, does not enter the main CI, and the main repo keeps only this
section.

**Re-running**:

```bash
MIKASA_DATA_DIR=build/eval-api/data .venv/Scripts/python.exe \
    experiments/ragas_baseline/export_answers.py --out experiments/ragas_baseline/out
experiments/ragas_baseline/.venv/Scripts/python.exe \
    experiments/ragas_baseline/run_ragas.py --out experiments/ragas_baseline/out
.venv/Scripts/python.exe experiments/ragas_baseline/compare.py
```

**Two gotchas (details in the control directory's README §5)**: ① a joint resolution like
`pip install ragas langchain-openai` backtracks for over half an hour on langchain-core's
version range — **install ragas first, then the rest**; ② RAGAS's default concurrency drives
SiliconFlow into 429, and ragas silently records the failures as NaN (in the first round 7~14
of the 20 questions had a whole metric as NaN, and the mean was computed only over the
surviving samples) — **cap the concurrency + retry, and write the failure counts into the
result**.

## 12. Retrieval definitions and candidate depth: two sets of numbers, one free A/B (2026-09-30)

**Why this section exists**: on 2026-09-30 a 21-question set was generated from the user's
**real corpus** (*Working performance of helical anchors under cyclic loading*, 911 chunks /
294k characters / 189 pages, mostly English body text) using the ADR-0026 auto-synth, and the
scores came out far below the sample corpus — and most of that gap turned out to be a
**definition** difference, not a capability difference.

### 12.1 Stage A is single-path, the product is dual-path: 0.500 vs 0.94 on the same questions

| Definition | Method | top-10 hits on that corpus |
| --- | --- | --- |
| Stage A (the reported recall@10) | **the original question only**, rerank off, window anchored at 10 | **8/16 = 0.500** |
| The product QA path | the original question **plus a Chinese→English second query**, RRF-fused (`retrieval.crosslingual`) | **15/16 = 0.94** |

**Mechanism**: the question is Chinese while the body text is English. Single-path, the gold
chunk lands at BM25 rank 39–546 and vector rank 43–764 (a Chinese embedding model matches
English prose poorly; BM25 only hits by accident on digits and symbols). **With the English
second query, 7 questions come straight back into the top-10**, leaving only one (a013)
unrecovered.

**Consequence (now written into the report)**: stage A deliberately makes no LLM calls (so it
stays reproducible) and therefore **systematically understates** product recall on
cross-lingual corpora. Stage B is the product's real chain. The evaluation report now says so
above the stage A table.

### 12.2 Candidate depth 20 → 60: no regression on the sample corpus, +2 questions on the real one

**Motivation**: fusion only sees **each path's top-20** (`bm25_top_k` / `dense_top_k`). If the
gold sits outside that, no window and no reranker can bring it back — and on the real corpus
7 of 16 golds were beyond rank 20 (two of them right at the edge: 24 and 30).

**Method**: mirror stage A's retrieval (rerank off, window anchored at
`RETRIEVAL_FUSION_TOP_K`), change **only the two depths from 20 to 60**, run both question
sets, and pair the per-question deltas with a bootstrap (no LLM calls; tens of seconds).

| Question set | recall@5 | recall@10 | MRR | nDCG@10 | Per question |
| --- | --- | --- | --- | --- | --- |
| Sample corpus (47 q, regression) | 0.982 → 0.982 | 0.982 → 0.982 | 0.924 → 0.924 | 0.933 → 0.933 | 0 recovered / 0 lost |
| Real corpus (16 q) | 0.500 → 0.562 | **0.500 → 0.625** | 0.406 → 0.432 | 0.431 → 0.480 | recovered **a007 / a013** / 0 lost |

Paired interval on the real corpus: recall@10 **Δ+0.125, CI [0.000, +0.312]** — by the
"interval must not cross 0" rule this is **not significant** (the lower bound sits exactly at
0, n=16 is small), but the direction is consistent, there is **zero regression**, and the cost
is one config line. The sample-corpus delta is exactly 0 (its candidate pool was already wide
enough), which is the evidence that this step does not damage the profile that already passes.

**Not done**: applying the line to the profile (that moves the baseline, so by our own rules it
needs accounting plus a full regression), and depths beyond 60 — the depth scan plateaus at
14/16 total pool coverage, and the remaining 2 questions are a **semantic gap** (Chinese
question, English body, no digits to latch onto) that only query rewriting or a stronger
embedder can close.

**Boundary**: the real question set is **auto-generated** (some question texts are statements,
and it is single-gold), so its absolute numbers indicate direction only; `citation gold` is
naturally low when one gold chunk must be found among 911.
