"""CLI smoke: add → search → probe → extract roundtrip through cli.main() on a temp DB."""

import json

from hermes_plugin_tiberius import cli


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


def test_cli_list(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    out = _run(capsys, "--db", db, "add", "The deploy pipeline uses Docker on the build host.")
    fid = out["fact_id"]

    out = _run(capsys, "--db", db, "list", "--top", "5")
    assert out["results"][0]["fact_id"] == fid
    assert "hrr_vector" not in out["results"][0]  # BLOBs never leak into CLI output
