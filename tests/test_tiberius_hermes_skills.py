"""Hermes provider skill catalog (docs/skill-retrieval.md §4 — Phase 3).

Covers the Phase 3 gate: skill-sync at initialize (in-process), the prefetch skills
block (trust floor + per-turn top-k, deduped per session), session-end usage feedback,
config handling, and the zero-behavior-change contract when skill_dirs is unset.
Fixtures follow the synthetic-content convention: fictional skills on tmp_path.
"""
from __future__ import annotations

import json

import hermes_yaml as yaml
import pytest

from hermes_plugin_tiberius import TiberiusMemoryProvider
from hermes_plugin_tiberius.provider import _skill_view_names, _truncate_skill_desc


def _make_skill(base, rel_path, name, description):
    d = base / rel_path
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: \"{description}\"\n---\n\n# Body\n\nFictional instructions.\n",
        encoding="utf-8")
    return d


def _skill_tree(base):
    """A categorized skill tree like the Hermes profile layout: flat + nested skills."""
    _make_skill(base, "widget-fixer", "widget-fixer",
                "Use when a widget wobbles or jams. Covers spring tension and lubrication.")
    _make_skill(base, "devops/deploy-bot", "deploy-bot",
                "Use when running the deploy pipeline for production rollouts.")
    _make_skill(base, "data/csv-wrangler", "csv-wrangler",
                "Use when wrangling messy CSV exports and ragged rows.")
    return base


def _provider(tmp_path, skill_dirs=None, **cfg):
    config = {"db_path": str(tmp_path / "m.db"), "auto_extract": "false"}
    if skill_dirs is not None:
        config["skill_dirs"] = skill_dirs
    config.update(cfg)
    return TiberiusMemoryProvider(config=config)


def _skill_rows(p) -> list[dict]:
    return p._store.list_facts(category="skill", limit=100)


def _trust(p, skill_name: str) -> float:
    rows = [r for r in _skill_rows(p) if r["content"].startswith(f"{skill_name}: ")]
    assert len(rows) == 1, f"expected exactly one fact for {skill_name}"
    return rows[0]["trust_score"]


# ── config handling ──────────────────────────────────────────────────────────────


def test_skill_dirs_config_normalization(tmp_path):
    p = _provider(tmp_path, skill_dirs="/a/skills  ")
    assert p._skill_dirs == ["/a/skills"] and p._skills_enabled
    p = _provider(tmp_path, skill_dirs="")  # empty string == unset
    assert p._skill_dirs == [] and not p._skills_enabled
    p = _provider(tmp_path)  # absent == unset
    assert not p._skills_enabled
    p = _provider(tmp_path, skill_dirs=["/a/skills", "/b/skills"], skills_top=0, skills_floor=0)
    assert p._skill_dirs == ["/a/skills", "/b/skills"] and p._skills_top == 1 and p._skills_floor == 1  # clamped, not 0


def test_skill_dirs_hermes_home_placeholder_expands(tmp_path):
    """A $HERMES_HOME placeholder in skill_dirs resolves against the active profile's home —
    the same manual expansion db_path gets (the config loader only resolves ${VAR})."""
    home = tmp_path / "hermes_test"  # conftest points HERMES_HOME here
    skills = _skill_tree(home / "skills")
    p = TiberiusMemoryProvider(config={"db_path": str(tmp_path / "m.db"), "auto_extract": "false",
                                       "skill_dirs": "$HERMES_HOME/skills"})
    assert p._skill_dirs == [str(skills)]
    p.initialize(session_id="t")
    try:
        assert len(_skill_rows(p)) == 3
    finally:
        p.shutdown()


