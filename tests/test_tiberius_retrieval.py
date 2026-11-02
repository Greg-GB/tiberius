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
# Discovery passes: alias expansion (candidate generation via entity name sets)
# and the prefix fallback (inflected tokens when exact FTS finds nothing)
# ---------------------------------------------------------------------------

def test_search_expands_entity_aliases(tmp_path):
    """A query that names an entity only by its alias must still reach facts that mention
    only the canonical name — alias expansion widens FTS candidate generation. The control
    query proves the target's own words are not what make it reachable."""
    store = MemoryStore(str(tmp_path / "alias.db"))
    try:
        store.add_fact("Redis caches the session tokens for the gateway workers.")
        store.add_fact("Redis aka KVStore.")  # one entity (Redis) with alias KVStore
        ret = FactRetriever(store=store)

        results = ret.search("kvstore cluster", limit=5)
        assert any(r["fact_id"] == 1 for r in results)  # reachable only via KVStore -> Redis

        assert all(r["fact_id"] != 1 for r in ret.search("cluster", limit=5))  # control: no alias named
    finally:
        store.close()


def test_search_prefix_fallback_on_zero_exact_matches(tmp_path):
    """Exact tokens miss inflections (backup/backups, rotations/rotate) — when the exact FTS
    pass finds no candidates at all, the prefix pass rescues the fact; the top hit is still
    ranked by the unchanged Jaccard/HRR/trust score."""
    store = MemoryStore(str(tmp_path / "prefix.db"))
    try:
        store.add_fact("The nightly backups rotate every seven days.")
        results = FactRetriever(store=store).search("backup rotations", limit=5)
        assert results and "backups" in results[0]["content"].lower()
    finally:
        store.close()


def test_match_count_counts_pool_without_recording(tmp_path):
    """Prefetch coverage counter: counts the FTS pool (exact, then prefix fallback) without
    recording retrievals — recall counts must reflect what the agent actually saw, not the
    pool counted per turn."""
    store = MemoryStore(str(tmp_path / "count.db"))
    try:
        for i in range(7):
            store.add_fact(f"The widget factory shipped batch {i} to the docks.")
        store.add_fact("The nightly backups rotate every seven days.")
        ret = FactRetriever(store=store)
        assert ret.match_count("widget factory") == 7
        assert ret.match_count("backup") == 1  # exact misses "backups" — prefix pass rescues
        assert ret.match_count("no such tokens anywhere") == 0
        fid = store._one("SELECT MAX(fact_id) AS m FROM facts")["m"]
        before = store._one("SELECT retrieval_count FROM facts WHERE fact_id = ?", (fid,))["retrieval_count"]
        ret.match_count("widget factory")
        after = store._one("SELECT retrieval_count FROM facts WHERE fact_id = ?", (fid,))["retrieval_count"]
        assert before == after
    finally:
        store.close()


def test_sanitize_fts_query_prefix_variant():
    assert FactRetriever._sanitize_fts_query("backup rotations", prefix=True) == '"backup"* OR "rotations"*'
    assert "*" not in FactRetriever._sanitize_fts_query("backup rotations")  # default stays exact


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


def test_related_dedupes_own_facts(tmp_path):
    """Regression: related's 1-hop scan excluded only self-pairs (fe2.fact_id != fe1.fact_id), so any
    entity with two or more facts had every own fact ALSO land in the hop set (each shares the queried
    entity with its siblings) and was returned twice. own ∪ hop is a union: each fact once."""
    pytest.importorskip("numpy")
    path = tmp_path / "tiberius_store.db"
    store = MemoryStore(path, hrr_dim=128)
    store.add_fact("ZetaDock ships crates to the northern docks.")
    store.add_fact("ZetaDock tracks crate status in the ledger.")
    try:
        r = FactRetriever(store).related("ZetaDock", limit=10)
        ids = [f["fact_id"] for f in r]
        assert len(ids) == len(set(ids))  # no duplicates
        assert len(ids) == 2  # exactly the entity's two facts (the store holds nothing else)
    finally:
        store.close()

# ---------------------------------------------------------------------------
# since windows on search / match_count / _fts_candidates (docs/since.md)
# ---------------------------------------------------------------------------

def _backdate(store, fact_id: int) -> None:
    """created_at is set only by the write path; tests backdate rows directly."""
    store._conn.execute("UPDATE facts SET created_at = '2026-01-01 00:00:00' WHERE fact_id = ?", (fact_id,))


