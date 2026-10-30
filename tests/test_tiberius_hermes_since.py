"""Hermes provider since windows on search/brief (docs/since.md).

Hermes-environment only (the provider imports Hermes core) — runs where the
other provider tests run (the accepted CI gap; see CONTRIBUTING.md). Pins the
provider wiring for the since parameter: the schema surface ('search'/'brief' in the
since description) and dispatch of the since argument on both recall actions.
The shared store/retrieval/brief modules are covered by the core tests.
Synthetic fixtures only (AcmeWidget — no real store content).
"""
from __future__ import annotations

import json

from hermes_plugin_tiberius import TiberiusMemoryProvider


def _provider(tmp_path):
    config = {"db_path": str(tmp_path / "m.db"), "auto_extract": "false"}
    return TiberiusMemoryProvider(config=config)


def _seed(p, old: str, new: str):
    out = json.loads(p.handle_tool_call("fact_store", {"action": "add", "content": old}))
    p.handle_tool_call("fact_store", {"action": "add", "content": new})
    st = p._store
    st._conn.execute("UPDATE facts SET created_at = '2026-01-01 00:00:00' WHERE fact_id = ?", (out["fact_id"],))


def test_schema_since_mentions_search_and_brief(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        schema = next(s for s in p.get_tool_schemas() if s["name"] == "fact_store")
        desc = schema["parameters"]["properties"]["since"]["description"]
        assert "search" in desc and "brief" in desc
    finally:
        p.shutdown()


def test_search_since_windows_results(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        _seed(p, "AcmeWidget crate line old ships to the docks.",
                 "AcmeWidget crate line new ships to the docks.")
        out = json.loads(p.handle_tool_call("fact_store", {
            "action": "search", "query": "AcmeWidget", "since": "24h", "limit": 10}))
        assert "line new" in json.dumps(out) and "line old" not in json.dumps(out)
    finally:
        p.shutdown()


def test_brief_since_windows_pack(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        _seed(p, "AcmeWidget crate line old ships to the docks.",
                 "AcmeWidget crate line new ships to the docks.")
        out = json.loads(p.handle_tool_call("fact_store", {
            "action": "brief", "query": "AcmeWidget", "budget": 500, "since": "24h"}))
        assert "line new" in json.dumps(out) and "line old" not in json.dumps(out)
        assert out["total"] == 1  # the window marker follows the window
    finally:
        p.shutdown()
