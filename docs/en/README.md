# Documentation

> *[中文说明见 ../README.md](../README.md) —— 本仓库以中文为主，这一页是英文版
> (Chinese is the primary language of this repository; this is the English edition).*

Technical documentation for Mikasa — written for people who want to understand *why* the system
is built the way it is, not just *what* it does. Each document is expected to be honest about
trade-offs, boundaries, and mistakes.

| Document | Contents |
| --- | --- |
| [Architecture](architecture.md) | How the system is organized, how data flows, and the three-layer anti-hallucination design |
| [Design decisions (ADR)](design-decisions.md) | Every "why": choices, trade-offs, and decisions later overturned by data |
| [Evaluation](evaluation.md) | Three-stage methodology, golden-set mismatch guards, judge-bias correction, acceptance baseline |
| [Limitations & failures](limitations-and-failures.md) | Ecosystem pitfalls, heuristic boundaries, and a postmortem archive of real bugs |
| [Usage guide](usage-guide.md) | End-to-end workflows: paper reading, follow-up questions, and what to do when it refuses |
| [Known issues](known-issues.md) | Unfixed issues (with rulings) and unscheduled candidates |
| [Design system](../../DESIGN.en.md) | The web UI's tokens (CSS variables), type scale, radius grammar, z-index ladder, states and component contract — kept honest by a guardrail test |

## Chinese originals

The Chinese versions live in [`../`](../) — Chinese is the working language of this project, and
those files are the source of truth when wording matters. The repository README is Chinese-first as
well ([`README.md`](../../README.md), with this tree's English edition at
[`README.en.md`](../../README.en.md)). The two trees correspond file by file, and
`tests/unit/web/test_docs_trees.py` fails if a document or a relative link goes missing.
Four documents are private: not shipped, no English mirror, and excluded from the correspondence
and link checks in `tests/unit/web/test_docs_trees.py` — `ideas-and-backlog.md` (the idea ledger),
`codex-handover.md` (internal hand-off notes), and `interview-handbook.md` / `advisor-readme.md`
(graduate-admissions interview preparation material).
