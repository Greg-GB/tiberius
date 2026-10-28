# Context brief (`tiberius brief` / `memory_brief`)

Status: **Phase 1 in progress** (2026-10-28). Phase 0 spike: **GO** (results in §6).
Companion to [skill-retrieval.md](skill-retrieval.md).

## 1. Why

Every fresh context in OpenCode — a new session, a **subagent spawn**, a
review of unfamiliar territory — needs hand-assembled material. The subagent
case is worst: the harness says "child sessions start with fresh context, so
include all relevant context," and today that costs the parent agent 3–5
separate memory calls (probe the entities, search the topic, check skills),
each a full round-trip, followed by hand-compression into the prompt.
Measured on real requests from 2026-10-28: 1–3k tokens of hand-assembled
context, answerable in 150–250 tokens from the store.

The retrieval primitives already exist (search, probe, related, reason,
trust ranking, per-session dedupe). What's missing is one composition
surface: **ask for a topic, get a token-budgeted context pack**.

## 2. What it is

One read-only call that returns a compact, budgeted block of store content
for a query:

```
tiberius brief <query> [--budget 300] [--floor 0.10] [--top 20] [--category C]
```

```
brief: tiberius release process (4 facts, ~240 tokens — 3 of 5 match, search has the rest; +1 entity-linked)
- Tiberius releases: v0.3.0 … v0.3.1 … main = 0f065da. Gates: 60 core tests, golden eval 19/19 @1 and @3.
- 'Version consistency' CI check on main (PR #19) … observation mode …
- User preference: NEVER merge a PR without explicit approval …
- Tiberius is PUBLIC … squash only (gh pr merge --admin --squash; flags -t/-b) …
skills (when a catalog is configured):
- tiberius-usage — Use when local memory tools are available …
```

Header rule: `(<shown> facts, ~<tokens> tokens` + ` — <k> of <total> match,
search has the rest` when the query-match pool exceeds the shown pool-sourced
facts (k = shown facts − entity-linked) + `; +<entity_only> entity-linked`
when probe added facts + `)`. Same "window, not the whole" semantics as the
prefetch coverage counter (v0.3.2) — an unmarked block reads as the whole
picture; a marked one says search has the rest.

The CLI wire format is JSON (like every other subcommand); the block above is
the tool/renderer shape (Phase 2):

```json
{"query": "tiberius release process", "total": 5, "entity_only": 1, "count": 4,
 "tokens": 240, "budget": 300,
 "facts": [{"fact_id": 15, "content": "…", "category": "project", "trust_score": 0.5, "score": 0.41}],
 "skills": []}
```

Properties:

- **Read-only.** `brief` never writes to the store (no feedback, no
  retrieval counts beyond what the underlying recall records — see §3.2).
- **Budgeted.** Output is capped in tokens (default 300). A fact that
  doesn't fit is dropped whole — a partial fact is worse than no fact.
- **Coverage marker.** `total` (the query-match pool via `match_count()` —
  FTS pool, no rerank, no retrieval recording) rides along so a rendered
  block can state it is a window, not the whole. Same marker semantics and
  wording family as the prefetch coverage counter (v0.3.2).
- **Deterministic.** Same store + query + parameters → same output. That
  makes it unit-testable with synthetic fixtures (the fictional-content
  rule, same as the eval fixture).
- **Doc pointers emerge from facts, not from the brief.** Durable facts
  name their canonical docs (`docs/skill-retrieval.md §5`,
  `CONTRIBUTING.md §releases`) — the usage skill's identifiers-verbatim
  rule makes that reliable. The brief adds no doc knowledge of its own,
  so there's nothing new to rot.

## 3. Design

### 3.1 Pipeline (all above the core trio)

```
query
  → search(query, limit=20)              # FTS5 + alias/prefix discovery,
                                         # trust-weighted score × temporal decay
  → probe(each entity the query names)   # entity-graph composition; targets
                                         # from a word-boundary scan of the
                                         # entities table (same convention as
                                         # the retriever's alias expansion)
  → dedupe by fact_id
  → relevance floor                      # drop pool facts below --floor;
                                         # entity-only facts kept (see §3.2)
  → rank: tier 1 pool facts, tier 2 entity-only — each tier: score desc →
     trust desc → updated_at desc → fact_id asc. The two score scales (query
     relevance vs entity centrality) are not comparable, so tiers never mix.
  → budget fill (char/4 token approximation)
  → optional skill block (pool facts with category 'skill', filled from the
     remaining budget, capped by --skills, default 2)
```

The core trio is untouched: `search`/`probe`/`related` are existing
`Retrieval` methods; composition stays in the SQL entity graph and ranking
stays within the composed set (invariant 3). `search()` already returns a
trust-weighted, temporally-decayed `score` per fact, which is exactly the
relevance signal the floor needs — no new ranking surface.

### 3.2 Semantics decisions

- **Recall counting.** `brief` calls the public `search()` and `probe()`
  (both the recorded variants), so surfaced facts count as retrievals like
  any other recall surface. A brief *is* a recall — in fact two (query
  search + entity probe), and a fact present in both sets counts once per
  underlying recall. ponytail: the overlap double-count; if it shows in
  trust tuning, dedupe before recording.
- **Coverage marker.** `total` comes from `match_count()` — the FTS pool,
  no rerank, no retrieval recording (the usage signal stays clean, as in the
  prefetch counter). It is the marker's "of N" (see §2 header rule).
- **Token approximation.** `len(text) / 4`, integer arithmetic, documented
  — deliberately simple and deterministic rather than pulling a tokenizer
  dependency into a stdlib-only core.
