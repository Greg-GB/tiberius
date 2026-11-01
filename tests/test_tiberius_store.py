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


def test_add_facts_batch_happy_path_duplicates_and_per_item_paths(tmp_path):
    """add_facts: strings + objects, dup content returns the existing id (docs/batch.md)."""
    store = MemoryStore(str(tmp_path / "m.db"))
    out = store.add_facts([
        "The AcmeWidget crate line ships on Fridays.",
        {"content": "The AcmeWidget crate line runs on SQLite.", "category": "project", "tags": "infra"},
        "The AcmeWidget crate line ships on Fridays.",  # duplicate of item 0
    ])
    assert out["added"] == 2 and out["duplicates"] == 1
    assert len(out["fact_ids"]) == 3
    assert out["fact_ids"][0] == out["fact_ids"][2]  # UNIQUE(content) -> existing id
    row = store.get_fact(out["fact_ids"][1])
    assert row["category"] == "project" and row["tags"] == "infra"  # per-item add_fact path ran
    assert [r["name"] for r in store.list_entities(top=1)] == ["AcmeWidget"]  # entity linking per item
    store.close()


def test_add_facts_bad_item_names_the_index_and_executes_nothing(tmp_path):
    store = MemoryStore(str(tmp_path / "m.db"))
    for items, frag in [([["nope"]], "facts[0]"), (["ok one", "   "], "facts[1]")]:
        try:
            store.add_facts(items)
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert frag in str(e)
    assert len(store.list_facts(limit=100)) == 0  # validated up front: nothing executed
    store.close()


def test_record_feedbacks_continue_on_error_per_item(tmp_path):
    store = MemoryStore(str(tmp_path / "m.db"))
    a = store.add_fact("The AcmeWidget crate line ships on Fridays.")
    b = store.add_fact("The AcmeWidget crate line runs on SQLite.")
    out = store.record_feedbacks([a, 99999, b], helpful=True)
    assert [r["ok"] for r in out] == [True, False, True]
    assert out[1]["fact_id"] == 99999 and "not found" in out[1]["error"]
    assert out[0]["new_trust"] > out[0]["old_trust"]
    store.close()

def test_stats_totals_categories_buckets_and_stale_window(tmp_path):
    """stats aggregates: totals, category mix, exact 0.3/0.5/0.7 bucket edges, 30-day stale window."""
    store = MemoryStore(str(tmp_path / "m.db"), hrr_dim=64)
    low = store.add_fact("The zephyr engine cache is keyed on route id.", category="general")
    mid = store.add_fact("The zephyr engine queue drains nightly.", category="project")
    high1 = store.add_fact("The zephyr engine deploys via the release channel.", category="general")
    high2 = store.add_fact("The zephyr engine tests run on every push.", category="tool")
    for _ in range(2):
        store.record_feedback(mid, helpful=False)       # 0.5 -> 0.3 : exactly the [0.3, 0.5) edge (unhelpful delta is -0.10)
    store.record_feedback(high1, helpful=True)
    store.record_feedback(high1, helpful=True)          # 0.5 -> 0.6 : [0.5, 0.7)
    for _ in range(4):
        store.record_feedback(high2, helpful=True)      # 0.5 -> 0.7 : exactly the 0.7+ edge
    store._conn.execute("UPDATE facts SET updated_at = datetime('now', '-40 days') WHERE fact_id = ?", (mid,))
    store._conn.commit()
    s = store.stats()
    assert s["total"] == 4 and s["entities"] == 0
    assert s["by_category"] == {"general": 2, "project": 1, "tool": 1}
    assert s["trust_buckets"] == {"0.3-0.5": 1, "0.5-0.7": 2, "0.7+": 1}  # low (0.5) sits on the 0.5 edge up
    assert s["stale_count"] == 1  # only mid: trust < 0.5 AND older than 30 days
    store.close()


