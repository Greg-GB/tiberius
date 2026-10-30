# Batch writes: `add_batch` action and multi-fact `feedback`

Status: unreleased — candidate. One standalone doc per branch (no roadmap index).

## Problem

Agents that record several facts in one step — a session summary, a review
loop, a set of findings — pay one tool round-trip per fact: N facts = N
`add` calls = N LLM turns, and the same for rating M facts after using them.
Each round-trip also re-enters the tool schema and re-serializes context that
the single-item calls already proved.

## Design

Two batch surfaces, one per existing write verb. No generic `batch`
dispatcher — the verbs are concrete and the shapes are typed.

### `fact_store` action `add_batch`

- New action; param `facts`: a non-empty array. Each item is either a plain
  string or an object `{content, category?, tags?}` (same fields as `add`).
- **Commits per item, in order.** The batch reuses the exact single-item
  `add_fact` path (entity linking, HRR rebuild included) — no core write-path
  change. A failure at item *i* (empty `content`, wrong item type) stops the
  batch; items before *i* stay committed. The error names the index and lists
  what was already committed:
  `{"error": "bad facts[2]: content must not be empty", "committed": [5, 6]}`.
- **Duplicate content is not a failure.** `UNIQUE(content)` returns the
  existing `fact_id`; the batch reports it in `duplicates` and still lists
  the id. Output: `{"added": N, "duplicates": M, "fact_ids": [...]}`.
- **No near-duplicate report per item.** `add`'s `possible_duplicates` note is
  skipped in batches (it is a per-content search; N extra searches per batch
  is the cost the single-item path exists for). Want the dup check: use
  `add`.

### `fact_feedback` with `fact_ids`

- New optional param `fact_ids`: non-empty integer array. The same `action`
  (`helpful`/`unhelpful`) applies to every id. Mutually exclusive with
  `fact_id` — both set is an error, neither is an error (as today).
- **Continue-on-error, per-item results.** Trust ratings are independent
  (and commit per item, same as a single call), so one bad id does not sink
  the rest: output gains
  `"results": [{"fact_id", "old_trust", "new_trust", "ok", "error"?}, ...]`
  plus an `applied` count. (Contrast with `add_batch`'s stop-at-first-error:
  facts in a batch are one logical unit being recorded together; ratings are
  N independent judgments — report each.)

### CLI parity

- `tiberius add-batch` reads a JSON array (strings or objects) from **stdin**
  and prints the same output object (the error object on failure). Stdin, not
  argv: N facts with quotes and newlines do not survive shell quoting well.
- `tiberius feedback`'s positional `fact_id` accepts `5` or `5,6,7` — same
  output shape as the tool (single id keeps today's exact output).

### OpenCode plugin

- New tool `memory_add_batch` (`facts`: string array) — tool-count pins move
  (15 → 16 full, 13 → 14 skill-filtered) with a fake-engine case and wiring
  test.
- `memory_feedback` gains an optional `fact_ids` integer array, forwarded as
  the comma-joined positional (`feedback 1,2,3 helpful`; omitted when unset).

## What stays unchanged

- `add` / `feedback` single-item behavior, byte-identical when the new
  params/actions are absent.
- No schema change to `fact_store`'s existing action enum entries; no change
  to trust deltas, entity linking, or HRR rebuild (each batched item runs the
  exact single-item path).

## Gates

Full pytest, golden eval 19/19, `opencode/` typecheck + tests, version
consistency (no bump — releases are separate), full pre-push leak scan.
