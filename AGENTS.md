# Agent instructions

You are working on Tiberius, a local SQLite memory engine. Read `CONTRIBUTING.md`
first, then `ARCHITECTURE.md` (how each module works and the invariants behind
the rules below). The invariants that actually matter:

1. **Golden eval is the gate.** Any change to `store.py`, `retrieval.py`, or
   `holographic.py` must run `python eval/run_eval.py` before and after and
   must not regress recall (fixture baseline: 18/18 @1, 18/18 @3). If a change is
   meant to move the numbers, update the PR with before/after and, if the
   change is intentional, update `eval/golden.json` in the same commit.
2. **Core stays harness-free.** `store.py` / `retrieval.py` / `holographic.py`
   must import nothing from any agent framework. Hermes-specific code lives
   in `provider.py`; everything else goes through `cli.py` (JSON in/out).
   Optional imports (numpy) degrade to a documented mode, never ImportError.
3. **HRR algebra.** Fact vectors are lossy *bundles* (content XOR entity
   parts). Unbinding a phase key from a bundle is algebraically meaningless.
   Composition (probe/related/reason) happens in the SQL entity graph; HRR
   only ranks within the composed set. Do not "fix" retrieval by unbinding.
4. **Tests.** Core suite (`test_tiberius_store`, `test_tiberius_retrieval`,
   `test_tiberius_cli`) runs anywhere: `python -m pytest -q`. Provider tests
   need a Hermes environment. Never add network calls to tests.
5. **No secrets, no network.** This is a local-memory project. Nothing here
   may phone home; do not add telemetry, update checks, or external endpoints.
6. **Commits.** Keep the author as the actual human (or their GitHub noreply
   identity). Agent-assisted work carries a `Co-authored-by:` trailer naming
   the agent, so authorship stays legible. Maintainer convention in this repo:
   `Co-authored-by: Hermes Agent <hermes@local>`.

Run the core suite and the eval before declaring any retrieval-adjacent
change done.
