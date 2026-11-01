"""SQLite-backed fact store with entity resolution and trust scoring (single-user Hermes memory plugin)."""

import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import holographic as hrr

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    fact_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content         TEXT NOT NULL UNIQUE,
    category        TEXT DEFAULT 'general',
    tags            TEXT DEFAULT '',
    trust_score     REAL DEFAULT 0.5,
    retrieval_count INTEGER DEFAULT 0,
    helpful_count   INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    hrr_vector      BLOB
);

CREATE TABLE IF NOT EXISTS entities (
    entity_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    entity_type TEXT DEFAULT 'unknown',
    aliases     TEXT DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fact_entities (
    fact_id   INTEGER REFERENCES facts(fact_id),
    entity_id INTEGER REFERENCES entities(entity_id),
    PRIMARY KEY (fact_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_facts_trust    ON facts(trust_score DESC);
CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category);
CREATE INDEX IF NOT EXISTS idx_entities_name  ON entities(name);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
    USING fts5(content, tags, content=facts, content_rowid=fact_id);

CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, content, tags)
        VALUES (new.fact_id, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
        VALUES ('delete', old.fact_id, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts
    WHEN old.content IS NOT new.content OR old.tags IS NOT new.tags BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
        VALUES ('delete', old.fact_id, old.content, old.tags);
    INSERT INTO facts_fts(rowid, content, tags)
        VALUES (new.fact_id, new.content, new.tags);
END;

-- Legacy from upstream: no longer read or written (composition happens in the
-- fact_entities SQL graph; probe/related/reason bundle context on the fly).
-- Kept so existing databases stay untouched.
CREATE TABLE IF NOT EXISTS memory_banks (
    bank_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    bank_name  TEXT NOT NULL UNIQUE,
    vector     BLOB NOT NULL,
    dim        INTEGER NOT NULL,
    fact_count INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

_HELPFUL_DELTA, _UNHELPFUL_DELTA = 0.05, -0.10

# Entity extraction patterns, applied in order: capitalized multi-word phrases ("John Doe"), single capitalized
# words (proper nouns: Alice, SQLite, PostgreSQL), double-quoted terms, single-quoted terms.
# "X aka Y" is handled separately by _link_entities as primary + alias.
_RE_MULTI_WORD_ENTITY = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b')
_RE_SINGLE_WORD_ENTITY = re.compile(r'\b([A-Z][a-zA-Z0-9]{3,})\b')
_RE_QUOTED = (re.compile(r'"([^"]+)"'), re.compile(r"'([^']+)'"))
# Common English words that get capitalized (sentence starts, pronouns, auxiliaries, frequent verbs):
# a single-word candidate in this set is not a proper noun, and a multi-word candidate STARTING with one
# of these is sentence-initial junk ("In The", "We Decided"). Quoted terms skip the filter (explicit intent).
# static list — an LLM entity pass is the upgrade path if junk entities become measurable (backlog).
_ENTITY_STOPWORDS = frozenset("""
    i you he she it we they me him her us them my your his its our their this that these those then there
    here now also just only very really still already because before after while when where which who what
    how why and or but not for with from into onto about above below between through during under over again
    if else can will should must shall may might could would do does did done having have has had be been
    being am is are was were want wanted need needed use used using prefer like love always never usually
    going gonna wanna sure yes no ok okay yeah yep hmm well right look looking think thought know knew see
    saw say said tell told ask asked give gave take took come came get got make made work worked build
    built start started stop stopped end ended plan planned try tried keep kept find found show showed
    run ran project task issue problem solution idea question answer note example case point time way
    thing things stuff corp inc ltd llc
""".split())
# AKA: 'X aka Y' — X is a capitalized proper-noun run (greedy \w+ swallowed whole clauses: "Alice told the team
# that TestCorp"); Y is one word (multi-word aliases miss — regex ceiling). Lowercase 'aka' only.
_RE_AKA = re.compile(r'([A-Z]\w*(?:\s+[A-Z]\w*)*)\s+(?:aka|also known as)\s+(\w+)')
_ENTITY_NAMES_SQL = "SELECT e.name FROM entities e JOIN fact_entities fe ON fe.entity_id = e.entity_id WHERE fe.fact_id = ?"
# Entity lookup order: exact name (case-insensitive), then aliases (comma-separated; wrapped in commas for whole-alias matching).
_ENTITY_LOOKUPS = ("SELECT entity_id FROM entities WHERE name = ? COLLATE NOCASE",
                   "SELECT entity_id FROM entities WHERE ',' || aliases || ',' LIKE '%,' || ? || ',%'")


def _clamp_trust(value: float) -> float:
    return max(0.0, min(1.0, value))


def _parse_since(raw: str) -> str:
    """--since value -> 'YYYY-MM-DD HH:MM:SS' UTC, the format created_at is stored in
    (CURRENT_TIMESTAMP), so the SQL filter is a plain string comparison — same-format
    ISO strings compare lexicographically in chronological order.
    Accepts ISO (date or datetime, space or T separator, taken as UTC) or relative
    30m / 2h / 7d / 2w from now. Raises ValueError on anything else (CLI contract:
    JSON error, not a traceback)."""
    m = re.fullmatch(r"(\d+)([mhdw])", raw.strip())
    if m:
        secs = int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
        dt = datetime.now(timezone.utc) - timedelta(seconds=secs)
    else:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(raw.strip(), fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"bad since {raw!r}: use ISO (YYYY-MM-DD[ HH:MM:SS] UTC) or relative (30m/2h/7d/2w)")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _tag_match(tag: str) -> tuple[str, list]:
    """Exact tag membership in the comma-separated tags column: the tag equals the
    whole column or is one of the comma-split elements (four boundary-anchored
    cases). All bound — no LIKE wildcards leak from tag content; session ids
    are plain strings."""
    t = tag.strip()
    return (f"(tags = ? OR tags LIKE ? || ',%' OR tags LIKE '%,' || ? || ',%' "
            f"OR tags LIKE '%,' || ?)", [t, t, t, t])


class MemoryStore:
    """SQLite-backed fact store with entity resolution and trust scoring.

    Process-wide shared connection registry: SQLite allows one writer at a time and several providers
    coexist per process (main agent + every delegate_task subagent), so all instances for the same database
    share ONE connection and ONE re-entrant lock — writes are fully serialized and "database is locked" is
    impossible. Refcounted: closing one instance never tears the connection out from under a sibling."""

    _shared: dict = {}
    _shared_guard = threading.Lock()

    def __init__(self, db_path: "str | Path | None" = None, default_trust: float = 0.5, hrr_dim: int = 1024) -> None:
        if db_path is None:
            from hermes_constants import get_hermes_home
            db_path = str(get_hermes_home() / "tiberius_store.db")
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.default_trust, self.hrr_dim, self._hrr_available = _clamp_trust(default_trust), hrr_dim, hrr._HAS_NUMPY
        try:  # resolve() so symlinked/relative paths to the same file share ONE connection
            self._key = str(self.db_path.resolve())
        except OSError:
            self._key = str(self.db_path)
        with MemoryStore._shared_guard:
            entry = MemoryStore._shared.get(self._key)
            if entry is None:
                # Autocommit: a write that raises mid-method can't leave a dangling transaction (and its
                # write lock) open; the explicit commit() calls in _write are then harmless no-ops.
                conn = sqlite3.connect(self._key, check_same_thread=False, timeout=10.0, isolation_level=None)
                conn.row_factory = sqlite3.Row
                entry = MemoryStore._shared[self._key] = {"conn": conn, "lock": threading.RLock(), "refs": 0, "ready": False}
            entry["refs"] += 1
            self._entry, self._conn, self._lock = entry, entry["conn"], entry["lock"]
        with self._lock:  # schema initialised once per shared connection
            if not entry["ready"]:
                self._init_db()
                entry["ready"] = True

    def _init_db(self) -> None:
        """Create schema, enable WAL (with graceful fallback on filesystems that can't do it),
        add hrr_vector to pre-HRR DBs. Uses Hermes's WAL helper when importable (in-Hermes),
        plain stdlib otherwise (bare containers)."""
        try:
            from hermes_state_wal import apply_wal_with_fallback
            apply_wal_with_fallback(self._conn, db_label="tiberius_store.db (tiberius)")
        except ImportError:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass  # journal mode unsupported on this filesystem — default journal still works
        self._conn.executescript(_SCHEMA)
        self._upgrade_fts_update_trigger()
        if "hrr_vector" not in {row[1] for row in self._conn.execute("PRAGMA table_info(facts)").fetchall()}:
            try:
                from hermes_cli.sqlite_util import add_column_if_missing
                add_column_if_missing(self._conn, "facts", "hrr_vector", "hrr_vector BLOB")
            except ImportError:
                self._conn.execute("ALTER TABLE facts ADD COLUMN hrr_vector BLOB")
        # v0.6.7: supersede pointers (docs/supersede.md). Same double-check shape as
        # hrr_vector: the helper re-checks the pragma inside; the bare ALTER is the
        # fallback for the race.
        fact_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(facts)").fetchall()}
        for col, col_def in (("superseded_by", "superseded_by INTEGER"), ("superseded_at", "superseded_at TEXT")):
            if col not in fact_cols:
                try:
                    from hermes_cli.sqlite_util import add_column_if_missing
                    add_column_if_missing(self._conn, "facts", col, col_def)
                except ImportError:
                    self._conn.execute(f"ALTER TABLE facts ADD COLUMN {col_def}")
        # v0.6.8: provenance (docs/provenance.md): which writer wrote the fact. Legacy
        # rows stay NULL = unknown. Same double-check shape as hrr_vector.
        if "origin" not in fact_cols:
            try:
                from hermes_cli.sqlite_util import add_column_if_missing
                add_column_if_missing(self._conn, "facts", "origin", "origin TEXT")
            except ImportError:
                self._conn.execute("ALTER TABLE facts ADD COLUMN origin TEXT")
        self._conn.commit()
        self._backfill_vectors()

    def _upgrade_fts_update_trigger(self) -> None:
        """One-time upgrade for older databases: their facts_au trigger (created before the
        WHEN clause existed) reindexes FTS on every UPDATE — including the trust-only writes
        from record_feedback. New databases already get the WHEN clause from _SCHEMA."""
        row = self._one("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'facts_au'")
        if row is not None and "WHEN old.content" in (row["sql"] or ""):
            return
        self._conn.execute("DROP TRIGGER IF EXISTS facts_au")
        self._conn.execute(
            "CREATE TRIGGER facts_au AFTER UPDATE ON facts "
            "WHEN old.content IS NOT new.content OR old.tags IS NOT new.tags BEGIN "
            "INSERT INTO facts_fts(facts_fts, rowid, content, tags) "
            "VALUES ('delete', old.fact_id, old.content, old.tags); "
            "INSERT INTO facts_fts(rowid, content, tags) "
            "VALUES (new.fact_id, new.content, new.tags); END")
        self._conn.commit()

    def _one(self, sql: str, params=()):
        return self._conn.execute(sql, params).fetchone()

    def _write(self, sql: str, params=()) -> sqlite3.Cursor:
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur

    def add_fact(self, content: str, category: str = "general", tags: str = "", origin: str | None = None) -> int:
        """Insert a fact and return its fact_id; on duplicate content (UNIQUE) return the existing fact_id untouched.
        Links extracted entities and rebuilds the category bank. origin (docs/provenance.md) records the writer;
        None keeps the row in the legacy 'unknown' bucket."""
        with self._lock:
            content = content.strip()
            if not content:
                raise ValueError("content must not be empty")
            try:
                fact_id: int = self._write(
                    "INSERT INTO facts (content, category, tags, trust_score, origin) VALUES (?, ?, ?, ?, ?)",
                    (content, category, tags, self.default_trust, origin)).lastrowid  # type: ignore[assignment]
            except sqlite3.IntegrityError:
                return int(self._one("SELECT fact_id FROM facts WHERE content = ?", (content,))["fact_id"])
            self._link_entities(fact_id, content)
            self._compute_hrr_vector(fact_id, content)
            return fact_id

    def add_facts(self, items: list, origin: str | None = None) -> dict:
        """Batch add: a list of content strings or {content, category?, tags?} objects.
        Validates every item first, then reuses add_fact per item (entity linking + HRR
        rebuild included), committing per item in order. A bad item (wrong type, empty
        content) raises ValueError naming the index and nothing executes. Duplicate
        content — already in the store or repeated within the batch (UNIQUE) — returns
        the existing fact_id, counted in 'duplicates' and still listed in 'fact_ids'.
        origin is stamped on every row the batch adds (docs/provenance.md).
        Returns {"added", "duplicates", "fact_ids"}."""
        prepared: list[tuple[str, str, str]] = []
        for i, item in enumerate(items):
            if isinstance(item, str):
                prepared.append((item, "general", ""))
            elif isinstance(item, dict):
                prepared.append((item.get("content") or "", item.get("category", "general"), item.get("tags", "")))
            else:
                raise ValueError(f"bad facts[{i}]: expected a string or an object with content")
        for i, (content, _category, _tags) in enumerate(prepared):
            if not content.strip():
                raise ValueError(f"bad facts[{i}]: content must not be empty")
        existing: set[int] = set()
        for content in {c for c, _c, _t in prepared}:
            row = self._one("SELECT fact_id FROM facts WHERE content = ?", (content,))
            if row is not None:
                existing.add(row["fact_id"])
        fact_ids: list[int] = []
        dup = 0
        for content, category, tags in prepared:
            fid = self.add_fact(content, category=category, tags=tags, origin=origin)
            if fid in fact_ids or fid in existing:
                dup += 1
            fact_ids.append(fid)
        return {"added": len(fact_ids) - dup, "duplicates": dup, "fact_ids": fact_ids}

    def update_fact(self, fact_id: int, content: str | None = None, trust_delta: float | None = None,
                    tags: str | None = None, category: str | None = None) -> bool:
        """Partially update a fact (trust clamped to [0, 1]). Returns True if the row existed."""
        with self._lock:
            if content is not None:
                # Same empty guard as add_fact: an empty row would merge into any other empty row
                # under UNIQUE(content), and the second such update would raise IntegrityError.
                content = content.strip()
                if not content:
                    raise ValueError("content must not be empty")
            row = self._one("SELECT fact_id, trust_score FROM facts WHERE fact_id = ?", (fact_id,))
            if row is None:
                return False
            changes = {col: val for col, val in {
                "content": content, "tags": tags, "category": category,
                "trust_score": _clamp_trust(row["trust_score"] + trust_delta) if trust_delta is not None else None,
            }.items() if val is not None}
            assignments = ", ".join(["updated_at = CURRENT_TIMESTAMP"] + [f"{col} = ?" for col in changes])
            self._write(f"UPDATE facts SET {assignments} WHERE fact_id = ?", [*changes.values(), fact_id])
            if content is not None:  # re-extract entities and recompute the HRR vector
                self._write("DELETE FROM fact_entities WHERE fact_id = ?", (fact_id,))
                self._link_entities(fact_id, content)
                self._compute_hrr_vector(fact_id, content)
            return True

    def remove_fact(self, fact_id: int) -> bool:
        """Delete a fact and its entity links. Returns True if the row existed."""
        with self._lock:
            row = self._one("SELECT fact_id FROM facts WHERE fact_id = ?", (fact_id,))
            if row is None:
                return False
            self._conn.execute("DELETE FROM fact_entities WHERE fact_id = ?", (fact_id,))
            self._write("DELETE FROM facts WHERE fact_id = ?", (fact_id,))
            return True

    # --- Session checkpoints (docs/checkpoint.md) ------------------------------
    # One active checkpoint per session: set replaces, show reads, clear deletes.
    # Checkpoint facts are category "checkpoint", tagged "checkpoint:<session>".

    def list_by_tag(self, tag: str, limit: int = 100) -> list[dict]:
        """Facts carrying the exact tag (tags is comma-separated), newest first.
        Tag lookup, not token recall — the checkpoint show surface. Read-only by
        tag is not a recall, so no retrieval recording (same convention as
        get_fact)."""
        clause, params = _tag_match(tag)
        with self._lock:
            rows = self._conn.execute(
                "SELECT fact_id, content, category, tags, trust_score, retrieval_count, helpful_count, "
                "created_at, updated_at FROM facts WHERE " + clause +
                " ORDER BY created_at DESC, fact_id DESC LIMIT ?", [*params, max(1, limit)]).fetchall()
            return [dict(r) for r in rows]

    def delete_by_tag(self, tag: str) -> int:
        """Delete every fact carrying the exact tag (and their entity links).
        ponytail: one statement at a time under the shared lock — checkpoint
        sets are <=8 rows; batch if it ever matters."""
        clause, params = _tag_match(tag)
        with self._lock:
            ids = [r["fact_id"] for r in self._conn.execute(
                f"SELECT fact_id FROM facts WHERE {clause}", params).fetchall()]
            for fid in ids:
                self._conn.execute("DELETE FROM fact_entities WHERE fact_id = ?", (fid,))
            if ids:
                self._write("DELETE FROM facts WHERE fact_id IN (" + ",".join("?" * len(ids)) + ")", ids)
            return len(ids)

    def set_checkpoint(self, session: str, facts: list[str], origin: str | None = None) -> dict:
        """Replace the session's active checkpoint: delete that session's existing
        checkpoint facts, then batch-add the new ones as category "checkpoint"
        tagged "checkpoint:<session>". One active checkpoint per session — the
        latest set is the whole story (docs/checkpoint.md). origin stamps the new
        rows (the calling writer; docs/provenance.md). Returns the add_facts
        result plus {session, replaced}."""
        if not 1 <= len(facts) <= 8:
            raise ValueError("checkpoint set takes 1-8 facts — a handoff, not a backup")
        if "," in session:
            # A comma in the session id would make the tag "checkpoint:a,b"
            # indistinguishable from two tags — the exact-match lookup then
            # crosses sessions.
            raise ValueError("checkpoint session ids must not contain a comma (tags are comma-separated)")
        session = session.strip()
        tag = f"checkpoint:{session}"
        replaced = self.delete_by_tag(tag)
        res = self.add_facts([{"content": f, "category": "checkpoint", "tags": tag} for f in facts], origin=origin)
        res["session"] = session
        res["replaced"] = replaced
        return res

    def supersede(self, fact_id: int, content: str, category: str | None = None,
                  tags: str | None = None, origin: str | None = None) -> dict:
        """Replace a fact: insert a replacement (normal add semantics — entities
        are extracted from the new content) and point the old row at it.
        Superseded rows are hidden from recall (search/probe/related/brief/list)
        but stay readable via get_fact, which now returns superseded_by /
        superseded_at (docs/supersede.md). Superseding an already-superseded
        fact re-points it (single-hop history). Two committed steps (add, then
        mark): a crash between them leaves a resolvable duplicate, never an
        orphaned replacement. Raises KeyError for an unknown id, ValueError if
        the content is empty or matches the target's or any other fact's
        content exactly (the UNIQUE path would otherwise hand back a foreign
        fact_id as the 'replacement')."""
        content = (content or "").strip()
        if not content:
            raise ValueError("supersede content must not be empty")
        with self._lock:
            row = self._one("SELECT fact_id FROM facts WHERE fact_id = ?", (fact_id,))
            if row is None:
                raise KeyError(f"fact {fact_id} not found")
            dup = self._one("SELECT fact_id FROM facts WHERE content = ? AND fact_id != ?",
                            (content, fact_id))
        if dup is not None:
            raise ValueError(f"supersede content matches fact {dup['fact_id']} exactly — "
                             f"remove that fact or use 'update' instead")
        new_id = self.add_fact(content, category=category or "general", tags=tags or "", origin=origin)
        if new_id == fact_id:
            # Race: the target's own content changed to ours between the dup check
            # and the insert (the UNIQUE path returned the target itself).
            raise ValueError("supersede content matches the fact's current content — nothing to change")
        with self._lock:
            self._write("UPDATE facts SET superseded_by = ?, superseded_at = CURRENT_TIMESTAMP "
                        "WHERE fact_id = ?", (new_id, fact_id))
            ts = self._one("SELECT superseded_at FROM facts WHERE fact_id = ?", (fact_id,))["superseded_at"]
        return {"superseded_fact_id": fact_id, "new_fact_id": new_id, "superseded_at": ts}

    def list_facts(self, category: str | None = None, min_trust: float = 0.0, limit: int = 50,
                   since: str | None = None, order: str = "trust", include_superseded: bool = False,
                   origin: str | None = None, exclude_origin: str | None = None) -> list[dict]:
        """Browse facts, optionally filtered by category / min trust / since
        (ISO date/datetime UTC or relative 30m/2h/7d/2w — facts created at or
        after it). Since filters on created_at, which only the write path
        sets — updates don't move it. Order: trust (default, trust_score DESC),
        retrieval (retrieval_count DESC), recent (created_at DESC), stale
        (updated_at ASC). Superseded rows are hidden by default;
        include_superseded shows them (docs/supersede.md). origin includes only
        one writer's rows; exclude_origin drops one writer but keeps legacy
        NULL-origin rows (docs/provenance.md)."""
        order_map = {"trust": "trust_score DESC", "retrieval": "retrieval_count DESC",
                     "recent": "created_at DESC", "stale": "updated_at ASC"}
        if order not in order_map:
            raise ValueError(f"order must be one of {sorted(order_map)}")
        with self._lock:
            category_clause = "AND category = ? " if category is not None else ""
            since_clause = "AND created_at >= ? " if since is not None else ""
            superseded_clause = "" if include_superseded else "AND superseded_by IS NULL "
            origin_clause = "AND origin = ? " if origin is not None else ""
            exclude_origin_clause = "AND (origin IS NULL OR origin != ?) " if exclude_origin else ""
            params = [min_trust] + ([category] if category is not None else []) \
                     + ([_parse_since(since)] if since is not None else []) \
                     + ([origin] if origin is not None else []) \
                     + ([exclude_origin] if exclude_origin else []) + [limit]
            sql = (f"SELECT fact_id, content, category, tags, trust_score, retrieval_count, helpful_count, "
                   f"created_at, updated_at, superseded_by, superseded_at, origin "
                   f"FROM facts WHERE trust_score >= ? {superseded_clause}{category_clause}{since_clause}"
                   f"{origin_clause}{exclude_origin_clause}"
                   f"ORDER BY {order_map[order]} LIMIT ?")
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def stats(self) -> dict:
        """Orientation aggregates: total facts, entity count, category mix,
        trust buckets (0.3/0.5/0.7 cuts), and the stale hygiene count
        (trust < 0.5 and updated_at older than 30 days). Read-only, no params."""
        with self._lock:
            total = self._one("SELECT COUNT(*) AS n FROM facts")["n"]
            entities = self._one("SELECT COUNT(*) AS n FROM entities")["n"]
            by_category = {r["category"]: r["n"] for r in self._conn.execute(
                "SELECT category, COUNT(*) AS n FROM facts GROUP BY category")}
            by_origin = {"null" if r["origin"] is None else r["origin"]: r["n"] for r in self._conn.execute(
                "SELECT origin, COUNT(*) AS n FROM facts GROUP BY origin")}
            trust_buckets = {}
            for label, lo, hi in (("0.0-0.3", 0.0, 0.3), ("0.3-0.5", 0.3, 0.5),
                                  ("0.5-0.7", 0.5, 0.7), ("0.7+", 0.7, 1.01)):
                n = self._one("SELECT COUNT(*) AS n FROM facts WHERE trust_score >= ? AND trust_score < ?",
                              (lo, hi))["n"]
                if n:
                    trust_buckets[label] = n
            stale = self._one(
                "SELECT COUNT(*) AS n FROM facts WHERE trust_score < 0.5 AND updated_at <= datetime('now', '-30 days')")["n"]
            return {"total": total, "entities": entities, "by_category": by_category,
                    "by_origin": by_origin,
                    "trust_buckets": trust_buckets, "stale_count": stale}

    def get_fact(self, fact_id: int) -> dict | None:
        """Read one fact by id: the full row plus its linked entities (name + aliases).
        None if the id is unknown — callers surface the shared 'not found' contract
        (same as remove/update/feedback). Read-only by id is not a recall, so no
        retrieval recording (retrieval_count tracks recall, not lookups)."""
        with self._lock:
            row = self._one(
                "SELECT fact_id, content, category, tags, trust_score, retrieval_count, "
                "helpful_count, created_at, updated_at, superseded_by, superseded_at, origin "
                "FROM facts WHERE fact_id = ?", (fact_id,))
            if row is None:
                return None
            out = dict(row)
            ents = self._conn.execute(
                "SELECT e.name, e.aliases FROM entities e "
                "JOIN fact_entities fe ON fe.entity_id = e.entity_id "
                "WHERE fe.fact_id = ? ORDER BY e.entity_id", (fact_id,)).fetchall()
            out["entities"] = [
                {"name": r["name"],
                 "aliases": [a.strip() for a in (r["aliases"] or "").split(",") if a.strip()]}
                for r in ents]
            return out

    def list_entities(self, top: int = 20) -> list[dict]:
        """List entities with their linked-fact count and aliases — entity discovery for
        probe/related/reason/brief/drift, which take an entity name but have no name-listing
        surface. One dict per entity ({name, aliases, fact_count}), sorted by fact_count desc,
        then name asc; capped at ``top``. LEFT JOIN: fact_count 0 is legal — orphaned entity
        rows (after fact deletion) must not be dropped. Read-only, not a recall, so no
        retrieval recording (the same convention as get_fact)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT e.name, e.aliases, COUNT(fe.fact_id) AS fact_count "
                "FROM entities e LEFT JOIN fact_entities fe ON fe.entity_id = e.entity_id "
                "GROUP BY e.entity_id "
                "ORDER BY fact_count DESC, e.name ASC LIMIT ?", (max(1, top),)).fetchall()
            return [
                {"name": r["name"],
                 "aliases": [a.strip() for a in (r["aliases"] or "").split(",") if a.strip()],
                 "fact_count": r["fact_count"]}
                for r in rows]

    def record_feedback(self, fact_id: int, helpful: bool) -> dict:
        """Adjust trust asymmetrically: helpful -> +0.05 and helpful_count += 1; unhelpful -> -0.10.
        Returns {fact_id, old_trust, new_trust, helpful_count}. Raises KeyError if fact_id is unknown."""
        with self._lock:
            row = self._one("SELECT fact_id, trust_score, helpful_count FROM facts WHERE fact_id = ?", (fact_id,))
            if row is None:
                raise KeyError(f"fact_id {fact_id} not found")
            old_trust: float = row["trust_score"]
            new_trust = _clamp_trust(old_trust + (_HELPFUL_DELTA if helpful else _UNHELPFUL_DELTA))
            increment = 1 if helpful else 0
            self._write("UPDATE facts SET trust_score = ?, helpful_count = helpful_count + ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE fact_id = ?", (new_trust, increment, fact_id))
            return {"fact_id": fact_id, "old_trust": old_trust, "new_trust": new_trust, "helpful_count": row["helpful_count"] + increment}

    def record_feedbacks(self, fact_ids: list, helpful: bool) -> list:
        """Apply the same feedback to each fact_id; per-item continue-on-error (an unknown id
        is reported, not raised). Returns the per-item record_feedback outputs with "ok"
        added, bad ids as {"fact_id", "ok": False, "error"} (docs/batch.md)."""
        results: list[dict] = []
        for fid in fact_ids:
            fid = int(fid)
            try:
                out = self.record_feedback(fid, helpful=helpful)
                out["ok"] = True
                results.append(out)
            except KeyError as e:
                results.append({"fact_id": fid, "ok": False, "error": str(e.args[0]) if e.args else f"fact_id {fid} not found"})
        return results

    def _extract_entities(self, text: str, aka_spans: tuple = ()) -> list[str]:
        """Regex entity candidates, deduplicated case-insensitively in first-seen order.
        Single-word captures are stoplist-filtered; multi-word captures are dropped when they start with a
        stopword (sentence-initial junk); quoted terms are kept verbatim. Matches inside AKA spans are
        excluded — _link_entities handles those as primary + alias."""
        def outside(m: re.Match) -> bool:
            return not any(a <= m.start() and m.end() <= b for a, b in aka_spans)
        raw = []
        for m in _RE_MULTI_WORD_ENTITY.finditer(text):
            if outside(m) and m.group(1).lower().split()[0] not in _ENTITY_STOPWORDS:
                raw.append(m.group(1))
        for m in _RE_SINGLE_WORD_ENTITY.finditer(text):
            name = m.group(1)
            # all-caps words (BUNDLES, LLM, SQL) are acronyms/screaming, not proper nouns
            if outside(m) and name.lower() not in _ENTITY_STOPWORDS and any(c.islower() for c in name[1:]):
                raw.append(name)
        for pattern in _RE_QUOTED:
            raw += [m.group(1) for m in pattern.finditer(text) if outside(m)]
        uniq: dict[str, str] = {}  # lower-cased key -> first-seen spelling, insertion-ordered
        for name in filter(None, (n.strip() for n in raw)):
            uniq.setdefault(name.lower(), name)
        return list(uniq.values())

    def _link_entities(self, fact_id: int, content: str) -> None:
        """Extract entities from content, resolve/create them, and link each to the fact.
        'X aka Y' links ONE entity (X) and records Y as its alias, so either name resolves to the same entity."""
        aka_spans = tuple(m.span() for m in _RE_AKA.finditer(content))
        for m in _RE_AKA.finditer(content):
            primary, alias = m.group(1).strip(), m.group(2).strip()
            if primary and alias and primary.lower() != alias.lower():
                entity_id = self._resolve_entity(primary)
                self._add_alias(entity_id, alias)
                self._write("INSERT OR IGNORE INTO fact_entities (fact_id, entity_id) VALUES (?, ?)", (fact_id, entity_id))
        for name in self._extract_entities(content, aka_spans):
            self._write("INSERT OR IGNORE INTO fact_entities (fact_id, entity_id) VALUES (?, ?)",
                        (fact_id, self._resolve_entity(name)))

    def _resolve_entity(self, name: str) -> int:
        """Return the entity_id for a case-insensitive name or alias match, creating the entity if absent."""
        for sql in _ENTITY_LOOKUPS:
            row = self._one(sql, (name,))
            if row is not None:
                return int(row["entity_id"])
        return int(self._write("INSERT INTO entities (name) VALUES (?)", (name,)).lastrowid)  # type: ignore[arg-type]

    def _add_alias(self, entity_id: int, alias: str) -> None:
        """Append a comma-separated alias if not already present (case-insensitive)."""
        row = self._one("SELECT aliases FROM entities WHERE entity_id = ?", (entity_id,))
        existing = {a.strip().lower() for a in (row["aliases"] or "").split(",") if a.strip()}
        if alias.strip().lower() in existing:
            return
        self._write("UPDATE entities SET aliases = ? WHERE entity_id = ?",
                    (f"{row['aliases']},{alias}".lstrip(",") if row["aliases"] else alias, entity_id))

    def _compute_hrr_vector(self, fact_id: int, content: str) -> None:
        """Compute and store the HRR vector for a fact (linked entities as roles). No-op without numpy."""
        if not self._hrr_available:
            return
        entities = [row["name"] for row in self._conn.execute(_ENTITY_NAMES_SQL, (fact_id,)).fetchall()]
        blob = hrr.phases_to_bytes(hrr.encode_fact(content, entities, self.hrr_dim))
        self._write("UPDATE facts SET hrr_vector = ? WHERE fact_id = ?", (blob, fact_id))

    def _backfill_vectors(self) -> None:
        """Compute HRR vectors for facts that lack one (pre-HRR imports, or rows written while numpy was
        unavailable). No-op when nothing is missing."""
        if not self._hrr_available:
            return
        rows = self._conn.execute("SELECT fact_id, content FROM facts WHERE hrr_vector IS NULL").fetchall()
        if not rows:
            return
        for row in rows:
            self._compute_hrr_vector(row["fact_id"], row["content"])

    @classmethod
    def release_all_under(cls, directory: "str | Path") -> int:
        """Force-close every shared connection whose database lives under ``directory``; returns the count.
        close() is refcount-driven, so a live holder (e.g. an agent's provider) keeps a profile's SQLite handle
        open, which on Windows makes rmtree of the profile fail. The directory is going away, so later use by a
        stale holder is expected to fail.

        That is exactly what a profile delete must break on Windows: the desktop's main ``serve`` process
        opens ``tiberius_store.db`` for every known profile, and ``rmtree`` of the profile directory fails
        with ``WinError 32`` while any of those handles is open (#88347). In a process that holds none (e.g.
        the CLI deleting from outside serve) this is a harmless no-op returning 0.
        """
        root = os.path.normcase(str(Path(directory).expanduser().resolve())) + os.sep
        with cls._shared_guard:
            doomed = [cls._shared.pop(key) for key in list(cls._shared) if os.path.normcase(key).startswith(root)]
            for entry in doomed:
                try:
                    with entry["lock"]:
                        entry["conn"].close()
                except Exception:
                    pass  # an already-closed/broken connection must not abort releasing siblings
        return len(doomed)

    def close(self) -> None:
        """Release this instance's reference; the connection closes with the last holder. Idempotent."""
        with MemoryStore._shared_guard:
            entry = getattr(self, "_entry", None)
            if entry is None:
                return
            entry["refs"] -= 1
            if entry["refs"] <= 0:
                try:
                    entry["conn"].close()
                finally:
                    # Pop only OUR entry: after release_all_under() a same-path store may have
                    # registered a FRESH entry under this key; a stale late close() must not evict it.
                    # See #88347.
                    if MemoryStore._shared.get(self._key) is entry:
                        MemoryStore._shared.pop(self._key, None)
            self._entry = None

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# Regex fact extraction: (patterns, category) — user preferences -> user_pref, decisions -> project.
# Shared by the Hermes plugin (session-end auto-extract) and the CLI (`extract` command), so every
# harness extracts with the same heuristics.
_EXTRACT_PATTERNS = (
    ([re.compile(r'\bI\s+(?:prefer|like|love|use|want)\s+(.+)', re.IGNORECASE),
      re.compile(r'\bmy\s+(?:favorite|preferred|default)\s+\w+\s+is\s+(.+)', re.IGNORECASE),
      re.compile(r'\bI\s+(?:always|never|usually)\s+(.+)', re.IGNORECASE)], "user_pref"),
    ([re.compile(r'\bwe\s+(?:decided|agreed|chose)\s+(?:to\s+)?(.+)', re.IGNORECASE),
      re.compile(r'\bthe\s+project\s+(?:uses|needs|requires)\s+(.+)', re.IGNORECASE)], "project"),
)


def extract_statements(text: str) -> list[tuple[str, str]]:
    """(statement, category) pairs in one message, ready for add_fact.

    Stores the matched statement, not the whole message: conversational preambles
    ("sure, but I don't think...") are not a fact. (.+) is greedy, so the span runs to
    end-of-line. A fact is not a question: keep the statement before the question sentence.
    """
    out: list[tuple[str, str]] = []
    if not isinstance(text, str) or len(text) < 10:
        return out
    for patterns, category in _EXTRACT_PATTERNS:
        match = next((p.search(text) for p in patterns if p.search(text)), None)
        if not match:
            continue
        statement = match.group(0).strip()[:400]
        if "?" in statement:
            stem = statement.split("?", 1)[0]
            boundary = stem.rfind(". ")
            statement = stem[:boundary + 1] if boundary >= 0 else stem
        statement = statement.rstrip()
        if len(statement) >= 20:
            out.append((statement, category))
    return out
