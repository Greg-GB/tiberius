"""Dream queue (docs/dreaming.md): mark store + CLI verbs.

Fictional content only (the eval fixture's rule): AcmeWidget, ZetaDock,
crates, the dock, the ledger — no real store content is ever a fixture.

Covers: mark validation (event enum, field caps), idempotent upsert and
re-open on re-mark, atomic done, SQL retention (prune), 0600 posture,
default path resolution, and the target bundle (via the CLI).
"""
import json
import os
import sqlite3
import stat
from datetime import datetime, timedelta, timezone

import pytest

from hermes_plugin_tiberius import cli
from hermes_plugin_tiberius.dream import DreamQueue, resolve_dream_db_path


def _run(capsys, *argv: str):
    rc = cli.main(list(argv))
    return rc, json.loads(capsys.readouterr().out.strip())


def _mark(capsys, dream_db: str, **kw):
    args = [
        "--dream-db", dream_db, "dream", "mark",
        "--source", kw.get("source", "hermes"),
        "--session", kw.get("session", "ses-1"),
        "--event", kw.get("event", "user_correction"),
        "--turn", kw.get("turn", "t1"),
        "--summary", kw.get("summary", "AcmeWidget deploy failed: crate rejected the ledger code"),
        "--evidence", kw.get("evidence", "user: no, that's not right — the release script is release.sh"),
    ]
    if kw.get("fact_ids") is not None:
        args += ["--fact-ids", kw["fact_ids"]]
    return _run(capsys, *args)


def test_mark_and_list(tmp_path, capsys):
    db = str(tmp_path / "dream.db")
    rc, out = _mark(capsys, db)
    assert rc == 0
    assert out["status"] == "pending"
    assert out["reopened"] is False
    assert out["fact_ids"] == []
    rc, out = _mark(capsys, db, event="repeated_tool_error", turn="t1", fact_ids="115,117")
    assert rc == 0
    rc, listed = _run(capsys, "--dream-db", db, "dream", "list")
    assert rc == 0
    assert listed["count"] == 2
    events = {m["event"] for m in listed["results"]}
    assert events == {"user_correction", "repeated_tool_error"}
    by_event = {m["event"]: m for m in listed["results"]}
    assert by_event["repeated_tool_error"]["fact_ids"] == [115, 117]
    assert by_event["user_correction"]["source"] == "hermes"
    # done list is disjoint: nothing processed yet
    rc, done = _run(capsys, "--dream-db", db, "dream", "list", "--status", "done")
    assert done["count"] == 0


def test_mark_idempotent_per_source_session_event_turn(tmp_path, capsys):
    db = str(tmp_path / "dream.db")
    _mark(capsys, db, summary="first observation")
    _mark(capsys, db, summary="second observation of the same turn")
    rc, listed = _run(capsys, "--dream-db", db, "dream", "list")
    assert listed["count"] == 1
    assert listed["results"][0]["summary"] == "second observation of the same turn"
    # A different turn in the same session is a different mark
    _mark(capsys, db, turn="t2", summary="a later turn")
    rc, listed = _run(capsys, "--dream-db", db, "dream", "list")
    assert listed["count"] == 2


def test_done_atomic_and_remark_reopens(tmp_path, capsys):
    db = str(tmp_path / "dream.db")
    _, m1 = _mark(capsys, db)
    rc, done = _run(capsys, "--dream-db", db, "dream", "done", str(m1["id"]))
    assert rc == 0
    assert done["status"] == "done"
    assert done["done_at"]
    # Second done on the same id: an error, not a no-op success
    rc, err = _run(capsys, "--dream-db", db, "dream", "done", str(m1["id"]))
    assert rc == 1
    assert "not pending" in err["error"]
    # Unknown id: same contract
    rc, err = _run(capsys, "--dream-db", db, "dream", "done", "9999")
    assert rc == 1
    # Re-marking the same quadruple re-opens the work item
    rc, rem = _mark(capsys, db, summary="the event fired again after a reload")
    assert rc == 0
    assert rem["status"] == "pending"
    assert rem["done_at"] is None
    assert rem["reopened"] is True


def test_prune_retention(tmp_path, capsys):
    db = str(tmp_path / "dream.db")
    _, old = _mark(capsys, db, turn="t1")
    _, recent = _mark(capsys, db, turn="t2")
    _, pending = _mark(capsys, db, turn="t3")
    _run(capsys, "--dream-db", db, "dream", "done", str(old["id"]))
    _run(capsys, "--dream-db", db, "dream", "done", str(recent["id"]))
    # Backdate the first done mark 20 days (beyond the 14-day default)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE marks SET done_at = ? WHERE id = ?", (cutoff, old["id"]))
    conn.commit()
    conn.close()
    rc, out = _run(capsys, "--dream-db", db, "dream", "prune")
    assert rc == 0
    assert out["deleted"] == 1
    _, listed = _run(capsys, "--dream-db", db, "dream", "list", "--status", "done")
    assert [m["turn_id"] for m in listed["results"]] == ["t2"]  # recent done survives
    _, pend = _run(capsys, "--dream-db", db, "dream", "list", "--status", "pending")
    assert [m["turn_id"] for m in pend["results"]] == ["t3"]  # pending is never pruned


