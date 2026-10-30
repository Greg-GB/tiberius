"""Hermes provider batch writes (docs/batch.md).

Hermes-environment only (the provider imports Hermes core) — runs where the
other provider tests run (the accepted CI gap; see CONTRIBUTING.md). Pins the
provider wiring for the batch surfaces: the 'add_batch' action + 'facts' param
on fact_store, 'fact_ids' on fact_feedback, and dispatch of both (including
the mutual-exclusion errors). The shared store batch methods are covered by
the core store tests.
Synthetic fixtures only (AcmeWidget — no real store content).
"""
from __future__ import annotations

import json

from hermes_plugin_tiberius import TiberiusMemoryProvider


def _provider(tmp_path):
    config = {"db_path": str(tmp_path / "m.db"), "auto_extract": "false"}
    return TiberiusMemoryProvider(config=config)


def test_schema_add_batch_action_and_facts_param(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        schema = next(s for s in p.get_tool_schemas() if s["name"] == "fact_store")
        props = schema["parameters"]["properties"]
        assert "add_batch" in props["action"]["enum"]
        assert "facts" in props
        assert props["facts"]["type"] == "array"
    finally:
        p.shutdown()


def test_schema_fact_feedback_fact_ids(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        schema = next(s for s in p.get_tool_schemas() if s["name"] == "fact_feedback")
        assert "fact_ids" in schema["parameters"]["properties"]
        assert schema["parameters"]["required"] == ["action"]  # fact_id/fact_ids resolved at dispatch
    finally:
        p.shutdown()


def test_add_batch_dispatch_and_per_item_paths(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        out = json.loads(p.handle_tool_call("fact_store", {"action": "add_batch", "facts": [
            "The AcmeWidget crate line ships on Fridays.",
            {"content": "The AcmeWidget crate line runs on SQLite.", "category": "project"},
        ]}))
        assert out["added"] == 2 and out["duplicates"] == 0
        assert len(out["fact_ids"]) == 2
        first_ids = out["fact_ids"]
        assert p._store.get_fact(first_ids[1])["category"] == "project"  # per-item add_fact path ran
        # Duplicate content returns the existing id and counts as a duplicate.
        out = json.loads(p.handle_tool_call("fact_store", {"action": "add_batch", "facts": [
            "The AcmeWidget crate line ships on Fridays.",
        ]}))
        assert out["added"] == 0 and out["duplicates"] == 1
        assert out["fact_ids"][0] == first_ids[0]
    finally:
        p.shutdown()


def test_add_batch_bad_item_errors_with_index_and_executes_nothing(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        out = json.loads(p.handle_tool_call("fact_store", {"action": "add_batch", "facts": [
            "The AcmeWidget crate line ships on Fridays.", "   "]}))
        assert "facts[1]" in out["error"]
        out = json.loads(p.handle_tool_call("fact_store", {"action": "list", "limit": 50}))
        assert out["count"] == 0  # validated up front: nothing executed
        out = json.loads(p.handle_tool_call("fact_store", {"action": "add_batch"}))
        assert "error" in out
    finally:
        p.shutdown()


def test_feedback_batch_dispatch_and_mutual_exclusion(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        a = json.loads(p.handle_tool_call("fact_store", {"action": "add", "content": "The AcmeWidget crate line ships on Fridays."}))["fact_id"]
        b = json.loads(p.handle_tool_call("fact_store", {"action": "add", "content": "The AcmeWidget crate line runs on SQLite."}))["fact_id"]
        out = json.loads(p.handle_tool_call("fact_feedback", {"action": "helpful", "fact_ids": [a, 99999, b]}))
        assert out["applied"] == 2 and len(out["results"]) == 3
        assert [r["ok"] for r in out["results"]] == [True, False, True]
        assert "not found" in out["results"][1]["error"]
        assert out["results"][0]["new_trust"] > out["results"][0]["old_trust"]
        # Mutual exclusion + neither-set errors.
        out = json.loads(p.handle_tool_call("fact_feedback", {"action": "helpful", "fact_id": a, "fact_ids": [b]}))
        assert "error" in out
        out = json.loads(p.handle_tool_call("fact_feedback", {"action": "helpful"}))
        assert "error" in out
    finally:
        p.shutdown()
