---
name: tiberius-usage
description: "Use when local memory tools are available (search / probe / related / reason / add / feedback). Recall before answering questions that may already be known; store only durable facts; rate recall quality after use."
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
- If recall returns nothing relevant, answer normally. Do not loop on
  rephrasings — one retry at most.

## Store — durable facts only

- **Save:** who the user is, stable project facts, tool quirks, decisions,
  standing preferences — things the user would expect you to remember next
  session.
- **Skip:** session state, task progress, anything easily re-discovered
  (file contents, versions, current time), one-off instructions.
- One fact per call, present-tense declarative: `User prefers X`, never
  `I should always X` (imperatives get re-read as directives later).
- **Search before adding.** Update or replace a stale fact instead of
  storing a duplicate.

## Feedback — train the store

After acting on a recalled fact, rate it: `feedback helpful` if it was
accurate, `unhelpful` if it was outdated. Trust scores are how the store
self-corrects — skipping feedback is how stale facts stay on top.

## Never

- Store secrets, tokens, or credentials as facts.
- Paraphrase exact identifiers (paths, hostnames, versions, values) —
  preserve them verbatim or the memory is worse than none.
