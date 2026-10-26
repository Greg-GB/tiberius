# Tiberius architecture

Read this before changing anything here. It documents how each module works, why the
tricky parts are the way they are, and the invariants that must not be broken.
AGENTS.md lists the hard invariants; this file explains them.

## Module map

| Module | Role | Depends on |
|---|---|---|
| `holographic.py` | HRR math: phase-encoded vectors, bind/unbind/bundle, blob (de)serialization | numpy (numpy) |
| `store.py` | SQLite schema, `MemoryStore` (shared connection), entity extraction/resolution, `extract_statements` | stdlib only |
| `retrieval.py` | `FactRetriever`: search / probe / related / reason / contradict | store, holographic |
| `provider.py` | Hermes `MemoryProvider` adapter (in-process) | Hermes core + all of the above |
| `cli.py` | Harness-agnostic JSON CLI (subprocess surface for any harness) | store, retrieval |
| `opencode/index.ts` | OpenCode V2 plugin: subprocess wrapper around `cli.py` | node, `cli.py` |
| `eval/` | Golden-query recall harness (CI-safe synthetic fixture) | store, retrieval |

The core (`store` / `retrieval` / `holographic`) imports **no** harness. That
decoupling is the point of the repo — adapters talk to the core, never the
other way around.

## Data model

Four tables (`store.py:_SCHEMA`, idempotent `IF NOT EXISTS`):

- **facts** — `content TEXT UNIQUE NOT NULL`, `category`, `tags`, `trust_score`
  (default 0.5), `hrr_vector BLOB`, timestamps. The unique content column is
  the dedupe key: `add_fact` returns the existing id instead of duplicating.
- **entities** — `name` with `COLLATE NOCASE` (case-insensitive identity),
  comma-joined `aliases`, usage count.
- **fact_entities** — the fact↔entity join; the *source of truth* for
  compositional queries.
- **facts_fts** — FTS5 **external-content** table over `facts(content)`. The
  `facts_ai`/`facts_ad`/`facts_au` triggers keep it in sync on
  insert/delete/update. Never write FTS content by hand.
- **memory_banks** — one `cat:{category}` row per category holding the
  bundle of that category's fact vectors (used by `probe` for set-level
  context). Rebuilt wholesale on any change to that category.

