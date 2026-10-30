# Drift tracking (`tiberius drift` / `memory_drift`)

Status: **Phase 1–2 complete** (2026-10-30; spike findings in §6): CLI `drift`
subcommand, OpenCode `memory_drift` tool (runtime-verified against live
OpenCode 2.0.28), Hermes provider `fact_store(action='drift')`. On local
branch `feature/drift`, version bumped to 0.5.0 — push/PR pending user
sign-off.
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
tiberius drift <project> [--since S] [--budget 300] [--top 20] [--support 0.15]
```

```
drift: Tiberius (anchor 2026-10-27; 5 of 6 plan, 9 of 14 state, ~280 tokens)
plan:
- Tiberius purpose: local-first personal memory engine; no network, no telemetry. (backed ×3 @ 0.38)
- Tiberius invariant: core changes must not regress the golden eval (19/19 @1 and @3). (backed ×2 @ 0.18)
- Tiberius invariant: the core (store/retrieval/holographic) imports no agent framework.  [unbacked]
state:
- Tiberius v0.4.0 released 2026-10-29: the brief feature (CLI + OpenCode tool + provider action).
- Tiberius workflow: feature branches, PRs via gh, user reviews and merges.
unbacked: 1 of 6 plan facts has no state echo >= 0.15 since the anchor (note: a claim with no state
evidence is stale or unbuilt — or code-level and never recorded; verify before treating as drift)
```

`backed ×N @ S`: N state facts at or above `--support`, S = the best overlap
coefficient (the top echo's score). `unbacked`: below the threshold. The
marker is a hint for the agent, not a verdict — the agent reads both sides
and decides.

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
- **Corroboration markers.** Per plan fact: the state facts that echo it,
  scored by **overlap coefficient** — `|A∩B| / min(|A|,|B|)` over cleaned
  token sets (house FTS stopword removal, ≥2-char tokens, the project token
  excluded), so a long state fact can't dilute the score. The spike (§6)
  retired the original entity-join marker: every plan fact and every state
  fact shares the project's hub entity, so the join marked *everything*
  corroborated (8/8 ×21) — zero discrimination.
- **`unbacked` supplemental field.** Plan facts with no state echo at or
  above the support threshold (default 0.15, `--support`) since the anchor —
  the actionable output ("is this claim stale, or unbuilt?"). The spike
  found a true positive: the harness-free-core invariant has no recorded
  state fact, and the marker surfaced it. Ships per the supplemental-output
  three-piece set (ARCHITECTURE.md): omitted when empty + a one-line `note`
  + a line in the tool schema description + a line in
  `skills/tiberius-usage`.
- **Window totals.** `plan_total` / `state_total` — the pre-fill pool sizes.
- **Deterministic.** Same store + parameters → same output; unit-testable with
  synthetic fictional fixtures (the eval-fixture rule).

## 3. Design

### 3.1 Pipeline (all above the core trio)

```
project
  → plan side:  facts linked to the project entity AND carrying tag 'plan'
    (one locked SQL query — exact, no N-cap, no centrality cutoff;
     ranked by trust tier, brief's _rank)
  → state side: search(project) ∪ probe(project) ∪ related(project), deduped
    → post-filter created_at >= anchor (in drift.py — see below)
    → minus plan facts
    → ranked by recency (created_at desc; tie: trust desc, fact_id asc)
  → corroboration: per plan fact, top state echoes by overlap coefficient
    (pure Python over the already-recalled window — no extra query path)
  → split budget fill: each side gets its own half (retired plan-first: see §3.2)
  → unbacked list + window totals
```

The core trio is untouched (invariants 1–3): composition stays in the SQL
entity graph (probe/related), HRR only ranks within the composed set, and
there is no new ranking surface.

Two corrections from the original sketch:

1. `retrieval.search()` has **no time dimension** — `--since` exists only on
   `list`/`list_facts`. The state side is therefore raw recalls post-filtered on `created_at` in
   `drift.py` (fact dicts already carry it). Zero changes to
   store/retrieval/holographic, so the golden eval cannot move.
2. The plan side does **not** ride on `probe`'s centrality ranking: with a
   project that accumulates many state facts, a top-N probe can crowd the
   plan facts out of the window. The plan side is an exact tag+entity query
   (the brief `_named_entities` direct-SQL pattern, under
   `MemoryStore._lock`), so no plan claim is ever silently cut by ranking.

State-side recency ranking: the three sources produce incomparable scores
(search relevance vs entity centrality — the same reason brief tiers), but
`created_at` is one comparable scale, and drift is a recency question: "what
has changed since the anchor".

### 3.2 Semantics decisions

- **Anchor.** Default: the oldest plan fact's `created_at` — "since the plan
  was declared". Stable, because `update` never moves `created_at` (only the
  write path sets it). Spike finding: seeding plan facts *now* makes the
  default window start *now*, so a first full-history check of an existing
  project needs an explicit `--since <project start>` — that is a feature,
  not a bug (ongoing tracking is "since the plan was recorded"). `--since`
  override reuses `store._parse_since` (ISO or relative 30m/2h/7d/2w).
- **Split fill, not tiered.** Each side gets its own half of the budget.
  Plan-first was retired by the first live run of `drift()`: seven plan claims
  consumed the whole 400-token budget and 0 of 16 state facts showed — a
  two-sided pack that shows one side is not two-sided. The window totals say
  how much of each side was cut, and a plan fact with no echo is still
  reported in `unbacked` regardless of the fill, so nothing uncorroborated is
  silently lost.
- **Marker window.** Echoes are computed over the recalled state window
  (`search ∪ probe ∪ related` at `--top`), not the whole store: an echo fact
  outside the window cannot corroborate. That is why the `unbacked` note
  says "verify before treating it as drift" — `unbacked` means "no echo in
  the window", and the remedy for a claim that matters is a wider window
  (`--top`) or a targeted probe/search.
- **`related()` on the state side.** Kept (spike decision): it is the
  structural net for state that never names the project. Note the dedupe
  requirement — the spike caught `related()` returning an entity's own facts
  twice (a multi-fact entity's facts all share the queried entity with their
  siblings; fixed on branch `feature/related-dedupe`); the union is deduped
  by fact_id either way.
- **Corroboration metric.** Overlap coefficient on cleaned token sets (house
  FTS stopword removal, ≥2-char tokens, project token excluded), computed in
  Python over the recalled pools. Threshold default **0.15** (`--support`),
  calibrated by the spike: noise floor ≈ 0.11, genuine support ≥ 0.18.
  Retired in the spike: entity-join counting (hub-entity degenerate) and raw
  Jaccard (state-fact length dilutes it).
- **State-side category.** No category filter: state is state — the spike's
  state side legitimately carried `user_pref` (binding user rules about the
  project) and `general` facts alongside `project`.
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
- **OpenCode**: `memory_drift(project, since?, budget?, support?)` tool —
  thin wrapper,
  renders the two-sided block, session dedupe like `memory_brief`. (Unlike
  `contradict` — a store-hygiene scan with no natural agent call moment — a
  drift check is a read-time question an agent actually asks.)
- **Hermes**: `fact_store(action='drift')` on the provider — the same
  `drift()` in-process (no subprocess).

## 5. Open questions

| # | Question | Blocks |
|---|----------|--------|
| 1 | ~~Keep `related()` on the state side?~~ resolved 2026-10-30 (spike): keep — structural net for unnamed state; union is deduped by fact_id (which also absorbs the `related()` own-fact duplication bug, fixed on `feature/related-dedupe`) | — |
| 2 | Anchor default: oldest plan fact (v1) vs newest stored `drift` verdict (Phase 3) | Phase 3 |
| 3 | Include `created_at` in the state-fact shape? (recency is meaningful on the state side; brief's shape omits it) — leaning yes, the state side is ranked by it | Phase 1 |
| 4 | ~~Default budget~~ resolved 2026-10-30 (spike): 300 (brief's) — the plan side of the dogfood fits with state to spare; the window totals say how much state was cut, and the agent can raise `--budget` | — |
| 5 | Environment facts (container, LiteLLM) pollute the state side a little — a `state` tag convention plus an optional filter is the remedy, or accept agent judgment | Phase 3 (if it shows) |

## 6. Phase 0 spike (2026-10-30) — **GO**

Dogfood on Tiberius itself, live store (28 pre-existing facts, zero plan facts):

1. **Seed plan facts** from the AGENTS.md invariants — 8 facts
   (live-store ids 35–42), category `project`, tag `plan`, SKILL.md claim
   format (claim not story, one claim each, identifiers verbatim). `add`
   reported no possible-duplicates.
2. **State facts already existed** — the live store had accumulated ~20
   Tiberius project facts over four days of sessions; nothing was patched in.
3. **Hand-assembled the pack** (retriever API, as `drift()` will call it:
   plan side `probe` + tag filter; state side `search ∪ probe ∪ related`,
   `created_at >= 2026-10-26` — the full project history — minus plan) and
   judged.

Findings that shaped the design:

1. **The entity-join marker is degenerate.** Every plan fact and every state
   fact shares the project's hub entity → all 8 plan facts showed ×21
   corroboration. Plan facts also carry *no other* extractable entities
   (filenames like `store.py`, `eval/golden.json` don't match the regex
   extractor), so "shared entity minus the hub" collapses to nothing.
   Retired: entity-join counting.
2. **Raw Jaccard is weak and noisy.** `_tokenize` keeps stopwords; long state
   facts then dominate the union and genuine support scores 0.03–0.10. With
   house FTS cleaning (stopwords stripped, ≥2 chars, project token out) the
   signal appears but is still length-diluted.
3. **The overlap coefficient works.** `|A∩B| / min(|A|,|B|)` on cleaned token
   sets discriminated cleanly: plan 42 (scan-before-push) ↔ fact 16
   (leak-scan rule) **0.75**; plan 41 (PR workflow) ↔ fact 12 (workflow
   decision) **0.33**; plan 36 (eval gate) ↔ fact 15 (release with 19/19
   gate) **0.18**; noise floor ≈ 0.11. Threshold set at **0.15**.
4. **A true-positive `unbacked` claim.** The harness-free-core invariant
   (plan 37) has best echo 0.11 → unbacked. The store never recorded a state
   fact confirming it (it is code-level, verifiable in source) — exactly the
   gap a drift check should surface.
5. **`related()` returned duplicates** — a multi-fact entity's own facts all
   share the queried entity with their siblings, so the 1-hop scan (which
   excluded only self-pairs) re-returned every own fact. Fixed on branch
   `feature/related-dedupe` (core suite 74 passed; eval 19/19 @1 and @3
   unchanged).
6. **Anchor semantics confirmed.** Plan facts seeded today make the default
   anchor "today" — a first full-history check of an existing project needs
   explicit `--since`. Ongoing tracking wants exactly that default.
7. **Probe top-N is the wrong plan-side path.** 8/8 plan facts surfaced at
   limit 30, but on a project with many state facts centrality can crowd them
   out of a top-N. The plan side is an exact tag+entity query instead.
8. **Drift verdict on Tiberius: no drift detected.** Every plan claim with
   state evidence agrees with the purpose (local-first memory engine, no
   network, eval gate enforced at release, PR workflow followed, leak-scan
   rule codified and applied). One claim (harness-free core) is unrecorded —
   code-level, not a drift. The state side carried environment facts
   (container, LiteLLM) that are noise for drift but harmless with agent
   judgment.

**GO** for Phase 1 as designed in §3 (overlap-coefficient markers, exact
plan-side query, recency-ranked state side).

## 7. Phases

| # | Work | Gate |
|---|------|------|
| 0 | ~~Spike per §6; findings recorded in §6~~ **done 2026-10-30 — GO** (§6) | — |
| 1 | ~~`drift.py` + CLI subcommand + `tests/test_tiberius_drift.py` (fictional fixtures, through `cli.main()`; the state-side `probe`/`related` paths need numpy, so those cases `importorskip` — the plan-side tag+entity query and the token-overlap markers do not) + README command table~~ **done 2026-10-30**: 12 tests, suite 85 passed, eval 19/19 @1 and @3 unchanged | — |
| 2 | ~~OpenCode `memory_drift` + session dedupe + plugin tests; Hermes `fact_store(action='drift')` + system-prompt line; tiberius-usage skill line; README tool count; version bump 0.4.0 → 0.5.0 in all version spots~~ **done 2026-10-30 (local, unpushed)**: plugin suite 38 passed + typecheck, version spots agree (the CI version-consistency check), `memory_drift` runtime-verified against live OpenCode 2.0.28; the provider test file runs in a Hermes env (accepted CI gap) | — |
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
