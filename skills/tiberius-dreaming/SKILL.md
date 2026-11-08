---
name: tiberius-dreaming
description: "Use when a salient failure occurs in a tiberius-memory session (user correction, loop guardrail, repeated identical tool error, board VERSION_CONFLICT) or when the user asks for memory consolidation, a dream pass, or to 'dream'."
---

# Dreaming: mark failures, consolidate offline

Design: `docs/dreaming.md` in the tiberius plugin repo. Facts are
predictions; the transcript is the ground truth that verifies them.

## On a salient failure (in-session)

1. Write one JSON file:
   `$HERMES_HOME/profiles/<profile>/dream_queue/<source>-<session_id>-<turn_id>.json`
   (create the dir if missing). Fields per the "Queue file format"
   section of `docs/dreaming.md`. One file per session + event + turn —
   never write the same mark twice.
2. No LLM at mark time; one write. Cost is a file.
3. Live session and the failure is serious: optionally spawn a dream
   subagent (delegate_task) with the mark file plus the "Dream pass"
   steps below.

## Dream pass (on demand or nightly)

1. Drain the queue: read each mark in
   `dream_queue/*.json`. Verify the claim against the session transcript
   (`session_search`, the session id is in the file). Repair via
   `fact_store`: `update`/`supersede` to correct, `merge` to dedup,
   `fact_feedback` to rate. Move processed files to `dream_queue/done/`.
2. Standard pass: `fact_store` with `contradict`, `drift`, `hygiene`.
   Fix only what has transcript or hard evidence; everything else is
   report-only.
3. Never duplicate facts already in the memory file or in skills.
   Never write self-logging (logs of the dream itself) into the store.
4. Output: a short report — facts changed (id + what + why), findings,
   or "nothing to dream".

## Nightly sweep (opt-in)

If the user wants a recurring pass, create a cron job from
`dreams/sweep-prompt.md` in the plugin repo (0 3 * * * UTC, continuity
on, deliver to the profile's bot chat). Do not create the job unless the
user asks — it is a recurring LLM run.