**Trust model.** New facts start at 0.5. `record_feedback` applies
`+0.05` per helpful, `−0.10` per unhelpful (asymmetric — decay should outpace
promotion), clamped to [0, 1]. Unknown fact_id raises `KeyError` (callers
convert it to an error, they don't swallow it). Trust *weights scores* in
search (`relevance × trust`) and vector ranking (`(sim+1)/2 × trust`);
structural queries (probe/related/reason) apply **no** min_trust filter —
they return everything linked, ranked by fit.

## Concurrency model (why there is no "database is locked")

Multiple `MemoryStore` instances for the same DB file (main agent + every
`delegate_task` subagent, all in one process) **share one connection and one
re-entrant lock** via the class-level `MemoryStore._shared` registry
(keyed by resolved path, refcounted, with a per-entry `ready` flag so a
partially-initialized entry can't be half-seen by a second opener).

- The connection is `isolation_level=None` (autocommit) + WAL. `commit()`
  calls in `_write` are no-ops kept for clarity.
- **The rule: no connection access without holding `MemoryStore._lock`.**
  Every public store method takes it; every `FactRetriever` method takes
  `self.store._lock` for its queries (the retriever has no lock of its own).
  A read that skips the lock can interleave with a writer on the *same*
  sqlite3 connection object.
- `close()` is refcounted and guarded: it only pops the registry entry if it
  still references *this* instance's connection (a stale `close()` after the
  entry was replaced can't close someone else's live connection).
- `release_all_under(prefix)` exists for harness shutdown (close every
  connection under a directory, without killing connections for DBs outside
  it) — see the docstring citing the Hermes profile-delete issue.

## Entity extraction (`store._extract_entities`)

A deterministic regex pass — not an LLM — run automatically on every
`add_fact`:

- Proper-noun phrases: 1–4 words, each starting uppercase (or an
  all-caps-single-word like `API` is *excluded*; `McDonald`-style initials
  pass), first word capitalized, not in the stoplist (`_STOPWORD_FIRST_WORDS`
  / `_STOPWORD_LAST_WORDS`), not starting with a sentence opener
  (`_SENTENCE_STARTERS`).
- Quoted names: `"..."`, `'...'`, `` `...` ``.
- `aka` clauses: `... aka "X" ...` adds X as an **alias**, not an entity.
  Known ceiling (marked in code): the entity pattern is greedy and can
  swallow a clause before the alias when the entity has no space
  (`Docker aka "Dkr"` → alias `Dkr` is missed). Upgrade path: LLM extraction.
- Dedupe is case-insensitive; the **first-seen spelling wins** and later
  spellings are recorded as aliases via `_add_alias` (comma-joined, guarded
  against duplicates and 128-char rows).
- Resolution (`_resolve_entity`): name (NOCASE) → alias (`LIKE`, which is
  case-insensitive for ASCII by SQLite default) → create. Never duplicates.

The stoplists are static and English-centric; that's the documented cost of
the regex approach. The golden fixture exercises the tricky cases.

## HRR — and the bundle rule

`holographic.py` uses **phase-encoded** HRRs: a concept is a vector of
angles in [0, 2π). Atoms derive from SHA-256 counter blocks (deterministic
across machines and Python versions — no RNG state).

- `bind(a, b) = (a + b) % 2π` (circular convolution)
- `unbind(m, k) = (m − k) % 2π` (circular correlation)
- `bundle(*v) = circular mean` (superposition) — holds O(√dim) items before
  similarity degrades; `snr_estimate` warns below SNR 2.0
- `similarity(a, b) = mean(cos(a − b))` ∈ [−1, 1]
- `encode_fact` = `bundle(bind(text, ROLE_CONTENT), bind(entity_i, ROLE_ENTITY)...)`

**THE BUNDLE RULE (read before touching retrieval):** a fact vector is a
*superposition* of several terms. Pointwise unbind subtracts a key from the
*whole* vector — it does **not** cleanly extract one term from a multi-term
bundle. Therefore compositional queries (which facts touch entities X and Y)
are answered by the **SQL entity graph** (`fact_entities` intersections in
`retrieval.reason`), and HRR vectors only *rank within* the composed set.
The one that in `search()`'s docstring and the probe docstring.

The one sanctioned soft use: `search()` unbinds `ROLE_CONTENT` from the fact
vector and scores the *approximate* content projection against the query —
a ranking heuristic, not an exact decomposition. It works (golden 18/18)
because the content term dominates the bundle; don't generalize this to
multi-term extraction.

**Blob format.** `phases_to_bytes` writes a `b"HRR1"`-prefixed float32 blob
(half the size of legacy float64). Deserialization handles both. Quirk: at
dim=1 the prefixed float32 and raw float64 blobs are the same 8 bytes, so
writes at dim=1 fall back to raw float64 to keep reads unambiguous — don't
'simplify' this away.

**numpy is optional.** Without it: atoms/vectors are NULL, search degrades to
FTS5+Jaccard (HRR term neutral at 0.5), entity composition still works
(SQL). A store opened with a dim that differs from its stored vectors keeps
the stored dim and warns; NULL vectors get backfilled on open
(`test_backfill_computes_vectors_for_null_rows`).

## Retrieval flows (`retrieval.py`)

All methods: canonicalize (probe/related/reason) → SQL → rank →
`_row` (drops `hrr_vector` from output — BLOBs never leak to callers).

**`search(q, category, min_trust, limit)`** — keyword pipeline:
1. `_sanitize_fts_query` quotes tokens and joins with ` OR ` (any token may
   match; FTS5 `rank` is relevance-ordered, most-negative = best).
2. Candidates: FTS match, `LIMIT 3×limit` (overfetch so later ranking has
   room). FTS5 rank is normalized to [0,1] by |rank|/max_abs.
3. Per candidate: Jaccard(query tokens, fact tokens) and HRR similarity of
   the query text against the fact's content projection (0.5 neutral when
   either vector is missing).
4. `relevance = 0.4·fts + 0.3·jaccard + 0.3·hrr`, then `× trust`, then
   optional temporal decay (`exp(−ln2 · age_days / half_life)`, off by
   default).

**`probe(entity)`** — canonicalize → all fact ids linked to the entity →
set context = bundle of those fact vectors (from `memory_banks`
`cat:{category}` when all ids share one category) → rank the set's facts by
content centrality to that context. (The old per-fact unbind-of-set-context
returned noise — that's how the bundle rule got earned.)

**`related(entity)`** — the entity's own facts ∪ one hop (facts sharing any
of the entity's other linked facts' entities) → same centrality ranking.
Category filter applies per candidate (N small SELECTs — ponytail: fine for
set sizes here).

**`reason(entities)`** — intersection of the per-entity fact-id sets
(compositional semantics via SQL, per the bundle rule) → bundle the
intersection → rank. Falls back to a joined-name `search` when no entity
resolves or the intersection is empty.

