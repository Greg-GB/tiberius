# Show a fact (`tiberius show` / `memory_show`)

Status: **unreleased** — first feature off the agent-experience review.

## 1. Why

Every write on a fact (`update`, `remove`) takes a `fact_id`, but the surface
had no way to read one fact by id. The usage skill says to read a fact before
editing it ("update a stale fact in place", "read before removing") — today
that means `list` plus a scan. `show` closes the gap: one call, full row plus
entities.

It also closes the entity-discovery half of the problem: the returned
`entities` list carries each entity's canonical name **and its aliases**, so
the caller can see which names resolve to the same entity — and feed the
right names back into `probe` / `related` / `reason` / `drift`.

## 2. What it is

One read-only call that returns a single fact by id:

```
tiberius show <fact_id>
```

```json
{"fact": {
  "fact_id": 7, "content": "...", "category": "general", "tags": "infra",
  "trust_score": 0.55, "retrieval_count": 3, "helpful_count": 1,
  "created_at": "...", "updated_at": "...",
  "entities": [{"name": "AcmeWidget", "aliases": ["AW"]}]
}}
```

Error contract: unknown id → `{"error": "fact_id N not found"}` — exit 1 on
the CLI, the same JSON error body through the provider and the OpenCode
tool. Same contract as `remove` and `feedback`.

## 3. Surface

One implementation, three consumers — the same shape as `brief` and `drift`:

- CLI: `tiberius show <fact_id>`
- OpenCode: the `memory_show` tool
- Hermes: `fact_store(action='show')`

The core method is `MemoryStore.get_fact(fact_id)` — a plain SQL read on the
primary key plus one join for the linked entities. The retrieval layer is not
involved.

## 4. Properties

- **Not a recall.** A by-id lookup does not touch `retrieval_count` — that
  counter feeds trust training and future "hot facts" ordering, and a
  maintenance read must not inflate it.
- **The stored row, not a recall shape.** `show` returns the row as written
  (every field a write needs), unlike recall output, which is score-ranked
  and trimmed.
- **The BLOB never leaks.** `hrr_vector` is not in the output — the same
  rule as search.
- **Read-only.** No write path of any kind.

## 5. Notes

- O(1) on the fact id; the entity join is the only extra query.
- `entities` is ordered by `entity_id` (stable, not by relevance — `show`
  reports what is linked, not what matches).
