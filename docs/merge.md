# `merge`: atomic near-duplicate resolution

Status: unreleased — candidate. One standalone doc per branch (no roadmap index).

## Problem

`add` detects near-duplicates and returns them as `possible_duplicates`, but
resolving one takes two calls: `update` the survivor with merged content, then
`remove` the duplicate. Hazards:

- `remove` drops the duplicate's entity links — knowledge silently dies
  (entities the duplicate had that the survivor lacked are lost).
- The two calls are not atomic: a failure in between leaves both facts with
  a half-merged survivor.

This happens every hygiene pass, and it is the only write workflow where the
naive path corrupts the entity graph.

## Design

One new action, one transaction:

| args | effect |
|---|---|
| `merge(from_fact_id, into_fact_id)` | fold `from` into `into`; `into`'s `fact_id` survives |

Effect on the survivor, all in one transaction:

1. `content` = survivor content + `"\n"` + from content (concat, no LLM —
   the agent can `update` the content afterwards if it wants a rewrite).
2. `trust_score` = max(survivor, from).
3. entities = union (survivor's rows + from's rows that are new).
4. `tags` = union, deduped.
5. `updated_at` = now`.
6. from row deleted.

Preconditions: both ids exist, are distinct, and neither is superseded
(superseded rows are history — merging into history is a contradiction;
error, telling the caller to use `show` first).

Returns the surviving `fact_id` plus the merged row.

Surfaces: `provider.py` (action), `cli.py` (`merge <from> <into>`),
`opencode/index.ts` (`memory_merge`).

## Non-goals

- No LLM content merge: concat is deterministic and reviewable; rewriting is
  a follow-up `update` the agent makes deliberately.
- No multi-way merge in one call: call repeatedly.
- No history: this is consolidation, not invalidation. If the caller wants
  the old text kept, `supersede` is the right action, not `merge`.

## Tests planned

- content/trust/entity/tag union on a fixture with disjoint entities.
- from row gone, survivor fact_id unchanged.
- errors: unknown id, same id twice, superseded participant.
- atomicity: simulate failure mid-transaction → store unchanged.
- CLI + OpenCode argv pins.