def test_search_since_windows_the_pool(tmp_path):
    """since (relative form) excludes a backdated fact from search and match_count;
    omitted since is unchanged."""
    store = MemoryStore(str(tmp_path / "since.db"))
    try:
        old = store.add_fact("AcmeWidget crate line 1 ships to the docks on Tuesdays.")
        new = store.add_fact("AcmeWidget crate line 2 ships to the docks on Wednesdays.")
        _backdate(store, old)
        ret = FactRetriever(store=store)
        assert {f["fact_id"] for f in ret.search("AcmeWidget")} == {old, new}
        assert [f["fact_id"] for f in ret.search("AcmeWidget", since="24h")] == [new]
        assert ret.match_count("AcmeWidget") == 2
        assert ret.match_count("AcmeWidget", since="24h") == 1
    finally:
        store.close()


def test_search_since_windows_the_prefix_fallback(tmp_path):
    """The prefix-fallback pass must honor the window: query 'rotat' exact-matches
    nothing (exact pass empty), the prefix pass finds 'rotation' (old) and
    'rotations' (new); the window keeps only the new one."""
    store = MemoryStore(str(tmp_path / "since_prefix.db"))
    try:
        old = store.add_fact("ZetaDock rotation table keeps weekly slots.")
        new = store.add_fact("ZetaDock rotations happen on Tuesdays.")
        _backdate(store, old)
        ret = FactRetriever(store=store)
        assert {f["fact_id"] for f in ret.search("rotat")} == {old, new}
        assert [f["fact_id"] for f in ret.search("rotat", since="24h")] == [new]
        assert ret._fts_candidates("rotat", None, 0.3, 30, prefix=True, since="24h") \
            and all(f["fact_id"] == new for f in ret._fts_candidates("rotat", None, 0.3, 30, prefix=True, since="24h"))
    finally:
        store.close()


def test_since_invalid_value_raises_valueerror(tmp_path):
    """CLI contract upstream: bad since -> ValueError -> JSON error, not a traceback."""
    store = MemoryStore(str(tmp_path / "since_bad.db"))
    try:
        store.add_fact("AcmeWidget crate ships.")
        ret = FactRetriever(store=store)
        with pytest.raises(ValueError):
            ret.search("AcmeWidget", since="banana")
    finally:
        store.close()

def test_probe_union_returns_facts_linked_to_any_of_the_entities(tmp_path):
    """Union probe (docs/probe-union.md): probe([A, B]) returns facts linked to ANY of A or B
    (the OR counterpart of reason, which requires ALL). A fact linked to both appears once;
    a fact linked only to a third entity is excluded; single-name probe is unchanged."""
    pytest.importorskip("numpy")
    path = tmp_path / "tiberius_store.db"
    store = MemoryStore(path, hrr_dim=128)
    fid_a = store.add_fact("Zephyr handles the nightly backups")
    fid_b = store.add_fact("Nimbus fronts the public API gateway")
    fid_both = store.add_fact("Zephyr and Nimbus share the staging network")
    fid_other = store.add_fact("Cirrus compiles the release artifacts")
    try:
        r = FactRetriever(store).probe(["Zephyr", "Nimbus"], limit=10)
        ids = {row["fact_id"] for row in r}
        assert {fid_a, fid_b, fid_both} <= ids  # any-of: each listed entity's facts present
        assert fid_other not in ids  # third entity excluded
        assert len(ids) == len(r)  # the shared fact is not returned twice
        # single-name probe through the same path: Zephyr's facts only
        r1 = FactRetriever(store).probe("Zephyr", limit=10)
        ids1 = {row["fact_id"] for row in r1}
        assert fid_a in ids1 and fid_both in ids1 and fid_b not in ids1
    finally:
        store.close()


def test_probe_union_unknown_names_fall_back_to_fts5(tmp_path):
    """Union probe with no structurally linked names falls back to FTS5 (space-joined query)
    instead of erroring — same degradation as the single-entity fallback."""
    pytest.importorskip("numpy")
    path = tmp_path / "tiberius_store.db"
    store = MemoryStore(path, hrr_dim=128)
    store.add_fact("The relay queue drains every hour on schedule")
    try:
        r = FactRetriever(store).probe(["Ghostbird", "Mistral"], limit=5)
        assert isinstance(r, list)  # fallback search, no exception
    finally:
        store.close()


