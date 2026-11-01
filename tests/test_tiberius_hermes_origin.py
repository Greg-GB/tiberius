"""Hermes provider wiring for provenance (docs/provenance.md).

Every fact the provider writes carries the runtime writer tag
('<profile>:<8 hex of the session id>'); recall actions accept origin /
exclude_origin. Synthetic fixtures only (the eval fixture's rule): AcmeWidget
— no real store content is ever a fixture.
"""

import json

from hermes_plugin_tiberius import TiberiusMemoryProvider


def _provider(tmp_path, session_id="local-a1b2c3d4ef"):
    config = {"db_path": str(tmp_path / "m.db"), "auto_extract": "false"}
    p = TiberiusMemoryProvider(config=config)
    p.initialize(session_id=session_id)
    return p


def test_schema_has_origin_params(tmp_path):
    p = _provider(tmp_path)
    try:
        schema = next(s for s in p.get_tool_schemas() if s["name"] == "fact_store")
        props = schema["parameters"]["properties"]
        assert "origin" in props and "exclude_origin" in props
    finally:
        p.shutdown()


def test_provider_add_stamps_runtime_origin(tmp_path):
    p = _provider(tmp_path)
    try:
        out = json.loads(p.handle_tool_call("fact_store",
                                            {"action": "add", "content": "AcmeWidget crate line 0 ships on Mondays."}))
        row = p._store.get_fact(out["fact_id"])
        prefix, short = row["origin"].rsplit(":", 1)
        # 'local' contributes the hex chars c,a: the tag is the first 8 hex of the whole id
        assert short == "caa1b2c3" and len(short) == 8
        assert prefix  # the profile name (env-dependent, so only shape-checked)
    finally:
        p.shutdown()


def test_provider_add_batch_threads_origin(tmp_path):
    p = _provider(tmp_path)
    try:
        out = json.loads(p.handle_tool_call("fact_store", {
            "action": "add_batch",
            "facts": ["AcmeWidget crate line 0 ships on Mondays.",
                      "AcmeWidget crate line 1 ships on Tuesdays."]}))
        for fid in out["fact_ids"]:
            assert p._store.get_fact(fid)["origin"].endswith(":caa1b2c3")
    finally:
        p.shutdown()


def test_provider_search_origin_filter_roundtrip(tmp_path):
    p = _provider(tmp_path)
    try:
        p.handle_tool_call("fact_store", {"action": "add", "content": "AcmeWidget crate line 0 ships on Mondays."})
        p.handle_tool_call("fact_store", {"action": "add", "content": "AcmeWidget crate line 1 ships on Tuesdays."})
        tag = p._origin()
        out = json.loads(p.handle_tool_call("fact_store",
                                            {"action": "search", "query": "AcmeWidget", "origin": tag}))
        assert out["count"] == 2 and {r["origin"] for r in out["results"]} == {tag}
        out = json.loads(p.handle_tool_call("fact_store",
                                            {"action": "search", "query": "AcmeWidget", "origin": "cli"}))
        assert out["count"] == 0  # nothing written by 'cli'
        out = json.loads(p.handle_tool_call("fact_store",
                                            {"action": "search", "query": "AcmeWidget", "exclude_origin": tag}))
        assert out["count"] == 0  # everything was written by the provider
    finally:
        p.shutdown()
