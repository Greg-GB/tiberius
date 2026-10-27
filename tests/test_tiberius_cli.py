"""CLI smoke: add → search → probe → extract roundtrip through cli.main() on a temp DB."""

import json

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