def _cat_store(tmp_path):
    """Three facts, one per category, all sharing the query tokens."""
    pytest.importorskip("numpy")
    store = MemoryStore(tmp_path / "t.db", hrr_dim=128)
    store.add_fact("The deploy pipeline runs on the build host", category="project")
    store.add_fact("The deploy pipeline uses containers on the build host", category="tool")
    store.add_fact("The deploy pipeline ships releases nightly", category="general")
    return store


def test_search_categories_inclusion_filters_the_pool(tmp_path):
    """categories=['project','tool'] keeps exactly those two facts of the three-candidate pool."""
    store = _cat_store(tmp_path)
    try:
        out = FactRetriever(store).search("deploy pipeline build host", categories=["project", "tool"], limit=10)
        cats = {r["category"] for r in out}
        assert cats == {"project", "tool"}  # general dropped, both named categories present
    finally:
        store.close()


def test_search_exclude_category_drops_from_pool(tmp_path):
    """exclude_category='general' removes that fact from results and from match_count's pool."""
    store = _cat_store(tmp_path)
    try:
        ret = FactRetriever(store)
        out = ret.search("deploy pipeline build host", exclude_category="general", limit=10)
        assert {r["category"] for r in out} == {"project", "tool"}
        assert ret.match_count("deploy pipeline build host", exclude_category="general") == 2
    finally:
        store.close()


def test_search_exclude_wins_when_category_included(tmp_path):
    """A fact named in categories that equals exclude_category is dropped (exclusion on top)."""
    store = _cat_store(tmp_path)
    try:
        out = FactRetriever(store).search("deploy pipeline build host",
                                          categories=["project", "general"],
                                          exclude_category="general", limit=10)
        assert {r["category"] for r in out} == {"project"}
    finally:
        store.close()


def test_search_categories_take_precedence_over_category(tmp_path):
    """Both set: the singular category is ignored, the IN list wins (house pattern)."""
    store = _cat_store(tmp_path)
    try:
        out = FactRetriever(store).search("deploy pipeline build host",
                                          category="general",
                                          categories=["project", "tool"], limit=10)
        assert {r["category"] for r in out} == {"project", "tool"}
    finally:
        store.close()


def test_match_count_agrees_with_search_under_filters(tmp_path):
    """The CLI/provider 'of N' marker must describe the same pool the caller sees."""
    store = _cat_store(tmp_path)
    try:
        ret = FactRetriever(store)
        n = ret.match_count("deploy pipeline build host", categories=["project", "general"],
                            exclude_category="general")
        got = len(ret.search("deploy pipeline build host", categories=["project", "general"],
                             exclude_category="general", limit=50))
        assert n == got == 1
    finally:
        store.close()


# ---------------------------------------------------------------------------
# supersede: superseded facts are hidden from every recall path (docs/supersede.md)
# ---------------------------------------------------------------------------

def test_superseded_fact_hidden_from_search_probe_related_and_count(tmp_path):
    """The pointer write alone must hide the old row from search, probe,
    related, and the match_count coverage counter; the replacement stays."""
    pytest.importorskip("numpy")
    store = MemoryStore(str(tmp_path / "sup.db"), hrr_dim=128)
    try:
        old = store.add_fact("The ZetaDock relay listens on the north gate.")
        new = store.supersede(old, "The ZetaDock relay now listens on the south gate.")["new_fact_id"]
        ret = FactRetriever(store=store)
        assert [f["fact_id"] for f in ret.search("ZetaDock relay gate")] == [new]
        assert all(f["fact_id"] != old for f in ret.probe("ZetaDock"))
        assert all(f["fact_id"] != old for f in ret.related("ZetaDock"))
        assert ret.match_count("ZetaDock relay gate") == 1
    finally:
        store.close()


def test_related_hop_superseded_fact_hidden(tmp_path):
    """A superseded fact that would otherwise be a 1-hop neighbour does not
    surface through related's hop scan."""
    pytest.importorskip("numpy")
    store = MemoryStore(str(tmp_path / "sup_hop.db"), hrr_dim=128)
    try:
        anchor = store.add_fact("ZetaDock ships crates to the northern docks.")
        hop = store.add_fact("ZetaDock tracks crate status in the ledger.")
        store.supersede(hop, "ZetaDock tracks crate status in the harbor log.")
        ret = FactRetriever(store=store)
        ids = {f["fact_id"] for f in ret.related("ZetaDock", limit=10)}
        assert hop not in ids
        assert anchor in ids  # the un-superseded sibling still comes back
    finally:
        store.close()


