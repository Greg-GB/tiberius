# Drift tracking (`tiberius drift` / `memory_drift`)

Status: **planned** (2026-10-30) — Phase 0 evidence-gated dogfood pending; design below.
Work in progress on local branch `feature/drift` (not pushed).
Companion to [brief.md](brief.md): the drift state side *is* the `--since`
state-brief variant that brief's open question #3 parked.

## 1. Why

Projects drift: what actually happens diverges from the purpose they were
started with, and the divergence is invisible until someone lines the two up.
The store already holds both sides — plan facts (purpose, invariants,
non-goals) and the state facts that accumulate session after session (what
shipped, what was decided). What is missing is the composition surface:
**ask "is this project drifting?" and get both sides in one budgeted pack.**

Tiberius itself is the first project tracked (dogfood): its AGENTS.md
invariants are already written as explicit, checkable claims — a ready-made
plan-fact corpus.

The judgment ("does this state contradict this intent?") is semantic and
belongs to the agent that reads the pack — never to the core. Tiberius
recalls and assembles; it never scores drift.

## 2. What it is

One read-only call that returns a two-sided, budgeted comparison pack for a
project:

```
tiberius drift <project> [--since S] [--budget 300] [--top 20]
```

```
drift: Tiberius (anchor 2026-10-27; 5 of 6 plan, 9 of 14 state, ~280 tokens)
plan:
- Tiberius invariant: the core (store/retrieval/holographic) imports no agent framework. (×4)
- Tiberius purpose: local-first personal memory engine; no network, no telemetry. (×7)
- Tiberius invariant: core changes must not regress the golden eval (19/19 @1 and @3). (×2)
state:
- Tiberius v0.4.0 released 2026-10-29: the brief feature (CLI + OpenCode tool + provider action).
- Tiberius workflow: feature branches, PRs via gh, user reviews and merges.
unbacked: 1 plan fact has no state evidence since the anchor (note: …)
```

Header rule: the "window, not the whole" convention from brief.md and the
prefetch counter — `k of N` per side, so an unmarked block never reads as the
whole picture.

The CLI wire format is JSON (like every other subcommand); the block above is
the tool/renderer shape (Phase 2).

Properties:

- **Read-only.** No writes beyond retrieval recording (a drift IS a recall —
  three underlying ones, see §3.2).
- **Budgeted.** Char/4 token estimate, whole facts only — a fact that does not
  fit is dropped whole, same rule as brief.
- **Corroboration markers.** Per plan fact: the count of state facts sharing
  ≥1 entity — structural (the `fact_entities` join), not semantic.
- **`unbacked` supplemental field.** Plan facts with zero state corroboration
  since the anchor — the actionable output ("is this claim stale, or unbuilt?").
  Ships per the supplemental-output three-piece set (ARCHITECTURE.md):
  omitted when empty + a one-line `note` + a line in the tool schema
  description + a line in `skills/tiberius-usage`.
- **Window totals.** `plan_total` / `state_total` — the pre-fill pool sizes.
- **Deterministic.** Same store + parameters → same output; unit-testable with
  synthetic fictional fixtures (the eval-fixture rule).

## 3. Design

### 3.1 Pipeline (all above the core trio)

```
project
  → probe(project) → tag 'plan' filter          # plan side
  → search(project) ∪ probe(project) ∪ related(project)   # state side, deduped
  → state post-filter: created_at >= anchor     # in drift.py (see below)
  → state minus plan facts
  → corroboration: per plan fact, count of state facts sharing ≥1 entity
    (one SQL join over fact_entities, under MemoryStore._lock)
  → rank (brief's _rank) and budget fill: plan side first, state fills the remainder
  → unbacked list + window totals
```

The core trio is untouched (invariants 1–3): composition stays in the SQL
entity graph (probe/related), HRR only ranks within the composed set, and
there is no new ranking surface.

Correction from the original sketch: `retrieval.search()` has **no time
dimension** — `--since` exists only on `list`/`list_facts`. The state side is
therefore raw recalls post-filtered on `created_at` in `drift.py` (fact dicts
already carry it). The entire feature is public methods + Python filtering +
one locked SQL join for the markers: zero changes to store/retrieval/
holographic, so the golden eval cannot move.

### 3.2 Semantics decisions

- **Anchor.** Default: the oldest plan fact's `created_at` — stable, because
  `update` never moves `created_at` (only the write path sets it). `--since`
  override reuses `store._parse_since` (ISO or relative 30m/2h/7d/2w).
- **Plan side first.** The claims being checked must not be cut: plan facts
  consume the budget before state (tiered fill, the brief rationale — the two
  sides have different scales and never interleave).
- **`related()` on the state side.** Catches state that never names the
  project (an indirectly linked feature). Phase 0 decides whether it is signal
  or neighborhood noise; it is cut if noise.
