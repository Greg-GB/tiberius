# `hygiene`: store health report (dry-run first)

Status: unreleased — candidate. One standalone doc per branch (no roadmap index).

## Problem

The store accrues debt that no current action surfaces:

- **Orphan entities** — measured on the live store: a large majority of
  entities link exactly one fact. They cost retrieval surface (probe/related
  fanout) with no graph value.
- **Prose-blob facts** — multi-paragraph facts that should be several
  smaller ones (they dilute FTS pools and are hard to supersede/merge).
- **Stale low-trust facts** — below the useful trust band with no feedback.
- **Near-duplicate clusters** — `add` catches them at write time, but
  nothing finds the pairs that slipped through.

`stats` reports totals; `contradict` audits claims. Neither answers "what is
the mess, and how big is it?"

## Design

One new action, **read-only in v1**:

| args | effect |
|---|---|
| `hygiene([limit = 20, max_words = 200, min_trust = 0.45])` | return a debt report |

Report sections (each capped, `limit` per section, default 20):

1. `orphan_entities` — entities with ≤1 linked fact, count + sample list.
2. `near_duplicates` — content-similarity clusters, reusing the detector
   `add` already runs (same threshold constant).
3. `oversized_facts` — content longer than `max_words` (default 200).
4. `low_trust` — facts below `min_trust` (default 0.45) with
   `helpful_count = 0`.
5. `superseded_count` — count only (section appears once `supersede` ships;
   absent before).

Apply (v1, the only safe auto-fix): `hygiene --apply orphans` prunes
entities with zero linked facts after fact deletions (true orphans, not the
single-fact ones, which are just young). Single-fact entities are report-only
in v1 — pruning them loses probe fanout for currently-valid single-fact
entities.

## Non-goals

- No auto-merge, no auto-supersede, no auto-remove of facts. Every fact-level
  fix is a human/agent decision made with the report in hand.
- No scheduling or background runs.
- No thresholds persisted in the store: they are call args with defaults.

## Tests (shipped)

- `test_hygiene_report_finds_all_debt_classes` — fixture carrying all four
  debt classes + a superseded row: every section finds its debt, and
  superseded rows never appear in a fact section.
- `test_hygiene_caps_respected_and_read_only` — `limit` trims items, not
  the total counts; the report writes nothing (row/link counts unchanged);
  `stats` unaffected.
- `test_hygiene_threshold_args` — `max_words` / `min_trust` call args move
  the oversized and low-trust boundaries.
- `test_prune_orphan_entities_only_zero_fact` — apply prunes zero-fact
  entities only, single-fact kept, idempotent.
- `test_cli_hygiene_report_and_apply` — CLI report shape (five sections),
  caps, `--apply orphans` (prunes once, 0 after), entities list after.
- `test_tiberius_hermes_hygiene` — provider schema (`hygiene` in the
  action enum, `max_words` / `min_trust` descriptions), report roundtrip,
  `limit` caps items not counts.
