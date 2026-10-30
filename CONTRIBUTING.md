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

- **Core** tests run anywhere with pytest alone: `python -m pytest -q`.
- **Provider** tests import Hermes core (`agent`, `hermes_cli`, ...) and only
  run in an environment where Hermes is installed; `tests/conftest.py` skips
  their collection outside one, so the plain command above stays green.
- **Provider tests are not part of CI** (their dependency is not available
  in public CI) — a green CI build does not cover `provider.py`. Provider
  changes are gated by a local run in a Hermes environment: `python -m
  pytest -q` there (the `HERMES_AGENT_REPO` recipe is in the
  `tests/conftest.py` docstring).

## Quality gate (non-negotiable for retrieval changes)

Any change touching `retrieval.py`, `store.py`, or `holographic.py` must run
the golden-query eval before and after and must not regress recall:

```bash
python eval/run_eval.py            # synthetic fixture; baseline: recall@1 19/19, recall@3 19/19
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

Conventional-ish, imperative subject, body explaining the why. Normal
commits — this repo has no `Co-authored-by:` trailer convention.

## Branches and releases

- `main` is the trunk; short-lived feature branches per PR. No long-lived
  dev branch — this is a solo-maintained repo, not a release train.
- `main` is protected by the `protect-main` ruleset: changes land via PR
  with a green `tests` check. The owner can bypass the ruleset by default;
  that is the approval gate.
- Releases are `vX.Y.Z` tags cut from `main` (semver: fix = patch, new
  command/adapter = minor, DB schema change = major). Pushing the tag
  auto-publishes the GitHub Release with commit-generated notes
  (`.github/workflows/release.yml`) — no UI clicks. Release notes act as
  the changelog; there is no separate CHANGELOG file.
- Bump the version in **all** places together at release time — one semantic
  version across four files: `pyproject.toml`, `plugin.yaml`,
  `opencode/package.json`, and `opencode/package-lock.json` (two spots in
  the lockfile: the top-level `version` and the `packages."."` entry). The
  release workflow only guards tag ↔ `pyproject.toml` — the rest are on you
  at bump time.
- A release PR is **not** version-bump-only. In the same commit it also
  lands:
  - the feature design doc's `Status:` line, flipped from `unreleased` to
    `shipped — vX.Y.Z (YYYY-MM-DD)`;
  - the README refresh: tool count and tool lists, the runtime-verified
    line (with the date it was verified live), a CLI example line for any
    new subcommand, and the "Full operation parity" surface list;
  - the `ARCHITECTURE.md` refresh: provider action list, CLI command list,
    runtime-verification clause;
  - the `skills/tiberius-usage/SKILL.md` refresh: description and a usage
    bullet for any new tool.

  Doc staleness is not CI-catchable — this checklist is the gate. v0.6.1
  (#40) was a version-bump-only release and all four items above had to be
  backfilled in a post-release docs PR (#41).

## Adding a new harness

Add an adapter, don't fork the core: one file (or small dir) that talks to
`cli.py` (or imports the core directly if the harness is Python), plus a
section in the README. The golden eval applies to the core, not the adapter.
