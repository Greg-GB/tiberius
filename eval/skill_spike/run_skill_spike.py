#!/usr/bin/env python3
"""Phase 0 skill-retrieval spike (see docs/skill-retrieval.md §6).

Question: can the existing FTS5 + entity-expansion pipeline retrieve skill
descriptions from natural user messages, with zero core changes?

Drives the REAL CLI surface (subprocess), one fresh throwaway DB per run.
Not part of the golden eval; never touches eval/golden.json.

Usage:
    python eval/skill_spike/run_skill_spike.py [--catalog PATH] [--db PATH]
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GO_THRESHOLD = 0.80  # recall@5 go/no-go from the plan


def run_cli(db: str, *args: str) -> dict:
    env = dict(os.environ, PYTHONPATH=str(REPO.parent))
    r = subprocess.run(
        [sys.executable, "-m", "tiberius.cli", "--db", db, *args],
        capture_output=True, text=True, env=env, cwd=str(REPO),
    )
    if r.returncode != 0:
        raise RuntimeError(f"cli {' '.join(args[:3])} failed: {r.stderr.strip()}")
    return json.loads(r.stdout)


def skill_id_of(content: str) -> str:
    return content.split(":", 1)[0]


def sync_skills(db: str, catalog: dict, use_triggers: bool = True) -> dict[str, int]:
    ids = {}
    for sk in catalog["skills"]:
        content = f"{sk['id']}: {sk['description']}"
        tags = [f"skill-id:{sk['id']}"]
        if use_triggers:
            tags.extend(sk.get("triggers", []))
        out = run_cli(db, "add", content, "--category", "skill",
                      "--tags", ", ".join(tags))
        ids[sk["id"]] = out["fact_id"]
    return ids


def search_recall(db: str, catalog: dict) -> list[dict]:
    rows = []
    for q in catalog["queries"]:
        out = run_cli(db, "search", q["query"], "--category", "skill", "--top", "5")
        results = out.get("results", [])
        ranked = [skill_id_of(r["content"]) for r in results]
        target = q["skill"]
        rows.append({
            "target": target,
            "query": q["query"],
            "ranked": ranked,
            "r1": ranked[:1] == [target],
            "r5": target in ranked,
        })
    return rows


def reason_probes(db: str, catalog: dict) -> list[dict]:
    """For each skill: pick two of its extracted entities, run reason, does it come back?"""
    con = sqlite3.connect(db)
    rows = []
    for sk in catalog["skills"]:
        ents = [r[0] for r in con.execute(
            "SELECT e.name FROM entities e "
            "JOIN fact_entities fe ON fe.entity_id = e.entity_id "
            "JOIN facts f ON f.fact_id = fe.fact_id "
            "WHERE f.content LIKE ? ORDER BY e.entity_id LIMIT 2",
            (sk["id"] + ":%",),
        )]
        if len(ents) < 2:
            rows.append({"target": sk["id"], "entities": ents, "hit": None})
            continue
        out = run_cli(db, "reason", ents[0], ents[1], "--top", "5")
        ranked = [skill_id_of(r["content"]) for r in out.get("results", [])]
        rows.append({
            "target": sk["id"], "entities": ents,
            "hit": sk["id"] in ranked,
        })
    con.close()
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default=str(Path(__file__).with_name("catalog.json")))
    ap.add_argument("--db", default="/tmp/opencode/skill-spike.db")
    ap.add_argument("--no-triggers", action="store_true",
                    help="ignore the optional 'triggers' field (run 1 = description-only surface)")
    a = ap.parse_args()

    catalog = json.loads(Path(a.catalog).read_text())
    if os.path.exists(a.db):
        os.remove(a.db)

    sync_skills(a.db, catalog, use_triggers=not a.no_triggers)
    rows = search_recall(a.db, catalog)
    probes = reason_probes(a.db, catalog)

    r5 = sum(r["r5"] for r in rows)
    r1 = sum(r["r1"] for r in rows)
    n = len(rows)

    print(f"{'TARGET':<18} {'R1':<3} {'R5':<3} TOP-5 RANKED")
    for r in rows:
        flag = "  " if r["r5"] else "!!"
        print(f"{r['target']:<18} {'ok' if r['r1'] else '-':<3} {'ok' if r['r5'] else 'MISS':<3} {flag} "
              + " > ".join(r["ranked"][:5]))

    print(f"\nrecall@5 = {r5}/{n} ({r5 / n:.0%})   recall@1 = {r1}/{n} ({r1 / n:.0%})")
    print(f"threshold: recall@5 >= {GO_THRESHOLD:.0%}  ->  "
          f"{'GO' if r5 / n >= GO_THRESHOLD else 'NO-GO: study miss patterns first'}")

    print("\ncompositional probes (reason <e1> <e2> -> target in top-5):")
    for p in probes:
        if p["hit"] is None:
            print(f"  {p['target']:<18} skipped (entities: {p['entities']})")
        else:
            mark = "ok" if p["hit"] else "miss"
            print(f"  {p['target']:<18} {mark:<4} entities={p['entities']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
