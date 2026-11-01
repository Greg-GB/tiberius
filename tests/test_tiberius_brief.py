"""`brief` (token-budgeted context pack): synthetic-fixture tests through cli.main().

Fictional content only (the eval fixture's rule): AcmeWidget, ZetaDock, crate
lines, the docks — no real store content is ever a fixture.
"""

import json

import pytest

from hermes_plugin_tiberius import cli


def _run(capsys, *argv: str):
    cli.main(list(argv))
    return json.loads(capsys.readouterr().out.strip())


def _seed(capsys, db: str, n: int = 4) -> list[int]:
    """n fictional facts, each matching 'AcmeWidget' (56 chars = 14 tokens)."""
    ids = []
    for i in range(n):
        out = _run(capsys, "--db", db, "add", f"AcmeWidget crate line {i} ships to the docks on Tuesdays.")
        ids.append(out["fact_id"])
    return ids


def test_brief_deterministic(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    _seed(capsys, db)
    a = _run(capsys, "--db", db, "brief", "AcmeWidget")
    b = _run(capsys, "--db", db, "brief", "AcmeWidget")
    assert a == b  # same store + query + params -> same output (retrieval counts excluded)


def test_brief_budget_fill(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    _seed(capsys, db, n=4)
    out = _run(capsys, "--db", db, "brief", "AcmeWidget", "--budget", "25")
    # each seeded fact is 14 tokens: one fits (14 <= 25), a second does not (28 > 25)
    assert out["count"] == 1
    assert out["tokens"] <= 25
    assert out["total"] == 4  # the window says the pool is bigger


def test_brief_whole_fact_drop(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    _run(capsys, "--db", db, "add", "AcmeWidget " + "filler " * 30)  # 161 chars = 41 tokens
    small = _run(capsys, "--db", db, "add", "AcmeWidget crates arrive Friday.")  # 30 chars = 8 tokens
    out = _run(capsys, "--db", db, "brief", "AcmeWidget", "--budget", "30")
    assert [f["fact_id"] for f in out["facts"]] == [small["fact_id"]]  # big fact dropped whole
    assert out["tokens"] <= 30


def test_brief_floor(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    _run(capsys, "--db", db, "add", "AcmeWidget shipping crates are loaded at the dock.")  # strong match
    _run(capsys, "--db", db, "add", "The dock ledger tracks status codes.")            # weak match
    out = _run(capsys, "--db", db, "brief", "AcmeWidget shipping status", "--floor", "0.25")
    assert len(out["facts"]) == 1
    assert out["facts"][0]["content"].startswith("AcmeWidget")  # weak match is below the floor
    out0 = _run(capsys, "--db", db, "brief", "AcmeWidget shipping status", "--floor", "0.0")
    assert out0["count"] == 2  # floor 0 keeps both


def test_brief_reports_total_and_window(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    _seed(capsys, db, n=5)
    # 'crate line docks' matches all five but names no entity -> no probe tier
    out = _run(capsys, "--db", db, "brief", "crate line docks", "--top", "3", "--budget", "500")
    assert out["total"] == 5  # whole matching pool (match_count, unrecorded)
    assert out["count"] == 3  # the window: top-3 pool, all fit the budget
    assert out["entity_only"] == 0


def test_brief_doc_pointer_passthrough(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    content = "AcmeWidget release procedure: see docs/acmewidget-release.md for the steps."
    _run(capsys, "--db", db, "add", content)
    out = _run(capsys, "--db", db, "brief", "AcmeWidget release")
    assert out["facts"][0]["content"] == content  # verbatim: brief never rewrites


def test_brief_dedupes_search_and_probe(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    fid = _run(capsys, "--db", db, "add", "AcmeWidget ships crates daily.")["fact_id"]
    out = _run(capsys, "--db", db, "brief", "AcmeWidget")
    ids = [f["fact_id"] for f in out["facts"]]
    assert ids.count(fid) == 1  # in both the search pool and the probe set -> shown once


def test_brief_skill_lane(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    _run(capsys, "--db", db, "add", "AcmeWidget usage: run the acmewidget CLI before shipping.", "--category", "skill")
    _run(capsys, "--db", db, "add", "AcmeWidget fleet policy requires two signoffs.", "--category", "skill")
    fid = _run(capsys, "--db", db, "add", "AcmeWidget crates ship on Tuesdays.")["fact_id"]
    out = _run(capsys, "--db", db, "brief", "AcmeWidget", "--budget", "500")
    assert all(f["category"] == "skill" for f in out["skills"])
    assert len(out["skills"]) == 2  # capped at the default --skills 2
    assert [f["fact_id"] for f in out["facts"]] == [fid]  # non-skill facts stay in the facts lane


def test_brief_entity_only_bypasses_floor(tmp_path, capsys):
    pytest.importorskip("numpy")  # probe's structural path (no numpy: probe falls back to search)
    db = str(tmp_path / "m.db")
    _run(capsys, "--db", db, "add", "AcmeWidget ships crates daily from ZetaDock.")
    b = _run(capsys, "--db", db, "add", "AcmeWidget quality reviews are annual.")
    _run(capsys, "--db", db, "update", str(b["fact_id"]), "--trust-delta=-0.35")  # 0.5 -> 0.15 < min_trust
    out = _run(capsys, "--db", db, "brief", "AcmeWidget shipping", "--floor", "0.5", "--budget", "500")
    assert [f["fact_id"] for f in out["facts"]] == [b["fact_id"]]
    assert out["entity_only"] == 1
    # the strong search fact scores below floor 0.5 and is dropped; the low-trust
    # entity-linked fact is not in the search pool at all (min_trust) and bypasses
    # the floor via the entity tier


def test_brief_records_retrievals(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    _seed(capsys, db, n=2)
    _run(capsys, "--db", db, "brief", "AcmeWidget", "--budget", "500")
    rows = _run(capsys, "--db", db, "list", "--top", "10")["results"]
    assert all(r["retrieval_count"] >= 1 for r in rows)  # a brief IS a recall


def test_brief_shared_function_direct_call(tmp_path, capsys):
    """The CLI subcommand and the Hermes provider action wrap the same brief()
    (one implementation, three consumers) — call it directly with kwargs,
    exactly as the provider's action handler does."""
    from hermes_plugin_tiberius.brief import brief
    from hermes_plugin_tiberius.retrieval import FactRetriever
    from hermes_plugin_tiberius.store import MemoryStore

    db = str(tmp_path / "m.db")
    _seed(capsys, db, n=3)
    store = MemoryStore(db_path=db)
    try:
        out = brief("AcmeWidget", FactRetriever(store), store, budget=25)
        assert out["query"] == "AcmeWidget" and out["budget"] == 25
        assert out["count"] == 1 and out["total"] == 3  # one 14-token fact fits; window says 3
    finally:
        store.close()


def test_brief_since_windows_pool_marker_and_probe_tier(tmp_path, capsys):
    """brief --since: search pool, the total marker, AND the entity-probe tier all
    honor the window (a stale entity-linked fact must not re-enter the pack)."""
    from hermes_plugin_tiberius.store import MemoryStore
    db = str(tmp_path / "m.db")
    ids = _seed(capsys, db, n=4)
    store = MemoryStore(db_path=db)
    store._conn.execute("UPDATE facts SET created_at = '2026-01-01 00:00:00' WHERE fact_id IN (?, ?)", (ids[0], ids[1]))
    store.close()
    out = _run(capsys, "--db", db, "brief", "AcmeWidget", "--budget", "500", "--since", "24h")
    assert out["count"] == 2
    assert out["total"] == 2  # the marker describes the window, not the store
    shown = {f["fact_id"] for f in out["facts"] + out["skills"]}
    assert ids[0] not in shown and ids[1] not in shown  # the probe tier would have re-admitted them
    out = _run(capsys, "--db", db, "brief", "AcmeWidget", "--budget", "500")
    assert out["count"] == 4 and out["total"] == 4


def test_brief_pool_excludes_checkpoint_facts(tmp_path, capsys):
    """docs/checkpoint.md: checkpoint facts are session state, not durable
    knowledge — the pool drops them and 'total' describes the same filtered
    pool (the house invariant from docs/search-categories.md)."""
    db = str(tmp_path / "m.db")
    _seed(capsys, db, n=2)
    _run(capsys, "--db", db, "checkpoint", "set", "s1", "task: AcmeWidget handoff line", "next: AcmeWidget test plan")
    out = _run(capsys, "--db", db, "brief", "AcmeWidget")
    assert out["total"] == 2  # the two checkpoint lines are out of the marker too
    assert out["count"] <= 2
    assert all(r["category"] != "checkpoint" for r in out["facts"])


def test_brief_entity_tier_skips_checkpoint_facts(tmp_path, capsys):
    """The probe tier applies the same rule: a ZetaDock-linked checkpoint fact
    that fell out of the token pool (low trust) must not re-enter the pack via
    the entity-probe path (docs/checkpoint.md)."""
    db = str(tmp_path / "m.db")
    normal = _run(capsys, "--db", db, "add", "The ZetaDock launcher starts on boot.")
    cp = _run(capsys, "--db", db, "checkpoint", "set", "s1", "task: migrate the ZetaDock database")
    cp_id = cp["fact_ids"][0]
    for _ in range(6):  # 0.5 -> clamped 0.0: below the pool's min_trust (0.3)
        _run(capsys, "--db", db, "feedback", str(cp_id), "unhelpful")
    out = _run(capsys, "--db", db, "brief", "ZetaDock")
    ids = [r["fact_id"] for r in out["facts"]]
    assert normal["fact_id"] in ids
    assert cp_id not in ids
    assert out["entity_only"] == 0


def test_brief_pool_excludes_superseded_facts(tmp_path, capsys):
    """docs/supersede.md: a superseded fact is out of the pool, and 'total'
    describes the same hidden pool, and the replacement is what surfaces."""
    db = str(tmp_path / "m.db")
    ids = _seed(capsys, db, n=3)
    _run(capsys, "--db", db, "supersede", str(ids[0]),
         "AcmeWidget crate line 0 ships to the docks on Wednesdays.")
    out = _run(capsys, "--db", db, "brief", "AcmeWidget")
    assert out["total"] == 3  # the superseded line is out of the marker too
    contents = {r["content"] for r in out["facts"]}
    assert "AcmeWidget crate line 0 ships to the docks on Tuesdays." not in contents
