"""Dream queue: the mark store for the offline consolidation pass (docs/dreaming.md).

A mark is a work item, not a memory: "something salient failed in this
session/turn — verify it against the transcript, then repair the store."
The queue lives in a dedicated SQLite file (default ``$HERMES_HOME/dream.db``)
managed by the CLI's ``dream`` verbs (``tiberius.cli dream mark/list/done/
prune/target``).

Where the marks live is a security decision, not an implementation detail:

- Not the memory store: the store DB is the ``read_stores`` federation
  surface (one writer, many readers, shared across profiles), and a mark's
  ``evidence`` field is a raw transcript excerpt — the most personal content
  in the system. It does not belong in a file whose contract is to be shared.
- Not loose files on disk: a directory of memory-bearing JSON is an
  unvalidated input channel to a write-capable LLM agent (a planted mark
  file is a prompt-injection vector into the dreamer), a disclosure surface
  (default-umask readable), and a retention/permissions mess (a growing set
  of files to get right). One 0600 file with a schema-validated write path
  replaces all three.

The CLI verbs are the only write surface: the queue has no second writer.
The dreamer itself is still an LLM agent with the existing ``fact_store``
tools — this module mechanizes the queue plumbing and the target
composition; the verify-and-repair judgment stays with the model.

Stdlib only (sqlite3); no agent-framework imports (core stays harness-free).
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Closed set of mark event types (docs/dreaming.md). A new source runtime adds
# triggers, not event types; the enum is what makes the queue a validated
# surface.
EVENTS = ("user_correction", "repeated_tool_error", "loop_guard", "version_conflict")

# Field caps: the queue is data an LLM agent reads, so the write path bounds
# what one mark can carry (the plugin's own caps — 200/1000 — are well inside).
SUMMARY_MAX = 500
EVIDENCE_MAX = 4000
ID_MAX = 128  # session_id / turn_id
SOURCE_MAX = 64

RETENTION_DAYS = 14

_SCHEMA = """
CREATE TABLE IF NOT EXISTS marks (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  source TEXT NOT NULL,
  session_id TEXT NOT NULL,
  event TEXT NOT NULL CHECK (event IN ('user_correction', 'repeated_tool_error', 'loop_guard', 'version_conflict')),
  turn_id TEXT NOT NULL,
  summary TEXT NOT NULL,
  evidence TEXT NOT NULL,
  fact_ids TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done')),
  done_at TEXT,
  UNIQUE (source, session_id, event, turn_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_dream_db_path(explicit: str | None = None) -> str:
    """``--dream-db`` > ``$TIBERIUS_DREAM_DB`` > ``$HERMES_HOME/dream.db``.

    A ``$HERMES_HOME`` placeholder in an explicit path is expanded from the
    environment (the provider's own path convention)."""
    raw = explicit or os.environ.get("TIBERIUS_DREAM_DB")
    home = os.environ.get("HERMES_HOME")
    if not raw:
        if not home:
            raise ValueError(
                "dream store path not resolvable: pass a path explicitly, or set "
                "TIBERIUS_DREAM_DB or HERMES_HOME (default: $HERMES_HOME/dream.db)"
            )
        raw = os.path.join(home, "dream.db")
    if home:
        raw = raw.replace("$HERMES_HOME", home).replace("${HERMES_HOME}", home)
    return str(Path(raw).expanduser())


class DreamQueue:
    """One profile's dream mark queue (a single SQLite file, 0600).

    Idempotency is a constraint, not a convention: the same
    (source, session_id, event, turn_id) is one row; re-marking upserts it
    and re-opens a done mark (a re-fired event is a new work item)."""

    def __init__(self, path: str | None = None):
        self.path = resolve_dream_db_path(path)
        parent = Path(self.path).parent
        parent.mkdir(parents=True, exist_ok=True)
        if not Path(self.path).exists():
            # 0600 at creation (umask can only weaken, never tighten, the
            # mode O_CREAT passes) — then re-asserted on every open.
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
        else:
            os.chmod(self.path, 0o600)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- write path (the only one) -----------------------------------------

    def mark(
        self,
        source: str,
        session_id: str,
        event: str,
        turn_id: str,
        summary: str,
        evidence: str,
        fact_ids: list[int] | None = None,
    ) -> dict:
        _bounded(source, "source", SOURCE_MAX)
        _bounded(session_id, "session_id", ID_MAX)
        _bounded(turn_id, "turn_id", ID_MAX)
        if event not in EVENTS:
            raise ValueError(f"unknown event {event!r}; expected one of: {', '.join(EVENTS)}")
        if len(summary) > SUMMARY_MAX:
            raise ValueError(f"summary exceeds {SUMMARY_MAX} chars")
        if len(evidence) > EVIDENCE_MAX:
            raise ValueError(f"evidence exceeds {EVIDENCE_MAX} chars")
        ids = [int(i) for i in (fact_ids or [])]
        ts = _now()
        key = (source, session_id, event, turn_id)
        old = self._conn.execute(
            'SELECT status FROM marks WHERE source = ? AND session_id = ? AND event = ? AND turn_id = ?',
            key,
        ).fetchone()
        self._conn.execute(
            """
            INSERT INTO marks (ts, source, session_id, event, turn_id, summary, evidence, fact_ids, status, done_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL)
            ON CONFLICT (source, session_id, event, turn_id) DO UPDATE SET
              ts = excluded.ts, summary = excluded.summary, evidence = excluded.evidence,
              fact_ids = excluded.fact_ids, status = 'pending', done_at = NULL
            """,
            (ts, *key, summary, evidence, json.dumps(ids)),
        )
        self._conn.commit()
        row = self._row(
            self._conn.execute(
                "SELECT * FROM marks WHERE source = ? AND session_id = ? AND event = ? AND turn_id = ?", key
            ).fetchone()
        )
        row["reopened"] = old is not None and old["status"] == "done"
        return row

    def done(self, mark_id: int) -> dict:
        """Flip one pending mark to done — atomic (only a pending row matches)."""
        cur = self._conn.execute(
            "UPDATE marks SET status = 'done', done_at = ? WHERE id = ? AND status = 'pending'",
            (_now(), mark_id),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"mark {mark_id} not found or not pending")
        return self._row(self._conn.execute("SELECT * FROM marks WHERE id = ?", (mark_id,)).fetchone())

    # -- read path -----------------------------------------------------------

    def list(self, status: str | None = "pending", limit: int = 100) -> list[dict]:
        """Queue contents, oldest first (``status=None``: everything)."""
        if status is not None and status not in ("pending", "done"):
            raise ValueError(f"unknown status {status!r}")
        sql = "SELECT * FROM marks"
        params: list = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY ts, id LIMIT ?"
        params.append(max(1, limit))
        return [self._row(r) for r in self._conn.execute(sql, params).fetchall()]

    def count(self, status: str | None = "pending") -> int:
        if status is not None and status not in ("pending", "done"):
            raise ValueError(f"unknown status {status!r}")
        if status is None:
            return self._conn.execute("SELECT COUNT(*) AS n FROM marks").fetchone()["n"]
        return self._conn.execute("SELECT COUNT(*) AS n FROM marks WHERE status = ?", (status,)).fetchone()["n"]

    def prune(self, days: int = RETENTION_DAYS) -> int:
        """Delete processed marks older than ``days`` (retention is SQL, not an
        LLM's file-reading discipline). Pending marks are never pruned."""
        if days < 0:
            raise ValueError("days must be >= 0")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cur = self._conn.execute(
            "DELETE FROM marks WHERE status = 'done' AND done_at < ?", (cutoff,)
        )
        self._conn.commit()
        return cur.rowcount

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        d["fact_ids"] = json.loads(d.get("fact_ids") or "[]")
        return d


def _bounded(value: str, name: str, limit: int) -> None:
    if not value or not isinstance(value, str):
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > limit:
        raise ValueError(f"{name} exceeds {limit} chars")


def compose_target(ret, store, project: str | None = None) -> dict:
    """The pass's focus set as one bundle: what the dreamer should concentrate
    on (Phase 2 targeting — composed from existing actions, no new retrieval
    primitives):

    - ``drift`` (needs a project name: pass the profile name; ``None`` when omitted) —
      plan claims with no state echo (``unbacked``)
    - ``hygiene`` — low-trust facts and near-duplicates
    - ``contradictions`` — entity-sharing pairs in tension
    - ``stats`` — store orientation (incl. ``zero_pool_rate``)

    The bundle is data: the agent reads it and judges. Defaults mirror the
    CLI's own action defaults."""
    from .drift import drift  # local: keeps module import cheap for queue-only callers

    return {
        "drift": drift(project, ret, store) if project else None,
        "hygiene": ret.hygiene(),
        "contradictions": ret.contradict(),
        "stats": store.stats(),
    }
