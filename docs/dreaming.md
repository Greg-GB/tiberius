# Dreaming (offline memory consolidation)

Status: **shipped — v0.6.16 (2026-11-09)** — spike 2026-11-06; dream CLI +
DB-backed mark store landed 2026-11-09 (PR #74, merged as 5d00422). See the
Implementation table; runtime wiring (the hermes cutover) lives on the
tiberius profile.

An offline LLM pass that replays session transcripts, checks stored facts
against what actually happened, and repairs the store through the existing
action surface. No new tables in the memory store, no new schema there:
the dreamer is an agent that already has `fact_store`, `fact_feedback`,
and `session_search` tools; the mark queue is a dedicated SQLite file
(default `$HERMES_HOME/dream.db`) managed by the CLI's `dream` verbs.

## Why

Industry-standard memory consolidation: memory entries are
predictions about what future sessions need; offline replay ("dreaming")
verifies those predictions against transcript ground truth and reorganizes.
Two findings drive the design:

1. The transcript is the ground truth that makes correction possible.
2. Don't impose fixed salience rules ("record only failures" *cost* the
   memory boost on strong models). Let the model judge; pre-screen targets
   cheaply instead.

## What Tiberius already has

| Dreaming capability | Existing primitive |
|---|---|
| Correct note against transcript | `supersede` / `update` + `origin` (fact → writing session link) |
| Find contradictions | `contradict` |
| Find stale / unbacked | `drift` (incl. `unbacked` list) |
| Dedup | `merge` + `hygiene` duplicate detector |
| Trust learning | `fact_feedback` → trust scores |
| Health report | `hygiene` |
| Session handoff / top of hierarchy | `checkpoint`, `brief` |
| Cross-profile view | `read_stores` (v0.6.15, read side) |

## The gap

Everything today is in-session and mechanical: the opencode plugin does
regex statement extraction at turn end (no LLM); the Hermes provider does
skill-usage feedback. Nothing reads transcripts, verifies facts against
outcomes, or writes corrections back. The loop is what's missing, not the
engine.

## Spike (manual pass, zero code, 2026-11-06)

Corpus: 1 indexed session (tiberius profile — its first). Detectors:
0 contradictions; 106 orphan entities (extraction noise); trust flat at 0.5
across all 116 facts (feedback loop never fired); `zero_pool_rate` 16%.

Verified the 2 hand-written facts against the transcript. Wrote 3
corrections, each with transcript evidence:

1. Patched an auth fact — it missed an in-session API scope limitation
   (403 on a security-events endpoint the fact did not anticipate).
2. First feedback signal in store history: `helpful` × 2
   (trust 0.5 → 0.55 / 0.60).
3. Episodic failure fact #117 — rule: "shipped" = published Release
   object, verified via `/releases`; a merged version bump is not enough.

Deliberately did **not** duplicate the PM-stamp pitfall (already in memory
file + `tiberius-workflow` skill). Redundant storage is the classic
failure mode of naive consolidation — stale entries pile up and bury the
useful ones.

## Design

Event-driven first, sweep as fallback — mirrors the hippocampal model
(mark at the event, consolidate offline).

**Layer 1 — mark (in-session, sync, no LLM).** On a salient failure
event — user correction, loop guardrail (`idempotent_no_progress`),
repeated identical tool error, board `VERSION_CONFLICT` — the agent
records one mark in the dream queue via `tiberius dream mark` (a
dedicated SQLite file, default `$HERMES_HOME/dream.db`, 0600; processed
marks flip to `status = done`): source, session id, event, what failed,
why, the fact ids involved. Cost: one local write.
Salience judged by the model at event time, where the cause is in
context — fixed salience rules underperform, and event-time
judgment beats nightly re-derivation from transcripts.

**Layer 2 — dream (async).**
- Live Hermes session: spawn a background subagent (`delegate_task`)
  right after the mark. It runs the pass scoped to that session:
  transcript via `session_search`, repair via `fact_store` /
  `fact_feedback`, episodic lesson fact. Parent keeps working; the
  result re-enters later and gets folded in.
- Opencode sessions and missed events: the nightly sweep drains the
  queue (`dream list --pending` → verify → `dream done` per mark). The
  opencode plugin's per-turn handler (on
  `session.execution.succeeded` / `.interrupted`, where it already
  regex-extracts and sends skill feedback) is the event source there —
  extension: record a mark (a `dream mark` engine call) when the turn
  shows failure markers (spec: Opencode mark triggers below).

