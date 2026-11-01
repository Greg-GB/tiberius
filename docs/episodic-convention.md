# Episodic memory: decision-log convention

Status: unreleased — candidate. One standalone doc per branch (no roadmap index).

## Problem

The store is declarative: "X is true." Day-to-day agentic work also produces
*episodic* facts — decisions and events with a date and a why: "on D we chose
X over Y because Z." The 2026 field comparison (A-MEM, LangMem) both keep an
episodic type distinct from semantic memory. Today these mix into
`general`/`project` with no shape, so "what did we decide, and when?" is a
full-text guess instead of a filter.

## Design

A convention, not an engine. Categories are free-form at write time, so
nothing new is required for storage or retrieval:

- **Category:** `episodic`
- **Content shape:**
  `YYYY-MM-DD: <decision> — <alternatives considered> — <reason>`
  One line. The date prefix is the first token, which is what makes
  `search` on a date or topic work.
- **Tags:** `episodic` plus entity names as usual.
- **Recall (all existing surfaces):**
  - `list --category episodic --since 2026-10-01` — the decision timeline.
  - `search "deploy"` — token recall across the date.
  - `probe <entity>` — decisions touching an entity, via the entity graph.

Code delta: none in v1. Documentation delta: the convention lands in
SKILL.md and README (a short section), which is what makes it a feature
rather than a footnote.

Optional follow-up (only if used): `stats` reports the episodic count.

## Non-goals

- No auto-extraction of decisions from conversation — that is the gated
  LLM-extraction work (#9), separate and heavier.
- No timeline UI or ordering guarantees beyond `list`'s `updated_at`/date
  order.
- No schema: if the convention outgrows free-form categories (unlikely),
  revisit then.

## Tests (shipped)

- None for the convention itself (doc-only).
- `test_episodic_convention_shape_is_reachable` — a fixture fact with category
  `episodic` and the content shape is found by `list_facts(category='episodic',
  since='1d')` and by `search('<topic>')`; pins that no future change silently
  breaks the shape.
