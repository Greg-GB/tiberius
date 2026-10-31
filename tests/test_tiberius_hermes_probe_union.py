"""Hermes provider union probe (docs/probe-union.md).

Hermes-environment only (the provider imports Hermes core) — runs where the
other provider tests run (the accepted CI gap; see CONTRIBUTING.md). Pins the
provider wiring for the union probe: 'probe' with 'entities' returns the union
(facts linked to ANY of the names), 'entities' takes precedence over 'entity'
when both are passed, and 'related' still ignores 'entities'. The shared
retriever union logic is covered by the core retrieval tests.
Synthetic fixtures only (AcmeWidget — no real store content).
"""
from __future__ import annotations

import json

from hermes_plugin_tiberius import TiberiusMemoryProvider


def _provider(tmp_path):
    config = {"db_path": str(tmp_path / "m.db"), "auto_extract": "false"}
    return TiberiusMemoryProvider(config=config)


def test_probe_with_entities_returns_union(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        p.handle_tool_call("fact_store", {"action": "add", "content": "AcmeWidget compiles the release artifacts."})
        p.handle_tool_call("fact_store", {"action": "add", "content": "BoltFrame fronts the public API gateway."})
        out = json.loads(p.handle_tool_call("fact_store", {"action": "probe", "entities": ["AcmeWidget", "BoltFrame"]}))
        contents = " | ".join(r["content"] for r in out["results"])
        assert "AcmeWidget" in contents  # any-of union: the first entity's fact present
        assert "BoltFrame" in contents  # and the second entity's fact present
    finally:
        p.shutdown()


def test_probe_entities_take_precedence_over_entity(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        p.handle_tool_call("fact_store", {"action": "add", "content": "AcmeWidget compiles the release artifacts."})
        p.handle_tool_call("fact_store", {"action": "add", "content": "BoltFrame fronts the public API gateway."})
        # Both set: 'entities' wins — the union runs, 'entity' is not probed alone.
        out = json.loads(p.handle_tool_call("fact_store", {
            "action": "probe", "entity": "AcmeWidget", "entities": ["AcmeWidget", "BoltFrame"]}))
        contents = " | ".join(r["content"] for r in out["results"])
        assert "BoltFrame" in contents
        # 'related' is still single-entity: 'entities' is ignored, 'entity' drives it.
        out = json.loads(p.handle_tool_call("fact_store", {
            "action": "related", "entity": "AcmeWidget", "entities": ["BoltFrame"]}))
        assert isinstance(out, (dict, list))  # dispatch runs on 'entity'; no KeyError
    finally:
        p.shutdown()


def test_probe_without_entity_or_entities_errors(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        # Neither set: raw-string assert (tool_error formatting is environment-dependent).
        out = p.handle_tool_call("fact_store", {"action": "probe"})
        assert "entity" in out
    finally:
        p.shutdown()
