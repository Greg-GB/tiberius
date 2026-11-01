"""Hermes provider wiring for `hygiene` (docs/hygiene.md).

Store health report: orphan entities, near-duplicate clusters, oversized
facts, low-trust no-feedback facts, superseded count. Read-only; capped
sections with total counts. Synthetic fixtures only (the eval fixture's
rule): AcmeWidget — no real store content is ever a fixture.
"""

import json

from hermes_plugin_tiberius import TiberiusMemoryProvider


def _provider(tmp_path):
    config = {"db_path": str(tmp_path / "h.db"), "auto_extract": "false"}
    return TiberiusMemoryProvider(config=config)


def test_schema_hygiene_action(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        schema = next(s for s in p.get_tool_schemas() if s["name"] == "fact_store")
        props = schema["parameters"]["properties"]
        assert "hygiene" in props["action"]["enum"]
        assert "hygiene" in props["max_words"]["description"]
        assert "0.45" in props["min_trust"]["description"]
    finally:
        p.shutdown()


def test_hygiene_roundtrip(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        a = json.loads(p.handle_tool_call("fact_store", {
            "action": "add", "content": "The AcmeWidget dock holds two gantries."}))["fact_id"]
        b = json.loads(p.handle_tool_call("fact_store", {
            "action": "add", "content": "The AcmeWidget dock holds three gantries."}))["fact_id"]
        low = json.loads(p.handle_tool_call("fact_store", {
            "action": "add", "content": "A shaky claim about the RelayX line."}))["fact_id"]
        json.loads(p.handle_tool_call("fact_store", {
            "action": "update", "fact_id": low, "trust_delta": -0.25}))  # 0.25 < 0.45
        gone = json.loads(p.handle_tool_call("fact_store", {
            "action": "add", "content": "OnlyQuarry is a quarry."}))["fact_id"]
        json.loads(p.handle_tool_call("fact_store", {"action": "remove", "fact_id": gone}))

        out = json.loads(p.handle_tool_call("fact_store", {"action": "hygiene"}))
        assert set(out) == {"orphan_entities", "near_duplicates", "oversized_facts",
                            "low_trust", "superseded_count"}
        names = {i["name"]: i["fact_count"] for i in out["orphan_entities"]["items"]}
        assert names.get("OnlyQuarry") == 0
        assert out["near_duplicates"]["count"] == 1
        assert sorted(x["fact_id"] for x in out["near_duplicates"]["clusters"][0]) == sorted((a, b))
        assert {i["fact_id"] for i in out["low_trust"]["items"]} == {low}
        # the report writes nothing
        assert len(json.loads(p.handle_tool_call("fact_store", {
            "action": "list", "min_trust": 0.0}))["facts"]) == 3
    finally:
        p.shutdown()


def test_hygiene_limit_caps_items_not_counts(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        for name in ("QuarryOne", "QuarryTwo", "QuarryThree"):
            fid = json.loads(p.handle_tool_call("fact_store", {
                "action": "add", "content": f"{name} is a quarry."}))["fact_id"]
            json.loads(p.handle_tool_call("fact_store", {"action": "remove", "fact_id": fid}))
        full = json.loads(p.handle_tool_call("fact_store", {"action": "hygiene"}))
        capped = json.loads(p.handle_tool_call("fact_store", {"action": "hygiene", "limit": 2}))
        assert full["orphan_entities"]["count"] == 3
        assert capped["orphan_entities"]["count"] == 3
        assert len(capped["orphan_entities"]["items"]) == 2
    finally:
        p.shutdown()
