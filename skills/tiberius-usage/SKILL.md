---
name: tiberius-usage
description: "Use when local memory tools are available (search / brief / drift / probe / show / entities / related / reason / add / add_batch / feedback / update / remove / list / stats / checkpoint). Recall before answering questions that may already be known; store only durable facts; rate recall quality after use; checkpoint session state at compaction."
---

# Using Tiberius memory effectively

Tiberius is local long-term memory: one SQLite file of facts about the user,
projects, and tools. Nothing leaves the machine.

## Recall — before answering

- **Probe first** when the question names an entity (a person, project,
  service): `probe` returns every stored fact about it. Several entity names
  = union (facts about any of them, the `entities` param / repeated CLI
  names); `reason` is the opposite shape (facts about ALL of them).
- **Search** for topic queries. Short noun phrases — `deploy workflow`,
  `db path` — not full sentences.
- **Brief** when starting a task or briefing a fresh context (e.g. a
  subagent prompt): `brief` composes search + entity probe into one
  token-budgeted block, whole facts only, with the window marker (how many
  more matches exist) — one call instead of several.
- **Drift** when the question is "is this project still doing what it set
  out to do?": `drift <project>` returns the project's plan facts (the
  `plan`-tagged claims: purpose, invariants) against the state facts since
  the anchor (default: the oldest plan fact), with per-claim echo markers.
  Read both sides and judge — the markers are hints, not verdicts. The
  `unbacked` field (when present) lists plan claims with no state echo in
  the window: stale, unbuilt, or code-level and never recorded — verify
  before treating them as drift, and widen the window (`top`) if a claim
  matters.
- **Reason** across multiple entities only when the question genuinely spans
  two or more (`what touches X and Y`).
- **Environment questions are prime recall targets** — "how do I do X here?".
  Tool and permission constraints live only in the store, not in a fresh
  session's context.
- **When in doubt, search**: one local call (~200ms). A miss is cheaper than
  an answer that contradicts a stored decision.
- **A marked memory block is a window, not the whole.** When prefetched facts
  carry a `(5 of N match — …)` header, more stored facts match the same query —
  search it before answering definitively.
- **What was just recorded?** `list` with `since` (`24h`, `7d`, or an ISO
  date) — recent facts by time, not by keyword. `search` and `brief` take the
  same `since` window (facts created at or after), plus `categories` (IN list)
  and `exclude_category` to filter the candidate pool — e.g. drop `skill`
  facts that dilute generic queries (`docs/search-categories.md`).
- **Conflicting recall:** if recalled facts disagree about the same thing,
  don't silently pick the higher-ranked one — ask which is current, then
  `update` or `remove` the stale one. A surfaced contradiction is cheaper
  than a confident wrong answer.
- **Show** — read one fact by id before `update` or `remove`: full content,
  entities (with aliases), trust, and counts. A by-id lookup is not a
  recall — it does not touch the trust signal.
- **Entities** — list the entity names with their linked-fact counts and
  aliases before `probe` / `related` / `reason`: those actions take a name,
  and nothing else lists the names. A listing is not a recall.
- **Stats** — orientation before hygiene: total facts, category mix, trust
  buckets, and the stale count (low trust, old). Call it once at session
  start or before `contradict` / bulk `remove` to size the sweep; browse the
  actual rows with `list` (`--order retrieval|recent|stale` for
  most-recalled / newest / oldest-updated).
- If recall returns nothing relevant, answer normally. Do not loop on
  rephrasings — one retry at most.

## Skills — when a ranked skill block appears

- A ranked skill block (a trust floor at session start, plus skills for the
  current turn) is a curated shortlist: load a skill's body before following
  it — the description is a teaser, the body is authoritative.
- With `memory_skill_search` / `memory_skill_load` (OpenCode, `skillDirs`
  set): search the catalog with a short query when the block has no match,
  then load the skill by id before acting on it.
- Without those tools (e.g. Hermes): the same block is loaded with the
  harness's native skill loader (`skill_view`).
- Loading a skill records usage automatically (session-end feedback) —
  often-loaded skills rise in the ranking; no manual rating needed.

## Store — durable facts only

- **Save:** who the user is, stable project facts, tool quirks, decisions,
  standing preferences — things the user would expect you to remember next
  session.
- **Skip:** session state, task progress, anything easily re-discovered
  (file contents, versions, current time), one-off instructions.
- One fact per call, present-tense declarative: `User prefers X`, never
  `I should always X` (imperatives get re-read as directives later).
- **Keep facts small and specific**: one statement per fact. A fact that
  keeps growing (project state, a running log) has outlived its shape —
  split it into several small facts and update them independently.
- **Store the claim, not the story.** Strip narrative framing ("the user
  said", "after we discussed"), meta-commentary ("this matters because",
  "so future sessions know"), and anything the fact's own words restate.
  What remains is the durable statement — and for decisions, the
  one-sentence reason that would settle a future re-litigation. Identifiers
  (names, paths, versions, values) are never summarized: verbatim, or not
  at all (see Never).
- **Search before adding.** Update a stale fact in place instead of storing
  a duplicate — `update` keeps the id, entity links, and retrieval history.
  `remove` only for facts that are wrong or obsolete. `add` reports
  `possible_duplicates` (near-identical prior facts) — if you agree one is a
  duplicate, `remove` the older. For several facts at once (a session summary,
  a set of findings) use `add_batch` (one statement per item; no per-item dup
  report — use `add` when the dup check matters). `feedback` takes `fact_ids`
  (list) to rate several in one call.
- **Checkpoint session state at compaction.** Before a long task ends or the
  context compacts, `checkpoint set` 3-8 short lines for the session (task /
  decisions / state / next). The next session `checkpoint show`s them instead
  of re-deriving state from prose. One active checkpoint per session: `set`
  replaces, `clear` deletes. Checkpoint facts are session state — `brief`
  never injects them into a pack.

## Feedback — train the store

After acting on a recalled fact, rate it: `feedback helpful` if it was
accurate, `unhelpful` if it was outdated. Rate it right after acting,
before moving on — a rating deferred to "later" doesn't happen. Trust
scores are how the store self-corrects — skipping feedback is how stale
facts stay on top.

## Never

- Store secrets, tokens, or credentials as facts.
- Paraphrase exact identifiers (paths, hostnames, versions, values) —
  preserve them verbatim or the memory is worse than none.
