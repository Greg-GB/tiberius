# `stats` action and `list` `order` parameter

Status: **shipped** — v0.6.3 (2026-10-31): `stats` action (Hermes provider `fact_store(action='stats')`, CLI `stats`, OpenCode `memory_stats`), `list` `order` parameter — `trust`/`retrieval`/`recent`/`stale` (all three surfaces).

## Problem

Two missing orientation surfaces:

1. **No store overview.** A fresh session has no single call that says "what
   shape is this store?" — category mix, trust distribution, entity count.
   Today that takes `list` plus eyeballing, or nothing. Before running
   `contradict` (a hygiene sweep) there is no count of what it will touch.
2. **`list` has one order.** `trust_score DESC` is hardcoded. The other
   useful browses — most-retrieved facts, newest facts, oldest-updated facts
   — have no ordering path, so the agent cannot browse them at all.

## Design

### `stats` — orientation aggregates

One call, read-only, no parameters. Output (all integers/objects, no
supplemental fields to omit):

```json
{
  "total": 211,
  "entities": 42,
  "by_category": {"general": 90, "project": 30, "skill": 91},
  "trust_buckets": {"0.0-0.3": 5, "0.3-0.5": 40, "0.5-0.7": 120, "0.7+": 46},
  "stale_count": 12
}
```

- `total` — fact count. `entities` — entity count.
- `by_category` — `GROUP BY category`; categories with zero facts are absent
  (never emit empty buckets).
- `trust_buckets` — fixed four buckets on `trust_score`:
  `[0.0, 0.3)`, `[0.3, 0.5)`, `[0.5, 0.7)`, `[0.7, 1.0]` — the same cut
  points the `min_trust` convention already uses (0.3 default floor, 0.7
  high-trust region). A bucket is omitted at 0.
- `stale_count` — hygiene number: facts with `trust_score < 0.5` AND
  `updated_at` older than 30 days. One integer; the sweep itself is
  `list --order stale` (below) plus `contradict`.

Deliberate non-goal: `stats` does not return top-N lists. With `order` on
`list`, the browsing side has one home — `list --order retrieval --top N`
and `list --order stale --top N` — and `stats` stays four aggregate
queries, one lock, no limit parameter to tune.

### `list` `order` parameter

`order` ∈ `trust` (default — byte-identical to today: `trust_score DESC`) |
`retrieval` (`retrieval_count DESC`) | `recent` (`created_at DESC`) |
`stale` (`updated_at ASC`). Existing filters (`--category`, `--min-trust`,
`--since`, `--top`) compose unchanged. Unknown value → `tool_error` / CLI
error naming the allowed set.

Note: `stale` is oldest-updated-first, a proxy, not a filter — combining
`--min-trust` with it is a floor, not a ceiling; the low-trust-and-old set
is exactly what `stats.stale_count` counts.

## Surfaces

- **store.py** — `stats() -> dict` (one `with self._lock` block, four
  aggregate queries); `list_facts(..., order: str = "trust")` with a
  validated order map (no raw SQL interpolation of the value).
- **cli.py** — `stats` subcommand (no flags); `list` gains
  `--order {trust,retrieval,recent,stale}` defaulting to `trust`.
- **provider.py** — action enum + `stats`; `list` tool params + `order`
  (enum). `stats` dispatch returns the dict as JSON.
- **opencode/index.ts** — `memory_stats` tool (no input); `memory_list`
  gains `order`; tool-count pins 14→15 (skill-filtered), 16→17 (full).
- **fake-engine.mjs** — `stats` case (aggregate fixture) + `list` `--order`
  passthrough.
- **skills/tiberius-usage/SKILL.md** — `stats` in the ops list; guidance:
  call `stats` once per session before hygiene (`contradict`, bulk
  `remove`); use `list --order` for browsing instead of scanning.
- README / ARCHITECTURE surface lines updated.

## Tests

- store: `stats` counts on a seeded store (category mix, bucket edges at
  exactly 0.3 / 0.5 / 0.7, stale window at the 30-day boundary); `list_facts`
  order variants each produce the expected sequence; default order unchanged;
  bad `order` raises `ValueError`.
- CLI: `stats` JSON output; `list --order retrieval/stale/recent`; bad
  `--order` rejected by argparse.
- Hermes-only: `stats` in the action enum with no extra params; dispatch
  returns the aggregate JSON; `list` + `order` round-trips; unknown order is
  an error.
- opencode: `memory_stats` sends the `stats` argv and passes the result
  through; `memory_list` passes `--order` when set, omits it when unset.

## Non-goals

- No max-trust filter on `list` (the floor is the existing contract; the
  stale set is counted by `stats`, browsed with `--order stale`).
- No per-entity breakdown in `stats` (`entities` + `probe` cover it).
- No `since` on `stats` — windowed views are `list`/`search` territory.
