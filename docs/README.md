# Documentation

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
| [Design system](../DESIGN.md) | The web UI's tokens (CSS variables), type scale, radius grammar, z-index ladder, states and component contract — kept honest by a guardrail test |

## Chinese originals

The Chinese versions live in [`zh-CN/`](zh-CN/) — Chinese is the working language of this project,
and those files are the source of truth when wording matters.
