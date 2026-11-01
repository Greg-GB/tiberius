# Provenance: who wrote a fact

Status: unreleased — candidate. One standalone doc per branch (no roadmap index).

## Problem

The store is fed by several writers: interactive Hermes sessions, a remote
OpenCode box, and machine writes (skill-sync). No fact records who wrote it.
Consequences hit in practice:

- Audit is impossible: when a fact is wrong or leaked into a context it
  should never have reached (the public-repo incident class), there is no
  trail back to the writer.
- Per-writer filtering is impossible: an agent cannot say "show me what the
  other box wrote" or "exclude machine-written facts".

## Design

One additive column:

- `origin TEXT NULL` on `facts` (`ALTER TABLE … ADD COLUMN` in `_migrate`;
  legacy rows stay `NULL` = unknown).

Write paths set it:

| writer | origin value |
|---|---|
| Hermes provider | runtime tag `<profile>:<first 8 hex of the session id>` (e.g. `default:caa1b2c3`) — stamped on every provider write: add, add_batch, checkpoint, supersede replacement, auto-extraction, memory-write mirror |
| CLI | `--origin` flag on `add`/`add-batch`/`supersede`/`extract`/`checkpoint set`, default `cli` |
| skill-sync | `skill-sync` (hardcoded in the shared sync function, so in-process and CLI runs agree) |
| OpenCode plugin | static `opencode` — appended to the `add`, `add-batch`, `supersede` and `extract` argvs; the tool context has no agent name, so the value is the plain writer id |

Read paths — same one-clause pattern as the category filters:

- `search`/`brief`/`probe`/`related`/`reason`/`list`: `origin` (include) +
  `exclude_origin` (exclude). The FTS pool clause lives in `_fts_candidates`;
  the entity paths apply the same clause in the shared `_rows_by_ids` fetch
  (plus the `related` hop check). `match_count` takes the same parameters so
  the `total` marker always describes the filtered pool.
- `exclude_origin` is NULL-safe — `origin IS NULL OR origin != ?` — so a
  writer exclusion never drops legacy rows whose origin is unknown.
- `stats`: per-origin breakdown (count per origin, `null` bucket for legacy).
- `show`/`get_fact` and `list` rows carry the `origin` field for audit.

`origin` is free-form text — no enum, same reason as categories: writers
appear over time and an enum would forbid filtering on the newest one.

## Non-goals

- No permissions or scoping: origin labels facts, it does not gate writes.
  (A `scope` concept — global vs agent-private — is a separate decision and
  deliberately out.)
- No cross-box sync or consistency protocol.
- No per-origin trust adjustment.

## Tests

- store: migration adds the column to a pre-columns DB; `add_fact` stamps
  origin (legacy rows stay `null`); `add_facts` threads origin; `list_facts`
  origin include / NULL-safe exclude; `stats` `by_origin` with the `null`
  bucket.
- retrieval: `search` origin include / exclude (legacy kept); `match_count`
  agrees on the filtered pool; the `probe` entity path applies the clause.
- brief: the pool and the `total` marker follow the origin filter.
- CLI: `add` defaults to `cli` (flag overrides); `list`/`search` filters;
  `stats` breakdown.
- provider (Hermes-only suite): schema carries both parameters; add and
  add_batch stamp the runtime tag; search round-trips the filter.
- OpenCode: argv pin for `--origin opencode` on `add` (plus the existing
  `add-batch`/`supersede`/`extract` pins updated).
