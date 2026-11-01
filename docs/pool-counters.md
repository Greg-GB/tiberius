# Passive pool-size counters (retrieval telemetry)

Status: unreleased — candidate. One standalone doc per branch (no roadmap index).

## Problem

Retrieval-quality decisions lack a real-traffic number. The offline
adversarial replay settled the *shape* of the miss problem (recall misses are
a minority of worst-case misses; most misses are rerank ordering) but an
adversarial set is an upper bound. Whether production queries hit zero pools
or thin pools is unknown, and it is the deciding input for the gated
semantic-recall work (#8): if real pools are healthy, embeddings buy little;
if zero pools are common, they are the fix.

## Design

Instrument the one place every search flows through: `_search` wraps the
rank pass, captures the final candidate pool (after the prefix fallback,
capped at its `limit*3`) and writes the daily row right after the rank pass
returns — the store lock is non-reentrant, so the write happens outside the
read lock, never inside it.

New table, one row per UTC day:

| column | meaning |
|---|---|
| `day` | UTC date (PK) |
| `searches` | count |
| `zero_pools` | pools that returned no candidates |
| `sum_pool` / `min_pool` / `max_pool` | pool-size aggregates |

Written in the next WAL transaction after the read (the store lock is
non-reentrant, so it cannot ride the read's own transaction). Bounded by
construction: one row per day, no query text, no per-query history — the
store cannot grow unboundedly from telemetry.

Read side:

- `stats` gains a `pools` section: trailing-7-day zero-pool rate, weighted
  avg pool, max pool. (Per-query p50/p90 are infeasible from daily
  aggregates — the non-goals rule out per-query history, and the gate needs
  the rate plus thickness.)
- CLI: visible via `stats`; no separate subcommand.

## Decision gate

After ~1 week of real traffic:

- zero-pool rate high (rule of thumb: >5% of searches) → green-light #8
  (semantic recall) with a production number to justify the dependency.
- pools healthy, misses reported by users → the problem is rerank ordering;
  #8 is the wrong tool, look at ranking instead.
- data inconclusive → extend the window before deciding.

## Non-goals

- No query text or query hashes stored (privacy + growth).
- No per-query history: daily aggregates are the resolution the decision
  needs.
- No feedback coupling (whether a result was marked helpful is not joined
  into the pool counter — keep the signal single-purpose).
- No counters for entity paths: the pool problem is a token-pool problem.
- No counting of maintenance passes: add-time duplicate detection and the
  hygiene scan route through `_search_impl` directly, so pool stats measure
  recall traffic only.

## Tests (shipped)

- `test_record_pool_upsert_and_day_roll` — one row per UTC day; searches/
  zero/sum/min/max aggregate in place; a new UTC day rolls a new row.
- `test_stats_pools_section_shape` — `pools` is stable-shaped: zeros/Nones
  with no traffic, trailing-7-day aggregates after some.
- `test_pool_counter_counts_final_pool` — `search()` counts the final
  candidate pool (prefix-fallback rescue is non-zero; a no-match search is a
  zero pool); adds and `hygiene()` (duplicate-detection passes) create no
  pool rows; a telemetry write failure never fails the search.