**Sweep (fallback).** Nightly cron job `tiberius-dream-sweep` (03:00,
continuity on): drain the queue (verify each pending mark, `dream done`
per processed one), then the targeting pass (`dream target --project
<profile>` → work the bundle), then `dream prune`. Morning report to
chat, never into the store (no self-logging).

**Phase 2 — targeting (no new primitives).** The focus set is composed
from existing actions: `drift` (`unbacked`), `hygiene` (low-trust),
`contradict` (pairs), plus `stats`. Landed as `dream target` — one CLI
call that composes the bundle (no new retrieval primitives); the dreamer
works the bundle each pass and concentrates on facts likely to have
changed, not the whole store (consolidation cadence).

**Phase 3 — cross-store reconciliation.** Shared store as ground truth;
per-profile stores reconciled against it via `read_stores`. Dreaming is the
write side of the read-only federation shipped in v0.6.15.

Corrections policy (from the spike): apply directly when a transcript is
ground truth for the claim; report-only otherwise.

## Mark store

One SQLite file per profile — `$HERMES_HOME/dream.db` by default (path
resolution: `--dream-db` > `$TIBERIUS_DREAM_DB` > `$HERMES_HOME/dream.db`),
created 0600 on the first mark and managed exclusively through the CLI's
`dream` verbs — the queue's only write path. Where the marks live is a
security decision, not an implementation detail:

- **Not the memory store.** The store DB is the `read_stores` federation
  surface (one writer, many readers, shared across profiles), and a mark's
  `evidence` field is a raw transcript excerpt — the most personal content
  in the system. It does not belong in a file whose contract is to be
  shared.
- **Not loose files on disk.** A directory of memory-bearing JSON is an
  unvalidated input channel to a write-capable LLM agent (a planted mark
  is a prompt-injection vector into the dreamer), a disclosure surface
  (default-umask readable), and a retention/permissions mess (a growing
  set of files to get right). One 0600 file with a schema-validated write
  path replaces all three.

### CLI verbs

| Verb | Contract |
|---|---|
| `dream mark --source S --session ID --event E --turn T --summary X --evidence Y [--fact-ids 1,2]` | record one mark. Idempotent per (source, session, event, turn); a re-mark upserts and re-opens a done mark. Field caps: summary 500, evidence 4000 |
| `dream list [--status pending\|done] [--limit N]` | queue contents, oldest first (default: pending only) |
| `dream done <mark_id>` | processed. Atomic: only a pending mark can be done |
| `dream prune [--days 14]` | retention: delete done marks older than N days (pending is never pruned) |
| `dream target [--project P]` | the pass's focus set as one bundle: `drift` (needs P — the profile name; omitted when absent) + `hygiene` + `contradict` + `stats` |

### Schema

```sql
CREATE TABLE marks (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  source TEXT NOT NULL,              -- hermes | opencode (writer tag)
  session_id TEXT NOT NULL,
  event TEXT NOT NULL CHECK (event IN ('user_correction', 'repeated_tool_error', 'loop_guard', 'version_conflict')),
  turn_id TEXT NOT NULL,
  summary TEXT NOT NULL,             -- one line: what failed and why (<= 500)
  evidence TEXT NOT NULL,            -- transcript excerpt / tool-error text (<= 4000)
  fact_ids TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done')),
  done_at TEXT,
  UNIQUE (source, session_id, event, turn_id)
);
```

`fact_ids` may be empty — the mark precedes fact identification; the
dreamer resolves ids. Idempotency is the UNIQUE key, not a convention:
the same session + event + turn is one row, and a re-mark of a done row
re-opens it (a re-fired event is a new work item).

## Opencode mark triggers (implementation spec)

Extends the existing per-turn handler in `opencode/index.ts` (the one
that already regex-extracts user messages and sends skill feedback on
`session.execution.succeeded` / `session.execution.interrupted`). The
transcript from `ctx.session.context` contains tool entries with
`state.status`, so both signals are visible without new plumbing:

1. **repeated_tool_error** — two or more tool messages with
   `state.status === "error"` in the turn.
2. **user_correction** — a new user message starting with a correction
   cue: `no`, `stop`, `wrong`, `that's not`, `don't`, `never`, `instead`
   (case-insensitive, word boundary).

