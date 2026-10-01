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
>
> **Re-run on 2026-09-28** (the marker arm of the structured-output A/B, current configuration of
> this profile): recall@10 = 0.982, refusals 11/16, citation gold 0.817, one question lost per
> session (see §3 E5). Same order of magnitude as the 09-09 numbers below, but **not equal** —
> the methodology moved in between (fusion window 10 → 14, explicit `num_ctx`, `think` off,
> cross-lingual second path on, timeout jitter). Compare directions, not decimal places.

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
| F3 | M6: knowledge base → notes | Four stages: **(1) write a note and have it searchable immediately — shipped 2026-09-16, ADR-0021** → **(2) photo upload converted to note text — shipped 2026-09-20, ADR-0027** → (3) knowledge links (mind the schema-migration discipline) → (4) knowledge map | Project notes | Stages 1 and 2 have landed. **Stage 3's foundation went in on 2026-09-22** (the `doc_links` table at schema v5 + a repo layer + migration tests), **and the deterministic half followed on 2026-09-25**: `[[title]]` wiki links in notes become edges, plus an **off-by-default** one-hop expansion on the retrieval side (ADR-0035) — **the table is no longer empty**. Still missing: the **LLM extraction** (an LLM reading document pairs and judging relations — the user archived it as unscheduled on 2026-09-23) and the **relation UI** (showing a note's outgoing and incoming edges; `links_for_document` already provides the read path); stage 4 unscheduled |
| F4 | Query rewriting for multi-turn chat | Retrieval quality for follow-up questions that use pronouns or elide context; the deliberate omission and its v2 candidate are documented in limitations §3 | limitations-and-failures.md | Unscheduled |
| F5 | Local reranker (local profile) | Enabling rerank requires a fastembed version survey first | ADR-0014 ②; the LocalReranker case in limitations §4 | Unscheduled |
| F6 | Session-management follow-ups | Drag-and-drop moves, titles that keep updating as a thread grows, search / archive / pin, persisted expand state | The "explicitly out of scope" list from the M4.5 plan | Unscheduled |
| F7 | Mobile (in the spirit of the DeepSeek / Doubao apps) | Explicitly "not polished, can wait". Two tiers: (1) same-Wi-Fi LAN access, which already works (`serve --host 0.0.0.0` plus a firewall rule, then open it in a phone browser) → (2) away from home: cloud server plus domain (paid, standalone milestone, domain and HTTPS solved together) | The essence is "which machine runs the service the phone can reach" — a web UI needs no app | Unscheduled |
| F8 | Two follow-ups for MCP | (1) **HTTP transport** (it would have to take on ADR-0033's password gate and the session semantics: who is asking, whose session, how the password travels — ADR-0036 already states it is "a precondition, not an option") → (2) **tool-call evaluation** (roadmap §4-B: tool-selection accuracy, number of calls per answered question, whether citations survive a round trip, failure-mode taxonomy). The 2026-09-27 headless run already produced the first trace: six rephrasings of `search` found nothing, so `ask` refused honestly (refused=true, empty citations) | ADR-0036; the schedule is in `tech-roadmap-reply.md` §5 (MCP is item 1, now done) | Unscheduled |

## 3. Engineering candidates (re-runs and hardening)

| # | Candidate | Description | Status |
| --- | --- | --- | --- |
| E1 | Local semantic-quality comparison | The judge is disabled in the local profile (ADR-0014 ③), so semantic quality has no judge confirmation — re-checking needs a local judge or a side-by-side comparison against the api profile | Unscheduled |
| E2 | Eval wall-clock cost | A full 63-question local run takes ~25–35 minutes (4–5 on api) — CI only runs the offline protocol layer | Accepted; recorded in evaluation.md §5 |
| E3 | Mock-LLM boundary | Offline runs carry no semantics (protocol self-check only); 7/16 unanswerable questions mis-answered is evidence of the mock's limits | Accepted; recorded in evaluation.md §5 and limitations §3 |
| E4 | Coverage baseline guard | The baseline was 91% — new M4.5 modules need a fresh measurement and an update in evaluation.md | Updated: **92%** (re-measured on 2026-09-30: 8594 statements / 694 missed, 1136 passed + 2 skipped; the previous 92% was 8578 statements / 1126 passed in the 2026-09-27 MCP round — see evaluation.md) |
| E5 | The local profile's 60s timeout loses questions — **fixed 2026-09-29** | `llm.timeout_seconds` defaults to 60s, while a `max_tokens=4096` answer on this profile occasionally runs past it: **one question lost per evaluation session** (2026-09-28, two sessions: q033 / q032 — not the same question, so it is jitter rather than a hard question). The lost item is counted as a "generation chain failure" and no answer is obtained at all (not a short answer — zero). **Corroborated 2026-09-29**: the Ollama log shows ~16.4 tokens/s on this profile, so 60s ≈ 1000 tokens — inside the 4096 cap, consistent with this story; and neither session's log shows any VRAM eviction, which rules out "stuck on memory pressure" | **Fixed 2026-09-29**: `config/profiles/local.yaml` now sets `timeout_seconds: 180` (~3000 tokens of headroom). That is a product-config change and it moves the evaluation baseline — recorded in evaluation.md §7.2 (local sessions no longer lose questions; compare against the 60s-configured historical sessions with the denominator in mind) |

| E6 | A **runtime** citation-support check (the residual face of the prompt leak) | The numbering check only looks for out-of-range `[n]`, and L2 support runs **only in the evaluation layer** (`eval/claims.py` implements the claim-level version) — nothing at runtime asks "does this sentence come from that passage". The 2026-09-30 prompt leak escaped through exactly that gap; the bait is gone (ADR-0040) but that is *prevention* only | Backlog |
| E7 | Page-image render DPI does not match the pane width | The backend renders at 110 DPI by default (≈910 px wide for A4) while the reading pane is ~1500 px at a 1920-wide window — at 100% the page fills only 60% of the pane, and zooming just stretches those 910 px. The endpoint already accepts a `dpi` parameter (clamped 60–240); what is missing is the front end asking for a pane-matched DPI plus a bandwidth/cache cost assessment | Backlog (found 2026-09-30 while reworking the reading area) |

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