**`contradict(entity, limit)`** — memory-hygiene tool: pairs up to the 500
most-recently-updated facts sharing the entity, scores
`overlap · (1 − shift(sim))` where overlap is entity-set Jaccard (≥ 0.3
gate) and shift maps cosine to [0,1]; reports pairs with near-opposite
vectors and overlapping topics. O(n²) by design, capped. Currently exposed
**only** via the Hermes provider (`fact_store` action) — not in the CLI or
the OpenCode plugin.

## Statement extraction (`store.extract_statements`)

Shared heuristic (CLI `extract`, provider `on_session_end`) that turns raw
user text into candidate facts — one match per category:

- `user_pref`: "I (really|very) (prefer|want|like|would like) X" → category
  `user_pref`; `project`: "the project uses X" / "we decided to use X" →
  `project`.
- Greedy capture to end-of-line, cut at a sentence-final `?` if one appears,
  20-char minimum, 400-char cap. Imperative-flavored, English-only — the
  LLM pass is the documented upgrade.

## Adapters

**Hermes (`provider.py`).** `TiberiusMemoryProvider(MemoryProvider)`:

- `initialize`: expand `$HERMES_HOME`, build store + retriever.
- `system_prompt_block`: fact count + 'use fact_store' guidance (this is the
  block at the top of the session prompt). Reads the count **under the store
  lock** (see concurrency rule).
- `prefetch`: top-5 search injected into context.
- `on_session_end`: auto-extract from **user** messages only, with a
  compaction-summary guard — Hermes compaction prepends a `## Historical
  Task Snapshot` summary to the latest user message; extracting from it
  would store the agent's own summary as 'user preference' facts. The guard
  finds the marker, keeps only text after it, and skips pure-summary
  messages. (The original 'skip when len > 500' guard was a size heuristic
  that misfired on short compactions — replaced by the marker.)
- `on_memory_write`: mirrors the built-in memory tool's writes as facts
  (`user` target → `user_pref` category).
- `shutdown`: closes the store **on the caller's thread** (Hermes may call
  from a worker after the gateway thread is gone).
- `save_config`: writes through the canonical Hermes config writer (lock,
  managed-mode refusal, atomic replace) — never `yaml.dump` the file
  directly. Normalizes a concrete own-profile db path back to the
  `$HERMES_HOME/...` placeholder so config stays portable.
- Tools: `fact_store` (add/search/probe/related/reason/contradict/update/
  remove/list — the full surface) and `fact_feedback`. `handle_tool_call`
  converts `KeyError` → 'missing argument' errors and any exception into
  `tool_error` (the agent sees a structured error, never a crash).

**CLI (`cli.py`).** The subprocess surface. DB resolution: `--db` >
`$TIBERIUS_DB` > `~/.tiberius/memory.db`. Commands: search/probe/related/
reason/add/feedback/extract. Contract: **JSON on stdout, exit 0**; errors
(e.g. unknown fact_id in feedback) are `{"error": ...}` + exit 1 — no
tracebacks (callers parse stdout as JSON unconditionally).

**OpenCode (`opencode/index.ts`).** Native V2 plugin; every operation shells
out to `cli.py` (the core never runs inside the node process).
**Runtime-verified against live OpenCode 2.0.28 (2026-10-26)** — all six
tools, context injection, and event-driven extraction observed working
end-to-end. Verified payload shapes (these were the untested assumptions):

- 6 tools mirroring the Hermes surface: `memory_search` / `memory_probe` /
  `memory_related` / `memory_reason` / `memory_add` / `memory_feedback`.
- `context` hook: on the **first** model call per session (tracked in an
  `injected` set), searches the last user message text and pushes top facts
  into the system prompt. One-shot per session; subsequent turns rely on
  tools. Two live-verified constraints:
  - `event.messages` is **provider-shaped**: `{ id, role, content:
    [{ type: "text", text }], metadata }` — *not* the API's `{ type, text }`
    shape that `ctx.session.context()` returns. Filter `role === "user"`,
    else the last assistant/tool message wins.
  - `event.system` is snapshotted when the hook's promise settles, so the
    search must be **awaited** inside the hook; fire-and-forget lands after
    dispatch (~130ms for a local Python start) and silently injects nothing.
- `event` hook: on `session.execution.succeeded` (name confirmed), the
  session id is at **`event.data.sessionID`** — the event envelope is
  `{ id, created, type, durable, data }` (there is no `properties` key).
  Extraction reads that session's user messages via
  `ctx.session.context({ sessionID })`, which *does* return the API shape
  (`{ id, type, text, ... }`), hence the `m.type === "user"` filter. The
  message list is fed to the CLI on **stdin** (`extract` blocks on
  `stdin.read()` until it is written *and closed* — the event loop wedges
  otherwise). Re-running per completed turn is safe: `add_fact` dedupes on
  UNIQUE(content).
