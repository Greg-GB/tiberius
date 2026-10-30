"""`drift` (two-sided plan/state comparison pack): synthetic-fixture tests through cli.main().

Fictional content only (the eval fixture's rule): AcmeWidget, ZetaDock, crates,
the dock, the ledger — no real store content is ever a fixture.
"""
import json

import pytest

from hermes_plugin_tiberius import cli
from hermes_plugin_tiberius.store import MemoryStore


def _run(capsys, *argv: str):
    cli.main(list(argv))
    return json.loads(capsys.readouterr().out.strip())


def _plan(capsys, db: str, content: str) -> int:
    return _run(capsys, "--db", db, "add", content, "--category", "project", "--tags", "plan")["fact_id"]


def _state(capsys, db: str, content: str) -> int:
    return _run(capsys, "--db", db, "add", content, "--category", "project", "--tags", "state")["fact_id"]


# Echo pair: the state fact repeats most of the plan claim's tokens, so the
# overlap coefficient is high (>=0.8) — well above the 0.15 default.
PLAN_ECHO = "AcmeWidget plan: crate loads happen at the dock before dawn."
STATE_ECHO = "AcmeWidget decision: crate loads at the dock now happen before dawn."
# Unbacked claim: shares zero cleaned tokens with any state fact below.
PLAN_UNBACKED = "AcmeWidget policy: the ledger keeps status codes."


def _seed_pair(capsys, db: str):
    return _plan(capsys, db, PLAN_ECHO), _state(capsys, db, STATE_ECHO)


