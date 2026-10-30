# Temporal windows on `search` and `brief` (`--since`)

Status: **shipped** — v0.6.1 (2026-10-30): `--since` window on CLI
`search` / `brief`, OpenCode `memory_search` / `memory_brief` `since`
param, Hermes provider `fact_store(action='search' | 'brief')` `since`.

## 1. Why

Temporal filtering exists on `list` only (`--since`, facts created at or after
the cutoff). Recall is unfiltered: a query like "what changed on X last week"
returns the same old facts every time, and the caller has to fetch a wide
window and discard dates by hand. `search` and `brief` are the two recall
surfaces an agent actually lives in; giving both a window makes recency a
first-class query dimension instead of post-processing.

## 2. What it is

One optional `since` argument on the two recall surfaces, reusing the
existing `store._parse_since` (ISO date/datetime UTC or relative `30m`/`2h`/`7d`/`2w`)
exactly as `list` already does. One implementation, three consumers per
surface, as with every other operation:

- CLI: `tiberius search <query> --since 7d`, `tiberius brief <query> --since 2026-10-01`
- OpenCode: `memory_search` / `memory_brief` gain an optional `since` string
  parameter (forwarded as `--since`)
- Hermes: `fact_store(action='search' | 'brief', since='7d')`

## 3. Semantics

- **Filters on `created_at`**, the same column `list` uses — writes set it,
  updates don't move it. A fact is in the window if it was *created* at or
  after the cutoff. (Updated-recency is already available: rank's temporal
  decay and `updated_at` ordering.)
- **Pool-level filter, not result-level.** The clause goes into the candidate
  SQL (FTS match), so `limit` and the brief budget are consumed from the
  filtered pool. A top-10 over "matches since 7d" is the top 10 *new* facts,
  not the top 10 facts with old ones cut off the end.
- **The brief's probe tier honors the window too.** A brief is search ∪
  entity-probe; the probe tier is filtered on `created_at` as well, otherwise
  a stale entity-linked fact re-enters the pack that search left out. Known
  ceiling: the filter runs after probe's top-N pick, so the entity tier can
  under-fill when the newest linked facts rank past the pool size.
- **The brief window marker stays honest.** `brief`'s `total` comes from
  `match_count`; `match_count` takes the same `since` clause, so "2 of 5
  match" describes the window, not the whole store.
- **Omitted = unchanged.** `since=None` generates no clause; every existing
  caller (prefetch, per-turn injection, skill lanes) is byte-identical in
  behavior.
- **Invalid values use the existing contract**: `_parse_since` raises
  `ValueError`, the CLI turns it into the standard JSON error (never a
  traceback) — same as `list --since` today.

## 4. What is deliberately not touched

- `probe`, `related`, `reason`, `contradict` — the public structural actions
  take no `since` argument (a time window there is a different question,
  history of an entity; the cheap path is `list --since` plus `probe`).
  Only `brief`'s internal probe tier honors the window (above).
- `drift --since` already means something else (the state-window anchor);
  this change does not interact with it.

## 5. Verification

- Retrieval tests: a fact created before the cutoff is excluded from `search`
  (exact and prefix-fallback passes) and from `match_count`; a fact at/after
  it is included; no `since` = current behavior (existing suite unchanged).
- Brief test: pool and `total` both respect the window; budget fill runs over
  the filtered pool.
- CLI tests: `search --since` round-trip and the invalid-value error contract.
- Provider test: `since` forwarded on both actions.
- OpenCode tests: `--since` present in the engine argv only when set.
- Golden eval: recall tables unchanged (search path unaltered when the
  argument is absent — the eval never passes it).
