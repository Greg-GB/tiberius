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
| Hermes provider | runtime-supplied tag (profile + session short id) |
| CLI | `--origin` flag, default `cli` |
| skill-sync | `skill-sync` |
| OpenCode MCP | `opencode` (+ agent name if provided) |

Read paths — same one-clause-set pattern as the category filters:

- `search`/`brief`: `origin` (include) + `exclude_origin` (exclude), applied
  in `_fts_candidates`; entity paths apply the same clause.
- `list`: `--origin` / `--exclude-origin`.
- `stats`: per-origin breakdown (count per origin, `null` bucket for legacy).

`origin` is free-form text — no enum, same reason as categories: writers
appear over time and an enum would forbid filtering on the newest one.

## Non-goals

- No permissions or scoping: origin labels facts, it does not gate writes.
  (A `scope` concept — global vs agent-private — is a separate decision and
  deliberately out.)
- No cross-box sync or consistency protocol.
- No per-origin trust adjustment.

## Tests planned

- add via provider/CLI sets origin; legacy rows read as `null` bucket.
- search `origin` include / `exclude_origin` exclude, entity path too.
- `list --origin` filter; `stats` breakdown counts (including `null`).
- `add_batch` threads origin; migration on a pre-columns DB.
- OpenCode argv pin for the flag.
