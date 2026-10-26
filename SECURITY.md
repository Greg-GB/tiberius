# Security policy

Tiberius is a local memory engine: it stores, retrieves, and ranks your own
memory content in a local SQLite file. Nothing it ships talks to the
network — by design.

## Scope

Reportable issues, in priority order:

1. **Data leaving the machine** — any code path that sends fact content,
   entities, or DB paths to an external endpoint.
2. **Extraction injection** — fact or transcript content that, via the
   regex-based `extract_statements()` / entity extraction, causes writes
   beyond what the content says (e.g. crafted strings creating runaway
   entities).
3. **Path handling** — CLI `--db` or config paths that write outside the
   intended location.

## How to report

Open a GitHub issue with a `[security]` label. The project is small and
solo-maintained: expect a fast triage, not a 90-day SLA.

## Out of scope

- The agent frameworks that use Tiberius (Hermes, OpenCode) — report to
  their projects.
- Local file permissions on the DB file — it holds whatever memory the
  agent stored; treat the containing directory like any other
  sensitive-data store.
