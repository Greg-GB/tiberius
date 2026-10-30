"""Tests for the tiberius MemoryStore shared-connection registry.

MemoryStore instances pointing at the same database file must share one
process-wide SQLite connection and one re-entrant lock. Multiple providers
coexist in a single process (the main agent plus every delegate_task
subagent); when each instance owned a private connection they raced as
independent WAL writers and intermittently failed with "database is locked".

Covers: connection sharing/refcounting, close() semantics, cross-instance
visibility, concurrent multi-instance writers, and write-lock release after
a failed write.
"""

import sqlite3
import threading

import pytest

from hermes_plugin_tiberius.store import MemoryStore

@pytest.fixture(autouse=True)
def _clean_shared_registry():
    """Each test starts and ends with an empty shared-connection registry."""
    # Drop any leakage from earlier tests in the same process.
    for entry in list(MemoryStore._shared.values()):
        try:
            entry["conn"].close()
        except sqlite3.Error:
            pass
    MemoryStore._shared.clear()
    yield
    leaked = list(MemoryStore._shared)
    for entry in list(MemoryStore._shared.values()):
        try:
            entry["conn"].close()
        except sqlite3.Error:
            pass
    MemoryStore._shared.clear()
    assert not leaked, f"test leaked shared connections: {leaked}"

@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "tiberius_store.db"

