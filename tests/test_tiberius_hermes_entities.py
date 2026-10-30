"""Hermes provider entities action: fact_store(action='entities').

Hermes-environment only (the provider imports Hermes core) — runs where the
other provider tests run (the accepted CI gap; see CONTRIBUTING.md). Pins the
provider wiring for entity discovery: the schema surface (entities in the
action enum), dispatch, the bare-array output contract, the zero-arg call
(no required arguments), and the optional limit. Synthetic fixtures only
(AcmeWidget/BetaCo — no real store content).
"""
from __future__ import annotations

import json

from hermes_plugin_tiberius import TiberiusMemoryProvider


def _provider(tmp_path, **cfg):
    config = {"db_path": str(tmp_path / "m.db"), "auto_extract": "false"}
    config.update(cfg)
    return TiberiusMemoryProvider(config=config)


def test_entities_schema_surface(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        schema = next(s for s in p.get_tool_schemas() if s["name"] == "fact_store")
        assert "entities" in schema["parameters"]["properties"]["action"]["enum"]
    finally:
        p.shutdown()


def test_entities_dispatch_returns_bare_array(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        p.handle_tool_call("fact_store", {"action": "add", "content": "AcmeWidget aka AW ships to the docks on Tuesdays."})
        out = json.loads(p.handle_tool_call("fact_store", {"action": "entities"}))
        assert isinstance(out, list)  # the contract is a bare array, not {"results": ...}
        by_name = {r["name"]: r for r in out}
        assert by_name["AcmeWidget"]["fact_count"] == 1
        assert by_name["AcmeWidget"]["aliases"] == ["AW"]
        assert by_name["Tuesdays"]["fact_count"] == 1  # every entity row is surfaced
    finally:
        p.shutdown()


def test_entities_empty_store_is_empty_array(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        out = json.loads(p.handle_tool_call("fact_store", {"action": "entities"}))
        assert out == []  # 0 is legal — an empty store is not an error
    finally:
        p.shutdown()


def test_entities_limit_caps_the_list(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        p.handle_tool_call("fact_store", {"action": "add", "content": "AcmeWidget aka AW ships to the docks on Tuesdays."})
        p.handle_tool_call("fact_store", {"action": "add", "content": "BetaCo signs the dock manifests every Friday."})
        out = json.loads(p.handle_tool_call("fact_store", {"action": "entities", "limit": 1}))
        assert len(out) == 1
        assert out[0]["name"] == "AcmeWidget"  # tied counts: name asc
    finally:
        p.shutdown()
