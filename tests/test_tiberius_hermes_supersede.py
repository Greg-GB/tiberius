"""Hermes provider wiring for `supersede` (docs/supersede.md).

Replace a stale fact: the replacement is a normal add, the old row is hidden
from recall but stays readable via `show` with superseded_by/superseded_at.
Synthetic fixtures only (the eval fixture's rule): AcmeWidget — no real store
content is ever a fixture.
"""

import json

from hermes_plugin_tiberius import TiberiusMemoryProvider


def _provider(tmp_path):
    config = {"db_path": str(tmp_path / "m.db"), "auto_extract": "false"}
    return TiberiusMemoryProvider(config=config)


def test_schema_supersede_action(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        schema = next(s for s in p.get_tool_schemas() if s["name"] == "fact_store")
        props = schema["parameters"]["properties"]
        assert "supersede" in props["action"]["enum"]
        assert "supersede" in props["fact_id"]["description"]
        assert "supersede" in props["content"]["description"]
    finally:
        p.shutdown()


def test_supersede_roundtrip_and_show_pointer(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        old_id = json.loads(p.handle_tool_call("fact_store", {
            "action": "add", "content": "The AcmeWidget service listens on port 7777."}))["fact_id"]
        out = json.loads(p.handle_tool_call("fact_store", {
            "action": "supersede", "fact_id": old_id,
            "content": "The AcmeWidget service now listens on port 8081.",
            "category": "tool", "tags": "ports,infra"}))
        assert out["superseded_fact_id"] == old_id
        assert out["new_fact_id"] != old_id
        assert out["superseded_at"]
        old = json.loads(p.handle_tool_call("fact_store", {"action": "show", "fact_id": old_id}))["fact"]
        assert old["superseded_by"] == out["new_fact_id"]
        new = json.loads(p.handle_tool_call("fact_store", {"action": "show", "fact_id": out["new_fact_id"]}))["fact"]
        assert new["superseded_by"] is None
        assert new["category"] == "tool"
        assert new["tags"] == "ports,infra"
        # The old row is out of the search pool; the replacement is in it.
        found = json.loads(p.handle_tool_call("fact_store", {"action": "search", "query": "AcmeWidget port"}))
        ids = [r["fact_id"] for r in found["results"]]
        assert old_id not in ids
        assert out["new_fact_id"] in ids
    finally:
        p.shutdown()


def test_supersede_errors(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        out = p.handle_tool_call("fact_store", {"action": "supersede", "fact_id": 42})
        assert "content" in json.loads(out)["error"]
        out = p.handle_tool_call("fact_store", {
            "action": "supersede", "fact_id": 99999, "content": "A brand new statement."})
        assert "not found" in json.loads(out)["error"]
    finally:
        p.shutdown()
