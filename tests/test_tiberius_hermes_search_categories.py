"""Hermes provider search category filters (docs/search-categories.md).

Hermes-environment only (the provider imports Hermes core) — runs where the
other provider tests run (the accepted CI gap; see CONTRIBUTING.md). Pins the
provider wiring for the new 'search'/'brief' params: 'categories' (IN list)
and 'exclude_category' reach the retrieval layer, and omitted params keep the
old behavior. The shared filter logic lives in the core retrieval tests.
Synthetic fixtures only (AcmeWidget/BoltFrame — no real store content).
"""
from __future__ import annotations

import json

from hermes_plugin_tiberius import TiberiusMemoryProvider


def _provider(tmp_path):
    config = {"db_path": str(tmp_path / "m.db"), "auto_extract": "false"}
    return TiberiusMemoryProvider(config=config)


def _seed(p):
    p.handle_tool_call("fact_store", {
        "action": "add", "content": "AcmeWidget compiles the release artifacts.", "category": "project"})
    p.handle_tool_call("fact_store", {
        "action": "add", "content": "BoltFrame fronts the public API gateway.", "category": "tool"})


def test_search_categories_and_exclude_forward(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        _seed(p)
        out = json.loads(p.handle_tool_call("fact_store", {
            "action": "search", "query": "release artifacts gateway",
            "categories": ["project", "tool"], "exclude_category": "tool"}))
        contents = " | ".join(r["content"] for r in out["results"])
        assert "AcmeWidget" in contents       # named category kept
        assert "BoltFrame" not in contents    # excluded category dropped
        # no filter at all: unchanged behavior, both facts reachable
        out = json.loads(p.handle_tool_call("fact_store", {
            "action": "search", "query": "release artifacts gateway"}))
        contents = " | ".join(r["content"] for r in out["results"])
        assert "AcmeWidget" in contents and "BoltFrame" in contents
    finally:
        p.shutdown()


def test_brief_exclude_category_filters_search_pool(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        _seed(p)
        # Query carries no entity names: only the search pool path is exercised
        # (the probe tier inside brief is deliberately unfiltered — non-goal).
        out = json.loads(p.handle_tool_call("fact_store", {
            "action": "brief", "query": "release artifacts gateway",
            "exclude_category": "tool", "budget": 500}))
        blob = json.dumps(out)
        assert "AcmeWidget" in blob
        assert "BoltFrame" not in blob
        assert out["total"] == 1  # match_count total describes the filtered pool
    finally:
        p.shutdown()