- `runEngine` is defensive: every call has a 30s timeout (a hung engine must
  never hang the model — the context hook awaits one before the session's
  first dispatch), CLI errors surface as structured `{"error": ...}` (the
  CLI contract is JSON on stdout, exit 1), tool failures become `{"error":
  ...}` results the model can read, and in-flight children are killed on
  plugin unload so a restart can't leave a python process holding the DB
  write-lock. `--db` is always passed first so a DB error can't be misread
  as a positional query.
- Config: `opencode.json` `plugins` entry — `{ "package":
  "file:///…/opencode", "options": { "db", "engine", "pythonpath",
  "injectTop" } }` (all optional; defaults match the CLI). Local plugin
  directories under `~/.config/opencode/plugins/` are auto-discovered and
  hot-reloaded on file change.

## Tests and eval

- **Core** (`test_tiberius_store` / `_retrieval` / `_cli`): run anywhere,
  pytest only. Cover the shared-connection registry (refcount, close
  semantics, concurrent writers), entity resolution (case, aliases,
  backfill), retrieval ranking, and the CLI roundtrip contract.
- **Provider** (remaining files): import Hermes core; only run in a Hermes
  environment. Cover save_config, profile db-path handling, shutdown,
  auto-extract, and the lock rule.
- **Golden eval** (`eval/run_eval.py`): 18 synthetic queries
  (`eval/golden.json`) over the committed synthetic fixture
  (`eval/fixture.json`, fictional 'Acme stack' — this repo is public and
  must never contain real user memory). Baseline: recall@1 18/18,
  recall@3 18/18. Run `python eval/run_eval.py` (fixture) or pass a DB
  path for a live store (copied, never mutated). **A retrieval change that
  moves these numbers is a regression until proven otherwise.**

CI runs the core suite + fixture eval on every push (job `tests`); the
release workflow publishes on `vX.Y.Z` tags.

## Pitfalls for the next agent

1. **Don't unbind to decompose.** SQL composes; HRR ranks. (Bundle rule.)
2. **Lock discipline.** Every connection access holds `MemoryStore._lock`.
   Adding a query path? Wrap it. The provider test
   `test_system_prompt_block_reads_count_under_store_lock` guards one
   violation; add similar checks for new unlocked access.
3. **`__init__.py` is a PEP 562 lazy loader.** `register` is only defined
   when `__getattr__` runs. Accessing `tiberius.register` *before* importing
   the provider module, or mocking the package as a bare namespace, breaks
   plugin loading. The test bootstrap (conftest) loads the tree as
   `hermes_plugin_tiberius` — keep it that way.
4. **Tests monkeypatch private names** (`_opened_db`, `_load_config`). If
   you rename internals, the provider tests break by design — that coupling
   is intentional (it pins the config-load path).
5. **Version lives in three places** (`pyproject.toml`, `plugin.yaml`,
   `opencode/package.json`) — plus the root of `opencode/package-lock.json`.
   Bump all at release time.
6. **WAL fallback:** some filesystems (NFS, overlay) don't support WAL;
   `_connect` falls back to DELETE journal. Don't 'fix' the fallback away.
7. **FTS is external-content**: delete/insert triggers are part of the
   schema. Dropping or rewriting `facts_fts` without the triggers silently
   desyncs search.
8. **Never put personal data in fixtures/eval/tests.** This repo is public.
   The Acme stack is synthetic on purpose.
9. **The gateway loads the install copy** (`plugins/tiberius`), not the git
   repo. After pushing, re-sync it; the running gateway needs a restart to
   pick up code changes.
10. **The OpenCode plugin is runtime-verified on OpenCode 2.0.28**
    (2026-10-26): all six tools, context injection, and event-driven
    extraction were observed working end-to-end; the verified payload
    shapes are documented in the adapter section above. The first
    deployment attempt shipped four silent failures (wrong `messages`
    field names, wrong event payload key, un-awaited hook, and extract
    input never written to the engine's stdin — the CLI then blocks
    forever on `stdin.read()` and wedges the event loop). A later code
    review also hardened the subprocess path (30s timeout, structured
    CLI errors, teardown kills). If a new OpenCode release changes the
    `event.data` envelope or the hook `messages` shape, re-verify against
    a live instance before relying on it.

## Known gaps (deliberate, with ceilings)

- Regex entity/statement extraction: English, static stoplists, greedy AKA
  ceiling — upgrade path is an LLM pass (marked in code).
- `contradict` not in CLI/OpenCode — add when hygiene is needed from
  another harness.
- `related()` hop filter is N SELECTs (ponytail: fine for current set sizes).
- OpenCode context injection is once per session; mid-session fact changes
  only reach the model via tools.
- `update_fact` cannot clear a field to NULL (sentinel is None); pass `""`
  for tags to clear.