def test_validation(tmp_path):
    q = DreamQueue(str(tmp_path / "q.db"))
    with pytest.raises(ValueError, match="unknown event"):
        q.mark("hermes", "s", "nope", "t", "x", "y")
    with pytest.raises(ValueError, match="summary exceeds"):
        q.mark("hermes", "s", "user_correction", "t", "x" * 501, "y")
    with pytest.raises(ValueError, match="evidence exceeds"):
        q.mark("hermes", "s", "user_correction", "t", "x", "y" * 4001)
    with pytest.raises(ValueError, match="non-empty"):
        q.mark("", "s", "user_correction", "t", "x", "y")
    with pytest.raises(ValueError, match="session_id exceeds"):
        q.mark("hermes", "s" * 129, "user_correction", "t", "x", "y")
    q.close()
    # The CLI boundary enforces the same rules (JSON error, exit 1) —
    # the unknown-event case is pinned below.


def test_event_enum_rejected_at_cli(tmp_path, capsys):
    db = str(tmp_path / "dream.db")
    rc, err = _mark(capsys, db, event="exploded")
    assert rc == 1
    assert "unknown event" in err["error"]
    for e in ("user_correction", "repeated_tool_error", "loop_guard", "version_conflict"):
        rc, _ = _mark(capsys, db, event=e, turn=f"t-{e}")
        assert rc == 0


def test_queue_file_is_0600(tmp_path):
    p = str(tmp_path / "q.db")
    q = DreamQueue(p)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    q.close()
    # An existing file with a weaker mode is re-asserted to 0600 on open
    fd = os.open(p, os.O_CREAT | os.O_RDWR, 0o644)
    os.close(fd)
    q = DreamQueue(p)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    q.close()


def test_default_path_resolution(tmp_path, monkeypatch):
    # conftest isolates HERMES_HOME per test
    home = os.environ["HERMES_HOME"]
    assert resolve_dream_db_path(None) == os.path.join(home, "dream.db")
    assert resolve_dream_db_path("$HERMES_HOME/elsewhere/dream.db") == os.path.join(home, "elsewhere", "dream.db")
    monkeypatch.setenv("TIBERIUS_DREAM_DB", str(tmp_path / "env.db"))
    assert resolve_dream_db_path(None) == str(tmp_path / "env.db")
    monkeypatch.setenv("TIBERIUS_DREAM_DB", "$HERMES_HOME/env2.db")
    assert resolve_dream_db_path(None) == os.path.join(home, "env2.db")
    # Explicit wins over the environment
    monkeypatch.setenv("TIBERIUS_DREAM_DB", str(tmp_path / "env.db"))
    assert resolve_dream_db_path(str(tmp_path / "explicit.db")) == str(tmp_path / "explicit.db")
    # Nothing set: a clear error, not a surprise location
    monkeypatch.delenv("TIBERIUS_DREAM_DB")
    monkeypatch.delenv("HERMES_HOME")
    with pytest.raises(ValueError, match="not resolvable"):
        resolve_dream_db_path(None)


def test_target_bundle(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    dream_db = str(tmp_path / "dream.db")
    _run(capsys, "--db", db, "add", "AcmeWidget plan: crate loads happen at the dock before dawn.",
         "--category", "project", "--tags", "plan")
    _run(capsys, "--db", db, "add", "AcmeWidget decision: crate loads at the dock now happen before dawn.",
         "--category", "project", "--tags", "state")
    rc, out = _run(capsys, "--db", db, "--dream-db", dream_db, "dream", "target", "--project", "AcmeWidget")
    assert rc == 0
    assert set(out) == {"drift", "hygiene", "contradictions", "stats"}
    assert out["drift"] is not None  # project given -> the drift side is present
    assert out["stats"]["total"] == 2
    rc, out = _run(capsys, "--db", db, "--dream-db", dream_db, "dream", "target")
    assert rc == 0
    assert out["drift"] is None  # no project -> no drift section


def test_mark_is_not_in_the_memory_store(tmp_path, capsys):
    """The queue is a separate file: the memory store stays mark-free (the
    read_stores federation surface must never carry raw transcript content)."""
    db = str(tmp_path / "m.db")
    dream_db = str(tmp_path / "dream.db")
    _mark(capsys, dream_db, source="opencode", session="ses-9",
          evidence="bash: sh: deploy.sh: Permission denied")
    rc, facts = _run(capsys, "--db", db, "list", "--top", "100")
    assert rc == 0
    assert facts["count"] == 0  # nothing leaked into the memory store
