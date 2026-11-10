# Dream sweep prompt (cron job body)

Per-profile nightly sweep. Create a cron job (schedule `0 3 * * *`,
continuity on, deliver to the profile's bot chat) with the text below as
its prompt, replacing `<profile>` and `<repo>` (path to this repo).

---

You are running the Tiberius dream pass (offline memory consolidation)
for the <profile> profile. Read <repo>/docs/dreaming.md first; it is the
design and the policy.

Steps (the queue is the engine's dream store — `$HERMES_HOME/dream.db`,
reached only through the `tiberius dream …` verbs):

1. Queue: `tiberius dream list --status pending`. Each mark has `id`,
   `ts`, `source`, `session_id`, `event`, `summary`, `evidence`,
   `fact_ids`. Verify the claim against the transcript:
   `source=hermes` → `session_search` (session id is in the mark);
   `source=opencode` → the mark's `evidence` field IS the transcript (the
   plugin records the decisive lines) — trust it, check referenced
   commits/branches via git if present. Then repair via the `fact_store`
   actions
   (`update`/`supersede` to correct, `merge` to dedup, `fact_feedback`
   to rate) and record the lesson as an episodic fact (shape
   `YYYY-MM-DD: ...`; see the doc's Spike section for the template).
   `tiberius dream done <id>` per processed mark — including
   report-only ones, so nothing is re-attempted nightly.
2. Targeting: `tiberius dream target --project <profile>` — one bundle
   (`drift` unbacked claims, `hygiene` low-trust/near-dupes,
   `contradict` pairs, `stats`). `stats` `misses` is the demand signal —
   when non-empty, name the top missed queries in the report
   (`docs/query-misses.md`). "No plan facts" for the project is a
   valid N/A (note it, move on). Fix only what has transcript or hard
   evidence; everything else is report-only.
3. Never duplicate facts that already live in the memory file or in
   skills. Never write self-logging (logs of the dream itself) into the
   store.
4. Policy: apply repairs only with transcript/ground-truth evidence;
   when unsure, report-only. Keep writes minimal — the spike of record
   (doc, "Spike" section) is the size template: 3 small
   evidence-backed writes.
5. Retention: `tiberius dream prune --days 14`.

Your final response is a morning report to the profile owner, caveman
style (terse, facts intact): events processed, each fact changed
(id + what + why), findings, or one line "nothing to dream" if clean.
No filler.
