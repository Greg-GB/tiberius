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

Instrument the one place every search flows through: `_fts_candidates`
returns the pool; `search` counts it before rerank.

New table, one row per UTC day:

| column | meaning |
|---|---|
| `day` | UTC date (PK) |
| `searches` | count |
| `zero_pools` | pools that returned no candidates |
| `sum_pool` / `min_pool` / `max_pool` | pool-size aggregates |

Written in the same transaction the read happens (WAL; concurrent writers
already the norm). Bounded by construction: one row per day, no query text,
no per-query history — the store cannot grow unboundedly from telemetry.

Read side:

- `stats` gains a `pools` section: trailing-7-day zero-pool rate, p50/p90
  (approximated from daily aggregates), max pool.
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

## Tests planned

- counter increments per search, day boundary rolls the row.
- zero-pool counted when FTS returns nothing (prefix-fallback pool excluded
  from the zero count only if it rescued candidates — decided in impl:
  count the *final* candidate pool).
- `stats` pools section shape with 0/1/7 days of data.
- boundedness: 30 days of tests → 30 rows max.
- telemetry write failure never fails the search (wrap in try/except,
  log-only).
