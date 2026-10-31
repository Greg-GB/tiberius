# Multi-category filter on `search` (and `brief`)

Status: **shipped** — v0.6.5 (2026-10-31). One standalone doc per branch (no roadmap index).

## Problem

`search`/`brief` take one `category` (inclusion, single value). The live store
is dominated by `skill` facts and skill descriptions share tokens with almost
everything: a measured control query returned an FTS pool of roughly
two-thirds of the store. Two needs are unexpressible:

- **include several categories** (`user_pref` + `project` without `skill`/`general` noise)
- **exclude a category** ("everything except `skill`") — impossible with an
  inclusion list because categories are free-form: the caller does not know the
  full set to list the complement.

## Design

Two new optional parameters on `search` (and `brief`, which routes through
`search` and reports its `total` via `match_count`):

| param | type | SQL |
|---|---|---|
| `categories` | string[] | `AND f.category IN (...)` |
| `exclude_category` | string | `AND f.category != ?` |

Precedence, applied in `_fts_candidates` (the one place both the exact and the
prefix-fallback passes generate candidates, so both are filtered identically):

1. `categories` takes precedence over the existing singular `category`
   (house pattern from `entities`/`entity` on `probe`). Both set → singular is
   ignored.
2. `exclude_category` applies on top of whatever inclusion won: a fact named in
   `categories` that equals `exclude_category` is dropped.

No enum on the new params: categories are free-form at write time (`add`
accepts any string; `skill-sync` writes `skill`), so an enum would forbid
filtering on the very category that causes the dilution. The existing `category`
param keeps its current shape untouched.

Plumbing (one call chain, one SQL clause set):

- `retrieval.py` — `_fts_candidates` gains both params (clause + bound params);
  `_search` / `search` / `match_count` pass them through. `match_count` matters:
  `brief`'s `total` and the CLI's `total` must describe the *same* pool the
  caller sees.
- `brief.py` — `brief()` gains both keyword params; forwards to its `ret.search`
  call and its `match_count` total. The probe (entity) path inside `brief` is
  **not** filtered — probe is entity-anchored, dilution is a token-pool problem.
- `provider.py` — two schema params; `search` and `brief` dispatch forward them.
- `cli.py` — `search`/`brief` gain `--categories` (nargs `+`) and
  `--exclude-category`; the `match_count` total call forwards both.
- `opencode/index.ts` — `memory_search`/`memory_brief` gain both inputs; argv
  built as `--categories a b --exclude-category x`.

## Non-goals

- Entity actions (`probe`/`related`/`reason`) stay single-`category` —
  entity-anchored recall has no pool-dilution problem.
- No `exclude_categories` list — one exclusion covers the measured need; a list
  is one clause away when a second case appears.
- OpenCode's missing singular `category` input stays missing (pre-existing
  parity gap, different branch).
- The `category` schema enum on the shared param is unchanged.

## Test matrix

| layer | file | cases |
|---|---|---|
| retrieval | test_tiberius_retrieval.py | `categories` includes exactly the named set; `exclude_category` drops; both set → exclusion wins; singular `category` ignored when `categories` set; `match_count` agrees with `search` under the filter |
| CLI | test_tiberius_cli.py | `search --categories a b` + `--exclude-category` |
| provider (Hermes) | test_tiberius_hermes_search_categories.py | `search` and `brief` forward both params; omitted params = unchanged behavior |
| OpenCode | index.test.ts | `memory_search` argv pins `--categories a b --exclude-category x`; omitted → no flags |

## Measured justification

Live-store shape at feature time: `skill` facts are the largest single
category (more than half of all facts) and skill descriptions share tokens
with almost everything. A measured control query returned a candidate pool
of roughly two-thirds of the whole store — the rerank has to bury skill
description matches under real facts on every generic query.