class TestSharedConnection:
    def test_same_path_shares_one_connection(self, db_path):
        a = MemoryStore(db_path)
        b = MemoryStore(db_path)
        try:
            assert a._conn is b._conn
            assert a._lock is b._lock
            assert len(MemoryStore._shared) == 1
            assert MemoryStore._shared[str(a.db_path)]["refs"] == 2
        finally:
            a.close()
            b.close()

    def test_different_paths_get_distinct_connections(self, tmp_path):
        a = MemoryStore(tmp_path / "one.db")
        b = MemoryStore(tmp_path / "two.db")
        try:
            assert a._conn is not b._conn
            assert len(MemoryStore._shared) == 2
        finally:
            a.close()
            b.close()

    @pytest.mark.require_symlinks
    def test_symlinked_path_shares_connection(self, tmp_path):
        """A symlink to the same DB file must hit the same registry entry —
        otherwise two connections to one file silently reintroduce the
        multi-writer contention the registry exists to prevent."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link_dir = tmp_path / "link"
        link_dir.symlink_to(real_dir)

        a = MemoryStore(real_dir / "tiberius_store.db")
        b = MemoryStore(link_dir / "tiberius_store.db")
        try:
            assert a._conn is b._conn
            assert len(MemoryStore._shared) == 1
        finally:
            a.close()
            b.close()

    def test_writes_visible_across_instances(self, db_path):
        a = MemoryStore(db_path)
        b = MemoryStore(db_path)
        try:
            fact_id = a.add_fact("Hermes likes shared connections", category="test")
            facts = b.list_facts(category="test")
            assert [f["fact_id"] for f in facts] == [fact_id]
        finally:
            a.close()
            b.close()

class TestCloseSemantics:
    def test_closing_one_instance_keeps_sibling_alive(self, db_path):
        a = MemoryStore(db_path)
        b = MemoryStore(db_path)
        a.close()
        try:
            # The shared connection must survive the sibling's close().
            fact_id = b.add_fact("survivor write")
            assert fact_id > 0
        finally:
            b.close()

    def test_last_close_releases_connection(self, db_path):
        a = MemoryStore(db_path)
        b = MemoryStore(db_path)
        conn = a._conn
        a.close()
        b.close()
        assert MemoryStore._shared == {}
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_close_is_idempotent(self, db_path):
        a = MemoryStore(db_path)
        b = MemoryStore(db_path)
        a.close()
        a.close()  # double close must not steal b's reference
        try:
            b.add_fact("still alive after double close")
            assert MemoryStore._shared[str(b.db_path)]["refs"] == 1
        finally:
            b.close()

    def test_context_manager_releases_reference(self, db_path):
        with MemoryStore(db_path) as store:
            store.add_fact("context managed")
        assert MemoryStore._shared == {}

    def test_reopen_after_full_close(self, db_path):
        with MemoryStore(db_path) as store:
            store.add_fact("first lifetime")
        with MemoryStore(db_path) as store:
            facts = store.list_facts()
        assert [f["content"] for f in facts] == ["first lifetime"]

class TestConcurrency:
    def test_concurrent_multi_instance_writers(self, db_path):
        """Many instances writing from many threads must never hit
        'database is locked' — the failure mode of per-instance connections."""
        n_threads, n_facts = 8, 15
        errors: list[BaseException] = []

        def writer(idx: int) -> None:
            store = MemoryStore(db_path)
            try:
                for i in range(n_facts):
                    store.add_fact(f"fact thread={idx} seq={i}", category="load")
            except BaseException as exc:  # noqa: BLE001 - recorded for assert
                errors.append(exc)
            finally:
                store.close()

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"concurrent writers failed: {errors[:3]}"
        with MemoryStore(db_path) as store:
            facts = store.list_facts(category="load", limit=500)
        assert len(facts) == n_threads * n_facts
        assert MemoryStore._shared == {}

    def test_failed_write_does_not_pin_write_lock(self, db_path, monkeypatch):
        """A write that raises mid-method must not leave an open transaction
        holding the SQLite write lock (autocommit isolation_level=None)."""
        broken = MemoryStore(db_path)
        sibling = MemoryStore(db_path)
        try:
            monkeypatch.setattr(
                MemoryStore,
                "_link_entities",
                lambda self, fact_id, content: (_ for _ in ()).throw(RuntimeError("boom")),
            )
            with pytest.raises(RuntimeError, match="boom"):
                broken.add_fact("write that fails after the INSERT")
            monkeypatch.undo()

            # No dangling transaction: the connection reports autocommit state
            # and the sibling can write immediately.
            assert broken._conn.in_transaction is False
            sibling.add_fact("sibling write right after the failure")
        finally:
            broken.close()
            sibling.close()


def test_fts_update_trigger_upgraded_to_when_clause(tmp_path):
    """Older databases have facts_au without the WHEN clause, so trust-only updates
    (record_feedback) reindex FTS pointlessly. Opening such a store recreates the
    trigger with the WHEN clause; a fresh database is already current."""
    old_path = str(tmp_path / "old.db")
    store = MemoryStore(old_path)
    try:
        store._conn.execute("DROP TRIGGER facts_au")
        store._conn.execute(
            "CREATE TRIGGER facts_au AFTER UPDATE ON facts BEGIN "
            "INSERT INTO facts_fts(facts_fts, rowid, content, tags) "
            "VALUES ('delete', old.fact_id, old.content, old.tags); "
            "INSERT INTO facts_fts(rowid, content, tags) "
            "VALUES (new.fact_id, new.content, new.tags); END")
    finally:
        store.close()

    upgraded = MemoryStore(old_path)
    try:
        sql = upgraded._one("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'facts_au'")["sql"]
        assert "WHEN old.content" in sql
    finally:
        upgraded.close()

    fresh = MemoryStore(str(tmp_path / "fresh.db"))
    try:
        sql = fresh._one("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'facts_au'")["sql"]
        assert "WHEN old.content" in sql  # _SCHEMA already creates it current
    finally:
        fresh.close()


def test_entity_extraction_covers_single_words_and_drops_sentence_start(tmp_path, db_path):
    """Single proper nouns (Alice, SQLite, Redis) are captured; sentence-initial junk is not."""
    store = MemoryStore(db_path, hrr_dim=64)
    store.add_fact("Alice uses SQLite for the tracker")
    store.add_fact("I Want to use Redis in a local instance")
    store.add_fact("In The old setup everything lived in one file")
    store.add_fact("Fact vectors are BUNDLES of content and entity parts")  # all-caps acronym: not an entity
    names = {r["name"].lower() for r in store._conn.execute("SELECT name FROM entities").fetchall()}
    assert {"alice", "sqlite", "redis"} <= names
    assert "want" not in names and "in the old setup" not in names
    assert "bundles" not in names
    store.close()


def test_entity_resolution_is_case_insensitive(tmp_path, db_path):
    """'alice johnson' resolves to the existing 'Alice Johnson' entity, never creates a duplicate
    (contract behind the COLLATE NOCASE lookup)."""
    store = MemoryStore(db_path, hrr_dim=64)
    store.add_fact("Alice Johnson reviews the reporting pipeline")
    assert store._resolve_entity("alice johnson") == store._resolve_entity("Alice Johnson")
    assert store._one("SELECT COUNT(*) AS c FROM entities WHERE lower(name) = 'alice johnson'")["c"] == 1
    store.close()


def test_backfill_computes_vectors_for_null_rows(tmp_path, db_path):
    """Facts stored while HRR was unavailable (or imported pre-HRR) get vectors
    on the next fresh open, and their category bank is rebuilt."""
    pytest.importorskip("numpy")
    store = MemoryStore(db_path, hrr_dim=64)
    fid = store.add_fact("we decided to use PostgreSQL for the persistence layer")
    assert store._one("SELECT hrr_vector FROM facts WHERE fact_id = ?", (fid,))["hrr_vector"] is not None
    store._write("UPDATE facts SET hrr_vector = NULL WHERE fact_id = ?", (fid,))
    store.close()  # release the shared connection; the next open re-runs _init_db -> backfill
    reopened = MemoryStore(db_path, hrr_dim=64)
    assert reopened._one("SELECT hrr_vector FROM facts WHERE fact_id = ?", (fid,))["hrr_vector"] is not None
    reopened.close()


def test_update_fact_partial(tmp_path, db_path):
    store = MemoryStore(db_path, hrr_dim=64)
    fid = store.add_fact("The deploy pipeline uses Docker on the build host")
    assert store.update_fact(fid, tags="infra", trust_delta=0.1) is True
    row = store._one("SELECT tags, trust_score, content FROM facts WHERE fact_id = ?", (fid,))
    assert row["tags"] == "infra" and row["trust_score"] > 0.5
    assert row["content"] == "The deploy pipeline uses Docker on the build host"  # untouched fields survive
    assert store.update_fact(999) is False  # unknown id, no error
    store.close()


def test_update_fact_rejects_empty_content(tmp_path, db_path):
    """Parity with add_fact: empty content would merge rows under UNIQUE(content)."""
    store = MemoryStore(db_path, hrr_dim=64)
    fid = store.add_fact("The deploy pipeline uses Docker on the build host")
    with pytest.raises(ValueError):
        store.update_fact(fid, content="   ")
    row = store._one("SELECT content FROM facts WHERE fact_id = ?", (fid,))
    assert row["content"] == "The deploy pipeline uses Docker on the build host"  # failed update left the row intact
    store.close()


def test_list_facts_since(tmp_path, db_path):
    """since filters on created_at (UTC): relative and ISO forms, future excludes, garbage raises."""
    store = MemoryStore(db_path, hrr_dim=64)
    fid = store.add_fact("The deploy pipeline uses Docker on the build host")
    assert [r["fact_id"] for r in store.list_facts(since="1h")] == [fid]      # fresh fact is within the last hour
    assert [r["fact_id"] for r in store.list_facts(since="2000-01-01")] == [fid]  # far-past ISO includes
    assert store.list_facts(since="2030-01-01") == []                          # future excludes
    assert store.list_facts(since="2030-01-01T00:00:00") == []                 # T-separator form
    with pytest.raises(ValueError):
        store.list_facts(since="banana")
    store.close()


def test_get_fact_returns_full_row_with_entities(tmp_path, db_path):
    """show's read path: the full row plus linked entities (name + aliases)."""
    store = MemoryStore(db_path, hrr_dim=64)
    fid = store.add_fact("AcmeWidget aka AW ships to the docks on Tuesdays.")
    f = store.get_fact(fid)
    assert f["fact_id"] == fid
    assert f["content"] == "AcmeWidget aka AW ships to the docks on Tuesdays."
    assert f["category"] == "general" and f["tags"] == ""
    assert f["trust_score"] == 0.5
    assert f["retrieval_count"] == 0 and f["helpful_count"] == 0
    assert f["created_at"] and f["updated_at"]
    ents = {e["name"].lower(): e for e in f["entities"]}
    assert "acmewidget" in ents
    assert "aw" in [a.lower() for a in ents["acmewidget"]["aliases"]]
    store.close()


