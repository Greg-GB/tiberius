"""Hermes provider drift action (docs/drift.md): fact_store(action='drift').

Hermes-environment only (the provider imports Hermes core) — runs where the
other provider tests run (the accepted CI gap; see CONTRIBUTING.md). Pins the
provider wiring over the shared drift() module: the schema surface (drift in
the action enum, the project/support parameters), dispatch, the JSON result
shape (same as the CLI subcommand), and the no-plan-facts error path.
Synthetic fixtures only (AcmeWidget — no real store content).
"""
from __future__ import annotations

import json

from hermes_plugin_tiberius import TiberiusMemoryProvider

PLAN = "AcmeWidget plan: crate loads happen at the dock before dawn."
STATE = "AcmeWidget decision: crate loads at the dock now happen before dawn."


def _provider(tmp_path, **cfg):
    config = {"db_path": str(tmp_path / "m.db"), "auto_extract": "false"}
    config.update(cfg)
    return TiberiusMemoryProvider(config=config)


def test_drift_schema_surface(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        schema = next(s for s in p.get_tool_schemas() if s["name"] == "fact_store")
        assert "drift" in schema["parameters"]["properties"]["action"]["enum"]
        assert "project" in schema["parameters"]["properties"]
        assert "support" in schema["parameters"]["properties"]
    finally:
        p.shutdown()


def test_drift_dispatch_returns_shared_shape(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        p.handle_tool_call("fact_store", {"action": "add", "content": PLAN, "category": "project", "tags": "plan"})
        p.handle_tool_call("fact_store", {"action": "add", "content": STATE, "category": "project"})
        out = json.loads(p.handle_tool_call("fact_store", {"action": "drift", "project": "AcmeWidget", "since": "2020-01-01"}))
        # same JSON shape as the CLI subcommand — the shared drift() output
        assert out["project"] == "AcmeWidget"
        assert out["plan"]["total"] == 1
        assert out["state"]["total"] >= 1
        assert {"fact_id", "content", "category", "trust_score", "support"} <= set(out["plan"]["facts"][0])
        # the plan fact echoes the state fact (same tokens, no stopword dilution)
        assert out["plan"]["facts"][0]["support"] is not None
        assert out["plan"]["facts"][0]["support"]["count"] >= 1
    finally:
        p.shutdown()


def test_drift_requires_project(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        err = p.handle_tool_call("fact_store", {"action": "drift"})
        assert "project" in err
    finally:
        p.shutdown()


def test_drift_no_plan_facts_is_error(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        p.handle_tool_call("fact_store", {"action": "add", "content": STATE, "category": "project"})
        err = p.handle_tool_call("fact_store", {"action": "drift", "project": "AcmeWidget"})
        assert "plan" in err
    finally:
        p.shutdown()
