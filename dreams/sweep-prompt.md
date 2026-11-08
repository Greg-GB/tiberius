# Dream sweep prompt (cron job body)

Per-profile nightly sweep. Create a cron job (schedule `0 3 * * *`,
continuity on, deliver to the profile's bot chat) with the text below as
its prompt, replacing `<profile>` and `<repo>` (path to this repo).

---

You are running the Tiberius dream pass (offline memory consolidation)
for the <profile> profile. Read <repo>/docs/dreaming.md first; it is the
design and the policy.

Steps:

1. Queue: list `$HERMES_HOME/profiles/<profile>/dream_queue/*.json`.
   Each file is one marked salient-failure event (`ts`, `source`,
   `session_id`, `event`, `summary`, `evidence`, `fact_ids`). For each:
   verify the claim against the session transcript via `session_search`
   (session id is in the file), then repair via the `fact_store` actions
   (`update`/`supersede` to correct, `merge` to dedup, `fact_feedback`
   to rate) and record the lesson as an episodic fact (shape
   `YYYY-MM-DD: ...`; see the doc's Spike section for the template).
   Move processed files to `dream_queue/done/`.
2. Standard pass: `fact_store` with `action=contradict`,
   `action=drift`, `action=hygiene`. Fix only what has transcript or
   hard evidence; everything else is report-only.
3. Never duplicate facts that already live in the memory file or in
   skills. Never write self-logging (logs of the dream itself) into the
   store.
4. Policy: apply repairs only with transcript/ground-truth evidence;
   when unsure, report-only. Keep writes minimal — the spike of record
   (doc, "Spike" section) is the size template: 3 small
   evidence-backed writes.

Your final response is a morning report to the profile owner, caveman
style (terse, facts intact): events processed, each fact changed
(id + what + why), findings, or one line "nothing to dream" if clean.
No filler.
