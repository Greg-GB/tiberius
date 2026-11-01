"""Hermes provider wiring for `merge` (docs/merge.md).

Fold one fact into another: content/trust/entities/tags union, the from row
deleted, atomic. Synthetic fixtures only (the eval fixture's rule):
AcmeWidget — no real store content is ever a fixture.
"""

import json

from hermes_plugin_tiberius import TiberiusMemoryProvider


def _provider(tmp_path):
    config = {"db_path": str(tmp_path / "m.db"), "auto_extract": "false"}
    return TiberiusMemoryProvider(config=config)


def test_schema_merge_action(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        schema = next(s for s in p.get_tool_schemas() if s["name"] == "fact_store")
        props = schema["parameters"]["properties"]
        assert "merge" in props["action"]["enum"]
        assert "merge" in props["from_fact_id"]["description"]
        assert "merge" in props["into_fact_id"]["description"]
    finally:
        p.shutdown()


def test_merge_roundtrip(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        survivor = json.loads(p.handle_tool_call("fact_store", {
            "action": "add", "content": "AcmeWidget ships crates to the northern docks.",
            "tags": "ships"}))["fact_id"]
        other = json.loads(p.handle_tool_call("fact_store", {
            "action": "add", "content": "ZetaCorp builds the RelayX line.",
            "tags": "builds"}))["fact_id"]
        out = json.loads(p.handle_tool_call("fact_store", {
            "action": "merge", "from_fact_id": other, "into_fact_id": survivor}))
        assert out["fact_id"] == survivor
        assert "RelayX" in out["content"] and "AcmeWidget" in out["content"]
        # The from row is gone from recall.
        found = json.loads(p.handle_tool_call("fact_store", {"action": "search", "query": "RelayX line"}))
        ids = [r["fact_id"] for r in found["results"]]
        assert survivor in ids
        assert other not in ids
    finally:
        p.shutdown()


def test_merge_errors(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        a = json.loads(p.handle_tool_call("fact_store", {
            "action": "add", "content": "The AcmeWidget dock holds two gantries."}))["fact_id"]
        b = json.loads(p.handle_tool_call("fact_store", {
            "action": "add", "content": "The ZetaCorp yard stores RelayX spares."}))["fact_id"]
        out = p.handle_tool_call("fact_store", {"action": "merge", "from_fact_id": a, "into_fact_id": a})
        assert "distinct" in json.loads(out)["error"]
        out = p.handle_tool_call("fact_store", {"action": "merge", "from_fact_id": 99999, "into_fact_id": b})
        assert "not found" in json.loads(out)["error"]
        out = p.handle_tool_call("fact_store", {"action": "merge", "from_fact_id": a})
        assert "into_fact_id" in json.loads(out)["error"]
    finally:
        p.shutdown()
