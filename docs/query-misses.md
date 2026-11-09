# Zero-pool query log (miss telemetry)

Status: **design complete** — 2026-11-08. Implementation pending (see
Implementation table). One standalone doc per branch (no roadmap index).

## Problem

`pool_stats` counts zero-pool searches per day (`zero_pool_rate`) but, by
deliberate design, stores no query text ("bounded by construction: one row
per UTC day, no query text, no per-query history" — `docs/pool-counters.md`).
The rate answers *how often* recall fails; it cannot answer *what the agent
keeps looking for and finding nothing*. That per-query list is the demand
signal for facts that should exist — the cheapest source of "what to
remember next" the store already has.

## Design

Log misses only. Zero-pool searches are recorded; hits are not. Bounded by
construction, same discipline as `pool_stats`:

- New table `query_misses`, one row per UTC day + normalized query,
  upserted (`count` increments on repeat).
- Retention: 30 days. One `DELETE` per write removes older rows.
- Normalization: lowercase, whitespace collapsed, truncated at 200 chars.
  `ponytail:` exact-text grouping — two phrasings of the same intent land in
  separate rows. Fine for a top-10 report; fuzzy grouping is the upgrade
  path if it proves to matter.

| column | meaning |
|---|---|
| `day` | UTC day, part of `PRIMARY KEY (day, query)` |
| `query` | normalized query text, ≤ 200 chars |
| `count` | zero-pool searches with this query on that day |
| `ts` | last write time (`DEFAULT CURRENT_TIMESTAMP`) |

## Instrumentation

One call site: every FTS search flows through `_search` in `retrieval.py`
(same chokepoint as the pool counters). `_search` captures the query
alongside the existing `_last_pool` / `_last_fallback`, and `_record_pool`
passes it to `store.record_pool(size, fallback, query)`. When `size == 0`,
the store upserts the miss row and prunes rows older than 30 days. Rides
the existing `telemetry` flag; failures swallowed — telemetry never fails
recall (same ceiling as `record_pool`).

Entity-path actions (`probe` / `related` / `reason`) have no FTS pool; out
of scope. `find_duplicates` and `match_count` already bypass `_search`;
unchanged.

## Surface

No new action. `stats()` gains one key: `misses` — top 10 by `count` over
the trailing 7 days, `{"query", "count", "last_day"}`; empty list when the
table is empty. The provider passes `stats` through, so the tool surface
needs no change.

## Consumer

The dreaming sweep reports the top misses in its morning pass ("agent
searched these, found nothing — fact candidates"), report-only: the sweep
never auto-writes facts from misses. Sweep-prompt line lands when this
feature merges, not before.

## Privacy

Query text lives in the local store DB only — same trust boundary as the
facts themselves (the owner's own queries, same machine, same file).

## Tests

1. A zero-pool search logs a miss row; a hit search logs none.
2. Same query, same day, second zero-pool search: `count` increments, no new row.
3. `stats()["misses"]` returns the miss with `query`, `count`, `last_day`.
4. A query > 200 chars is truncated before storage.
5. Rows older than 30 days are deleted by the write-path prune.

## Implementation (this PR)

| Piece | Owner | State |
|---|---|---|
| `docs/query-misses.md` (this doc) | tiberius | done |
| `query_misses` table + `record_pool(..., query)` in `store.py` | tiberius | done |
| Query capture + pass in `retrieval.py` (`_search` / `_record_pool`) | tiberius | done |
| `stats()` `misses` key in `store.py` | tiberius | done |
| Tests (5 cases above, in `tests/test_tiberius_store.py`) | tiberius | done |
| Sweep-prompt miss-report line | tiberius | at merge of this PR |

Release folds into the next version bump (house style: version, tag,
Status → shipped on release).
