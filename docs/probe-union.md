# Union-mode `probe`

Status: unreleased — candidate. One standalone doc per branch (no roadmap index).

## Problem

The entity actions cover two of three set shapes:

- **one** — `probe <X>`: facts linked to X; `related <X>`: X's facts + 1-hop
  neighbors.
- **all** — `reason <X, Y, …>`: facts linked to **every** entity (graph
  intersection).
- **any** — *missing*. "What do I know about Zephyr or Nimbus?" needs the
  N-entity union, which has no surface: the agent must probe each name
  separately and merge, paying one recall (and one retrieval-record each)
  per entity and losing any cross-ranking.

## Design

`probe` accepts one **or more** entity names; one method, one code path:

- `probe("X")` — unchanged: X's linked facts, ranked by centrality to X's
  fact set (current behavior, byte-identical).
- `probe(["X", "Y", …])` — facts linked to **any** of them (`fact_entities`
  `IN` over the union of the per-entity fact-id sets), ranked by
  centrality to the union's own fact set — the exact pattern `reason`
  already uses for the intersection.
- Aliases canonicalize per name before the union (either AKA of any listed
  name works, same as single probe).
- No structural hits, or no numpy, → the existing FTS5 fallback, searching
  the space-joined canonical names (the `reason` already does.

Single entity goes through the same path (union of one set is the set):
no branching, no second method.

### Surfaces

- **retrieval.py** — `probe(entity: str | list[str], …)`.
- **provider.py** — no new schema param: `entities` (already the `reason`
  param) now also drives `probe` — when present it takes precedence over
  `entity`. `entity` remains the single-name path for `probe`/`related`;
  `related` is unchanged (still single).
- **cli.py** — `probe ENTITY [ENTITY …]` (variadic positional; one name
  still works exactly as before).
- **opencode/index.ts** — `memory_probe` gains `entities` (string array);
  `entity` no longer `required` (either one), client-side error when
  neither is set (the `memory_feedback` both/neither pattern) — the engine
  is not called on the error path.
- **fake-engine.mjs** — `probe` already consumes its trailing names as a
  single search string; multiple names pass through unchanged.
- **skills/tiberius-usage/SKILL.md** — probe bullet: single name = one
  entity; multiple = union (any); `reason` = intersection (all).
- README operation-surface line + ARCHITECTURE retrieval section note.

## Tests

- retrieval: union of two entities returns the union set (a fact linked to
  both appears once), a fact linked only to a third entity is excluded, a
  fact linked to only one of the two is included; single-entity probe is
  unchanged (existing tests keep pinning it); unknown-name union falls back
  to FTS5 without error.
- CLI: `probe a b` returns the union; `probe a` unchanged; one name is
  still accepted.
- Hermes-only: `probe` + `entities` returns the union via the provider;
  `entities` takes precedence over `entity` when both are passed; `related`
  + `entities` still uses `entity` (guard: `related` ignores `entities`).
- opencode: `memory_probe` with `entities` sends `probe a b`; with `entity`
  sends `probe a`; with neither returns the client-side error without
  calling the engine.

## Non-goals

- No `mode` flag / no `probe-union` action: the parameter is the mode;
  adding a second action name would be a surface without a user.
- `related` stays single-entity: 1-hop over a union set is a different
  query shape (neighborhood of a set, not of an entity); build it when
  someone asks for it.
- No ranking change: union ranking reuses `reason`'s centrality-to-the-set
  scheme as-is; 'best per entity' grouping is a presentation concern the
  agent can do on the returned rows.