def test_drift_deterministic(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    _seed_pair(capsys, db)
    a = _run(capsys, "--db", db, "drift", "AcmeWidget", "--since", "2020-01-01")
    b = _run(capsys, "--db", db, "drift", "AcmeWidget", "--since", "2020-01-01")
    assert a == b  # same store + params -> same pack (retrieval counts are not in the output)


def test_drift_two_sided_split_fill(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    _seed_pair(capsys, db)
    out = _run(capsys, "--db", db, "drift", "AcmeWidget", "--since", "2020-01-01", "--budget", "400")
    # Split fill: both sides are primary, so a default-sized budget shows each side
    assert out["plan"]["count"] == 1
    assert out["state"]["count"] >= 1
    assert out["tokens"] <= 400
    assert out["plan"]["total"] == 1
    assert out["state"]["total"] >= 1


def test_drift_plan_side_exact(tmp_path, capsys):
    """The plan side is an exact tag+entity query: 15 state facts naming the project
    must not crowd either plan fact out of the pack (a probe top-N design would rank
    by centrality and could drop them)."""
    db = str(tmp_path / "m.db")
    p1, p2 = _plan(capsys, db, PLAN_ECHO), _plan(capsys, db, PLAN_UNBACKED)
    for i in range(15):
        _state(capsys, db, f"AcmeWidget shipped crate batch {i} from the dock on Tuesday.")
    out = _run(capsys, "--db", db, "drift", "AcmeWidget", "--since", "2020-01-01", "--budget", "400")
    assert out["plan"]["total"] == 2
    assert out["plan"]["count"] == 2
    assert {f["fact_id"] for f in out["plan"]["facts"]} == {p1, p2}
    assert out["state"]["total"] >= 15


def test_drift_state_excludes_plan_facts(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    _seed_pair(capsys, db)
    out = _run(capsys, "--db", db, "drift", "AcmeWidget", "--since", "2020-01-01")
    plan_ids = {f["fact_id"] for f in out["plan"]["facts"]}
    state_ids = {f["fact_id"] for f in out["state"]["facts"]}
    assert plan_ids & state_ids == set()


def test_drift_support_markers(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    p1, s1 = _seed_pair(capsys, db)
    p2 = _plan(capsys, db, PLAN_UNBACKED)
    out = _run(capsys, "--db", db, "drift", "AcmeWidget", "--since", "2020-01-01", "--budget", "400")
    by_id = {f["fact_id"]: f for f in out["plan"]["facts"]}
    assert by_id[p1]["support"]["count"] >= 1
    assert by_id[p1]["support"]["best"] >= 0.8
    assert s1 in by_id[p1]["support"]["echoes"]
    # The unbacked claim: support null on the plan side, listed in 'unbacked', and the
    # supplemental note is present (three-piece set: field + note)
    assert by_id[p2]["support"] is None
    assert [u["fact_id"] for u in out["unbacked"]] == [p2]
    assert "no state echo" in out["note"]


def test_drift_unbacked_omitted_when_empty(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    _seed_pair(capsys, db)  # the single plan fact has a strong echo
    out = _run(capsys, "--db", db, "drift", "AcmeWidget", "--since", "2020-01-01")
    assert "unbacked" not in out and "note" not in out


def test_drift_default_anchor_is_oldest_plan_fact(tmp_path, capsys):
    """Default anchor = oldest plan fact's created_at: state facts that predate the plan facts are
    out of window. created_at is only set by the write path, so the test backdates rows."""
    db = str(tmp_path / "m.db")
    pid = _plan(capsys, db, PLAN_ECHO)
    early = _state(capsys, db, STATE_ECHO)
    late = _state(capsys, db, "AcmeWidget note: crate loads still happen at the dock before dawn.")
    store = MemoryStore(db)
    with store._lock:
        store._conn.execute("UPDATE facts SET created_at = ? WHERE fact_id = ?", ("2026-01-02 00:00:00", pid))
        store._conn.execute("UPDATE facts SET created_at = ? WHERE fact_id = ?", ("2026-01-01 00:00:00", early))
        store._conn.execute("UPDATE facts SET created_at = ? WHERE fact_id = ?", ("2026-01-03 00:00:00", late))
        store._conn.commit()
    store.close()
    out = _run(capsys, "--db", db, "drift", "AcmeWidget")
    assert out["anchor"] == "2026-01-02 00:00:00"
    assert [f["fact_id"] for f in out["state"]["facts"]] == [late]  # early fact is out of window


def test_drift_since_override(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    _plan(capsys, db, PLAN_ECHO)
    mid = _state(capsys, db, STATE_ECHO)
    late = _state(capsys, db, "AcmeWidget note: crate loads still happen at the dock before dawn.")
    store = MemoryStore(db)
    with store._lock:
        store._conn.execute("UPDATE facts SET created_at = ? WHERE fact_id = ?", ("2026-01-02 00:00:00", mid))
        store._conn.execute("UPDATE facts SET created_at = ? WHERE fact_id = ?", ("2026-01-03 00:00:00", late))
        store._conn.commit()
    store.close()
    out = _run(capsys, "--db", db, "drift", "AcmeWidget", "--since", "2026-01-02")
    assert out["anchor"] == "2026-01-02 00:00:00"
    assert {f["fact_id"] for f in out["state"]["facts"]} == {mid, late}
    out = _run(capsys, "--db", db, "drift", "AcmeWidget", "--since", "2026-01-03")
    assert [f["fact_id"] for f in out["state"]["facts"]] == [late]


def test_drift_bad_since_is_json_error(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    _plan(capsys, db, PLAN_ECHO)
    out = _run(capsys, "--db", db, "drift", "AcmeWidget", "--since", "not-a-date")
    assert "error" in out and "since" in out["error"]


def test_drift_requires_plan_facts(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    _state(capsys, db, "AcmeWidget shipped crate batch 0 from the dock on Tuesday.")
    out = _run(capsys, "--db", db, "drift", "AcmeWidget", "--since", "2020-01-01")
    assert "error" in out and "plan" in out["error"]


def test_drift_related_reaches_unnamed_state(tmp_path, capsys):
    """The state fact names no token 'AcmeWidget' (absent from search) and is not linked
    to the AcmeWidget entity (absent from probe) — only related's 1-hop (shared ZetaDock
    entity) reaches it. Needs the vector path: without numpy, related falls back to search."""
    pytest.importorskip("numpy")
    db = str(tmp_path / "m.db")
    _seed_pair(capsys, db)
    _state(capsys, db, "AcmeWidget crates stage at ZetaDock.")  # bridge: links AcmeWidget + ZetaDock
    hidden = _state(capsys, db, "ZetaDock crane maintenance is quarterly.")
    out = _run(capsys, "--db", db, "drift", "AcmeWidget", "--since", "2020-01-01", "--budget", "400")
    assert hidden in {f["fact_id"] for f in out["state"]["facts"]}


def test_drift_budget_caps_each_side(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    _plan(capsys, db, PLAN_ECHO + " The dock foreman checks the manifest twice.")  # ~90 chars = 23 tokens
    _state(capsys, db, STATE_ECHO + " The dock foreman checks the manifest twice.")  # ~97 chars = 25 tokens
    out = _run(capsys, "--db", db, "drift", "AcmeWidget", "--since", "2020-01-01", "--budget", "40")
    # budget 40 -> 20 per side: each side shows at most its one fact (23 > 20: nothing
    # fits on the plan side, 25 > 20: nothing on the state side)
    assert out["tokens"] == 0
    assert out["plan"]["count"] == 0 and out["state"]["count"] == 0
    out = _run(capsys, "--db", db, "drift", "AcmeWidget", "--since", "2020-01-01", "--budget", "60")
    # budget 60 -> 30 per side: both single facts fit
    assert out["plan"]["count"] == 1 and out["state"]["count"] == 1
    assert out["tokens"] <= 60
