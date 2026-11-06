"""Cross-store recall (docs/cross-store.md): provider-level merge for search/probe.

Hermes-only: imports the provider (registered in tests/conftest.py).
"""
import json
import os
import sqlite3
from pathlib import Path

from hermes_plugin_tiberius import TiberiusMemoryProvider
from hermes_plugin_tiberius import store as tstore


def _provider(tmp_path, read_stores=None, **extra):
    config = {"db_path": str(tmp_path / "primary.db"), "auto_extract": "false"}
    if read_stores is not None:
        config["read_stores"] = read_stores
    config.update(extra)
    p = TiberiusMemoryProvider(config=config)
    p.initialize(session_id="t")
    return p


def _add(p, content, category=None):
    args = {"action": "add", "content": content}
    if category is not None:
        args["category"] = category
    out = json.loads(p.handle_tool_call("fact_store", args))
    assert "error" not in out, out
    return out


def _search(p, query, limit=20, **extra):
    args = {"action": "search", "query": query, "limit": limit}
    args.update(extra)
    out = json.loads(p.handle_tool_call("fact_store", args))
    assert "error" not in out, out
    return out["results"]


def test_search_spans_read_stores(tmp_path):
    shared = tmp_path / "shared.db"
    s = tstore.MemoryStore(db_path=shared)
    s.add_fact("Zorbax widget assemblies ship on Fridays", category="project")
    s.add_fact("The Zorbax vendor invoice lands in June", category="project")
    s.close()
    p = _provider(tmp_path, read_stores=[str(shared)])
    _add(p, "Zorbax primary-side note about the staging window", category="project")
    facts = _search(p, "zorbax")
    # Union of both stores; strictly greater than either alone.
    assert len(facts) == 3
    assert {f.get("store") for f in facts} == {"primary", str(shared)}
    assert len({(f.get("store"), f["fact_id"]) for f in facts}) == 3
    p.shutdown()


def test_single_store_output_untagged(tmp_path):
    p = _provider(tmp_path)
    _add(p, "Single store fact about quorums")
    facts = _search(p, "quorums")
    assert len(facts) == 1
    assert "store" not in facts[0]
    p.shutdown()


def test_bad_read_store_excluded(tmp_path):
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    p = _provider(tmp_path, read_stores=[str(tmp_path / "missing.db"), str(empty)])
    assert p._read_stores == []
    _add(p, "Primary fact while read stores are broken")
    facts = _search(p, "primary")
    assert len(facts) == 1
    assert "store" not in facts[0]  # one contributing store only: untagged
    p.shutdown()


def test_read_stores_string_form_and_home_expansion(tmp_path):
    home = Path(os.environ["HERMES_HOME"])
    shared = home / "shared2.db"
    s = tstore.MemoryStore(db_path=shared)
    s.add_fact("Homestead shared fact about beacons")
    s.close()
    p = _provider(tmp_path, read_stores="$HERMES_HOME/shared2.db")
    _add(p, "Primary beacon note for the merge check")
    facts = _search(p, "beacon")
    assert len(facts) == 2
    assert {f["store"] for f in facts} == {"primary", "$HERMES_HOME/shared2.db"}
    p.shutdown()


def test_probe_spans_read_stores(tmp_path):
    shared = tmp_path / "shared.db"
    s = tstore.MemoryStore(db_path=shared)
    s.add_fact("Gryphon found the acme beacon", category="project")
    s.close()
    p = _provider(tmp_path, read_stores=[str(shared)])
    _add(p, "Gryphon shipped the staging run", category="project")
    out = json.loads(p.handle_tool_call("fact_store", {"action": "probe", "entity": "Gryphon"}))
    assert "error" not in out, out
    facts = out["results"]
    assert len(facts) == 2
    assert {f.get("store") for f in facts} == {"primary", str(shared)}
    p.shutdown()


def test_merged_recall_does_not_write_read_store(tmp_path):
    shared = tmp_path / "shared.db"
    s = tstore.MemoryStore(db_path=shared)
    s.add_fact("Ledger audit entry for the zorbax cycle")
    s.close()
    c = sqlite3.connect(shared)
    rc_before = c.execute("select coalesce(sum(retrieval_count), 0) from facts").fetchone()[0]
    pools_before = c.execute("select count(*) from pool_stats").fetchone()[0]
    c.close()
    p = _provider(tmp_path, read_stores=[str(shared)])
    _add(p, "Zorbax primary cycle note")
    _search(p, "zorbax")
    c = sqlite3.connect(shared)
    rc_after = c.execute("select coalesce(sum(retrieval_count), 0) from facts").fetchone()[0]
    pools_after = c.execute("select count(*) from pool_stats").fetchone()[0]
    c.close()
    assert rc_after == rc_before == 0  # read store untouched by merged recall
    assert pools_after == pools_before
    p.shutdown()


def test_filters_apply_across_stores(tmp_path):
    shared = tmp_path / "shared.db"
    s = tstore.MemoryStore(db_path=shared)
    s.add_fact("Zorbax billing fact from the shared store", category="billing")
    s.close()
    p = _provider(tmp_path, read_stores=[str(shared)])
    _add(p, "Zorbax deployment fact from the primary store", category="ops")
    assert len(_search(p, "zorbax")) == 2
    facts = _search(p, "zorbax", category="ops")
    assert len(facts) == 1
    assert facts[0]["store"] == "primary"
    p.shutdown()