def _origin_store(tmp_path):
    """Three facts sharing the query tokens and the 'AcmeWidget' entity,
    one per origin bucket (legacy/cli/opencode)."""
    pytest.importorskip("numpy")
    store = MemoryStore(str(tmp_path / "m.db"), hrr_dim=128)
    store.add_fact("AcmeWidget relay keeps the legacy route.")
    store.add_fact("AcmeWidget relay keeps the backup route.", origin="cli")
    store.add_fact("AcmeWidget relay keeps the primary route.", origin="opencode")
    return store


def test_search_origin_include_filters_pool(tmp_path):
    """docs/provenance.md: origin includes only that writer's rows in the
    candidate pool, and match_count agrees on the same pool."""
    store = _origin_store(tmp_path)
    try:
        ret = FactRetriever(store)
        unfiltered = {r["fact_id"] for r in ret.search("AcmeWidget relay route", min_trust=0.0)}
        assert len(unfiltered) == 3
        cli_rows = ret.search("relay service route", min_trust=0.0, origin="cli")
        assert {r["origin"] for r in cli_rows} == {"cli"}
        assert ret.match_count("AcmeWidget relay route", min_trust=0.0, origin="cli") == 1
    finally:
        store.close()


def test_search_exclude_origin_keeps_legacy_null_rows(tmp_path):
    """exclude_origin drops one writer but keeps legacy NULL-origin rows."""
    store = _origin_store(tmp_path)
    try:
        ret = FactRetriever(store)
        rows = ret.search("relay service route", min_trust=0.0, exclude_origin="cli")
        assert {r["origin"] for r in rows} == {None, "opencode"}
        assert ret.match_count("AcmeWidget relay route", min_trust=0.0, exclude_origin="cli") == 2
    finally:
        store.close()


def test_probe_entity_path_respects_origin(tmp_path):
    """The entity path applies the same origin clause: probing a shared entity
    returns only the requested writer's facts (or falls back to a filtered
    FTS search — same writer either way)."""
    store = _origin_store(tmp_path)
    try:
        ret = FactRetriever(store)
        oc_ids = {r["fact_id"] for r in store.list_facts(origin="opencode")}
        other_ids = {r["fact_id"] for r in store.list_facts(exclude_origin="opencode")}
        q1, q2 = ",".join("?" * len(oc_ids)), ",".join("?" * len(other_ids))
        row = store._conn.execute(
            f"SELECT e.name FROM fact_entities fe "
            f"JOIN entities e ON e.entity_id = fe.entity_id WHERE fe.fact_id IN ({q1}) "
            f"INTERSECT SELECT e2.name FROM fact_entities fe2 "
            f"JOIN entities e2 ON e2.entity_id = fe2.entity_id WHERE fe2.fact_id IN ({q2}) LIMIT 1",
            (*sorted(oc_ids), *sorted(other_ids))).fetchone()
        assert row, "the shared template should extract a shared entity"
        rows = ret.probe(row[0], origin="opencode")
        assert rows, "expected the opencode fact for the shared entity"
        assert {r["fact_id"] for r in rows} <= oc_ids
    finally:
        store.close()


def test_related_hop_filter_keeps_passing_facts(tmp_path):
    """Regression: the hop filter's param list carried fid LAST while the SQL binds it
    FIRST (WHERE fact_id = ? AND ...), so any filtered related() call emptied the hop
    set silently — the pre-existing category path included."""
    pytest.importorskip("numpy")
    store = MemoryStore(str(tmp_path / "hop.db"), hrr_dim=128)
    try:
        store.add_fact("AcmeWidget uses RelayX in the X route.", category="project")
        store.add_fact("RelayX is built by ZetaCorp.", category="project", origin="opencode")
        ret = FactRetriever(store)
        r = ret.related("AcmeWidget", limit=10, category="project")
        assert {f["content"] for f in r} == {
            "AcmeWidget uses RelayX in the X route.",
            "RelayX is built by ZetaCorp.",
        }, "category-filtered hop fact must survive the hop check"
        r2 = ret.related("AcmeWidget", limit=10, origin="opencode")
        assert [f["content"] for f in r2] == ["RelayX is built by ZetaCorp."], \
            "origin-filtered related keeps only the passing hop fact"
    finally:
        store.close()


