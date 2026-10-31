"""Harness-agnostic CLI: the same core the Hermes plugin uses, callable from any harness.

Usage (repo dir importable as package ``tiberius``, i.e. its PARENT on PYTHONPATH):
  python3 -m tiberius.cli [--db PATH] search "query" [--top N] [--category C] [--min-trust T]
  python3 -m tiberius.cli [--db PATH] brief "query" [--budget N] [--floor T] [--top N]
  python3 -m tiberius.cli [--db PATH] drift "project" [--since S] [--budget N] [--top N] [--support T]
  python3 -m tiberius.cli [--db PATH] probe "entity" [entity ...]
  python3 -m tiberius.cli [--db PATH] related "entity"
  python3 -m tiberius.cli [--db PATH] reason "entity" "entity" [...]
  python3 -m tiberius.cli [--db PATH] contradict [--category C] [--threshold T] [--top N]
  python3 -m tiberius.cli [--db PATH] add "fact content" [--category C] [--tags a,b]
  echo '["fact one", {"content": "fact two", "category": "project"}]' | python3 -m tiberius.cli [--db PATH] add-batch
  python3 -m tiberius.cli [--db PATH] feedback <fact_id|id,id,...> helpful|unhelpful
  python3 -m tiberius.cli [--db PATH] remove <fact_id>
  python3 -m tiberius.cli [--db PATH] update <fact_id> [--content TEXT] [--tags a,b] [--category C] [--trust-delta T]
  python3 -m tiberius.cli [--db PATH] list [--category C] [--min-trust T] [--since S] [--top N] [--order trust|retrieval|recent|stale]
  python3 -m tiberius.cli [--db PATH] stats
  python3 -m tiberius.cli [--db PATH] skill-sync --dir DIR [--dir DIR ...] [--prune]
  echo '[{"role":"user","content":"..."}]' | python3 -m tiberius.cli [--db PATH] extract

DB path: --db > $TIBERIUS_DB > ~/.tiberius/memory.db. All output is JSON on stdout.
numpy is optional: without it the engine degrades to keyword mode (FTS5 + Jaccard,
entity composition still via SQL; HRR reranking off).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from .brief import brief
from .drift import drift
from .retrieval import FactRetriever
from .store import MemoryStore, extract_statements


def _db_path(args: argparse.Namespace) -> str:
    return args.db or os.environ.get("TIBERIUS_DB") or str(Path.home() / ".tiberius" / "memory.db")


def _row(r) -> dict:
    d = dict(r) if not isinstance(r, dict) else dict(r)
    d.pop("hrr_vector", None)
    return d


def _unquote(value: str) -> str:
    """Strip one pair of matching surrounding quotes (frontmatter values)."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_skill_frontmatter(text: str) -> dict[str, str]:
    """SKILL.md frontmatter (the ``---``-fenced head) as the YAML subset skills actually
    use: single-line ``key: value`` with optional surrounding quotes, and ``>``/``|`` block
    scalars (folded/literal, optional chomping indicator) for multi-line descriptions.
    Returns {} when there is no (properly closed) frontmatter — callers skip the skill."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    closed = False
    i = 1
    while i < len(lines):
        line = lines[i]
        if line.strip() in ("---", "..."):  # closing fence
            closed = True
            break
        i += 1
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or line[0] in " \t":
            continue  # blank, comment, or a line this subset does not model
        key, sep, value = line.partition(":")
        if not sep or not key.strip():
            continue
        key, value = key.strip(), value.strip()
        if value in (">", "|", ">-", "|-", ">+", "|+"):
            block = []
            while i < len(lines) and (not lines[i].strip() or lines[i][0] in " \t"):
                block.append(lines[i].strip())
                i += 1
            joiner = " " if value.startswith(">") else "\n"  # folded vs literal
            fields[key] = joiner.join(b for b in block if b).strip()
            continue
        fields[key] = _unquote(value)
    return fields if closed else {}


def _parse_triggers(raw: str) -> list[str]:
    """Optional ``triggers:`` frontmatter value -> tag list. Comma-separated or a YAML flow
    list [a, b, c]; items are stripped of whitespace and quotes. Empty value -> []."""
    raw = _unquote(raw.strip())
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [t.strip() for t in raw.split(",") if t.strip()]


# "all" for skill-fact listing: far beyond any realistic personal skill catalog.
_SYNC_ALL_LIMIT = 100_000

# Walk pruning mirrors the Hermes core's skill index (agent/skill_utils.py): hidden/excluded
# dirs are never skill roots, and a skill root's support dirs hold files, not skills.
_EXCLUDED_SKILL_DIRS = frozenset((
    ".git", ".github", ".hub", ".archive", ".curator_backups", ".locks",
    ".venv", "venv", "node_modules", "site-packages", "__pycache__",
    ".tox", ".nox", ".pytest_cache", ".mypy_cache", ".ruff_cache",
))
_SKILL_SUPPORT_DIRS = frozenset(("references", "templates", "assets", "scripts"))


def _iter_skill_dirs(base: Path) -> list[Path]:
    """Skill dirs (dirs holding a SKILL.md) under *base*, sorted walk order. Recursive:
    flat layouts (<base>/<skill>/SKILL.md) and category-nested ones
    (<base>/<category>/<skill>/SKILL.md — the Hermes profile layout) both work. The base
    itself is never a skill (a --dir is a dir OF skills, as before)."""
    found: list[Path] = []
    for root, subdirs, files in os.walk(base):
        if "SKILL.md" in files:
            subdirs[:] = [d for d in subdirs if d not in _SKILL_SUPPORT_DIRS]
        subdirs[:] = sorted(d for d in subdirs
                            if not d.startswith(".") and d not in _EXCLUDED_SKILL_DIRS)
        if root != str(base) and "SKILL.md" in files:
            found.append(Path(root))
    return found


def _skill_sync(args: argparse.Namespace, store: MemoryStore) -> dict:
    """Upsert one fact per skill (category 'skill') from --dir directories containing
    <skill-name>/SKILL.md subdirectories (scanned recursively, including category-nested
    layouts; a base dir itself is never treated as a skill). Content is '<name>:
    <description>' (name from frontmatter, falling back to the directory name), tags are
    'skill-id:<name>', 'skill-dir:<abs path>' plus any optional 'triggers:' frontmatter
    entries. Idempotent: a fact whose content and tags are unchanged is left alone. With
    --prune, skill facts carrying a skill-id tag whose id no longer appears in the given
    dirs are removed — pass the COMPLETE dir list; unmanaged facts (no skill-id tag) are
    never touched."""
    dirs = []
    skipped = []
    for d in args.dir:
        p = Path(d).expanduser().resolve()
        if not p.is_dir():
            # A partially-bad dir list syncs the good dirs and reports the bad ones; only an
            # entirely missing list is a hard error (CLI contract: JSON error, exit 1).
            skipped.append({"skill": str(p), "reason": "dir not found"})
            continue
        dirs.append(p)
    if not dirs:
        raise ValueError(f"skill dir not found: {args.dir[0]}")  # nothing to sync

    skills: list[dict] = []
    for base in dirs:
        for entry in _iter_skill_dirs(base):
            skill_md = entry / "SKILL.md"
            try:
                fm = _parse_skill_frontmatter(skill_md.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as exc:
                skipped.append({"skill": entry.name, "reason": f"unreadable SKILL.md: {exc}"})
                continue
            name = (fm.get("name") or entry.name).strip()
            description = (fm.get("description") or "").strip()
            if not description:
                skipped.append({"skill": name, "dir": str(entry),
                                "reason": "missing or empty description (frontmatter 'description:' key)"})
                continue
            skills.append({"name": name, "description": description,
                           "triggers": _parse_triggers(fm.get("triggers", "")),
                           "dir": str(entry)})

    # Existing skill facts keyed by their skill-id tag (the upsert key).
    by_id: dict[str, dict] = {}
    for row in store.list_facts(category="skill", limit=_SYNC_ALL_LIMIT):
        for tag in row["tags"].split(","):
            if tag.strip().startswith("skill-id:"):
                by_id[tag.strip()[len("skill-id:"):]] = row
                break

    added: list[dict] = []
    updated: list[dict] = []
    unchanged = 0
    synced_ids: set[str] = set()
    for sk in skills:
        synced_ids.add(sk["name"])
        content = f"{sk['name']}: {sk['description']}"
        tags = ", ".join([f"skill-id:{sk['name']}", f"skill-dir:{sk['dir']}"] + sk["triggers"])
        existing = by_id.get(sk["name"])
        if existing is None:
            fact_id = store.add_fact(content, category="skill", tags=tags)
            by_id[sk["name"]] = {"fact_id": fact_id, "content": content, "tags": tags}
            added.append({"skill": sk["name"], "fact_id": fact_id})
        elif existing["content"] == content and existing["tags"] == tags:
            unchanged += 1
        else:
            # Same content keeps entities/vector; a changed content re-extracts and re-embeds
            # inside update_fact. Tags-only changes (triggers) touch only the FTS-indexed column.
            store.update_fact(existing["fact_id"], content=content, tags=tags)
            by_id[sk["name"]] = {"fact_id": existing["fact_id"], "content": content, "tags": tags}
            updated.append({"skill": sk["name"], "fact_id": existing["fact_id"]})

    pruned: list[dict] = []
    if args.prune:
        for name, row in by_id.items():
            if name not in synced_ids and store.remove_fact(row["fact_id"]):
                pruned.append({"skill": name, "fact_id": row["fact_id"]})

    return {"dirs": [str(d) for d in dirs], "skills": len(skills),
            "added": added, "updated": updated, "unchanged": unchanged,
            "pruned": pruned, "skipped": skipped}


# brief: the token-budgeted context pack (design: docs/brief.md) lives in
# brief.py — the shared implementation the CLI subcommand, the OpenCode
# plugin tool, and the Hermes provider action all wrap.


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tiberius")
    p.add_argument("--db", help="SQLite path (default: $TIBERIUS_DB or ~/.tiberius/memory.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="FTS5 + rerank keyword search")
    s.add_argument("query")
    s.add_argument("--top", type=int, default=5)
    s.add_argument("--category")
    s.add_argument("--min-trust", type=float, default=0.3)
    s.add_argument("--since", help="only facts created at or after: ISO (YYYY-MM-DD[ HH:MM:SS] UTC) or relative (30m/2h/7d/2w)")
    s = sub.add_parser("brief", help="token-budgeted context pack: search ∪ probe, floored, budget-filled")
    s.add_argument("query")
    s.add_argument("--budget", type=int, default=300, help="token budget, char/4 estimate (default 300)")
    s.add_argument("--floor", type=float, default=0.10, help="min search-pool score; entity-only facts bypass (default 0.10)")
    s.add_argument("--top", type=int, default=20, help="candidate pool size per search/probe (default 20)")
    s.add_argument("--category", help="restrict to one fact category")
    s.add_argument("--min-trust", type=float, default=0.3, help="min trust for the search pool (default 0.3)")
    s.add_argument("--since", help="window the search pool to facts created at or after: ISO (YYYY-MM-DD[ HH:MM:SS] UTC) or relative (30m/2h/7d/2w)")
    s.add_argument("--skills", type=int, default=2, help="max skill-lane entries (default 2)")
    s = sub.add_parser("drift", help="two-sided plan/state pack: plan claims vs state since the anchor, echo markers")
    s.add_argument("project")
    s.add_argument("--since", help="state window start: ISO (YYYY-MM-DD[ HH:MM:SS] UTC) or relative (30m/2h/7d/2w); default = oldest plan fact")
    s.add_argument("--budget", type=int, default=300, help="token budget, char/4 estimate (default 300)")
    s.add_argument("--top", type=int, default=20, help="candidate pool size per state-side recall (default 20)")
    s.add_argument("--support", type=float, default=0.15, help="min overlap coefficient for a state echo to count (default 0.15)")
    s = sub.add_parser("probe", help="all facts linked to one entity (several names: union, any of them)")
    s.add_argument("entity", nargs="+", help="one or more entity names (several = union: facts linked to any of them; the OR counterpart of 'reason')")
    s = sub.add_parser("show", help="read one fact by id (content, entities, trust, counts)")
    s.add_argument("fact_id", type=int)
    s = sub.add_parser("entities", help="list entities with linked-fact counts and aliases (entity discovery)")
    s.add_argument("--top", type=int, default=20)
    s = sub.add_parser("related", help="one hop: facts sharing any of the entity's entities")
    s.add_argument("entity")
    s = sub.add_parser("reason", help="compositional: facts linked to ALL entities")
    s.add_argument("entities", nargs="+")
    s.add_argument("--top", type=int, default=5)
    s = sub.add_parser("contradict", help="pairs of entity-sharing facts in tension (store hygiene scan)")
    s.add_argument("--category")
    s.add_argument("--threshold", type=float, default=0.3)
    s.add_argument("--top", type=int, default=10)
    s = sub.add_parser("add", help="store one fact (entities extracted automatically)")
    s.add_argument("content")
    s.add_argument("--category", default="general")
    s.add_argument("--tags", default="")
    s = sub.add_parser("add-batch", help="store many facts from a JSON array on stdin (strings or {content, category?, tags?} objects)")
    s = sub.add_parser("feedback", help="train trust: fact_id + helpful|unhelpful (fact_id may be a comma list: 5,6,7)")
    s.add_argument("fact_id", help="fact id, or comma-separated ids for a batch (5 or 5,6,7)")
    s.add_argument("action", choices=["helpful", "unhelpful"])
    s = sub.add_parser("remove", help="delete a fact by id (entity links + FTS cleaned)")
    s.add_argument("fact_id", type=int)
    s = sub.add_parser("update", help="partially update a fact: --content / --tags / --category / --trust-delta")
    s.add_argument("fact_id", type=int)
    s.add_argument("--content", help="replace the content (entities re-extracted, HRR vector rebuilt)")
    s.add_argument("--tags", help="replace the comma-separated tags")
    s.add_argument("--category", help="replace the category")
    s.add_argument("--trust-delta", type=float, help="nudge trust_score by this amount (clamped to 0..1)")
    s = sub.add_parser("skill-sync", help="upsert skill facts (category 'skill') from <skill>/SKILL.md dirs")
    s.add_argument("--dir", action="append", required=True, metavar="DIR",
                   help="a directory of <skill-name>/SKILL.md subdirs (repeatable; pass the full list)")
    s.add_argument("--prune", action="store_true",
                   help="remove skill facts (those with a skill-id tag) whose id no longer appears in the given dirs")
    s = sub.add_parser("list", help="browse facts (find fact_ids to remove/update)")
    s.add_argument("--category")
    s.add_argument("--min-trust", type=float, default=0.0)
    s.add_argument("--since", help="only facts created at or after: ISO (YYYY-MM-DD[ HH:MM:SS] UTC) or relative (30m/2h/7d/2w)")
    s.add_argument("--top", type=int, default=50)
    s.add_argument("--order", choices=["trust", "retrieval", "recent", "stale"], default="trust",
                   help="ordering: trust (default), retrieval (most-recalled first), recent (newest first), stale (oldest-updated first)")
    sub.add_parser("stats", help="store orientation: totals, category mix, trust buckets, stale count")
    sub.add_parser("extract", help="regex-extract facts from a JSON message list on stdin")

    a = p.parse_args(argv)
    if a.cmd in ("search", "brief", "reason", "contradict", "list"):
        a.top = max(1, a.top)  # SQLite LIMIT -1 means unlimited — a negative --top must not dump the store
    store = MemoryStore(db_path=_db_path(a))
    try:
        if a.cmd == "skill-sync":
            # Store-only operation (no retrieval) — runs before the FactRetriever is built.
            # A missing dir raises ValueError, surfaced by the shared JSON-error contract below.
            print(json.dumps(_skill_sync(a, store)))
            return 0
        ret = FactRetriever(store)
        if a.cmd == "brief":
            a.budget, a.floor, a.skills = max(1, a.budget), max(0.0, a.floor), max(0, a.skills)
            print(json.dumps(brief(a.query, ret, store, budget=a.budget, floor=a.floor,
                                   top=a.top, category=a.category, min_trust=a.min_trust,
                                   since=a.since, skills=a.skills)))
            return 0
        if a.cmd == "drift":
            a.budget, a.top, a.support = max(1, a.budget), max(1, a.top), max(0.0, a.support)
            print(json.dumps(drift(a.project, ret, store, since=a.since, budget=a.budget,
                                   top=a.top, support=a.support)))
            return 0
        total = None
        if a.cmd == "search":
            rows = ret.search(a.query, category=a.category, min_trust=a.min_trust, limit=a.top, since=a.since)
            total = ret.match_count(a.query, category=a.category, min_trust=a.min_trust, since=a.since)
        elif a.cmd == "probe":
            rows = ret.probe(a.entity, limit=10)
        elif a.cmd == "show":
            fact = store.get_fact(a.fact_id)
            if fact is None:
                print(json.dumps({"error": f"fact_id {a.fact_id} not found"}))  # same contract as remove/feedback
                return 1
            print(json.dumps({"fact": fact}))
            return 0
        elif a.cmd == "related":
            rows = ret.related(a.entity, limit=10)
        elif a.cmd == "reason":
            rows = ret.reason(a.entities, limit=a.top)
        elif a.cmd == "contradict":
            rows = ret.contradict(category=a.category, threshold=a.threshold, limit=a.top)
        elif a.cmd == "add":
            new_id = store.add_fact(a.content, category=a.category, tags=a.tags)
            out: dict = {"fact_id": new_id}
            dups = ret.find_duplicates(a.content, new_id)
            if dups:  # supplemental field: omitted when empty, note when present (see ARCHITECTURE.md)
                out["possible_duplicates"] = dups
                out["note"] = (f"{len(dups)} near-identical existing fact(s) — if one is a true duplicate, "
                               "remove the older fact_id and keep this one.")
            print(json.dumps(out))
            return 0
        elif a.cmd == "add-batch":
            items = json.loads(sys.stdin.read())
            if not isinstance(items, list) or not items:
                raise ValueError("add-batch expects a non-empty JSON array on stdin")
            print(json.dumps(store.add_facts(items)))
            return 0
        elif a.cmd == "feedback":
            ids = [int(x) for x in str(a.fact_id).split(",") if x.strip()]
            if len(ids) == 1:
                print(json.dumps(store.record_feedback(ids[0], helpful=a.action == "helpful")))
            else:
                results = store.record_feedbacks(ids, helpful=a.action == "helpful")
                print(json.dumps({"applied": sum(1 for r in results if r.get("ok")), "results": results}))
            return 0
        elif a.cmd == "remove":
            if not store.remove_fact(a.fact_id):
                # Same JSON-error contract as feedback: unknown id is an error, not a traceback.
                print(json.dumps({"error": f"fact_id {a.fact_id} not found"}))
                return 1
            print(json.dumps({"removed": a.fact_id}))
            return 0
        elif a.cmd == "update":
            if a.content is None and a.tags is None and a.category is None and a.trust_delta is None:
                # A bare update would only touch updated_at — an invisible write the caller almost
                # certainly did not mean. (The store method stays permissive for the provider path.)
                print(json.dumps({"error": "nothing to update: pass --content, --tags, --category, or --trust-delta"}))
                return 1
            if not store.update_fact(a.fact_id, content=a.content, trust_delta=a.trust_delta, tags=a.tags, category=a.category):
                print(json.dumps({"error": f"fact_id {a.fact_id} not found"}))  # same contract as remove/feedback
                return 1
            print(json.dumps({"updated": a.fact_id}))
            return 0
        elif a.cmd == "stats":
            print(json.dumps(store.stats()))
            return 0
        elif a.cmd == "list":
            rows = store.list_facts(category=a.category, min_trust=a.min_trust, limit=a.top, since=a.since,
                                    order=a.order)
        elif a.cmd == "entities":
            # Output contract (docs/entities.md): a bare JSON array — one object per entity,
            # not the {"results": ...} wrapper the fact-browsing commands use.
            print(json.dumps(store.list_entities(top=a.top)))
            return 0
        else:  # extract
            raw = sys.stdin.read()
            try:
                msgs = json.loads(raw)
            except json.JSONDecodeError:
                msgs = [line for line in raw.splitlines() if line.strip()]
            if not isinstance(msgs, list):
                msgs = [msgs]
            added = 0
            for m in msgs:
                if isinstance(m, str):
                    text = m
                elif isinstance(m, dict) and m.get("role") == "user":
                    text = m.get("content") or ""
                else:
                    continue
                for statement, category in extract_statements(text):
                    try:
                        store.add_fact(statement, category=category)
                        added += 1
                    except Exception:
                        pass  # UNIQUE content collision or similar — extraction is best-effort
            print(json.dumps({"added": added}))
            return 0
        out: dict = {"results": [_row(r) for r in rows], "count": len(rows)}
        if total is not None:  # search only: full pool size, so callers can tell a window from the whole
            out["total"] = total
        print(json.dumps(out))
        return 0
    except (KeyError, ValueError, sqlite3.Error) as exc:
        # Unknown fact_id (feedback), empty add content, and sqlite-level
        # failures (locked/corrupt DB) surface as a JSON error, not a
        # traceback — the CLI contract is JSON on stdout, exit 1 on error.
        print(json.dumps({"error": str(exc)}))
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
