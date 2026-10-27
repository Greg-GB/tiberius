"""Tests for FactRetriever FTS5 query sanitization.

These tests cover the fix where raw natural-language queries passed to
FTS5 MATCH were AND-joined by default, dropping recall to zero on any
multi-word prose query. The sanitizer drops stopwords and OR-joins the
remaining content tokens as phrase literals.
"""
from __future__ import annotations

import pytest

pytest.importorskip("numpy")  # retrieval module imports numpy indirectly

from hermes_plugin_tiberius.retrieval import FactRetriever
from hermes_plugin_tiberius.store import MemoryStore

# ---------------------------------------------------------------------------
# _sanitize_fts_query — unit tests (no DB required)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "query,expected_tokens",
    [
        # stopwords dropped
        ("what happened with the deployment rollback", {"happened", "deployment", "rollback"}),
        # single content word passes through
        ("compaction", {"compaction"}),
        # all stopwords → falls back to raw
        ("the and of", None),  # None = sentinel for fallback-to-raw
        # empty string → empty output
        ("", ""),
        # FTS5 operator characters stripped
        ("context: length-probe", {"context", "lengthprobe"}),
        # trailing punctuation stripped by tokenizer
        ("hello, world!", {"hello", "world"}),
    ],
)
def test_sanitize_fts_query_extracts_content_tokens(query, expected_tokens):
    result = FactRetriever._sanitize_fts_query(query)

    if expected_tokens == "":
        assert result == ""
        return

    if expected_tokens is None:
        # Pathological case: all stopwords — should fall back to raw query
        assert result == query
        return

    # OR-joined phrase literals: `"tok1" OR "tok2" OR ...`
    # Extract the tokens between quotes, order-independent.
    import re
    matches = re.findall(r'"([^"]+)"', result)
    assert set(matches) == expected_tokens, f"got {result!r}"

# ---------------------------------------------------------------------------
# Integration test — actually run _fts_candidates against an in-memory DB
# ---------------------------------------------------------------------------

@pytest.fixture
def retriever_with_facts(tmp_path):
    """MemoryStore seeded with a few facts for retrieval tests."""
    db_path = tmp_path / "test_facts.db"
    store = MemoryStore(str(db_path))
    store.add_fact(
        content="The Thursday deployment rollback failed because of stale migration state.",
        category="project",
    )
    store.add_fact(
        content="Compaction settings tuned to 0.85 threshold.",
        category="tool",
    )
    store.add_fact(
        content="Venice.ai advertises availableContextTokens inside model_spec.",
        category="tool",
    )
    retriever = FactRetriever(store=store)
    yield retriever
    store.close()

def test_prefetch_recovers_prose_query(retriever_with_facts):
    """A natural-language query should now match the relevant fact.

    Before the sanitizer fix, 'what happened with the deployment rollback'
    returned zero hits because FTS5 required every token to co-occur.
    """
    results = retriever_with_facts.search(
        "what happened with the deployment rollback"
    )
    assert len(results) >= 1
    # The top hit should be the deployment rollback fact
    assert "deployment rollback" in results[0]["content"].lower()


def test_find_duplicates_flags_paraphrase_not_distinct(tmp_path):
    """Near-identical content is flagged; a token-sharing-but-distinct fact is not;
    the new fact is never its own duplicate."""
    store = MemoryStore(str(tmp_path / "dup.db"))
    try:
        fid_dup = store.add_fact("The deploy pipeline uses Docker on the build host.")
        fid_distinct = store.add_fact("We use Docker to build the mobile app images.")
        ret = FactRetriever(store=store)
        new_id = store.add_fact("Deploy pipeline uses Docker on the build host.")
        dups = ret.find_duplicates("Deploy pipeline uses Docker on the build host.", new_id)
        ids = [d["fact_id"] for d in dups]
        assert fid_dup in ids              # paraphrase (dropped article) is flagged
        assert fid_distinct not in ids     # shares docker/build/the but is a distinct fact
        assert new_id not in ids           # a fact is never its own duplicate
        assert all(d["score"] >= 0.5 for d in dups)
    finally:
        store.close()

# ---------------------------------------------------------------------------
# Loop-invariant encode hoists (perf) — search/probe/related must encode
# constant vectors ONCE per call, not once per candidate/row.
# encode_text/encode_atom are deterministic (SHA-256 counter blocks), so the
# hoisted vectors are bit-identical to the per-iteration values they replace.
# ---------------------------------------------------------------------------

from hermes_plugin_tiberius import holographic as hrr

def test_encode_functions_are_deterministic():
    """Soundness premise of the hoists: same input -> identical vector."""
    import numpy as np

    assert np.array_equal(hrr.encode_text("deploy target", 1024),
                          hrr.encode_text("deploy target", 1024))
    assert np.array_equal(hrr.encode_atom("__hrr_role_content__", 1024),
                          hrr.encode_atom("__hrr_role_content__", 1024))


