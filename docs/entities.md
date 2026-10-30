# List entities (`tiberius entities` / `memory_entities`)

Status: **shipped** — v0.6.0 (2026-10-30): CLI `entities` subcommand,
OpenCode `memory_entities` tool, Hermes provider `fact_store(action='entities')`.

## 1. Why

The entity actions (`probe`, `related`, `reason`, `brief`, `drift`) all take
an entity name as input, but nothing lists the names. Agents guess, and a
guess that misses the canonical spelling (or an alias) silently comes back
empty. `entities` closes that gap: one call lists the names, each with its
linked-fact count (so the caller can prioritize) and its aliases (so the
caller can see which spellings resolve to the same entity).

It is the list-side complement of `show`: `show` exposes the aliases of one
fact's entities; `entities` exposes every entity up front, so the name is
known before the first `probe` is attempted.

## 2. What it is

One read-only call that returns the store's entities:

```
tiberius entities [--top N]        # default N = 20
```

```json
[
  {"name": "AcmeWidget", "aliases": ["AW"], "fact_count": 3},
  {"name": "User", "aliases": [], "fact_count": 1}
]
```

Sorted by `fact_count` desc, then `name` asc. `fact_count` is the number of
`fact_entities` rows for that entity; **0 is legal** — orphaned entity rows
(an entity whose facts were all deleted) are reported with `fact_count: 0`,
not dropped and not an error.

## 3. Surface

One implementation, three consumers — the same shape as `show` and `drift`:

- CLI: `tiberius entities [--top N]` (default 20)
- OpenCode: the `memory_entities` tool (optional `top`, default 20)
- Hermes: `fact_store(action='entities')` — no required args; the schema's
  optional `limit` caps the list (default 20)

The core method is `MemoryStore.list_entities(top)` — a single SQL statement
over `entities` with a `LEFT JOIN` to `fact_entities` and a `GROUP BY`. The
retrieval layer is not involved.

The output contract is a **bare JSON array** on all three surfaces — a
deliberate deviation from the `{"results": ...}` wrapper the fact-browsing
commands use. The array *is* the result; there is nothing to wrap.

## 4. Properties

- **Not a recall.** A listing does not touch `retrieval_count` — that
  counter feeds trust training and future "hot facts" ordering, and a
  discovery read must not inflate it (the same convention as `show`).
- **Orphans are first-class.** The join is a `LEFT JOIN`, not an inner join:
  an entity with zero linked facts still appears, with `fact_count: 0`. An
  inner join would silently drop exactly the rows this action exists to
  surface.
- **No domain error path.** There is no id to miss and nothing to be
  "not found" — an empty store returns `[]`. Infrastructure failures use the
  shared contracts (CLI: `{"error": ...}` + exit 1; provider: `tool_error`).
- **Read-only.** No write path of any kind.

## 5. Notes

- Aliases are parsed from the comma-joined column with the same idiom as
  `get_fact` (`[a.strip() for a in (r["aliases"] or "").split(",") if a.strip()]`).
- `top` is clamped to ≥ 1 inside the method (SQLite `LIMIT -1` means
  unlimited — a negative cap must not dump the whole store).
- `name` asc as the tie-breaker keeps the listing deterministic for equal
  counts (SQLite `ORDER BY` alone does not guarantee a stable order).
