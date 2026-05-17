# Implementation State Machine

This file is the source of truth for project status. Updated at every phase boundary.

## Phase ladder

| # | Phase | Status | Artifact(s) | Notes |
|---|---|---|---|---|
| -1 | Clarification gate | ✅ DONE | (AskUserQuestion answers in chat) | 4 questions resolved 2026-05-17. |
| 0 | Right-thing audit + complexity calibration | ✅ DONE | SPEC.md § Step 0 | Three one-sentence answers. |
| 1 | Intake and contract | ✅ DONE | SPEC.md | Goals, scope, A1–A10 criteria. |
| 2 | Research | 🔵 IN PROGRESS | RESEARCH.md | Conductor schema known; SQLite FTS5 to verify. |
| 3 | Task graph | 🔵 IN PROGRESS | PLAN.md | Nodes + dependencies. |
| 3.5 | CTO sanity check | ⏸️ SKIP (trivial project) | — | Single-developer Mac utility; no team coordination. |
| 4 | Architecture decision | ⬜ TODO | ARCHITECTURE.md | Python stdlib server + vanilla JS front. |
| 4.5 | Prompt-architect | ⬜ TODO | prompts/P01_*.md … | One prompt per impl node. |
| 5 | Implementation | ⬜ TODO | src/ | Subagent reads prompts, builds slices. |
| 5.8 | Integration gate | ⬜ TODO | STATE.md update | WIRED vs ISLAND classification per new module. |
| 5.9 | Retrospective + plan amendment | ⬜ TODO | AMENDMENTS.md | Drift, knowledge, downstream amendments. |
| 6 | Verification | ⬜ TODO | tests/ + verification.md | Live tests + user journey. |
| 7 | Review | ⬜ TODO | review.md | Adversarial review pre-merge. |
| 8 | Audit | ⬜ TODO | audit.md | Stranded code / quality sweeps. |
| 9 | Repair | ⬜ TODO | — | Fix anything found in 7/8. |
| 10 | Release gate | ⬜ TODO | — | Open PR; CI green; merge to main. |
| 11 | Summary | ⬜ TODO | RELEASE_NOTES.md | What shipped, how to use. |

## Sub-project decision

This is a small, coherent project — **NOT split into sub-projects**. One bounded scope: search GUI + reuse-existing-export. Splitting would add coordination overhead with no benefit. Re-evaluate if scope grows.

## Currently active phase

**Phase 2 — Research** (parallel with Phase 3 task-graph draft). After both committed and pushed, pause for operator review before Phase 4.

## Phase 5 implementation node status

| Node ID | Description | Status | Owner |
|---|---|---|---|
| P01 | Server: list sessions (db.py) | ✅ DONE | (impl subagent) |
| P02 | Server: search messages (search.py) | ✅ DONE | (impl subagent) |
| P03 | Server: export endpoint (export.py) | ✅ DONE | (impl subagent) |
| P04 | Frontend: single-page HTML | ✅ DONE | (impl subagent) |
| P05 | Launch script + browser opener | ⬜ NOT STARTED | (impl subagent) |
| P06 | Shared extraction module (render.py) | ✅ DONE | (impl subagent) |
| P07 | Tests + verification | ⬜ NOT STARTED | (verifier subagent) |
