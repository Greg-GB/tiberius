# Dreaming (offline memory consolidation)

Status: **design complete** — spike 2026-11-06. Implementation: opencode
mark + test pending (see Implementation table); docs, sweep prompt, and
runtime wiring live on the tiberius profile.

An offline LLM pass that replays session transcripts, checks stored facts
against what actually happened, and repairs the store through the existing
action surface. No new tables, no new schema: the dreamer is an agent that
already has `fact_store`, `fact_feedback`, and `session_search` tools.

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
appends one JSON file to the dream queue
`$HERMES_HOME/profiles/<profile>/dream_queue/` (processed files move to
its `done/` subdir):
session id, what failed, why, fact ids involved. Cost: one write.
Salience judged by the model at event time, where the cause is in
context — fixed salience rules underperform, and event-time
judgment beats nightly re-derivation from transcripts.

**Layer 2 — dream (async).**
- Live Hermes session: spawn a background subagent (`delegate_task`)
  right after the mark. It runs the pass scoped to that session:
  transcript via `session_search`, repair via `fact_store` /
  `fact_feedback`, episodic lesson fact. Parent keeps working; the
  result re-enters later and gets folded in.
- Opencode sessions and missed events: the nightly sweep processes the
  queue and moves processed files to `dream_queue/done/`. The opencode
  plugin's per-turn handler (on
  `session.execution.succeeded` / `.interrupted`, where it already
  regex-extracts and sends skill feedback) is the event source there —
  extension: write a mark file when the turn shows failure markers
  (spec: Opencode mark triggers below).

**Sweep (fallback).** Nightly cron job `tiberius-dream-sweep` (03:00,
continuity on): process queued marks, then the standard pass
(`contradict` / `drift` / `hygiene`). Morning report to chat, never
into the store (no self-logging).

**Phase 2 — targeting (no new code).** The focus set is composed from
three existing actions: `drift` (`unbacked`), `hygiene` (low-trust),
`contradict` (pairs), plus recall counts from `stats`. The dreamer runs
all three each pass and concentrates on facts likely to have changed, not
the whole store (consolidation cadence). A dedicated action
only if composing the three becomes a bottleneck.

**Phase 3 — cross-store reconciliation.** Shared store as ground truth;
per-profile stores reconciled against it via `read_stores`. Dreaming is the
write side of the read-only federation shipped in v0.6.15.

Corrections policy (from the spike): apply directly when a transcript is
ground truth for the claim; report-only otherwise.

## Queue file format

One JSON file per mark, named
`<source>-<session_id>-<turn_id>.json`. Fields:

```json
{
  "ts": "2026-11-08T16:27:00Z",
  "source": "hermes | opencode",
  "session_id": "…",
  "event": "user_correction | repeated_tool_error | loop_guard | version_conflict",
  "summary": "one line: what failed and why",
  "evidence": "short transcript excerpt or tool-error text",
  "fact_ids": [115, 117]
}
```

`fact_ids` may be empty — the mark precedes fact identification; the
dreamer resolves ids. Writes are idempotent: the same
session + event + turn is never written twice.

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
existing `skillFeedbackSent` map). The write is a plain `fs.writeFile`,
best-effort, never breaks the turn (same error handling as the engine
calls). Queue dir from plugin options, default
`$HERMES_HOME/profiles/<profile>/dream_queue/`. No engine call: the mark
is a file, not a store op — the Python engine surface is unchanged.

Test: one new case in `opencode/tests/index.test.ts` — synthetic
transcript with two tool errors plus one correction message; expect the
mark file exists with the right `source`/`event`, and a second turn with
no new failures writes nothing (dedupe).

## Per-profile enablement

1. `mkdir -p $HERMES_HOME/profiles/<profile>/dream_queue/done`
2. Standing rule in the profile's memory (one line): salient failure in
   session → write mark file, plus spawn a dream subagent
   (`delegate_task`) for live sessions.
3. Cron sweep from `dreams/sweep-prompt.md` (nightly 03:00, continuity
   on, deliver to the profile's bot chat).

## Implementation (this PR)

| Piece | Owner | State |
|---|---|---|
| `docs/dreaming.md` (this doc) | tiberius | done |
| `dreams/sweep-prompt.md` (cron job body) | tiberius | done |
| Opencode mark + test (`opencode/index.ts`, `opencode/tests/index.test.ts`) | opencode | pending |
| README bullet ("What's changed vs upstream") — must say opt-in and link `docs/dreaming.md` (new installs get the provider, not the behavior; the bullet is the only route to the enablement recipe) | tiberius | at merge |
| Runtime wiring (cron job, queue dirs, standing rule) | tiberius | live on tiberius profile |

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