def test_save_config_round_trips_skill_options(tmp_path, monkeypatch):
    """skill_dirs / skills_top / skills_floor must survive the canonical config writer."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model:\n  default: keep-me\n")

    TiberiusMemoryProvider(config={}).save_config(
        {"skill_dirs": [str(tmp_path / "skills")], "skills_top": 4, "skills_floor": 1}, str(tmp_path))

    raw = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert raw["plugins"]["tiberius"]["skill_dirs"] == [str(tmp_path / "skills")]
    assert raw["plugins"]["tiberius"]["skills_top"] == 4
    assert raw["plugins"]["tiberius"]["skills_floor"] == 1
    assert raw["model"]["default"] == "keep-me"


# ── initialize: skill-sync ───────────────────────────────────────────────────────


def test_initialize_syncs_nested_skill_dirs(tmp_path):
    skills = _skill_tree(tmp_path / "skills")
    p = _provider(tmp_path, skill_dirs=str(skills))
    p.initialize(session_id="t")
    try:
        rows = {r["content"].split(":")[0]: r for r in _skill_rows(p)}
        assert set(rows) == {"widget-fixer", "deploy-bot", "csv-wrangler"}  # nested ones included
        assert "skill-id:deploy-bot" in rows["deploy-bot"]["tags"]
        assert str(skills / "devops" / "deploy-bot") in rows["deploy-bot"]["tags"]
    finally:
        p.shutdown()

    # A second session on the same store is idempotent (same facts, no duplicates).
    p2 = _provider(tmp_path, skill_dirs=str(skills))
    p2.initialize(session_id="t2")
    try:
        assert len(_skill_rows(p2)) == 3
    finally:
        p2.shutdown()


def test_initialize_bad_skill_dir_does_not_break_start(tmp_path):
    skills = _skill_tree(tmp_path / "skills")
    p = _provider(tmp_path, skill_dirs=[str(skills), str(tmp_path / "nope")])
    p.initialize(session_id="t")  # must not raise
    try:
        assert p._store is not None and p._retriever is not None
        # The good dir still synced; the bad one is reported in the walk report, not fatal.
        assert len(_skill_rows(p)) == 3
    finally:
        p.shutdown()


# ── prefetch: skills block ───────────────────────────────────────────────────────


def test_prefetch_floor_and_turn_block(tmp_path):
    skills = _skill_tree(tmp_path / "skills")
    p = _provider(tmp_path, skill_dirs=str(skills), skills_floor=1, skills_top=3)
    p.initialize(session_id="t")
    try:
        # Bump one skill's trust so the floor order is deterministic.
        fid = next(r["fact_id"] for r in _skill_rows(p) if r["content"].startswith("widget-fixer: "))
        p._store.record_feedback(fid, helpful=True)

        first = p.prefetch("how do I run the deploy pipeline for a production rollout?")
        assert p._SKILL_FLOOR_HEADER in first
        assert "widget-fixer" in first  # floor: top-1 by trust
        assert p._SKILL_TURN_HEADER in first
        assert "deploy-bot" in first  # turn: query-relevant, not yet shown

        # Same query again: nothing new to inject — no skill headers at all.
        second = p.prefetch("how do I run the deploy pipeline for a production rollout?")
        assert p._SKILL_FLOOR_HEADER not in second and p._SKILL_TURN_HEADER not in second

        # A query for the not-yet-shown third skill gets a fresh turn block.
        third = p.prefetch("wrangling messy CSV exports")
        assert p._SKILL_FLOOR_HEADER not in third
        assert p._SKILL_TURN_HEADER in third and "csv-wrangler" in third
    finally:
        p.shutdown()


def test_prefetch_no_skill_block_when_disabled(tmp_path):
    skills = _skill_tree(tmp_path / "skills")
    p = _provider(tmp_path)  # no skill_dirs
    p.initialize(session_id="t")
    try:
        p._store.add_fact("plain memory about a project")
        out = p.prefetch("project")
        assert "## Tiberius Memory" in out  # normal facts still prefetch
        assert p._SKILL_FLOOR_HEADER not in out and p._SKILL_TURN_HEADER not in out
        assert len(_skill_rows(p)) == 0  # no sync happened either
    finally:
        p.shutdown()


def test_new_session_reoffers_floor(tmp_path):
    skills = _skill_tree(tmp_path / "skills")
    p = _provider(tmp_path, skill_dirs=str(skills), skills_floor=2)
    p.initialize(session_id="t1")
    try:
        assert p._SKILL_FLOOR_HEADER in p.prefetch("anything at all here")
        # Same provider instance, new session (the manager re-initializes per session).
        p.initialize(session_id="t2")
        assert p._SKILL_FLOOR_HEADER in p.prefetch("something else entirely")
    finally:
        p.shutdown()


# ── render helpers ───────────────────────────────────────────────────────────────


def test_skill_fact_line_rendering(tmp_path):
    skills = _skill_tree(tmp_path / "skills")
    p = _provider(tmp_path, skill_dirs=str(skills))
    p.initialize(session_id="t")
    try:
        row = next(r for r in _skill_rows(p) if r["content"].startswith("widget-fixer: "))
        line = p._skill_fact_line(row)
        assert line == "- widget-fixer — Use when a widget wobbles or jams. Covers spring tension and lubrication."
        # A colon inside the description must survive (the prefix is stripped, never re-partitioned).
        colon_row = {"content": "api: Rotate: the key before the rotation policy", "tags": "skill-id:api"}
        assert p._skill_fact_line(colon_row) == "- api — Rotate: the key before the rotation policy"
        # No skill-id tag: id from the content prefix.
        assert p._skill_fact_line({"content": "bare-skill: does bare things", "tags": ""}) == \
            "- bare-skill — does bare things"
    finally:
        p.shutdown()


def test_truncate_skill_desc_word_boundary():
    assert _truncate_skill_desc("short") == "short"
    s = " ".join(f"w{i}" for i in range(30))
    out = _truncate_skill_desc(s, 12)
    body = out[:-1]  # drop the ellipsis
    assert len(body) <= 12 and not body.endswith(" ")
    assert s.startswith(body) and s[len(body)] == " "  # cut right before a space = at a word boundary
    assert _truncate_skill_desc("x" * 200, 10)  # no-space string: hard cut


# ── session-end: usage feedback ──────────────────────────────────────────────────


def _transcript_with_loads(*skill_args: dict) -> list:
    tcs, results = [], []
    for i, args in enumerate(skill_args):
        tcs.append({"id": str(i), "type": "function",
                    "function": {"name": "skill_view", "arguments": json.dumps(args)}})
        results.append({"role": "tool", "tool_call_id": str(i), "content": "skill body"})
    return [{"role": "user", "content": "do the thing"},
            {"role": "assistant", "content": None, "tool_calls": tcs},
            *results,
            {"role": "assistant", "content": "done"}]


def test_session_end_skill_feedback(tmp_path):
    skills = _skill_tree(tmp_path / "skills")
    p = _provider(tmp_path, skill_dirs=str(skills))
    p.initialize(session_id="t")
    try:
        transcript = _transcript_with_loads({"name": "widget-fixer"}, {"name": "devops/deploy-bot"})
        p.on_session_end(transcript)
        assert _trust(p, "widget-fixer") == 0.55  # +0.05 helpful
        assert _trust(p, "deploy-bot") == 0.55  # path-qualified load matches by basename
        assert _trust(p, "csv-wrangler") == 0.5  # not loaded — untouched

        # A second session-end pass on the same transcript must not re-rate.
        p.on_session_end(transcript)
        assert _trust(p, "widget-fixer") == 0.55

        # A new session (fresh feedback dedupe) can rate the same loads again.
        p.initialize(session_id="t2")
        p.on_session_end(transcript)
        assert _trust(p, "widget-fixer") == pytest.approx(0.6)
    finally:
        p.shutdown()


def test_session_end_feedback_skips_uncatalog_and_disabled(tmp_path):
    skills = _skill_tree(tmp_path / "skills")
    p = _provider(tmp_path, skill_dirs=str(skills))
    p.initialize(session_id="t")
    try:
        p.on_session_end(_transcript_with_loads({"name": "no-such-skill-xyz"}, {"name": "widget-fixer"}))
        assert _trust(p, "widget-fixer") == 0.55
        assert sum(1 for r in _skill_rows(p) if "no-such" in r["content"]) == 0

        # A provider without skill_dirs never touches trust, even with loads in the transcript.
        p2 = _provider(tmp_path)
        p2.initialize(session_id="t2")
        p2._store = p._store  # same store; only the gate differs
        p2.on_session_end(_transcript_with_loads({"name": "widget-fixer"}))
        assert _trust(p, "widget-fixer") == 0.55
        p2.shutdown()
    finally:
        p.shutdown()


def test_skill_view_names_shapes():
    """The detector mirrors the core's: OpenAI-shaped dict tool_calls, object-shaped rows,
    bad JSON skipped, non-skill_view tools ignored, non-assistant rows ignored."""
    class _Fn:
        def __init__(self, name, arguments):
            self.name, self.arguments = name, arguments

    class _TC:
        def __init__(self, fn):
            self.function = fn

    good_dict = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "1", "type": "function",
         "function": {"name": "skill_view", "arguments": json.dumps({"name": "alpha"})}},
        {"id": "2", "type": "function",
         "function": {"name": "skill_view", "arguments": {"name": "beta"}}},  # dict args also fine
        {"id": "3", "type": "function",
         "function": {"name": "skill_view", "arguments": "{not json"}},
        {"id": "4", "type": "function",
         "function": {"name": "skills_list", "arguments": json.dumps({})}},
    ]}
    good_obj = {"role": "assistant", "content": None, "tool_calls": [_TC(_Fn("skill_view", '{"name": "gamma"}'))]}
    noise = [{"role": "user", "content": "hi"},
             {"role": "tool", "tool_call_id": "1", "content": "x"},
             {"role": "assistant", "content": "no tools here"}]
    assert _skill_view_names([good_dict, good_obj, *noise]) == ["alpha", "beta", "gamma"]
    assert _skill_view_names([]) == []


# ── zero behavior change ─────────────────────────────────────────────────────────


def test_zero_behavior_change_without_skill_dirs(tmp_path):
    """skill_dirs unset: no sync, no skill headers in prefetch, tool schemas untouched."""
    skills = _skill_tree(tmp_path / "skills")
    p = _provider(tmp_path)
    p.initialize(session_id="t")
    try:
        assert len(_skill_rows(p)) == 0
        assert [t["name"] for t in p.get_tool_schemas()] == ["fact_store", "fact_feedback"]
        p._store.add_fact("plain memory fact about a project")
        out = p.prefetch("project")
        assert "## Tiberius Memory" in out
        assert p._SKILL_FLOOR_HEADER not in out and p._SKILL_TURN_HEADER not in out
    finally:
        p.shutdown()