def test_search_ranks_multi_entity_fact_on_content_match(tmp_path):
    """search()'s HRR term compares the query against the fact's content component
    (ROLE_CONTENT unbound). A fact carrying several entity-role bindings must still
    rank first on its content match, not be drowned by its own role bindings."""
    path = tmp_path / "tiberius_store.db"
    store = MemoryStore(path, hrr_dim=128)
    store.add_fact("Acme Corp uses PostgreSQL for the billing service")
    store.add_fact("Acme Corp hired a new CFO last quarter")
    store.close()
    store = MemoryStore(path, hrr_dim=128)
    try:
        results = FactRetriever(store).search("PostgreSQL billing service", limit=5)
        assert results and "PostgreSQL for the billing service" in results[0]["content"]
        # hrr_dim drift (config change between sessions): decode must degrade, not raise
        assert FactRetriever(store, hrr_dim=256).search("PostgreSQL billing service", limit=5)
    finally:
        store.close()


def test_probe_works_without_category_bank(tmp_path):
    """Probe composes via the entity graph (fact_entities), not the category bank: a missing bank row
    (pre-feature stores, or banks dropped) must not break probe — results still rank by entity links."""
    pytest.importorskip("numpy")
    path = tmp_path / "tiberius_store.db"
    store = MemoryStore(path, hrr_dim=128)
    store.add_fact("Acme Corp runs the billing service", category="project")
    store._write("DELETE FROM memory_banks WHERE bank_name = 'cat:project'")  # simulate a missing bank
    assert store._one("SELECT vector FROM memory_banks WHERE bank_name = 'cat:project'") is None
    try:
        results = FactRetriever(store).probe("Acme Corp", category="project", limit=5)
        assert results and "Acme Corp" in results[0]["content"]
    finally:
        store.close()


def test_aka_alias_resolves_to_same_entity(tmp_path):
    """'X aka Y' creates ONE entity (X) with Y as an alias — not two entities — and probe works with either name."""
    pytest.importorskip("numpy")
    path = tmp_path / "tiberius_store.db"
    store = MemoryStore(path, hrr_dim=128)
    store.add_fact("Acme Corp aka Acme provides the billing infrastructure")
    names = {r["name"].lower(): (r["aliases"] or "") for r in store._conn.execute("SELECT name, aliases FROM entities").fetchall()}
    assert "acme corp" in names and "acme" in names["acme corp"].lower()
    assert "acme" not in names  # the alias is not its own entity
    try:
        r1 = FactRetriever(store).probe("Acme Corp", limit=5)
        r2 = FactRetriever(store).probe("Acme", limit=5)  # alias -> canonical
        assert r1 and "Acme Corp" in r1[0]["content"]
        assert r2 and "Acme Corp" in r2[0]["content"]
    finally:
        store.close()


def test_probe_ranks_entity_linked_fact_above_longer_unrelated(tmp_path):
    """Regression: the old probe unbound a key from the fact vector's BUNDLE (lossy superposition), so the
    residual was phase-shifted noise and longer/higher-trust unrelated facts outranked the entity's own
    fact. probe must rank the structurally linked fact first."""
    pytest.importorskip("numpy")
    path = tmp_path / "tiberius_store.db"
    store = MemoryStore(path, hrr_dim=256)
    store.add_fact("Acme Corp runs the billing service", category="project")
    store.add_fact(
        "The quarterly infrastructure migration moved every service, database, queue, cache, "
        "scheduler and monitor to the new cluster with zero downtime across all regions",
        category="project",
    )
    try:
        r = FactRetriever(store).probe("Acme Corp", limit=5)
        assert r and "Acme Corp" in r[0]["content"]
        assert r[0]["fact_id"] == store._one("SELECT MAX(fact_id) AS m FROM facts WHERE content LIKE '%Acme%'")["m"]
    finally:
        store.close()


def test_recall_counts_retrievals_exactly_once(tmp_path):
    """retrieval_count is the usage signal: each public recall increments it exactly once per
    returned fact — the keyword path via search(), the vector paths via _recorded, and the
    unlinked-entity fallback through the public search (no path double-counts)."""
    pytest.importorskip("numpy")
    path = tmp_path / "tiberius_store.db"
    store = MemoryStore(path, hrr_dim=128)
    store.add_fact("Acme Corp uses PostgreSQL for the billing service")
    fid = store._one("SELECT MAX(fact_id) AS m FROM facts")["m"]
    try:
        def count():
            return store._one("SELECT retrieval_count FROM facts WHERE fact_id = ?", (fid,))["retrieval_count"]

        r = FactRetriever(store).search("PostgreSQL billing service", limit=5)
        assert r and r[0]["fact_id"] == fid
        assert count() == 1
        FactRetriever(store).probe("Acme Corp", limit=5)  # vector path: entity linked at add time
        assert count() == 2
        FactRetriever(store).related("Acme Corp", limit=5)  # vector path, own facts
        assert count() == 3
        FactRetriever(store).probe("billing service", limit=5)  # unlinked entity -> keyword fallback via search()
        assert count() == 4
    finally:
        store.close()
