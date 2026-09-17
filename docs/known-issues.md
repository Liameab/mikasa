# Known Issues and Backlog

> **Purpose**: one place to track what is unfinished or deliberately postponed, with its current
> status. Evidence and reasoning are **not** repeated here — see
> [evaluation.md](evaluation.md) §5 for the full evidence chain and ablations,
> [limitations-and-failures.md](limitations-and-failures.md) for recorded boundaries and failure
> cases, and [design-decisions.md](design-decisions.md) for the "why" behind each trade-off. This
> file answers three questions only: **what is missing, where the evidence is, and what the status
> is**.
>
> **Maintenance discipline**: update the status and date when something is fixed or lands, and
> never delete a row — keep the trace of "this was once unresolved" (for example "closed after
> Mx"). If a status here contradicts the code, that is document drift: fix one or the other, never
> leave them disagreeing.

---

## 1. Unfixed quality gaps (investigated; ruled as local-profile boundaries)

> All ruled on 2026-09-09 (M4.1 investigation, see evaluation.md §5). Conclusion: all three are
> **capability boundaries** of the model or retriever, with no low-cost engineering fix. Re-check
> entry point after any relevant change: `mikasa eval run --profile local` (63 questions, ~30
> minutes).

| # | Issue | Measured | Evidence | Fixes considered | Status |
| --- | --- | --- | --- | --- | --- |
| 1 | Retrieval gap on hard questions in the local profile (512-d bge-small vs 1024-d bge-m3 on api) | recall@10 = 0.982 overall, **0.931 across the 12 hard questions**; the misses are exactly q036's second gold chunk and q041's third (fused rank 14–24) | evaluation.md §5 "M4.1 investigation": ablating over 3 windows gives **zero change** in recall@10 | Tuning fusion and sub-window parameters (empirically ineffective); switching to a larger qwen/bge model | **Accepted as a boundary** (2026-09-09) |
| 2 | Refusal discipline breaks in the local profile (qwen3:8b is less compliant than DeepSeek — the two are mirror images of each other) | 13/16 refused; q057 answered with fabricated citations, q058 answered bare, q059 hallucinated (a deliberately planted sentinel question that catches the small model) | evaluation.md §5 plus per-item traces; prompt rule 3 and its worked example already cover these cases, and DeepSeek refuses 16/16 under the same rules | Switching to qwen3:14b (~9 GB more to download, tight on 8 GB VRAM, markedly slower, **no guarantee of zero failures**); tightening the prompt (touches a shared protocol, needs a full replay, low marginal gain) | **Accepted as a boundary** (2026-09-09) |
| 3 | Citation precision is lower in the local profile | citation gold ratio 0.798 vs 0.848 on api (p50 is 1.000 for both; a few questions partially miss) | evaluation.md §5 | Same as #2 — a generation-capability issue | **Accepted as a boundary** (2026-09-09) |

## 2. Feature backlog (wanted, not scheduled)

> All of these are already recorded in ADRs, project notes, or milestone plans; this table keeps
> only the pointer and the status. Scheduling rule: pick them up one at a time, as time allows,
> after M4.5 / M5 (git + GitHub + CI) wraps up.

| # | Candidate | Description | Origin / motivation | Status |
| --- | --- | --- | --- | --- |
| F1 | Automatic hybrid mode | When KB evidence is insufficient, fall back to free chat and say so, instead of only refusing | Reported study workflow (2026-09-09); ADR-0013 context | Unscheduled |
| F2 | One-click ingest of a free-chat answer | Turn a good free-chat answer into a stored note | The loop the product is aiming at (2026-09-09) | Unscheduled |
| F3 | M6: knowledge base → notes | Four stages: **(1) write a note and have it searchable immediately — shipped 2026-09-16, ADR-0021** → (2) photo upload converted to note text → (3) LLM-generated knowledge links (mind the schema-migration discipline) → (4) knowledge map | Project notes | Stage 1 done; stages 2-4 unscheduled |
| F4 | Query rewriting for multi-turn chat | Retrieval quality for follow-up questions that use pronouns or elide context; the deliberate omission and its v2 candidate are documented in limitations §3 | limitations-and-failures.md | Unscheduled |
| F5 | Local reranker (local profile) | Enabling rerank requires a fastembed version survey first | ADR-0014 ②; the LocalReranker case in limitations §4 | Unscheduled |
| F6 | Session-management follow-ups | Drag-and-drop moves, titles that keep updating as a thread grows, search / archive / pin, persisted expand state | The "explicitly out of scope" list from the M4.5 plan | Unscheduled |
| F7 | Mobile (in the spirit of the DeepSeek / Doubao apps) | Explicitly "not polished, can wait". Two tiers: (1) same-Wi-Fi LAN access, which already works (`serve --host 0.0.0.0` plus a firewall rule, then open it in a phone browser) → (2) away from home: cloud server plus domain (paid, standalone milestone, domain and HTTPS solved together) | The essence is "which machine runs the service the phone can reach" — a web UI needs no app | Unscheduled |

## 3. Engineering candidates (re-runs and hardening)

| # | Candidate | Description | Status |
| --- | --- | --- | --- |
| E1 | Local semantic-quality comparison | The judge is disabled in the local profile (ADR-0014 ③), so semantic quality has no judge confirmation — re-checking needs a local judge or a side-by-side comparison against the api profile | Unscheduled |
| E2 | Eval wall-clock cost | A full 63-question local run takes ~25–35 minutes (4–5 on api) — CI only runs the offline protocol layer | Accepted; recorded in evaluation.md §5 |
| E3 | Mock-LLM boundary | Offline runs carry no semantics (protocol self-check only); 7/16 unanswerable questions mis-answered is evidence of the mock's limits | Accepted; recorded in evaluation.md §5 and limitations §3 |
| E4 | Coverage baseline guard | The baseline was 91% — new M4.5 modules need a fresh measurement and an update in evaluation.md | Updated: 93% (M4.5 wrap-up, 2026-09-09; see evaluation.md) |

## 4. Explicitly not doing (so it does not get re-litigated)

- **No** session drag-and-drop (menu-based moves are the decided interaction); **no** cascading
  folder deletion (the non-empty → 409 semantics is settled);
- **No** drag-and-drop, continuous title updates, session search / archive / pin, or persisted
  expand state (outside the M4.5 scope — see F6);
- **No new dependencies** (no alembic, no frontend framework, no JS library — migrations are
  hand-written, per the discipline of the ADR-0004 revision);
- The single-process web shape is a deliberate design, not a defect (limitations §3), and is not
  tracked as backlog;
- Unit tests never call external services, and `doctor` never triggers a model download — see
  evaluation.md for the CI and testing discipline.

---

*Created 2026-09-09 (during M4.5, as a single tracking file). Maintained as milestones land.*
