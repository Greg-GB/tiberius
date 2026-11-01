# `merge`: atomic near-duplicate resolution

Status: **shipped** — v0.6.9 (2026-11-01). One standalone doc per branch (no roadmap index).

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

## Tests

- `test_merge_unions_content_trust_tags_entities` (store): content is the
  survivor's plus the from's (newline-joined), trust the max, tags the
  deduped union, entities the union, the from row and its links gone, the
  survivor id unchanged, the survivor's HRR vector recomputed.
- `test_merge_is_atomic_on_mid_transaction_failure` (store): a failure at
  the `DELETE FROM fact_entities` step (proxy on the connection) rolls the
  whole transaction back — both rows, trust, tags, and every entity link are
  exactly as before.
- `test_merge_errors` (store + provider): identical ids (`ValueError`),
  unknown id either side (`KeyError`), a superseded participant on either
  side (`ValueError`) — the store is left untouched.
- `test_cli_merge_roundtrip` (CLI): merged row returned, from row gone from
  `list`, errors are the JSON error envelope.
- `test_schema_merge_action` / `test_merge_roundtrip` / `test_merge_errors`
  (provider, `tests/test_tiberius_hermes_merge.py`): the action to the schema
  enum + `from_fact_id`/`into_fact_id` params, a roundtrip through
  `handle_tool_call`, and the error paths.
- OpenCode: `memory_merge` argv pin (`merge <from> <into>`) + tool-count
  pins (18 base, 20 with skill dirs).

## Implementation notes

- `MemoryStore.merge_fact` does the preconditions as plain reads, then one
  explicit `BEGIN IMMEDIATE` ... `commit` block (the shared-connection
  methods auto-commit; merge must not, or the transaction guarantee is
  gone). Any statement failure rolls back and re-raises; the whole method
  runs under the shared re-entrant lock, so no other writer can interleave
  a commit inside the transaction.
- A merged content that already is another fact raises `ValueError` (same
  house pattern as `supersede`).