def test_list_facts_order_variants_and_bad_order(tmp_path):
    """order: trust (default, unchanged), retrieval, recent, stale; bad value raises ValueError."""
    store = MemoryStore(str(tmp_path / "m.db"), hrr_dim=64)
    a = store.add_fact("Zephyr engine starts at dawn.")
    b = store.add_fact("Zephyr engine stops at dusk.")
    c = store.add_fact("Zephyr engine idles at noon.")
    store.record_feedback(b, helpful=True)                       # b 0.55
    store.record_feedback(c, helpful=True)
    store.record_feedback(c, helpful=True)                       # c 0.60
    store._conn.execute("UPDATE facts SET retrieval_count = 3 WHERE fact_id = ?", (a,))
    store._conn.execute("UPDATE facts SET retrieval_count = 1 WHERE fact_id = ?", (b,))
    store._conn.execute("UPDATE facts SET created_at = '2026-01-01 00:00:00' WHERE fact_id = ?", (a,))
    store._conn.execute("UPDATE facts SET created_at = '2026-01-03 00:00:00' WHERE fact_id = ?", (b,))
    store._conn.execute("UPDATE facts SET updated_at = '2026-03-01 00:00:00' WHERE fact_id = ?", (a,))
    store._conn.execute("UPDATE facts SET updated_at = '2026-02-15 00:00:00' WHERE fact_id = ?", (b,))
    store._conn.execute("UPDATE facts SET updated_at = '2026-02-01 00:00:00' WHERE fact_id = ?", (c,))
    store._conn.commit()
    assert [r["fact_id"] for r in store.list_facts()] == [c, b, a]          # default = trust desc (unchanged)
    assert [r["fact_id"] for r in store.list_facts(order="trust")] == [c, b, a]
    assert [r["fact_id"] for r in store.list_facts(order="retrieval")] == [a, b, c]  # 3, 1, 0
    assert [r["fact_id"] for r in store.list_facts(order="recent")] == [c, b, a]    # c newest, a oldest
    assert [r["fact_id"] for r in store.list_facts(order="stale")] == [c, b, a]     # c oldest-updated
    with pytest.raises(ValueError):
        store.list_facts(order="banana")
    store.close()


def test_checkpoint_set_show_clear_roundtrip_and_replace(tmp_path):
    """docs/checkpoint.md: one active checkpoint per session; set replaces the
    previous one (old fact ids gone), show reads, clear deletes."""
    store = MemoryStore(str(tmp_path / "m.db"), hrr_dim=64)
    r1 = store.set_checkpoint("s1", ["task: ship the AcmeWidget", "next: write tests"])
    assert r1["session"] == "s1"
    assert r1["replaced"] == 0
    assert r1["added"] == 2
    rows = store.list_by_tag("checkpoint:s1")
    # Same-second created_at ties break by fact_id DESC — assert as a set.
    assert {r["content"] for r in rows} == {"task: ship the AcmeWidget", "next: write tests"}
    assert all(r["category"] == "checkpoint" and r["tags"] == "checkpoint:s1" for r in rows)
    old_ids = {r["fact_id"] for r in rows}
    r2 = store.set_checkpoint("s1", ["task: ship the AcmeWidget v2"])
    assert r2["replaced"] == 2
    rows2 = store.list_by_tag("checkpoint:s1")
    assert [r["content"] for r in rows2] == ["task: ship the AcmeWidget v2"]
    assert not (old_ids & {r["fact_id"] for r in rows2})
    assert store.delete_by_tag("checkpoint:s1") == 1
    assert store.list_by_tag("checkpoint:s1") == []
    store.close()


def test_checkpoint_sessions_distinct_and_tag_exactness(tmp_path):
    """Session ids do not cross: s1 and s1x are different checkpoints, and tag
    matching is exact at element boundaries (checkpoint:ab must not match
    checkpoint:abc or a comma list containing checkpoint:abx)."""
    store = MemoryStore(str(tmp_path / "m.db"), hrr_dim=64)
    store.set_checkpoint("ab", ["task: alpha session"])
    store.set_checkpoint("abc", ["task: alpha-b session"])
    store.add_fact("An unrelated fact tagged for the x session.", tags="misc,checkpoint:abx,misc2")
    assert [r["content"] for r in store.list_by_tag("checkpoint:ab")] == ["task: alpha session"]
    assert [r["content"] for r in store.list_by_tag("checkpoint:abc")] == ["task: alpha-b session"]
    assert [r["content"] for r in store.list_by_tag("checkpoint:abx")] == ["An unrelated fact tagged for the x session."]
    store.close()


