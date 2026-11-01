"""CLI smoke: add → search → probe → extract roundtrip through cli.main() on a temp DB."""

import json

import pytest

from hermes_plugin_tiberius import cli
from hermes_plugin_tiberius import holographic as hrr


def _run(capsys, *argv: str):
    cli.main(list(argv))
    return json.loads(capsys.readouterr().out.strip())


def test_cli_roundtrip(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    out = _run(capsys, "--db", db, "add", "The deploy pipeline uses Docker on the build host.")
    fid = out["fact_id"]

    out = _run(capsys, "--db", db, "search", "docker deploy host", "--top", "3")
    assert out["results"][0]["fact_id"] == fid
    assert "hrr_vector" not in out["results"][0]  # BLOBs never leak into CLI output


def test_cli_search_reports_total_pool(tmp_path, capsys):
    """search output carries 'total' (full matching pool) so callers can tell a window
    from the whole; 'count' stays the number returned. Non-search commands are untouched."""
    db = str(tmp_path / "c.db")
    for i in range(7):
        _run(capsys, "--db", db, "add", f"The widget factory shipped batch {i} to the docks.")
    out = _run(capsys, "--db", db, "search", "widget factory", "--top", "3")
    assert out["count"] == 3 and out["total"] == 7
    out = _run(capsys, "--db", db, "probe", "widget factory")
    assert "total" not in out


def test_cli_show_roundtrip(tmp_path, capsys):
    """show: one fact by id — full row plus entities; BLOBs never leak into CLI output."""
    db = str(tmp_path / "s.db")
    out = _run(capsys, "--db", db, "add", "AcmeWidget aka AW ships to the docks on Tuesdays.")
    fid = out["fact_id"]
    out = _run(capsys, "--db", db, "show", str(fid))
    f = out["fact"]
    assert f["fact_id"] == fid
    assert f["content"] == "AcmeWidget aka AW ships to the docks on Tuesdays."
    assert {"category", "tags", "trust_score", "retrieval_count", "created_at", "updated_at", "entities"} <= set(f)
    ents = {e["name"].lower(): e for e in f["entities"]}
    assert "aw" in [a.lower() for a in ents["acmewidget"]["aliases"]]
    assert "hrr_vector" not in f  # same rule as search output


def test_cli_show_unknown_id_is_error(tmp_path, capsys):
    """Unknown id: {"error": ...} + exit 1 — the same contract as remove/feedback."""
    db = str(tmp_path / "s2.db")
    _run(capsys, "--db", db, "add", "The AcmeWidget ships to the docks on Tuesdays.")
    rc = cli.main(["--db", db, "show", "99999"])
    out = json.loads(capsys.readouterr().out.strip())
    assert rc == 1 and "not found" in out["error"]


def test_cli_entities_lists_bare_array(tmp_path, capsys):
    """entities: the contract is a bare JSON array (no {"results": ...} wrapper),
    fact_count desc then name asc, aliases parsed to a list."""
    db = str(tmp_path / "e.db")
    _run(capsys, "--db", db, "add", "AcmeWidget aka AW ships to the docks on Tuesdays.")
    _run(capsys, "--db", db, "add", "AcmeWidget crates are stamped with the batch number.")
    _run(capsys, "--db", db, "add", "BetaCo signs the dock manifests every Friday.")
    out = _run(capsys, "--db", db, "entities")
    assert isinstance(out, list)
    assert [r["name"] for r in out] == ["AcmeWidget", "BetaCo", "Friday", "Tuesdays"]
    assert [r["fact_count"] for r in out] == [2, 1, 1, 1]
    assert out[0]["aliases"] == ["AW"]


def test_cli_entities_top_caps_the_list(tmp_path, capsys):
    db = str(tmp_path / "e.db")
    _run(capsys, "--db", db, "add", "AcmeWidget aka AW ships to the docks on Tuesdays.")
    _run(capsys, "--db", db, "add", "BetaCo signs the dock manifests every Friday.")
    out = _run(capsys, "--db", db, "entities", "--top", "1")
    assert [r["name"] for r in out] == ["AcmeWidget"]  # tied counts: name asc


def test_cli_probe_and_extract(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "m.db")
    _run(capsys, "--db", db, "add", "The deploy pipeline uses Docker on the build host.")

    out = _run(capsys, "--db", db, "probe", "Docker")
    assert any(f["fact_id"] == 1 for f in out["results"])  # Docker extracted as entity at add time

    import io
    import sys
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        [{"role": "user", "content": "I prefer concise direct answers and the project uses Fastify with SQLite"}])))
    out = _run(capsys, "--db", db, "extract")
    assert out["added"] == 2  # one user_pref statement + one project statement


