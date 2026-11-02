# Fallback-search counter (degraded-recall telemetry)

Status: unreleased — candidate. One standalone doc per branch (no roadmap index).

## Problem

Tiberius has two recall modes. Full mode: FTS candidates ranked by FTS +
Jaccard + HRR. Fallback mode: keyword-only — no HRR term. It triggers when
numpy is missing (the weights redistribute at init) and whenever
`probe`/`related`/`reason` find nothing structurally linked.

Since the eval fixture disambiguated the `alice` tie case (recall@1 19/19 in
**both** modes), nothing detects a silent numpy loss in production: the eval
passes either way, tests pass either way, and `pool_stats` (v0.6.11) would
record keyword-mode pools as if they were full-mode pools. That poisons the
exact telemetry the gated semantic-recall decision (#8, embeddings) consumes — a degraded
fleet would look healthy on the daily pool counters.

## Design

Count, at the same chokepoint as pool telemetry, how many of the day's
searches ran in fallback mode. One column, one flag, no new surface:

- `pool_stats` gains `fallback_searches INTEGER NOT NULL DEFAULT 0` (same
  UTC-day upsert as the rest of the table).
- `record_pool(size, fallback=False)` — the existing signature grows one
  defaulted keyword; every prior call site keeps its behavior.
- `FactRetriever` tracks the mode per call: `search` counts as fallback when
  the HRR term is disabled (`hrr_weight == 0`, i.e. no numpy at init) or when
  it is called with the internal `_fallback=True` mark; `probe`/`related`/
  `reason` reset the mark at entry and re-set it in every FTS-fallback
  branch, so full-mode compositional calls never inherit a stale mark.
- `stats` pools section gains `fallback_rate` (trailing 7 days, 4 dp;
  `None` with no traffic, like the other rates).

The counter is mode telemetry, not a ranking: it never influences scores or
results. Like `record_pool`, a failed counter write never fails the search.

## Migration

Existing databases (including the live store, which has `pool_stats` rows
from v0.6.11) get the column on next open via the same
`add_column_if_missing` double-check shape used for `facts.origin`. Legacy
rows read 0 fallbacks — they predate the counter.

## Non-goals

- No per-query rows, no query text, no per-mode pool-size breakdown. The
  consumer is one number: is the fleet degraded?
- No OpenCode/Hermes tool surface. It is a `stats` field and a store
  column; the CLI `stats` subcommand prints it for free.
- No alerting. A human reads `stats` when the #8 decision window opens.

## Tests

- store: `record_pool(size, fallback=True)` upserts the counter in place;
  `stats` pools shape gains `fallback_rate`; a pre-counter `pool_stats`
  table gains the column on open with legacy rows reading 0.
- retriever: full-mode search and compositional ops record 0; each of the
  `probe`/`related`/`reason` FTS fallbacks records 1; a no-numpy retriever
  (fresh init, weights redistributed) marks both `search` and `probe` as
  fallback.
