"""Hermes provider stats + list order (docs/stats.md).

Hermes-environment only (the provider imports Hermes core) — runs where the
other provider tests run (the accepted CI gap; see CONTRIBUTING.md). Pins the
provider wiring for the stats surface: the 'stats' action on fact_store, the
'order' param on the 'list' action, and dispatch of both (including the
unknown-order error). The shared store methods are covered by the core store
tests.
Synthetic fixtures only (AcmeWidget — no real store content).
"""
from __future__ import annotations

import json

from hermes_plugin_tiberius import TiberiusMemoryProvider


def _provider(tmp_path):
    config = {"db_path": str(tmp_path / "m.db"), "auto_extract": "false"}
    return TiberiusMemoryProvider(config=config)


def test_schema_stats_action_and_order_param(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        schema = next(s for s in p.get_tool_schemas() if s["name"] == "fact_store")
        props = schema["parameters"]["properties"]
        assert "stats" in props["action"]["enum"]
        assert props["order"]["enum"] == ["trust", "retrieval", "recent", "stale"]
    finally:
        p.shutdown()


def test_stats_dispatch_returns_aggregates(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        p.handle_tool_call("fact_store", {"action": "add", "content": "The AcmeWidget crate line ships on Fridays."})
        out = json.loads(p.handle_tool_call("fact_store", {"action": "stats"}))
        assert out["total"] == 1
        assert out["by_category"] == {"general": 1}
        assert out["trust_buckets"] == {"0.5-0.7": 1}
        assert out["stale_count"] == 0
        assert out["entities"] >= 1  # 'AcmeWidget' extracted
    finally:
        p.shutdown()


def test_list_dispatch_order_roundtrip_and_unknown_order_errors(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        p.handle_tool_call("fact_store", {"action": "add", "content": "The AcmeWidget crate line runs on SQLite."})
        out = json.loads(p.handle_tool_call("fact_store", {"action": "list", "order": "recent"}))
        assert out["count"] == 1
        out = json.loads(p.handle_tool_call("fact_store", {"action": "list"}))  # default order unchanged
        assert out["count"] == 1
        # Unknown order: raw-string assert (tool_error formatting is environment-dependent).
        out = p.handle_tool_call("fact_store", {"action": "list", "order": "banana"})
        assert "order must be one of" in out
    finally:
        p.shutdown()