def test_checkpoint_set_bounds(tmp_path):
    """set_checkpoint takes 1-8 facts — a handoff, not a backup;
    session ids strip and may not contain a comma (tag format is
    comma-separated, a comma would cross sessions)."""
    store = MemoryStore(str(tmp_path / "m.db"), hrr_dim=64)
    with pytest.raises(ValueError):
        store.set_checkpoint("s1", [])
    with pytest.raises(ValueError):
        store.set_checkpoint("s1", [f"line {i}" for i in range(9)])
    ok = store.set_checkpoint("s1", [f"line {i}" for i in range(8)])
    assert ok["added"] == 8
    with pytest.raises(ValueError):
        store.set_checkpoint("a,b", ["task: comma session"])
    store.close()


def test_supersede_roundtrip_pointer_and_hiding(tmp_path):
    """docs/supersede.md: the old row keeps its row but is hidden from the
    default list, gets superseded_by/superseded_at, and stays readable via
    get_fact; the replacement is live."""
    store = MemoryStore(str(tmp_path / "m.db"), hrr_dim=64)
    old_id = store.add_fact("The AcmeWidget service listens on port 7777.")
    res = store.supersede(old_id, "The AcmeWidget service now listens on port 8081.")
    assert res["superseded_fact_id"] == old_id
    assert res["new_fact_id"] != old_id
    assert res["superseded_at"]
    old = store.get_fact(old_id)
    assert old["superseded_by"] == res["new_fact_id"]
    assert old["superseded_at"] == res["superseded_at"]
    assert store.get_fact(res["new_fact_id"])["superseded_by"] is None
    default_ids = {r["fact_id"] for r in store.list_facts()}
    assert old_id not in default_ids
    assert res["new_fact_id"] in default_ids
    included = {r["fact_id"] for r in store.list_facts(include_superseded=True)}
    assert old_id in included
    store.close()


def test_supersede_chain_single_hop(tmp_path):
    """A superseded fact may be superseded again: it keeps pointing at its
    FIRST replacement (single-hop history — show follows one link)."""
    store = MemoryStore(str(tmp_path / "m.db"), hrr_dim=64)
    a = store.add_fact("The AcmeWidget service listens on port 7777.")
    b = store.supersede(a, "The AcmeWidget service now listens on port 8081.")["new_fact_id"]
    c = store.supersede(b, "The AcmeWidget service now listens on port 9090.")["new_fact_id"]
    assert store.get_fact(a)["superseded_by"] == b
    assert store.get_fact(b)["superseded_by"] == c
    assert store.get_fact(c)["superseded_by"] is None
    assert {r["fact_id"] for r in store.list_facts()} == {c}
    store.close()


def test_supersede_errors(tmp_path):
    """Unknown id raises KeyError; empty content, content equal to the
    target's own content, and content equal to any other fact's content all
    raise ValueError (the UNIQUE path would otherwise hand back a foreign
    fact_id as the 'replacement')."""
    store = MemoryStore(str(tmp_path / "m.db"), hrr_dim=64)
    a = store.add_fact("The AcmeWidget service listens on port 7777.")
    store.add_fact("The ZebraBelt service listens on port 5555.")
    with pytest.raises(KeyError):
        store.supersede(99999, "A brand new statement about nothing.")
    with pytest.raises(ValueError):
        store.supersede(a, "   ")
    with pytest.raises(ValueError):
        store.supersede(a, "The AcmeWidget service listens on port 7777.")
    with pytest.raises(ValueError, match="matches fact"):
        store.supersede(a, "The ZebraBelt service listens on port 5555.")
    # The failed attempts left no replacement rows behind.
    assert len(store.list_facts(include_superseded=True)) == 2
    store.close()


def test_supersede_migration_adds_columns(tmp_path):
    """A pre-supersede database (created with the older schema) gains both
    columns on open; fresh databases already have them."""
    import sqlite3
    from hermes_plugin_tiberius.store import _SCHEMA
    db = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    conn.close()
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(facts)").fetchall()}
    assert "superseded_by" not in cols  # sanity: _SCHEMA predates the migration
    store = MemoryStore(db, hrr_dim=64)
    cols = {r[1] for r in store._conn.execute("PRAGMA table_info(facts)").fetchall()}
    assert {"superseded_by", "superseded_at"} <= cols
    store.close()


