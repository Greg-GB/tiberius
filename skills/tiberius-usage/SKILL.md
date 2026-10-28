---
name: tiberius-usage
description: "Use when local memory tools are available (search / probe / related / reason / add / feedback / update / remove / list). Recall before answering questions that may already be known; store only durable facts; rate recall quality after use."
---

# Using Tiberius memory effectively

Tiberius is local long-term memory: one SQLite file of facts about the user,
projects, and tools. Nothing leaves the machine.

## Recall — before answering

- **Probe first** when the question names an entity (a person, project,
  service): `probe` returns every stored fact about it.
- **Search** for topic queries. Short noun phrases — `deploy workflow`,
  `db path` — not full sentences.
- **Reason** across multiple entities only when the question genuinely spans
  two or more (`what touches X and Y`).
- **Environment questions are prime recall targets** — "how do I do X here?".
  Tool and permission constraints live only in the store, not in a fresh
  session's context.
- **When in doubt, search**: one local call (~200ms). A miss is cheaper than
  an answer that contradicts a stored decision.
- **What was just recorded?** `list` with `since` (`24h`, `7d`, or an ISO
  date) — recent facts by time, not by keyword.
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
- **Search before adding.** Update a stale fact in place instead of storing
  a duplicate — `update` keeps the id, entity links, and retrieval history.
  `remove` only for facts that are wrong or obsolete. `add` reports
  `possible_duplicates` (near-identical prior facts) — if you agree one is a
  duplicate, `remove` the older.

## Feedback — train the store

After acting on a recalled fact, rate it: `feedback helpful` if it was
accurate, `unhelpful` if it was outdated. Trust scores are how the store
self-corrects — skipping feedback is how stale facts stay on top.

## Never

- Store secrets, tokens, or credentials as facts.
- Paraphrase exact identifiers (paths, hostnames, versions, values) —
  preserve them verbatim or the memory is worse than none.