def test_cli_add_reports_possible_duplicates(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    _run(capsys, "--db", db, "add", "The deploy pipeline uses Docker on the build host.")

    out = _run(capsys, "--db", db, "add", "Deploy pipeline uses Docker on the build host.")

    dups = out["possible_duplicates"]
    assert dups and dups[0]["fact_id"] == 1  # near-identical prior fact is surfaced
    assert "content" in dups[0] and "score" in dups[0]
    assert "remove the older" in out["note"]  # note is present and tells the agent what to do


def test_cli_add_omits_supplemental_fields_when_clean(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    out = _run(capsys, "--db", db, "add", "The deploy pipeline uses Docker on the build host.")
    assert "possible_duplicates" not in out  # empty supplement is omitted — no noise on a clean add
    assert "note" not in out


def test_cli_list_since(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    _run(capsys, "--db", db, "add", "The deploy pipeline uses Docker on the build host.")

    assert _run(capsys, "--db", db, "list", "--since", "24h")["count"] == 1   # relative form
    assert _run(capsys, "--db", db, "list", "--since", "2000-01-01")["count"] == 1  # ISO date form

    rc = cli.main(["--db", db, "list", "--since", "banana"])
    assert rc == 1  # bad since is a JSON error + exit 1, not a traceback
    assert "error" in json.loads(capsys.readouterr().out.strip())


def test_cli_feedback_unknown_fact_id_is_json_error(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    _run(capsys, "--db", db, "add", "The deploy pipeline uses Docker on the build host.")

    rc = cli.main(["--db", db, "feedback", "999", "helpful"])

    assert rc == 1  # JSON error on stdout, exit 1 — not a traceback (the CLI contract)
    assert "error" in json.loads(capsys.readouterr().out.strip())


def test_cli_remove(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    out = _run(capsys, "--db", db, "add", "The deploy pipeline uses Docker on the build host.")
    fid = out["fact_id"]

    out = _run(capsys, "--db", db, "remove", str(fid))
    assert out == {"removed": fid}

    out = _run(capsys, "--db", db, "search", "docker deploy host", "--top", "3")
    assert out["results"] == []  # fact row and its FTS entry are both gone

    rc = cli.main(["--db", db, "remove", "999"])
    assert rc == 1  # unknown id → JSON error, same contract as feedback
    assert "error" in json.loads(capsys.readouterr().out.strip())


def test_cli_update(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    out = _run(capsys, "--db", db, "add", "The deploy pipeline uses Docker on the build host.")
    fid = out["fact_id"]

    out = _run(capsys, "--db", db, "update", str(fid),
               "--content", "The deploy pipeline uses Docker on the CI host.", "--tags", "deploy,docker")
    assert out == {"updated": fid}

    # New content is searchable under the new tokens; the removed token no longer matches
    # (the FTS update trigger reindexed, not just the facts row).
    out = _run(capsys, "--db", db, "search", "docker ci host", "--top", "3")
    assert out["results"][0]["fact_id"] == fid
    assert out["results"][0]["tags"] == "deploy,docker"
    out = _run(capsys, "--db", db, "search", "build", "--top", "3")
    assert out["results"] == []


def test_cli_update_trust_delta(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    out = _run(capsys, "--db", db, "add", "The deploy pipeline uses Docker on the build host.")
    fid = out["fact_id"]

    out = _run(capsys, "--db", db, "update", str(fid), "--trust-delta", "0.25")
    assert out == {"updated": fid}
    out = _run(capsys, "--db", db, "list", "--top", "5")
    assert out["results"][0]["trust_score"] == 0.75  # 0.5 default + 0.25, clamped to [0, 1]


def test_cli_update_errors(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    _run(capsys, "--db", db, "add", "The deploy pipeline uses Docker on the build host.")

    rc = cli.main(["--db", db, "update", "999", "--content", "x"])
    assert rc == 1  # unknown id → JSON error, same contract as remove/feedback
    assert "error" in json.loads(capsys.readouterr().out.strip())

    rc = cli.main(["--db", db, "update", "1"])
    assert rc == 1  # no fields: a bare update is an error, not a silent updated_at touch
    assert "error" in json.loads(capsys.readouterr().out.strip())


def test_cli_contradict(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    _run(capsys, "--db", db, "add", "The Tiberius project stores facts in SQLite.")
    _run(capsys, "--db", db, "add", "The Tiberius project stores facts in PostgreSQL.")

    out = _run(capsys, "--db", db, "contradict", "--threshold", "0.0", "--top", "5")
    if not hrr._HAS_NUMPY:
        # contradict is HRR-only by design: content similarity needs vectors, so keyword
        # mode degrades to empty (no crash, no entity-sharing false positives)
        assert out["count"] == 0
        return
    assert out["count"] == 1
    pair = out["results"][0]
    assert "tiberius" in pair["shared_entities"]
    assert {pair["fact_a"]["content"], pair["fact_b"]["content"]} == {
        "The Tiberius project stores facts in SQLite.",
        "The Tiberius project stores facts in PostgreSQL.",
    }


def test_cli_list(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    out = _run(capsys, "--db", db, "add", "The deploy pipeline uses Docker on the build host.")
    fid = out["fact_id"]

    out = _run(capsys, "--db", db, "list", "--top", "5")
    assert out["results"][0]["fact_id"] == fid
    assert "hrr_vector" not in out["results"][0]  # BLOBs never leak into CLI output


def test_cli_search_since_windows_results_and_total(tmp_path, capsys):
    """search --since: windowed results AND windowed total (the 'of N' marker)."""
    from hermes_plugin_tiberius.store import MemoryStore
    db = str(tmp_path / "c.db")
    first = _run(capsys, "--db", db, "add", "The widget factory shipped batch 0 to the docks.")
    _run(capsys, "--db", db, "add", "The widget factory shipped batch 1 to the docks.")
    _run(capsys, "--db", db, "add", "The widget factory shipped batch 2 to the docks.")
    store = MemoryStore(db_path=db)
    store._conn.execute("UPDATE facts SET created_at = '2026-01-01 00:00:00' WHERE fact_id = ?", (first["fact_id"],))
    store.close()
    out = _run(capsys, "--db", db, "search", "widget factory", "--top", "10", "--since", "24h")
    assert out["count"] == 2 and out["total"] == 2
    out = _run(capsys, "--db", db, "search", "widget factory", "--top", "10")
    assert out["count"] == 3 and out["total"] == 3


def test_cli_search_since_invalid_is_json_error(tmp_path, capsys):
    """bad --since value: JSON error on stdout, not a traceback."""
    db = str(tmp_path / "c.db")
    _run(capsys, "--db", db, "add", "The widget factory shipped batch 0.")
    out = _run(capsys, "--db", db, "search", "widget factory", "--since", "banana")
    assert "error" in out and "banana" in out["error"]


def test_cli_search_categories_and_exclude_filter_results_and_total(tmp_path, capsys):
    """search --categories a b + --exclude-category: both results and the 'of N' total are filtered."""
    db = str(tmp_path / "m.db")
    _run(capsys, "--db", db, "add", "The widget factory shipped batch 0 to the docks.", "--category", "project")
    _run(capsys, "--db", db, "add", "The widget factory shipped batch 1 to the docks.", "--category", "tool")
    _run(capsys, "--db", db, "add", "The widget factory shipped batch 2 to the docks.")  # general
    out = _run(capsys, "--db", db, "search", "widget factory docks",
               "--categories", "project", "tool", "--top", "10")
    assert out["count"] == 2 and out["total"] == 2
    assert {r["category"] for r in out["results"]} == {"project", "tool"}
    out = _run(capsys, "--db", db, "search", "widget factory docks",
               "--categories", "project", "tool", "--exclude-category", "tool", "--top", "10")
    assert out["count"] == 1 and out["total"] == 1
    assert out["results"][0]["category"] == "project"
    # --categories wins over --category when both are passed
    out = _run(capsys, "--db", db, "search", "widget factory docks",
               "--category", "general", "--categories", "project", "tool", "--top", "10")
    assert {r["category"] for r in out["results"]} == {"project", "tool"}


def test_cli_add_batch_reads_json_array_from_stdin(tmp_path, capsys, monkeypatch):
    """add-batch: JSON array on stdin (strings or objects) -> {added, duplicates, fact_ids}."""
    import io
    import sys
   
    db = str(tmp_path / "c.db")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps([
        "The widget factory ships batch 0 to the docks.",
        {"content": "The widget factory keeps its ledger in SQLite.", "category": "project"},
    ])))
    out = _run(capsys, "--db", db, "add-batch")
    assert out["added"] == 2 and out["duplicates"] == 0 and len(out["fact_ids"]) == 2
    ids = set(out["fact_ids"])
    out = _run(capsys, "--db", db, "search", "widget factory", "--top", "10")
    assert {r["fact_id"] for r in out["results"]} == ids


def test_cli_add_batch_empty_array_is_json_error(tmp_path, capsys, monkeypatch):
    import io
    import sys
    db = str(tmp_path / "c.db")
    monkeypatch.setattr(sys, "stdin", io.StringIO("[]"))
    out = _run(capsys, "--db", db, "add-batch")
    assert "error" in out and "non-empty" in out["error"]


def test_cli_feedback_comma_ids_batches_and_single_id_unchanged(tmp_path, capsys):
    """feedback 5,6,7 -> batch shape; feedback 5 -> today's exact single shape."""
    db = str(tmp_path / "c.db")
    a = _run(capsys, "--db", db, "add", "The widget factory ships batch 0 to the docks.")["fact_id"]
    b = _run(capsys, "--db", db, "add", "The widget factory keeps its ledger in SQLite.")["fact_id"]
    out = _run(capsys, "--db", db, "feedback", f"{a},{b}", "helpful")
    assert out["applied"] == 2 and all(r["ok"] for r in out["results"])
    out = _run(capsys, "--db", db, "feedback", str(a), "helpful")
    assert out["fact_id"] == a and "results" not in out  # single-id path byte-identical

def test_cli_stats_output(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    _run(capsys, "--db", db, "add", "The zephyr engine starts at dawn.")
    out = _run(capsys, "--db", db, "stats")
    assert out["total"] == 1
    assert out["by_category"] == {"general": 1}
    assert out["trust_buckets"] == {"0.5-0.7": 1}
    assert out["stale_count"] == 0


def test_cli_list_order_and_rejects_bad_order(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    fid = _run(capsys, "--db", db, "add", "The zephyr engine starts at dawn.")["fact_id"]
    out = _run(capsys, "--db", db, "list", "--order", "recent")
    assert out["results"][0]["fact_id"] == fid
    with pytest.raises(SystemExit):
        _run(capsys, "--db", db, "list", "--order", "banana")  # argparse rejects unknown --order


def test_cli_probe_union_and_single(tmp_path, capsys):
    """CLI probe is variadic (docs/probe-union.md): `probe A B` unions the entity sets;
    one name still behaves exactly as before. The shared fact (linked to both entities)
    is pinned explicitly so an intersection bug — which would drop it — cannot hide
    behind the FTS fallback."""
    pytest.importorskip("numpy")
    db = str(tmp_path / "m.db")
    a = _run(capsys, "--db", db, "add", "Zephyr handles the nightly backups.")["fact_id"]
    b = _run(capsys, "--db", db, "add", "Nimbus fronts the public API gateway.")["fact_id"]
    shared = _run(capsys, "--db", db, "add", "Zephyr and Nimbus share the staging network.")["fact_id"]
    _run(capsys, "--db", db, "add", "Cirrus compiles the release artifacts.")["fact_id"]
    out = _run(capsys, "--db", db, "probe", "Zephyr", "Nimbus")
    ids = {r["fact_id"] for r in out["results"]}
    assert {a, b, shared} <= ids  # any-of union, shared fact included once
    assert len(ids) == len(out["results"])  # no duplicate rows
    out1 = _run(capsys, "--db", db, "probe", "Zephyr")
    ids1 = {r["fact_id"] for r in out1["results"]}
    assert a in ids1 and shared in ids1 and b not in ids1  # single name unchanged


def test_cli_checkpoint_roundtrip_replace_and_clear(tmp_path, capsys):
    """docs/checkpoint.md via the CLI: set (replace semantics) -> show -> set
    again -> clear."""
    db = str(tmp_path / "m.db")
    out = _run(capsys, "--db", db, "checkpoint", "set", "s1", "task: ship the AcmeWidget", "next: write tests")
    assert out["session"] == "s1"
    assert out["replaced"] == 0
    assert out["added"] == 2
    out = _run(capsys, "--db", db, "checkpoint", "show", "s1")
    assert out["count"] == 2
    assert {r["content"] for r in out["results"]} == {"task: ship the AcmeWidget", "next: write tests"}
    assert all(r["category"] == "checkpoint" for r in out["results"])
    out = _run(capsys, "--db", db, "checkpoint", "set", "s1", "task: done")
    assert out["replaced"] == 2
    assert _run(capsys, "--db", db, "checkpoint", "show", "s1")["count"] == 1
    assert _run(capsys, "--db", db, "checkpoint", "clear", "s1")["cleared"] == 1
    assert _run(capsys, "--db", db, "checkpoint", "show", "s1")["count"] == 0
    # an unknown session shows empty, not an error
    assert _run(capsys, "--db", db, "checkpoint", "show", "nope") == {"session": "nope", "results": [], "count": 0}


def test_cli_checkpoint_bounds_error(tmp_path, capsys):
    """More than 8 lines is a handoff-size violation: JSON error, exit 1, no
    partial write."""
    db = str(tmp_path / "m.db")
    rc = cli.main(["--db", db, "checkpoint", "set", "s1"] + [f"line {i}" for i in range(9)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert "1-8" in out["error"]
    assert _run(capsys, "--db", db, "checkpoint", "show", "s1")["count"] == 0


def test_cli_supersede_roundtrip_and_list_flag(tmp_path, capsys):
    """docs/supersede.md via the CLI: supersede replaces a fact, the old row
    is hidden from list by default, --include-superseded shows it, show still
    returns it with the pointer."""
    db = str(tmp_path / "m.db")
    fid = _run(capsys, "--db", db, "add", "The AcmeWidget service listens on port 7777.")["fact_id"]
    out = _run(capsys, "--db", db, "supersede", str(fid),
               "The AcmeWidget service now listens on port 8081.",
               "--category", "tool", "--tags", "ports,infra")
    assert out["superseded_fact_id"] == fid
    assert out["new_fact_id"] != fid
    assert _run(capsys, "--db", db, "show", str(fid))["fact"]["superseded_by"] == out["new_fact_id"]
    listed = _run(capsys, "--db", db, "list")["results"]
    assert all(r["fact_id"] != fid for r in listed)
    included = _run(capsys, "--db", db, "list", "--include-superseded")["results"]
    assert {r["fact_id"] for r in included} == {fid, out["new_fact_id"]}


def test_cli_supersede_errors(tmp_path, capsys):
    """Unknown id and duplicate content are JSON errors with exit 1 — same
    contract as remove/update."""
    db = str(tmp_path / "m.db")
    fid = _run(capsys, "--db", db, "add", "The AcmeWidget service listens on port 7777.")["fact_id"]
    rc = cli.main(["--db", db, "supersede", "999", "A brand new statement."])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert "not found" in out["error"]
    rc = cli.main(["--db", db, "supersede", str(fid), "The AcmeWidget service listens on port 7777."])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert "content" in out["error"]
    assert len(_run(capsys, "--db", db, "list", "--include-superseded")["results"]) == 1