Dedupe: at most one mark per event type per session (same pattern as the
existing `skillFeedbackSent` map); the queue's UNIQUE key makes any
re-mark idempotent. The mark is an engine call (`dream mark`), best-effort
through the plugin's existing `runEngine` path, and never breaks the turn
(a failed call leaves the event unmarked, so the next turn retries).
Queue path from the plugin option (`dreamDb`, where a `$HERMES_HOME`
placeholder is expanded from the environment); unset: the feature is inert
(no marks).

Test: new cases in `opencode/tests/index.test.ts` — synthetic transcript
with two tool errors plus one correction message; expect two `dream mark`
engine calls with the right `--source`/`--event`/`--turn`/evidence, and a
second turn with no new failures makes no mark calls (dedupe). Plus an
inert test: the same salient transcript with `dreamDb` unset makes no
`dream` calls at all.

Implementation notes (landed with the mark): "in the turn" (the trigger
scope) is the messages NEW to the handler pass — the per-turn handler
re-fetches the full transcript every turn, so the turn delta is a
per-session seen-id set (the extract-delta pattern); on a session's
first pass (fresh plugin / hot reload) the whole history counts as the
turn, bounded by the per-event dedupe. `<turn_id>` (the queue's
idempotency key) is the last transcript message id at mark time — stable
across a reload, so a re-mark after a lost dedupe map upserts the same
row. The plugin's own field caps (summary 200, evidence 1000) sit well
inside the queue's write caps (500/4000).

## Per-profile enablement

1. Mark store: nothing to create — the engine creates
   `$HERMES_HOME/dream.db` (0600) on the first mark. Opencode profiles:
   set the plugin option `dreamDb` (e.g. `"$HERMES_HOME/dream.db"`);
   unset = no marks.
2. In-session behavior (hermes): the bundled skill `tiberius-dreaming`
   ships with the plugin and is discovered automatically — zero steps for
   a fresh install. Optional reinforcement: a one-line standing rule in
   the profile's memory (tiberius runs both).
3. Cron sweep from `dreams/sweep-prompt.md` (nightly 03:00, continuity
   on, deliver to the profile's bot chat) — opt-in; the plugin never
   schedules it.

## Implementation (this PR)

| Piece | Owner | State |
|---|---|---|
| `docs/dreaming.md` (this doc) | tiberius | done |
| `dreams/sweep-prompt.md` (cron job body) | tiberius | done |
| `skills/tiberius-dreaming/SKILL.md` (behavior delivery for new installs — plugin-bundled skill, same vehicle as `tiberius-usage`) | tiberius | done |
| Opencode mark + test (`opencode/index.ts`, `opencode/tests/index.test.ts`) | opencode | done |
| Dream CLI + mark store (`dream.py`, `cli.py` `dream` group, `tests/test_tiberius_dream.py`) — medium decision: dedicated 0600 SQLite file, not the memory store, not loose files (see Mark store) | opencode | done |
| README bullet ("What's changed vs upstream") — must say opt-in and link `docs/dreaming.md` (new installs get the provider, not the behavior; the bullet is the only route to the enablement recipe) | tiberius | at merge |
| Runtime wiring (cron job from `dreams/sweep-prompt.md`, standing rule) | tiberius | live on tiberius profile |
| Release: version bump (`plugin.yaml`, `pyproject.toml`, `opencode/package.json` + lock), doc Status → shipped, tag `vX.Y.Z` on the release commit | tiberius | post-merge |

Explicitly out of scope: targeting-query code (composed from existing
actions), cross-store reconciliation (Phase 3, design only), entity
extraction fix (separate PR), config flags (feature is inert without the
per-profile wiring above).

## Findings that constrain the design

- Dream coverage = the indexed session corpus. At spike time the tiberius
  profile had exactly one indexed session; other profiles need checking.
- Entity over-extraction: prose words ("Authorization", "Status",
  "Installed") become entities → 106 orphans, diluting entity recall.
  Candidate code fix in the resolver, separate PR.
- Skill-sync facts (114/116) have zero feedback history. Dreaming is the
  only training signal they get.

## Open questions

- Auto-apply threshold: spike applied 3/3 (all transcript-backed). Keep
  that bar, or require two independent sources?
- Where the dream report lives per profile (chat DM to owner vs Obsidian
  vault).
- Cadence trigger: fixed nightly vs drift-signal-driven (run — start nightly, move to
  signal-driven once Phase 2's targeting exists.
