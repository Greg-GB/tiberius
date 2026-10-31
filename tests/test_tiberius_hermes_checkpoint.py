"""Hermes provider wiring for `checkpoint` (docs/checkpoint.md).

Checkpoint facts are session state, not durable knowledge: one active
checkpoint per session, written at compaction, read by the next session via
`show` instead of re-deriving state from prose. Synthetic fixtures only (the
eval fixture's rule): AcmeWidget — no real store content is ever a fixture.
"""

import json

from hermes_plugin_tiberius import TiberiusMemoryProvider


def _provider(tmp_path):
    config = {"db_path": str(tmp_path / "m.db"), "auto_extract": "false"}
    return TiberiusMemoryProvider(config=config)


def test_schema_checkpoint_action_and_params(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        schema = next(s for s in p.get_tool_schemas() if s["name"] == "fact_store")
        props = schema["parameters"]["properties"]
        assert "checkpoint" in props["action"]["enum"]
        assert props["op"]["enum"] == ["set", "show", "clear"]
        assert "session" in props
        assert "facts" in props
    finally:
        p.shutdown()


def test_checkpoint_set_show_clear_roundtrip(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        out = json.loads(p.handle_tool_call("fact_store", {
            "action": "checkpoint", "session": "s1",
            "facts": ["task: ship the AcmeWidget", "next: write tests"]}))
        assert out["session"] == "s1"
        assert out["replaced"] == 0
        assert out["added"] == 2
        out = json.loads(p.handle_tool_call("fact_store", {"action": "checkpoint", "op": "show", "session": "s1"}))
        assert out["count"] == 2
        assert all(r["category"] == "checkpoint" for r in out["results"])
        # op defaults to set
        out = json.loads(p.handle_tool_call("fact_store", {
            "action": "checkpoint", "session": "s1", "facts": ["task: done"]}))
        assert out["replaced"] == 2
        out = json.loads(p.handle_tool_call("fact_store", {"action": "checkpoint", "op": "clear", "session": "s1"}))
        assert out["cleared"] == 1
        out = json.loads(p.handle_tool_call("fact_store", {"action": "checkpoint", "op": "show", "session": "s1"}))
        assert out["count"] == 0
    finally:
        p.shutdown()


def test_checkpoint_validation_errors(tmp_path):
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        for args, needle in (
            ({"action": "checkpoint", "facts": ["x"]}, "session"),
            ({"action": "checkpoint", "session": "  "}, "session"),
            ({"action": "checkpoint", "session": "s1"}, "facts"),
            ({"action": "checkpoint", "session": "s1", "facts": []}, "facts"),
            ({"action": "checkpoint", "session": "s1", "facts": ["   "]}, "facts"),
            ({"action": "checkpoint", "session": "s1", "facts": [str(i) for i in range(9)]}, "8"),
            ({"action": "checkpoint", "session": "s1", "op": "banana"}, "op"),
        ):
            assert needle in p.handle_tool_call("fact_store", args), args
    finally:
        p.shutdown()
