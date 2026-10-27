"""Harness-agnostic CLI: the same core the Hermes plugin uses, callable from any harness.

Usage (repo dir importable as package ``tiberius``, i.e. its PARENT on PYTHONPATH):
  python3 -m tiberius.cli [--db PATH] search "query" [--top N] [--category C] [--min-trust T]
  python3 -m tiberius.cli [--db PATH] probe "entity"
  python3 -m tiberius.cli [--db PATH] related "entity"
  python3 -m tiberius.cli [--db PATH] reason "entity" "entity" [...]
  python3 -m tiberius.cli [--db PATH] add "fact content" [--category C] [--tags a,b]
  python3 -m tiberius.cli [--db PATH] feedback <fact_id> helpful|unhelpful
  python3 -m tiberius.cli [--db PATH] remove <fact_id>
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

from .retrieval import FactRetriever
from .store import MemoryStore, extract_statements


def _db_path(args: argparse.Namespace) -> str:
    return args.db or os.environ.get("TIBERIUS_DB") or str(Path.home() / ".tiberius" / "memory.db")


def _row(r) -> dict:
    d = dict(r) if not isinstance(r, dict) else dict(r)
    d.pop("hrr_vector", None)
    return d


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tiberius")
    p.add_argument("--db", help="SQLite path (default: $TIBERIUS_DB or ~/.tiberius/memory.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="FTS5 + rerank keyword search")
    s.add_argument("query")
    s.add_argument("--top", type=int, default=5)
    s.add_argument("--category")
    s.add_argument("--min-trust", type=float, default=0.3)
    s = sub.add_parser("probe", help="all facts linked to one entity")
    s.add_argument("entity")
    s = sub.add_parser("related", help="one hop: facts sharing any of the entity's entities")
    s.add_argument("entity")
    s = sub.add_parser("reason", help="compositional: facts linked to ALL entities")
    s.add_argument("entities", nargs="+")
    s.add_argument("--top", type=int, default=5)
    s = sub.add_parser("add", help="store one fact (entities extracted automatically)")
    s.add_argument("content")
    s.add_argument("--category", default="general")
    s.add_argument("--tags", default="")
    s = sub.add_parser("feedback", help="train trust: fact_id + helpful|unhelpful")
    s.add_argument("fact_id", type=int)
    s.add_argument("action", choices=["helpful", "unhelpful"])
    s = sub.add_parser("remove", help="delete a fact by id (entity links + FTS cleaned)")
    s.add_argument("fact_id", type=int)
    s = sub.add_parser("list", help="browse facts by trust (find fact_ids to remove)")
    s.add_argument("--category")
    s.add_argument("--min-trust", type=float, default=0.0)
    s.add_argument("--top", type=int, default=50)
    sub.add_parser("extract", help="regex-extract facts from a JSON message list on stdin")

    a = p.parse_args(argv)
    if a.cmd in ("search", "reason", "list"):
        a.top = max(1, a.top)  # SQLite LIMIT -1 means unlimited — a negative --top must not dump the store
    store = MemoryStore(db_path=_db_path(a))
    try:
        ret = FactRetriever(store)
        if a.cmd == "search":
            rows = ret.search(a.query, category=a.category, min_trust=a.min_trust, limit=a.top)
        elif a.cmd == "probe":
            rows = ret.probe(a.entity, limit=10)
        elif a.cmd == "related":
            rows = ret.related(a.entity, limit=10)
        elif a.cmd == "reason":
            rows = ret.reason(a.entities, limit=a.top)
        elif a.cmd == "add":
            print(json.dumps({"fact_id": store.add_fact(a.content, category=a.category, tags=a.tags)}))
            return 0
        elif a.cmd == "feedback":
            print(json.dumps(store.record_feedback(a.fact_id, helpful=a.action == "helpful")))
            return 0
        elif a.cmd == "remove":
            if not store.remove_fact(a.fact_id):
                # Same JSON-error contract as feedback: unknown id is an error, not a traceback.
                print(json.dumps({"error": f"fact_id {a.fact_id} not found"}))
                return 1
            print(json.dumps({"removed": a.fact_id}))
            return 0
        elif a.cmd == "list":
            rows = store.list_facts(category=a.category, min_trust=a.min_trust, limit=a.top)
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
        print(json.dumps({"results": [_row(r) for r in rows], "count": len(rows)}))
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