def test_supersede_replacement_carries_category_tags_old_row_untouched(tmp_path):
    """The replacement is a normal add (category/tags applied, entities
    extracted from the new content); the old row's content, entities, and
    trust are untouched by the pointer write."""
    store = MemoryStore(str(tmp_path / "m.db"), hrr_dim=64)
    old_id = store.add_fact("The AcmeWidget service listens on port 7777.")
    old_before = store.get_fact(old_id)
    res = store.supersede(old_id, "The ZebraBelt service now listens on port 6666.",
                          category="tool", tags="ports,infra")
    new = store.get_fact(res["new_fact_id"])
    assert new["category"] == "tool"
    assert new["tags"] == "ports,infra"
    assert [e["name"] for e in new["entities"]], "entities extracted from the new content"
    old_after = store.get_fact(old_id)
    for key in ("content", "category", "tags", "trust_score", "entities"):
        assert old_after[key] == old_before[key]
    store.close()


def test_origin_migration_adds_column(tmp_path):
    """docs/provenance.md: a pre-provenance database (created with the older
    schema) gains the origin column on open; fresh databases already have it."""
    from hermes_plugin_tiberius.store import _SCHEMA
    db = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    conn.close()
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(facts)").fetchall()}
    assert "origin" not in cols  # sanity: _SCHEMA predates the provenance migration
    store = MemoryStore(db, hrr_dim=64)
    cols = {r[1] for r in store._conn.execute("PRAGMA table_info(facts)").fetchall()}
    assert "origin" in cols
    store.close()


def test_add_fact_stamps_origin_and_legacy_stays_null(tmp_path):
    """Writers stamp origin on add; a row written without one stays in the
    legacy 'unknown' (NULL) bucket, visible via get_fact."""
    store = MemoryStore(str(tmp_path / "m.db"), hrr_dim=64)
    legacy = store.add_fact("The relay service keeps the legacy route.")
    stamped = store.add_fact("The relay service keeps the stamped route.", origin="cli")
    assert store.get_fact(legacy)["origin"] is None
    assert store.get_fact(stamped)["origin"] == "cli"
    store.close()


def test_add_facts_threads_origin_to_every_row(tmp_path):
    """add_batch stamps the batch-level origin on every row it adds."""
    store = MemoryStore(str(tmp_path / "m.db"), hrr_dim=64)
    res = store.add_facts(["First batched row.", "Second batched row."], origin="opencode")
    assert res["added"] == 2
    for fid in res["fact_ids"]:
        assert store.get_fact(fid)["origin"] == "opencode"
    store.close()


def test_list_facts_origin_include_exclude(tmp_path):
    """origin includes one writer's rows only; exclude_origin drops one writer
    but keeps legacy NULL-origin rows (NULL != 'x' is NULL, not true)."""
    store = MemoryStore(str(tmp_path / "m.db"), hrr_dim=64)
    legacy = store.add_fact("Legacy row about the backup plan.")
    cli = store.add_fact("CLI row about the backup plan.", origin="cli")
    oc = store.add_fact("Opencode row about the backup plan.", origin="opencode")
    assert {r["fact_id"] for r in store.list_facts(origin="cli")} == {cli}
    assert store.list_facts(origin="cli")[0]["origin"] == "cli"
    assert {r["fact_id"] for r in store.list_facts(exclude_origin="cli")} == {legacy, oc}
    store.close()


def test_stats_by_origin_counts_with_null_bucket(tmp_path):
    """stats reports the per-origin breakdown; legacy rows count under 'null'."""
    store = MemoryStore(str(tmp_path / "m.db"), hrr_dim=64)
    store.add_fact("Stats row with no writer recorded.")
    store.add_fact("Stats row written by cli one.")
    store.add_fact("Stats row written by cli two.", origin="cli")
    store.add_fact("Stats row written by cli three.", origin="cli")
    store.add_fact("Stats row written by opencode.", origin="opencode")
    assert store.stats()["by_origin"] == {"null": 2, "cli": 2, "opencode": 1}
    store.close()