# ---------------------------------------------------------------------------
# hygiene — store health report (docs/hygiene.md)
# ---------------------------------------------------------------------------

def _hygiene_fixture(tmp_path):
    """A store carrying all four debt classes plus one superseded row."""
    store = MemoryStore(str(tmp_path / "hyg.db"))
    dup_a = store.add_fact("The AcmeWidget dock holds two gantries.")
    dup_b = store.add_fact("The AcmeWidget dock holds three gantries.")
    big = store.add_fact("The ZetaCorp yard. " * 70)  # 210 words > 200 default
    low = store.add_fact("A shaky claim about the RelayX line.")
    store._write("UPDATE facts SET trust_score = 0.30 WHERE fact_id = ?", (low,))
    orphan_f = store.add_fact("OnlyQuarry is a quarry.")
    store.remove_fact(orphan_f)  # leaves a zero-fact entity
    store.add_fact("The SoloMine runs one shift.")
    old = store.add_fact("Old note about the SoloMine.")
    store.supersede(old, "The SoloMine note has been corrected.")
    return store, {"dup_a": dup_a, "dup_b": dup_b, "big": big, "low": low}


def test_hygiene_report_finds_all_debt_classes(tmp_path):
    pytest.importorskip("numpy")
    store, f = _hygiene_fixture(tmp_path)
    try:
        rep = FactRetriever(store).hygiene()
        names = {i["name"]: i["fact_count"] for i in rep["orphan_entities"]["items"]}
        assert names["OnlyQuarry"] == 0, "true orphan (zero linked facts) reported"
        assert rep["orphan_entities"]["count"] == len(names)
        clusters = rep["near_duplicates"]["clusters"]
        assert rep["near_duplicates"]["count"] == 1
        assert sorted(x["fact_id"] for x in clusters[0]) == sorted((f["dup_a"], f["dup_b"]))
        assert rep["oversized_facts"]["count"] == 1
        assert rep["oversized_facts"]["items"][0]["fact_id"] == f["big"]
        assert rep["oversized_facts"]["items"][0]["words"] == 210
        assert rep["low_trust"] == {"count": 1, "items": [{"fact_id": f["low"], "trust_score": 0.3}]}
        assert rep["superseded_count"] == 1
        # superseded rows are history: never inside a fact section
        all_fact_ids = [i["fact_id"] for i in rep["oversized_facts"]["items"]] + \
                       [i["fact_id"] for i in rep["low_trust"]["items"]] + \
                       [x["fact_id"] for c in clusters for x in c]
        superseded_ids = {r["fact_id"] for r in store.list_facts(min_trust=0.0, limit=100, include_superseded=True)
                          if r["superseded_by"] is not None}
        assert not (set(all_fact_ids) & superseded_ids)
    finally:
        store.close()


def test_hygiene_caps_respected_and_read_only(tmp_path):
    pytest.importorskip("numpy")
    store, _f = _hygiene_fixture(tmp_path)
    try:
        ret = FactRetriever(store)
        full = ret.hygiene()
        capped = ret.hygiene(limit=1)
        for section in ("orphan_entities", "near_duplicates", "oversized_facts", "low_trust"):
            items = capped[section]["items" if section != "near_duplicates" else "clusters"]
            assert len(items) <= 1
            assert capped[section]["count"] == full[section]["count"], "cap limits items, not the count"
        # report writes nothing: row counts and every link unchanged
        before = (store._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
                  store._conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
                  store._conn.execute("SELECT COUNT(*) FROM fact_entities").fetchone()[0])
        ret.hygiene()
        after = (store._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
                 store._conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
                 store._conn.execute("SELECT COUNT(*) FROM fact_entities").fetchone()[0])
        assert before == after
        # stats unaffected by the report run
        stats = store.stats()
        assert stats["total"] == before[0]
    finally:
        store.close()


def test_hygiene_threshold_args(tmp_path):
    pytest.importorskip("numpy")
    store = MemoryStore(str(tmp_path / "th.db"))
    try:
        store.add_fact("One fact about the AcmeWidget line only.")
        ret = FactRetriever(store)
        rep = ret.hygiene(max_words=5, min_trust=0.6)
        assert rep["oversized_facts"]["count"] == 1  # 8 words > 5
        assert rep["low_trust"]["count"] == 1        # default trust 0.5 < 0.6
        rep = ret.hygiene(max_words=10, min_trust=0.1)
        assert rep["oversized_facts"]["count"] == 0
        assert rep["low_trust"]["count"] == 0
    finally:
        store.close()


