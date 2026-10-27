#!/usr/bin/env python3
"""Golden-query eval: recall@1/@3 over a query set.

Usage:
  python eval/run_eval.py            # runs against the committed synthetic fixture (CI-safe)
  python eval/run_eval.py [db_path]  # or against any existing store (e.g. your live DB)

Add queries to golden.json as they come up in real use; keep expect[0] = the fact you
want #1, other acceptable IDs after it. A FAIL line is either a regression or a bad
expectation — check the printed top hit before changing the query set.

The fixture is deliberately synthetic (fictional Acme stack): this repo is public and
must never contain a real user's memory content.
"""
import json, os, sqlite3, sys, tempfile, types

HERE = os.path.dirname(os.path.abspath(__file__))
TIBERIUS_DIR = os.path.dirname(HERE)

# Load the core as an aliased package (same trick as the tests) — bare
# "store"/"retrieval" names would collide with whatever else is on PYTHONPATH.
pkg = types.ModuleType("tiberius_eval"); pkg.__path__ = [TIBERIUS_DIR]; sys.modules["tiberius_eval"] = pkg
import importlib.util as iu
for name in ("holographic", "store", "retrieval"):
    spec = iu.spec_from_file_location(f"tiberius_eval.{name}", f"{TIBERIUS_DIR}/{name}.py")
    mod = iu.module_from_spec(spec); sys.modules[f"tiberius_eval.{name}"] = mod; spec.loader.exec_module(mod)
MemoryStore = sys.modules["tiberius_eval.store"].MemoryStore
FactRetriever = sys.modules["tiberius_eval.retrieval"].FactRetriever

def run_case(r, case):
    via, q = case.get("via", "search"), case["q"]
    if via == "search":
        return r.search(q, limit=5)
    if via == "probe":
        return r.probe(q, limit=5)
    if via == "related":
        return r.related(q, limit=5)
    return r.reason(case["entities"], limit=5)  # via == "reason"

def open_store(tmp):
    if len(sys.argv) > 1:  # existing DB (live store): back it up, never mutate the original
        # SQLite backup API, not a file copy: a plain copy of a live WAL-mode DB
        # can miss the last un-checkpointed transactions.
        src = sqlite3.connect(sys.argv[1])
        dst = sqlite3.connect(tmp)
        with dst:
            src.backup(dst)
        dst.close(); src.close()
        return MemoryStore(db_path=tmp, hrr_dim=1024)
    s = MemoryStore(db_path=tmp, hrr_dim=1024)  # fresh fixture DB
    for f in json.load(open(os.path.join(HERE, "fixture.json"))):
        s.add_fact(f["content"])
    return s

def main():
    tmp = os.path.join(tempfile.gettempdir(), f"tiberius_eval_{os.getpid()}.db")
    s = open_store(tmp)
    r = FactRetriever(s)
    hits1 = hits3 = 0
    for case in json.load(open(os.path.join(HERE, "golden.json"))):
        via, expect = case.get("via", "search"), case["expect"]
        res = run_case(r, case)
        ids = [f["fact_id"] for f in res]
        top = ids[0] if ids else None
        h1, h3 = top == expect[0], any(e in ids[:3] for e in expect)
        hits1 += h1; hits3 += h3
        mark = "ok  " if h1 else ("~   " if h3 else "FAIL")
        print(f"{mark} [{via:<7}] {case['q'][:42]!r:45} -> {top} (want {expect[0]})"
              + ("" if h3 else f"  top3={ids[:3]}"))
        if not h3:
            print(f"       hits: {[(f['fact_id'], f['content'][:60]) for f in res[:3]]}")
    n = len(json.load(open(os.path.join(HERE, "golden.json"))))
    print(f"\nrecall@1 {hits1}/{n}   recall@3 {hits3}/{n}")
    s.close()
    os.unlink(tmp)

if __name__ == "__main__":
    main()
