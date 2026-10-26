# Tiberius Memory

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](pyproject.toml)
[![Tests](https://github.com/Greg-GB/tiberius/actions/workflows/tests.yml/badge.svg)](.github/workflows/tests.yml)

Local SQLite memory engine: structured facts, FTS5 search, entity graph, trust scoring, and
HRR-based compositional retrieval. Harness-agnostic core with one adapter per harness —
**no network, no service**: every harness runs the engine locally against its own DB file.

Tiberius is a fork of [hermes-plugin-holographic](https://github.com/NousResearch/hermes-plugin-holographic)
(Nous Research, MIT) — the HRR core is kept, but it is decoupled from a single harness: it runs
in-process under Hermes, and over a local CLI under OpenCode (and any other harness). See
[What's changed](#whats-changed-vs-upstream).

- `store.py` — fact store (SQLite + FTS5), entity extraction, regex statement extraction
- `retrieval.py` — search / probe / related / reason (SQL entity graph composes, HRR ranks within set)
- `holographic.py` — HRR phase-vector algebra (numpy optional; without it, keyword mode)
- `provider.py` — Hermes `MemoryProvider` adapter (in-process plugin)
- `cli.py` — generic entrypoint for every other harness (`python3 -m tiberius.cli`)
- `opencode/` — native OpenCode V2 TypeScript plugin (wraps the CLI locally)
- `eval/` — golden-query recall harness (18 queries over a committed synthetic fixture; CI runs it on every push)

## What's changed vs upstream

- **HRR algebra fix**: fact vectors are lossy bundles of content + entity parts, so unbinding a
  phase key from a bundle is meaningless. Composition (probe/related/reason) now happens in the
  SQL entity graph; HRR only ranks within the composed set.
- **Compositional `reason()`**: facts connecting multiple named entities, ranked by shared-entity
  count — the query class the upstream could not answer at all.
- **Standalone core**: `store.py`/`retrieval.py`/`holographic.py` have no hard dependency on any
  harness (stdlib-only fallbacks; numpy optional).
- **Harness adapters**: `provider.py` (Hermes, in-process), `cli.py` (generic JSON CLI),
  `opencode/` (native OpenCode V2 plugin with per-session context injection and session-end
  extraction).
- **Quality gates**: 54 tests + an 18-query golden recall eval (`eval/`) that any retrieval
  change must not regress (fixture baseline recall@1 18/18, recall@3 18/18).

## Requirements

Python 3.11+. NumPy optional — without it the engine degrades to keyword mode (FTS5 + Jaccard;
entity composition still works via SQL, HRR reranking off).

## Hermes

```bash
hermes memory setup    # select "tiberius" + "tiberius"
```

Config in `config.yaml` under `plugins.tiberius`:

| Key | Default | Description |
|-----|---------|-------------|
| `db_path` | `$HERMES_HOME/tiberius_store.db` | SQLite database path |
| `auto_extract` | `false` | Auto-extract facts at session end |
| `default_trust` | `0.5` | Default trust score for new facts |
| `hrr_dim` | `1024` | HRR vector dimensions |

## OpenCode (native V2 plugin)

In the OpenCode container (or baked into its image):

```bash
git clone <this-repo> /opt/tiberius
pip install numpy                # optional: HRR reranking
```

`opencode.json`:

```jsonc
{
  "plugins": [{
    "package": "file:///opt/tiberius/opencode",
    "options": {
      "db": "/opt/tiberius/memory.db",      // default: ~/.tiberius/memory.db
      "pythonpath": "/opt"                  // repo parent, so package `tiberius` imports
    }
  }]
}
```

The plugin gives the agent `memory_search` / `memory_probe` / `memory_add` tools, injects the
top relevant facts into the system prompt on each session's first model call, and regex-extracts
facts from user messages when a session finishes. No cross-container traffic — the engine runs
in the same container as OpenCode.

## Any other harness

The repo dir must be importable as package `tiberius` (its parent on `PYTHONPATH`), then:

```bash
python3 -m tiberius.cli --db PATH search "query" [--top N]
python3 -m tiberius.cli --db PATH probe "entity"
python3 -m tiberius.cli --db PATH related "entity"
python3 -m tiberius.cli --db PATH reason "a" "b"
python3 -m tiberius.cli --db PATH add "fact" [--category C] [--tags a,b]
python3 -m tiberius.cli --db PATH feedback <id> helpful|unhelpful
echo '[{"role":"user","content":"..."}]' | python3 -m tiberius.cli --db PATH extract
```

DB path: `--db` > `$TIBERIUS_DB` > `~/.tiberius/memory.db`. All output is JSON.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — the one rule that matters: retrieval
changes must not regress the golden eval (fixture baseline recall@1 18/18, recall@3 18/18).
AI coding agents: [AGENTS.md](AGENTS.md).

## Development

Core tests (`test_tiberius_store`, `test_tiberius_retrieval`, `test_tiberius_cli`) need nothing
beyond Python 3.11+ and pytest; provider tests additionally import Hermes core. Retrieval
quality is gated by the golden eval: `python eval/run_eval.py` runs the committed
synthetic fixture (fixture baseline recall@1 18/18, recall@3 18/18); pass a DB path to
eval against your own live store: `python eval/run_eval.py /path/to/your.db`. CI runs
the core suite + fixture eval on every push.
