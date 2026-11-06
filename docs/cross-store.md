# Cross-store recall (read-only shared stores)

Status: **in development** — `feat/cross-store-recall` branch (one standalone
doc per branch; no roadmap index).

## Problem

Tiberius keeps one SQLite store per profile (`$HERMES_HOME/tiberius_store.db`
by default): each agent's memory is its own file. That isolation is the
point. But durable facts often span profiles — a shared store owned by one
profile (say, a shared ops profile other agents run under) that another
profile's recall should be able to read.

The zero-code way today is to point several profiles at the same `db_path`.
It works, but then every profile is a *writer* on that file: one SQLite
writer at a time, per-profile telemetry mixed together, and no per-profile
private store. Cross-store recall keeps the N+1 file model (N private stores
plus one shared store) and makes the shared store a read for everyone else.

## Design

- **Config** — `read_stores` under `plugins.tiberius`: a YAML list of paths
  (or a comma-separated string). Each entry gets the same `$HERMES_HOME`
  expansion as `db_path`. Unset: exactly one database file, zero behavior
  change.
- **Open** — `MemoryStore(read_only=True)` opens `file:<path>?mode=ro`.
  Read-only instances join the existing shared-connection registry under a
  mode-suffixed key (`<path>|ro`), so they never share a connection with a
  read-write instance of the same file. No schema init, no WAL switch, no
  file or directory creation: the file must already exist, owned by its
  writer.
- **Compatibility gate** — the store carries no schema version (it evolves
  by additive column upgrades), so the gate checks what recall actually
  reads: the four tables (`facts`, `entities`, `fact_entities`,
  `facts_fts`) plus the fact columns every query path SELECTs. A store too
  old for the build raises `StoreIncompatible`; the provider excludes it
  with a logged warning and continues. A bad `read_stores` entry can never
  break a session.
- **Recall** — `search` and `probe` run the identical query (same filters:
  `category`/`categories`/`exclude_category`, `min_trust`, `since`,
  `origin`/`exclude_origin`) against the primary store and each read store.
  Each store's top N (N = requested limit) is pooled, re-ranked by score,
  and truncated to N.
- **Attribution** — when more than one store contributes, every row is
  tagged `store` (`"primary"` or the configured path). Single-store output
  is byte-identical to before: no new key.
- **Writes stay primary-only** — pool recording and retrieval-count
  updates are telemetry on the primary store only
  (`FactRetriever(telemetry=False)` per read store); `MemoryStore._write`
  raises on a read-only instance as a second layer. The shared file has one
  writer and many readers; no write fan-out, no sync, no server.
- **Scope** — `search` and `probe` only. Every other action
  (`brief`, `related`, `reason`, `drift`, `contradict`, `show`, entities,
  feedback, …) and the session prefetch stay primary-only: entity graphs
  and trust feedback are per-store by design.

## File model

N+1 files for N profiles: N per-profile stores plus one shared store.
`read_stores` never creates files — a missing path is excluded with a
warning. The OpenCode CLI remains single-database by design; cross-store
recall is a Hermes config surface.

## Non-goals

- Writing into read stores (refused by construction).
- Cross-store `related`/`reason`/`drift`/`contradict` — entity graphs are
  per-store; joining them across stores is a separate feature.
- Schema migration coordination: the gate excludes an incompatible store;
  it never migrates it.
- Cross-machine sync: plain local files, same box, same user.

## Tests

- Core tier (bare, no Hermes): `tests/test_tiberius_store_readonly.py` —
  read-only open and read, missing file, empty file → `StoreIncompatible`,
  write refusal, no side-effect files.
- Hermes tier: `tests/test_tiberius_hermes_cross_store.py` — search/probe
  unions over disjoint fixtures (strictly greater than either store alone,
  no duplicate `(store, fact_id)`), single-store output untagged, bad read
  stores excluded (string form and `$HERMES_HOME` expansion included),
  merged recall leaves read stores byte-identical (`retrieval_count` and
  `pool_stats` unchanged), filters applied across stores.
- The single-store path is untouched: the golden recall eval gate in CI
  (baseline in README.md) passes before and after.
