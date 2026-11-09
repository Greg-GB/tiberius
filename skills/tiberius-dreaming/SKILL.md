---
name: tiberius-dreaming
description: "Use when a salient failure occurs in a tiberius-memory session (user correction, loop guardrail, repeated identical tool error, board VERSION_CONFLICT) or when the user asks for memory consolidation, a dream pass, or to 'dream'."
---

# Dreaming: mark failures, consolidate offline

Design: `docs/dreaming.md` in the tiberius package (or its repo). Facts
are predictions; the transcript is the ground truth that verifies them.
The mark queue is the engine's own file (`$HERMES_HOME/dream.db`),
reached only through the `tiberius dream …` CLI verbs — no file juggling by you.

## On a salient failure (in-session)

1. Record one mark:
   `tiberius dream mark --source hermes --session <session_id>
   --event <type> --turn <turn_id> --summary "one line: what failed and
   why" --evidence "short transcript excerpt" [--fact-ids 115,117]`.
   Event types: `user_correction` | `repeated_tool_error` | `loop_guard`
   | `version_conflict`. One mark per session + event + turn — a repeat
   is an idempotent upsert (it re-opens a done mark).
2. No LLM at mark time; one local write. Cost is one CLI call.
3. Live session and the failure is serious: optionally spawn a dream
   subagent (delegate_task) with the mark (id, summary, evidence) plus
   the "Dream pass" steps below.

## Dream pass (on demand or nightly)

1. Drain the queue: `tiberius dream list --status pending`. Verify each
   mark's claim against the transcript:
   `source=hermes` → `session_search` (session id is in the mark);
   `source=opencode` → the mark's `evidence` field IS the transcript (the
   plugin records the decisive lines) — trust it, check referenced
   commits/branches via git. Repair via
   `fact_store`: `update`/`supersede` to correct, `merge` to dedup,
   `fact_feedback` to rate. `tiberius dream done <id>` per processed
   mark — including report-only ones, so nothing is re-attempted.
2. Targeting: `tiberius dream target --project <profile>` — one bundle
   (`drift` unbacked, `hygiene` low-trust/near-dupes, `contradict`
   pairs, `stats`; "no plan facts" = N/A).
   Fix only what has transcript or hard evidence; everything else is
   report-only.
3. Never duplicate facts already in the memory file or in skills.
   Never write self-logging (logs of the dream itself) into the store.
4. Output: a short report — facts changed (id + what + why), findings,
   or "nothing to dream".

## Nightly sweep (opt-in)

If the user wants a recurring pass, create a cron job from
`dreams/sweep-prompt.md` in the package (or repo; 0 3 * * * UTC,
continuity on, deliver to the profile's bot chat). Do not create the job
unless the user asks — it is a recurring LLM run.
