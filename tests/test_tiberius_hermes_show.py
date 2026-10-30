"""Hermes provider show action: fact_store(action='show').

Hermes-environment only (the provider imports Hermes core) — runs where the
other provider tests run (the accepted CI gap; see CONTRIBUTING.md). Pins the
provider wiring for the by-id read: the schema surface (show in the action
enum), dispatch, the JSON result shape, and the not-found contract. Synthetic
fixtures only (AcmeWidget — no real store content).
"""
from __future__ import annotations

import json

from hermes_plugin_tiberius import TiberiusMemoryProvider


def _provider(tmp_path, **cfg):
    config = {"db_path": str(tmp_path / "m.db"), "auto_extract": "false"}
    config.update(cfg)
    return TiberiusMemoryProvider(config=config)


def test_show_schema_surface(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        schema = next(s for s in p.get_tool_schemas() if s["name"] == "fact_store")
        assert "show" in schema["parameters"]["properties"]["action"]["enum"]
    finally:
        p.shutdown()


def test_show_dispatch_returns_full_fact(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        out = json.loads(p.handle_tool_call("fact_store", {"action": "add", "content": "AcmeWidget ships to the docks on Tuesdays."}))
        fid = out["fact_id"]
        out = json.loads(p.handle_tool_call("fact_store", {"action": "show", "fact_id": fid}))
        f = out["fact"]
        assert f["fact_id"] == fid and f["content"] == "AcmeWidget ships to the docks on Tuesdays."
        assert {"category", "tags", "trust_score", "retrieval_count", "created_at", "updated_at", "entities"} <= set(f)
        assert f["retrieval_count"] == 0  # a by-id lookup is not a recall
    finally:
        p.shutdown()


def test_show_unknown_id_is_error(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        out = json.loads(p.handle_tool_call("fact_store", {"action": "show", "fact_id": 99999}))
        assert "not found" in out["error"]
    finally:
        p.shutdown()


def test_show_missing_fact_id_is_error(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        out = json.loads(p.handle_tool_call("fact_store", {"action": "show"}))
        assert "fact_id" in out["error"]
    finally:
        p.shutdown()
