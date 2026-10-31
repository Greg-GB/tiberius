# Session checkpoint (structured compaction handoff)

Status: unreleased — candidate. One standalone doc per branch (no roadmap index).

## Problem

Long sessions end in context compaction: the running history is replaced by a
prose summary. That summary is (a) token-costly to produce and carry, (b)
marked reference-only by the harness, and (c) lossy — state survives only to
the extent the summary author remembered to write it down. A new session then
re-derives "where were we" from prose, or re-asks the user.

The store already holds durable facts, but it has no shape for *session
state*: current task, key decisions made this session, and next steps.

## Design

One new action, `checkpoint`, with three operations:

| op | args | effect |
|---|---|---|
| `set` | `session` (string), `facts` (string[], 1-8) | write the session's checkpoint |
| `show` | `session` (string) | read the active checkpoint for a session |
| `clear` | `session` (string) | delete the session's checkpoint facts |

Rules:

1. **One active checkpoint per session.** `set` first deletes existing facts
   tagged for that session, then batch-adds the new ones. No pile-up across
   compactions; the latest `set` is the whole story.
2. **Shape:** each checkpoint fact is one line, imperative or declarative:
   `task: …`, `decision: …`, `state: …`, `next: …`. The prefix is content,
   not a schema — the reader is an agent.
3. **Tagging:** facts carry `category = "checkpoint"` and
   `tags = "checkpoint:<session>"`. Retrieval is a tag-filtered `list`
   (newest first), not a search — checkpoints are looked up by session id,
   never recalled by tokens.
4. **Plumbing:** `set` reuses the existing `add_batch` write path (one
   transaction). `show`/`clear` are new read/delete paths keyed on the tag.

Surfaces:

- `provider.py` — `checkpoint` action (op dispatch), schema for `session` +
  `facts`.
- `cli.py` — `checkpoint set <session> --facts …`, `checkpoint show <session>`,
  `checkpoint clear <session>`.
- `opencode/index.ts` — `memory_checkpoint` with `op`, `session`, `facts`.

## Usage pattern

At compaction time (or before ending a long task), the agent calls
`checkpoint set` with 3-8 lines. On session start after a break, the agent
calls `checkpoint show` with the known session id and resumes from the store
instead of from prose.

## Non-goals

- No automatic compaction hook — the agent decides when to checkpoint.
  Hermes compaction is a harness event Tiberius does not receive.
- No TTL: a session's checkpoint stays until the next `set` or an explicit
  `clear`. (Store growth is bounded in practice: active sessions only.)
- No cross-session merge or diff of checkpoints.
- Checkpoint facts are excluded from `brief` by default (they are session
  state, not durable knowledge). The exclusion is built into `brief` itself —
  both tiers (pool and entity-probe) and the `total` marker, which
  subtracts the checkpoint matches so the window invariant holds — not a
  caller-side filter.

## Tests planned

- set → show roundtrip (content, order, tags, category).
- second `set` for same session replaces (no duplicates, old fact ids gone).
- `clear` removes; `show` on unknown session returns empty, not error.
- CLI + OpenCode argv pins.
- brief excludes category `checkpoint` facts.