def test_pool_counter_counts_final_pool(tmp_path):
    """docs/pool-counters.md: search() counts the FINAL candidate pool (after the
    prefix fallback), zero pools included; find_duplicates (add-time detection
    and the hygiene scan) bypasses the counter; a telemetry write failure never
    fails the search."""
    store = MemoryStore(str(tmp_path / "pools.db"))
    r = FactRetriever(store)
    try:
        store.add_fact("Alice manages the SQLite deployment at Acme.")
        store.add_fact("The Acme backup rotation runs nightly.")
        assert store._conn.execute("SELECT COUNT(*) AS n FROM pool_stats").fetchone()["n"] == 0, \
            "plain adds create no pool rows"
        res = r.search("sqlite deployment")
        assert len(res) >= 1
        # exact pass misses the inflection, prefix fallback rescues: final pool > 0
        assert r.search("backup") and r._last_pool >= 1
        r.search("zzzqqqxxwvuu")  # nothing matches at all: zero pool
        row = store._conn.execute("SELECT searches, zero_pools, sum_pool FROM pool_stats").fetchone()
        assert row["searches"] == 3 and row["zero_pools"] == 1
        assert row["sum_pool"] >= 1
        # hygiene runs the duplicate detector over every live fact: no pool rows
        r.hygiene()
        assert store._conn.execute("SELECT searches FROM pool_stats").fetchone()["searches"] == 3
        # telemetry failure never fails the search
        def _boom(size, fallback=False):
            raise RuntimeError("disk full")
        store.record_pool = _boom
        assert len(r.search("sqlite")) >= 1
    finally:
        store.close()


def test_fallback_counter_marks_degraded_calls(tmp_path, monkeypatch):
    """docs/fallback-counter.md: full-mode search/probe are not fallbacks; the
    probe/related/reason FTS fallbacks and a no-numpy search (HRR term disabled)
    each increment fallback_searches exactly once."""
    from hermes_plugin_tiberius import holographic as hrr

    def _fb():
        return store._conn.execute("SELECT COALESCE(SUM(fallback_searches), 0) AS n FROM pool_stats").fetchone()["n"]

    store = MemoryStore(str(tmp_path / "fb.db"))
    r = FactRetriever(store)
    try:
        store.add_fact("Alice manages the SQLite deployment at Acme.")
        store.add_fact("Bob reviews the deployment runbook.")
        r.search("deployment")
        assert _fb() == 0, "full-mode search is not a fallback"
        r.probe("zzz-noentity")   # unlinked name -> keyword fallback
        assert _fb() == 1
        r.related("zzz-noentity")
        r.reason(["zzz-noentity"])
        assert _fb() == 3, "each compositional fallback counts once"
        # no numpy: fresh retriever redistributes weights (hrr_weight 0) -> search
        # is fallback; probe takes the no-numpy branch
        monkeypatch.setattr(hrr, "_HAS_NUMPY", False)
        r2 = FactRetriever(store)
        r2.search("deployment")
        r2.probe("Alice")
        assert _fb() == 5
    finally:
        store.close()


def test_episodic_convention_shape_is_reachable(tmp_path):
    """docs/episodic-convention.md guard: a decision fact written in the
    convention's shape (category `episodic`, `YYYY-MM-DD: <decision> —
    <alternatives> — <reason>`) stays reachable on both recall surfaces —
    `list_facts(category='episodic', since=...)` and `search('<topic>')`.
    Pins that no future change silently breaks the shape."""
    store = MemoryStore(str(tmp_path / "episodic.db"))
    r = FactRetriever(store)
    try:
        store.add_fact("2026-11-01: Chose SQLite WAL over Postgres for the store — "
                       "single writer, zero extra process — because the store is single-user.",
                       category="episodic", tags="episodic")
        rows = store.list_facts(category="episodic", since="1d", order="recent")
        assert len(rows) == 1 and rows[0]["content"].startswith("2026-11-01:")
        hits = r.search("postgres store decision")
        assert any(row["content"].startswith("2026-11-01:") for row in hits)
    finally:
        store.close()