- **Floor default.** 0.10 on the trust-weighted search score, calibrated
  against the spike's noise cases (§6, finding 2). Entity-only facts bypass
  the floor: an exact entity match is strong evidence even at a low
  centrality score, and probe's structural path is not trust-gated (same as
  the existing `probe` command), so low-trust entity-linked facts can
  surface — deliberately, like `probe`.
- **Dedupe against the session.** When called from inside a session (the
  OpenCode tool path), returned fact_ids join the session's already-shown
  set, so the per-turn injector doesn't re-surface them.
- **Skills.** Pool facts with category `'skill'` fill a separate lane from
  the remaining budget, capped by `--skills` (default 2) — the
  same lane the session-start skill block renders. No catalog → empty lane,
  zero behavior change, as always.

## 4. Surface

- **CLI first**: `tiberius brief …` in `cli.py` — testable anywhere, usable
  by any harness, and the shape other adapters wrap. JSON on stdout like
  every other subcommand; the compact block in §2 is the renderer (Phase 2
  tool) shape, not the wire format.
- **OpenCode**: `memory_brief(query, budget?)` tool — a thin wrapper over
  the same engine call, registered like the other `memory_*` tools.
- **Hermes**: available via the CLI immediately; provider wiring is a
  later phase, not v1.

## 5. Open questions

| # | Question | Blocks |
|---|----------|--------|
| 1 | ~~Entity extractor for probe targets~~ resolved 2026-10-28: no exposed
      extractor — brief scans the entities table with the same word-boundary
      convention as the retriever's alias expansion (in `cli.py`, core
      trio untouched). Probe ships in v1. | — |
| 2 | Floor calibration: 0.10 on trust-weighted score vs. a percentile of the query's own score distribution (adapts to noisy queries) | Phase 1 tests |
| 3 | Should `--since` (state-brief variant: "what changed since X") be in v1 or a v1.1 — it overlaps the session-digest idea | v1 scope |
| 4 | Default budget: 300 validated by the spike for in-session use; subagent spawns may want 400–500. Flag exists either way | none |

## 6. Phase 0 spike (2026-10-28) — GO

Five real requests from the week's sessions, served from the live store
using only the primitives `brief` composes:

| Request | Answered by | Ideal brief | Fits 300 tokens |
|---------|-------------|-------------|-----------------|
| Subagent: run provider tests | env fact (exact) + noise | 1 fact + doc pointer | yes (~150) |
| Cut a patch release | release-state + version-check + merge-rule facts | 3 facts + doc pointer | yes (~250) |
| Open a PR | ruleset + scan-rule + approval facts | 3 facts | yes (~250) |
| Modify retrieval.py | baseline fact (procedure lives in AGENTS.md) | 1 fact + doc pointer | yes (~150) |
| What is Phase 4 | decision fact (rationale + re-eval trigger) | 1 fact + doc pointer | yes (~150) |

Findings that shape this design:

1. **Relevance floor, not top-N.** Pure top-N surfaced off-topic facts on
   2 of 5 requests (an unrelated tool-quirk fact on a test-suite request;
   an unrelated decline-on-record on a release request). The floor +
   probe-bypass is the fix.
2. **Store carries state/decisions/why; repo files carry how.** The
   retrieval-gate *procedure* lives in AGENTS.md, not the store. The
   brief therefore points (via facts that name their docs) rather than
   restates — the no-enumeration rule applies to brief output too.
3. **Trust is flat (~0.5) until feedback trains.** Ranking currently leans
   on match quality; `brief` improves as the feedback loop matures, which
   is the loop's intended behavior, not a defect of this design.
4. **Fact quality is the ceiling.** The claim-not-story / small-facts
   rules (shipped in v0.3.1) directly showed up as better briefs.

## 7. Phases

| # | Work | Gate |
|---|------|------|
| 1 | CLI `brief` subcommand (search ∪ probe) + synthetic-fixture tests: determinism, budget fill, floor, whole-fact drop, dedupe, doc-pointer pass-through, coverage total, entity-only floor bypass, retrieval recording | Core suite green; golden eval unchanged (no core-trio change); fictional-content fixtures |
| 2 | OpenCode `memory_brief` tool + session-dedupe wiring + plugin tests (tool-surface assertions when catalog unset: zero behavior change) | Plugin suite + typecheck; zero-change contract asserted by test |
| 3 | (Optional, later) `--since` state-brief variant; Hermes provider exposure | Evidence-gated on actual use |

## 8. Concerns / notes

- **Store hygiene degrades brief quality over time.** The spike observed
  auto-extracted user-chat lines polluting search results. That's a
  separate work item (extraction heuristic), but it bounds how long a
  `brief` stays high-signal without pruning.
- **Budget is an approximation.** char/4 is a ~±15% estimate; the
  guarantee is "small and bounded," not "exactly N tokens."
- **Not a replacement for the prefetch.** The session-start block answers
  "what should I know by default"; `brief` answers "what do I need for
  *this* task." They share dedupe state, not purpose.
- **Same marker, different window.** The prefetch counter (v0.3.2) marks
  the default window at session start ("5 of N match — fact_store search
  has the rest"); brief marks the task window with the same `match_count()`
  primitive and wording family. Together: an injected block says the store
  holds more than the default; a brief says its pack is a budgeted slice of
  a query's pool.
- **v1 is deliberately CLI-shaped.** One implementation, three consumers;
  the plugin tool and any future provider surface are wrappers.