- **Recall counting.** Three underlying recalls (search/probe/related); a fact
  in several sets counts per recall — the same overlap ponytail brief
  documents.
- **The verdict is not stored by `drift`.** Recording a drift verdict is a
  separate explicit `add` by the agent (tag `drift`) — which is also the
  Phase 3 anchor hook.
- **Tags, not a category.** Plan facts carry tag `plan` (state facts `state`)
  with category `project`. Tags are first-class, FTS-indexed, and need no
  surface changes; "plan" is on `_ENTITY_STOPWORDS`, so no junk `Plan`
  entity forms.

## 4. Surface

- **CLI first**: `tiberius drift …` in `cli.py` — testable anywhere, usable by
  any harness, and the shape other adapters wrap: they import the shared
  `drift()` (`drift.py`), not a subprocess. JSON on stdout like every other
  subcommand.
- **OpenCode**: `memory_drift(project, since?, budget?)` tool — thin wrapper,
  renders the two-sided block, session dedupe like `memory_brief`. (Unlike
  `contradict` — a store-hygiene scan with no natural agent call moment — a
  drift check is a read-time question an agent actually asks.)
- **Hermes**: `fact_store(action='drift')` on the provider — the same
  `drift()` in-process (no subprocess).

## 5. Open questions

| # | Question | Blocks |
|---|----------|--------|
| 1 | Keep `related()` on the state side — signal or neighborhood noise? | Phase 0 |
| 2 | Anchor default: oldest plan fact (v1) vs newest stored `drift` verdict (Phase 3) | Phase 3 |
| 3 | Include `created_at` in the state-fact shape? (recency is meaningful on the state side; brief's shape omits it) | Phase 1 |
| 4 | Default budget: 300 (brief's) vs 400 — two sides share one budget | Phase 0 |

## 6. Phase 0 spike (pending)

Dogfood on Tiberius itself:

1. **Seed plan facts** from the AGENTS.md invariants + README purpose. Format
   follows the SKILL.md fact rules: the claim, not the story; one small fact
   per claim; identifiers verbatim. Category `project`, tag `plan`.
2. **State facts mostly already exist** — the live store accumulates Tiberius
   project facts every session. Verify via
   `list --since <anchor> --category project`; patch only genuine gaps,
   tagged `state`.
3. **Hand-assemble the pack** (`probe Tiberius` plan side + `list --since`
   state side; trial `related Tiberius`), the agent judges drift from the two
   sides.
4. **Record here**: recall quality, tag-filter behavior, anchor default,
   related-side verdict, budget behavior, GO/NO-GO for Phase 1.

**Exit criterion:** if plain `probe` + `list --since` is already sufficient,
Phase 1 shrinks to a thin wrapper — the command's value is one call + anchor +
markers, not new retrieval.

## 7. Phases

| # | Work | Gate |
|---|------|------|
| 0 | Spike per §6; findings recorded in §6 | GO/NO-GO judgment |
| 1 | `drift.py` + CLI subcommand + `tests/test_tiberius_drift.py` (fictional fixtures, through `cli.main()`, numpy `importorskip` where the structural probe path matters) + README command table | Core suite green; golden eval unchanged (no core-trio change — expected 19/19 @1 and @3) |
| 2 | OpenCode `memory_drift` + session dedupe + plugin tests; Hermes `fact_store(action='drift')` + system-prompt line; tiberius-usage skill line; README tool count; version bump 0.4.0 → 0.5.0 in all version spots (ARCHITECTURE.md pitfall #5 — v0.3.2 missed the lockfile) | Plugin suite + typecheck; release gates before the push |
| 3 | (Optional, later) store drift verdicts as facts → default anchor = newest verdict (drift-over-time for free); `contradict` plan-vs-state tension as a hint, not a verdict | Evidence-gated on actual use |

**Non-goals:** no drift scoring/judgment in the core; no HRR similarity as a
drift signal; no versioned plan *documents* — facts only.

## 8. Concerns / notes

- **Plan-fact quality is the ceiling** (brief.md §6 finding 4, generalized):
  vague plan claims ("be fast") make drift detection noise. The
  invariant-style claim format is the mitigation, and AGENTS.md is the seed
  corpus that proves the format works.
- **Scope-creep irony.** Tiberius's purpose is memory, not project tracking.
  The drift command is recall-and-assembly only — the moment the core starts
  scoring drift, the project has drifted from its own plan.
- **Resolves brief.md open question #3**: the `--since` state-brief variant
  ("what changed since X") is this feature's state side. Both docs point at
  each other.
- **The state side trusts the store's accumulation.** Auto-extracted junk
  (brief.md §8's hygiene concern) lands on the state side and degrades the
  pack the same way it degrades briefs — a separate work item, not a blocker.
