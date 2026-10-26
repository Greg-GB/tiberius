# Contributing

Thanks for taking a look. Tiberius is a local memory engine — nothing here
talks to the network, and no test should make it do so.

## Quickstart

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install pytest            # required
pip install "numpy>=1.26,<3"  # optional: HRR reranking (keyword mode without it)
python -m pytest -q
```

Python 3.11+.

## Tests

- **Core** (`test_tiberius_store`, `test_tiberius_retrieval`, `test_tiberius_cli`)
  run anywhere with pytest alone.
- **Provider** tests import Hermes core (`agent`, `hermes_cli`, ...) and only
  run in an environment where Hermes is installed.

## Quality gate (non-negotiable for retrieval changes)

Any change touching `retrieval.py`, `store.py`, or `holographic.py` must run
the golden-query eval before and after and must not regress recall:

```bash
python eval/run_eval.py            # synthetic fixture; baseline: recall@1 18/18, recall@3 18/18
```

Report the before/after numbers in the PR. If a change *is meant* to move the
numbers, say which direction and why, and update `eval/golden.json` in the
same PR.

## Architecture in one paragraph

`store.py` / `retrieval.py` / `holographic.py` are the harness-agnostic core
(stdlib + optional numpy). `provider.py` is the in-process Hermes adapter,
`cli.py` the generic JSON entrypoint, and `opencode/` a native OpenCode V2
plugin that shells out to the CLI locally. Keep the core free of harness
imports — that decoupling is the point of the repo. Note: fact vectors are
lossy HRR *bundles*; composition happens in the SQL entity graph, HRR only
ranks within the composed set. Don't "fix" retrieval by unbinding from a
bundle.

## Commits

Conventional-ish, imperative subject, body explaining the why. Agent-assisted
commits carry a `Co-authored-by: Hermes Agent <hermes@local>` trailer — keep
that pattern so authorship stays legible.

## Adding a new harness

Add an adapter, don't fork the core: one file (or small dir) that talks to
`cli.py` (or imports the core directly if the harness is Python), plus a
section in the README. The golden eval applies to the core, not the adapter.
