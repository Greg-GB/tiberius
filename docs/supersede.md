# `supersede`: fact invalidation with history

Status: unreleased — candidate. One standalone doc per branch (no roadmap index).

## Problem

Facts go stale (versions, service states, "current" pointers), but the only
update path today is in-place: `update` overwrites the row and the old text is
gone. Long prose facts accumulate rewritten "Current: …" lines in a single
blob, and nothing records what the fact said before.

Industry signal: Zep/Graphiti is the only system in the 2026 comparison that
keeps a validity window on every fact; their core rule is that a
contradiction *invalidates* a fact rather than deleting it. Tiberius has the
ingredients (trust, feedback, a real store) but no invalidation primitive.

## Design

One new action:

| args | effect |
|---|---|
| `supersede(fact_id, content, [category], [entities], [tags])` | insert a new fact (normal `add` semantics, one transaction) and mark the old fact superseded by the new one's `fact_id` |

Schema: two additive columns on `facts` (SQLite `ALTER TABLE … ADD COLUMN`,
backward-compatible migration in `_migrate`):

- `superseded_by INTEGER NULL`
- `superseded_at TEXT NULL`

Exclusion, applied in the same places as the `min_trust` filter:

- Token path: `_fts_candidates` gains `AND f.superseded_by IS NULL` (exact and
  prefix passes inherit it, house pattern from the category filters).
- Entity path: `probe`/`related`/`reason` graph queries gain the same clause.
- `brief` inherits both via `search`/`match_count`.
- `show` and direct reads still return superseded facts, with
  `superseded_by` / `superseded_at` in the row — history stays readable.
-
  `list` excludes superseded by default; `--include-superseded` shows them.

Chains: superseding a superseded fact is legal and produces a chain;
`show` follows only one hop (the pointer), chains are walked by the caller.

## Non-goals

- No bi-temporal model: supersede is transaction time only (when Tiberius
  learned the fact was stale). No `valid_at`/`invalid_at` on event time —
  that is a Zep-class engine and a different product.
- No automatic contradiction detection: `contradict` stays a manual,
  claim-level audit tool; supersede is the manual resolution step.
- No un-supersede. If a supersede was a mistake, `remove` the new fact and
  the old one… stays marked (documented; a `restore` op can be added if this
  bites).
- No trust transfer: the new fact starts at the default trust; feedback
  re-earns it.

## Tests planned

- supersede hides the old fact from search/probe/related/brief.
- new fact present and correct; old row carries pointer + timestamp.
- `show` old fact returns pointer; chain of two supersedes walks one hop.
- `list` default excludes; `--include-superseded` includes.
- migration: fresh DB and a pre-columns DB both work.
- error on unknown `fact_id`; superseding an already-superseded fact allowed.