def test_get_fact_is_not_a_recall(tmp_path, db_path):
    """By-id lookup must not touch retrieval_count (the counter tracks recall only)."""
    store = MemoryStore(db_path, hrr_dim=64)
    fid = store.add_fact("The AcmeWidget ships to the docks on Tuesdays.")
    for _ in range(3):
        store.get_fact(fid)
    row = store._one("SELECT retrieval_count FROM facts WHERE fact_id = ?", (fid,))
    assert row["retrieval_count"] == 0
    store.close()


def test_get_fact_unknown_id_returns_none(tmp_path, db_path):
    store = MemoryStore(db_path, hrr_dim=64)
    assert store.get_fact(99999) is None
    store.close()


def test_list_entities_orders_by_count_then_name(tmp_path, db_path):
    """entities discovery: sorted by fact_count desc, then name asc; aliases parsed from the column."""
    store = MemoryStore(db_path, hrr_dim=64)
    store.add_fact("AcmeWidget aka AW ships to the docks on Tuesdays.")
    store.add_fact("AcmeWidget crates are stamped with the batch number.")
    store.add_fact("BetaCo signs the dock manifests every Friday.")
    rows = store.list_entities()
    assert [r["name"] for r in rows] == ["AcmeWidget", "BetaCo", "Friday", "Tuesdays"]
    assert [r["fact_count"] for r in rows] == [2, 1, 1, 1]
    assert rows[0]["aliases"] == ["AW"]  # comma column parsed to a list
    assert rows[1]["aliases"] == []      # no aliases -> empty list, not None
    store.close()


def test_list_entities_reports_orphans_with_zero_count(tmp_path, db_path):
    """fact_count 0 is legal: an entity whose facts were all deleted still appears (LEFT JOIN)."""
    store = MemoryStore(db_path, hrr_dim=64)
    fid = store.add_fact("GammaCorp filed the quarterly report on Monday.")
    assert any(r["name"] == "GammaCorp" and r["fact_count"] == 1 for r in store.list_entities())
    assert store.remove_fact(fid)
    orphan = next(r for r in store.list_entities() if r["name"] == "GammaCorp")
    assert orphan["fact_count"] == 0
    store.close()


def test_list_entities_caps_at_top(tmp_path, db_path):
    store = MemoryStore(db_path, hrr_dim=64)
    store.add_fact("AcmeWidget aka AW ships to the docks on Tuesdays.")
    store.add_fact("BetaCo signs the dock manifests every Friday.")
    assert [r["name"] for r in store.list_entities(top=1)] == ["AcmeWidget"]  # tied counts: name asc
    assert len(store.list_entities(top=100)) == 4  # AcmeWidget, BetaCo, Friday, Tuesdays
    assert [r["name"] for r in store.list_entities(top=-5)] == \
           [r["name"] for r in store.list_entities(top=1)]  # clamped to 1, not an unlimited dump
    store.close()
