"""Hermes provider brief action (docs/brief.md): fact_store(action='brief').

Hermes-environment only (the provider imports Hermes core) — runs where the
other provider tests run (the accepted CI gap; see CONTRIBUTING.md). Pins the
provider wiring over the shared brief() module: the schema surface (brief in
the action enum, the budget parameter), dispatch, the JSON result shape,
budget pass-through, and the configured min_trust default. Synthetic fixtures
only (AcmeWidget — no real store content).
"""
from __future__ import annotations

import json

from hermes_plugin_tiberius import TiberiusMemoryProvider


def _provider(tmp_path, **cfg):
    config = {"db_path": str(tmp_path / "m.db"), "auto_extract": "false"}
    config.update(cfg)
    return TiberiusMemoryProvider(config=config)


def _seed(p, n: int = 2):
    """n fictional facts, each matching 'AcmeWidget' (~56 chars = 14 tokens)."""
    for i in range(n):
        p.handle_tool_call("fact_store", {"action": "add", "content": f"AcmeWidget crate line {i} ships to the docks on Tuesdays."})


def test_brief_schema_surface(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        schema = next(s for s in p.get_tool_schemas() if s["name"] == "fact_store")
        assert "brief" in schema["parameters"]["properties"]["action"]["enum"]
        assert "budget" in schema["parameters"]["properties"]
    finally:
        p.shutdown()


def test_brief_dispatch_returns_shared_shape(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        _seed(p)
        out = json.loads(p.handle_tool_call("fact_store", {"action": "brief", "query": "AcmeWidget", "budget": 500}))
        # same JSON shape as the CLI subcommand — the shared brief() output
        assert out["budget"] == 500 and out["count"] == 2 and out["total"] == 2
        assert all(f["content"].startswith("AcmeWidget") for f in out["facts"])
        assert {"fact_id", "content", "category", "trust_score", "score"} <= set(out["facts"][0])
    finally:
        p.shutdown()


def test_brief_budget_truncates_whole_facts(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        _seed(p)
        out = json.loads(p.handle_tool_call("fact_store", {"action": "brief", "query": "AcmeWidget", "budget": 15}))
        assert out["count"] == 1 and out["tokens"] <= 15  # the second fact (28 tokens) is dropped whole
        assert out["total"] == 2  # the window still says the pool is bigger
    finally:
        p.shutdown()


def test_brief_min_trust_gates_pool_not_entity_tier(tmp_path):
    """The configured threshold gates the search pool; the entity tier is not
    trust-gated (probe's structural path — same as the existing probe action)."""
    p = _provider(tmp_path, min_trust_threshold=0.9)
    p.initialize(session_id="t")
    try:
        _seed(p)  # default trust 0.5 < 0.9
        out = json.loads(p.handle_tool_call("fact_store", {"action": "brief", "query": "AcmeWidget"}))
        assert out["total"] == 0  # the configured threshold empties the search pool
        assert out["count"] == 2 and out["entity_only"] == 2  # the entity tier still surfaces them
    finally:
        p.shutdown()


def test_brief_requires_query(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        err = p.handle_tool_call("fact_store", {"action": "brief"})
        assert "query" in err  # missing required argument, the standard tool_error contract
    finally:
        p.shutdown()
