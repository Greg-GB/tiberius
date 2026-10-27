"""Hybrid keyword/BM25 retrieval for the memory store: FTS5 candidates reranked with
Jaccard similarity and HRR vector similarity, trust-weighted (ported from KIK memory_agent.py)."""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import MemoryStore

from . import holographic as hrr

_FACT_COLUMNS = "fact_id, content, category, tags, trust_score, retrieval_count, helpful_count, created_at, updated_at"
_ROLE_ENTITY, _ROLE_CONTENT = hrr.ROLE_ENTITY, hrr.ROLE_CONTENT
_PUNCT = ".,;:!?\"'()[]{}#@<>"
_FTS_OPERATORS = str.maketrans("", "", '"()*^:-+')
# Stopwords dropped before FTS5 OR-expansion: short English function words that
# carry no retrieval signal and force false-negative AND matches.
_FTS_STOPWORDS = frozenset("""
    a about above after again all am an and any are as at be because been before being between both but by can could
    did do does doing don down during each few for from further had has have having he her here hers herself him himself
    his how i if in into is it its itself just me more most my myself no nor not now of off on once only or other our
    ours ourselves out over own same she should so some such than that the their theirs them themselves then there these
    they this those through to too under until up very was we were what when where which while who whom why will with
    would you your yours yourself yourselves""".split())


logger = logging.getLogger(__name__)


def _shift(sim: float) -> float:
    """Cosine similarity [-1, 1] -> [0, 1]."""
    return (sim + 1.0) / 2.0


class FactRetriever:
    """Multi-strategy fact retrieval with trust-weighted scoring."""

    def __init__(self, store: MemoryStore, temporal_decay_half_life: int = 0,  # days, 0 = disabled
                 fts_weight: float = 0.4, jaccard_weight: float = 0.3, hrr_weight: float = 0.3,
                 hrr_dim: int | None = None):
        self.store, self.half_life = store, temporal_decay_half_life
        self.hrr_dim = hrr_dim if hrr_dim is not None else store.hrr_dim  # store dim is the truth for existing blobs
        if hrr_weight > 0 and not hrr._HAS_NUMPY:  # redistribute weights without numpy
            fts_weight, jaccard_weight, hrr_weight = 0.6, 0.4, 0.0
        self.fts_weight, self.jaccard_weight, self.hrr_weight = fts_weight, jaccard_weight, hrr_weight
        self._role_content = hrr.encode_atom(hrr.ROLE_CONTENT, self.hrr_dim) if self.hrr_weight > 0 else None

    def _phases(self, blob: bytes):
        try:
            return hrr.bytes_to_phases(blob, dim=self.hrr_dim)
        except ValueError:
            return None  # blob written under a different hrr_dim (config drift) — treat as unvectorized, don't crash

    def _canonical(self, name: str) -> str:
        """Resolve an entity name or alias to its canonical stored name (entities.aliases, comma-separated)."""
        row = self.store._one(
            "SELECT name FROM entities WHERE name = ? COLLATE NOCASE OR ',' || aliases || ',' LIKE '%,' || ? || ',%'",
            (name, name))
        return row["name"] if row else name

    def search(self, query: str, category: str | None = None, min_trust: float = 0.3, limit: int = 10) -> list[dict]:
        """Public keyword search: _search plus retrieval recording. probe/related/reason fallbacks
        call _search directly so each recall counts its returned facts exactly once."""
        return self._recorded(self._search(query, category, min_trust, limit))

    def find_duplicates(self, content: str, new_id: int, threshold: float = 0.5, limit: int = 3) -> list[dict]:
        """Near-duplicate candidates for a just-added fact (report-only — the caller decides;
        a memory store must never silently refuse to save). Candidate generation reuses the FTS
        pipeline at min_trust=0.0 (a low-trust near-duplicate is exactly the junk to surface);
        ranking is raw token Jaccard, not search's trust-weighted score, so trust cannot hide a
        near-identical row. Calls _search (not search) so the check doesn't inflate retrieval_count.
        candidate pool is the FTS top-5 — a near-duplicate buried past rank 5 is missed;
        widen the pool or scan all facts if misses become measurable."""
        query_tokens = self._tokenize(content)
        out = []
        for row in self._search(content, min_trust=0.0, limit=5):
            if row["fact_id"] == new_id:
                continue
            score = self._jaccard_similarity(query_tokens, self._tokenize(row["content"]))
            if score >= threshold:
                out.append({"fact_id": row["fact_id"], "content": row["content"], "score": round(score, 3)})
            if len(out) >= limit:
                break
        return out

    def _search(self, query: str, category: str | None = None, min_trust: float = 0.3, limit: int = 10) -> list[dict]:
        with self.store._lock:  # retriever reads must not interleave with a store writer on the shared connection
            """FTS5 candidates (limit*3) → Jaccard + HRR rerank → trust weighting → optional temporal decay
            0.5^(age_days / half_life). Returns fact dicts with 'score', sorted desc."""
            # Discovery passes — candidate generation only. Ranking still scores the ORIGINAL
            # query (Jaccard + HRR), and the prefix fallback fires only when the exact pass
            # found nothing, so a working candidate set is never diluted.
            expanded = self._expand_aliases(query)
            candidates = self._fts_candidates(expanded, category, min_trust, limit * 3)
            if not candidates:
                # Exact tokens miss inflections (backup/backups, rotation/rotate): retry with
                # prefix tokens ("tok"*) when — and only when — the exact pass returned nothing.
                candidates = self._fts_candidates(expanded, category, min_trust, limit * 3, prefix=True)
            query_tokens = self._tokenize(query)
            # Query vector is loop-invariant; encode lazily on the first candidate that carries an HRR vector
            # so stores whose hrr_vector was never backfilled don't pay for it.
            query_vec = None
            for fact in candidates:
                jaccard = self._jaccard_similarity(query_tokens, self._tokenize(fact["content"]) | self._tokenize(fact.get("tags", "")))
                hrr_sim = 0.5  # neutral
                if self.hrr_weight > 0 and fact.get("hrr_vector"):
                    fact_vec = self._phases(fact["hrr_vector"])
                    if fact_vec is not None:
                        if query_vec is None:
                            query_vec = hrr.encode_text(query, self.hrr_dim)
                        # Compare the query to the fact's content component (unbind the ROLE_CONTENT binding) —
                        # same convention as probe(). Against the full role-structured vector the HRR term is
                        # systematically diluted by entity-role components a bare query can never share.
                        hrr_sim = _shift(hrr.similarity(query_vec, hrr.unbind(fact_vec, self._role_content)))
                relevance = self.fts_weight * fact.get("fts_rank", 0.0) + self.jaccard_weight * jaccard + self.hrr_weight * hrr_sim
                fact["score"] = relevance * fact["trust_score"]
                if self.half_life > 0:
                    fact["score"] *= self._temporal_decay(fact.get("updated_at") or fact.get("created_at"))
            results = sorted(candidates, key=lambda x: x["score"], reverse=True)[:limit]
            for fact in results:
                fact.pop("hrr_vector", None)  # callers expect JSON-serializable dicts
            return results

    def _recorded(self, results: list) -> list:
        """Record returned facts as retrievals (retrieval_count) and pass them through unchanged."""
        self._record_retrievals(results)
        return results

    def _record_retrievals(self, facts: list) -> None:
        """Count the facts a recall surfaced, one batched UPDATE per call. retrieval_count is the
        usage signal — which facts the agent actually pulls (input to any future trust/decay tuning)."""
        if not facts:
            return
        self.store._write(
            "UPDATE facts SET retrieval_count = retrieval_count + 1 WHERE fact_id IN ({})".format(",".join("?" * len(facts))),
            [f["fact_id"] for f in facts])

    def _entity_fact_ids(self, entity: str, category: str | None) -> set:
        """Fact IDs structurally linked to an entity name (case-insensitive), optionally within a category."""
        sql = ("SELECT DISTINCT fe.fact_id FROM fact_entities fe "
               "JOIN entities e ON e.entity_id = fe.entity_id WHERE e.name = ? COLLATE NOCASE")
        params: list = [entity]
        if category:
            sql += " AND fe.fact_id IN (SELECT fact_id FROM facts WHERE category = ?)"
            params.append(category)
        return {row[0] for row in self.store._conn.execute(sql, params)}

    def _rows_by_ids(self, ids: set, category: str | None) -> list:
        qmarks = ",".join("?" * len(ids))
        where = f"WHERE f.fact_id IN ({qmarks})"
        params: list = sorted(ids)
        if category:
            where += " AND f.category = ?"
            params.append(category)
        return self.store._conn.execute(
            f"SELECT {_FACT_COLUMNS}, hrr_vector FROM facts f {where}", params).fetchall()

    def _ctx_bundle(self, rows: list):
        """Content context of a fact set: bundle of the decodable fact vectors (None if none)."""
        # re-decodes each blob (once for ctx, again in _rank_by_vector); cache if this shows in a profile
        vecs = [self._phases(r["hrr_vector"]) for r in rows]
        vecs = [v for v in vecs if v is not None]
        return hrr.bundle(*vecs) if vecs else None

    def probe(self, entity: str, category: str | None = None, limit: int = 10) -> list[dict]:
        """Compositional entity query: facts where the entity is a linked subject, ranked by content
        centrality to the entity's fact set. Entity structure lives in fact_entities (SQL); the HRR is
        used only to rank within the set — unbinding a key from a bundled fact vector is algebraically
        meaningless (a bundle is lossy), which made the old per-fact unbind return noise.
        Falls back to FTS5 when nothing is structurally linked or without numpy."""
        with self.store._lock:  # retriever reads must not interleave with a store writer on the shared connection
            entity = self._canonical(entity)  # alias -> canonical, so either AKA name works
            if not hrr._HAS_NUMPY:
                return self.search(entity, category=category, limit=limit)
            rows = self._rows_by_ids(self._entity_fact_ids(entity, category), category)
            if not rows:
                return self.search(entity, category=category, limit=limit)
            ctx = self._ctx_bundle(rows)
            return self._recorded(self._rank_by_ctx(rows, ctx, limit))

    def related(self, entity: str, category: str | None = None, limit: int = 10) -> list[dict]:
        """Facts structurally connected to an entity: its own facts plus 1-hop neighbors (facts sharing any
        of its entities), ranked by content centrality to the entity's own fact set. Falls back to FTS5
        when nothing is linked or without numpy."""
        with self.store._lock:
            entity = self._canonical(entity)  # alias -> canonical
            if not hrr._HAS_NUMPY:
                return self.search(entity, category=category, limit=limit)
            own_ids = self._entity_fact_ids(entity, category)
            if not own_ids:
                return self.search(entity, category=category, limit=limit)
            own = self._rows_by_ids(own_ids, category)
            # 1-hop: facts linked to any of the entity's entities, excluding the entity's own facts
            qmarks = ",".join("?" * len(own_ids))
            hop_ids = {row[0] for row in self.store._conn.execute(
                f"SELECT DISTINCT fe2.fact_id FROM fact_entities fe1 "
                f"JOIN fact_entities fe2 ON fe2.entity_id = fe1.entity_id AND fe2.fact_id != fe1.fact_id "
                f"WHERE fe1.fact_id IN ({qmarks})", sorted(own_ids))}
            if category:  # hop set bypasses the category filter above
                hop_ids = {fid for fid in hop_ids if self.store._conn.execute(
                    "SELECT 1 FROM facts WHERE fact_id = ? AND category = ?", (fid, category)).fetchone()}
            rows = own + (self._rows_by_ids(hop_ids, category) if hop_ids else [])
            ctx = self._ctx_bundle(own)  # centrality to the entity's own facts, not the neighborhood
            return self._recorded(self._rank_by_vector(rows, lambda _f, v: hrr.similarity(v, ctx) if ctx is not None else 0.0, limit))

    def reason(self, entities: list[str], category: str | None = None, limit: int = 10) -> list[dict]:
        """Multi-entity compositional query (entity-graph JOIN): facts linked to ALL entities, ranked by
        content centrality to the intersection. Falls back to FTS5 on empty intersection or without numpy."""
        with self.store._lock:
            if not hrr._HAS_NUMPY or not entities:
                return self.search(" ".join(entities), category=category, limit=limit)
            ids = set.intersection(*(self._entity_fact_ids(self._canonical(e), category) for e in entities))
            if not ids:
                return self.search(" ".join(entities), category=category, limit=limit)
            rows = self._rows_by_ids(ids, category)
            ctx = self._ctx_bundle(rows)
            return self._recorded(self._rank_by_vector(rows, lambda _f, v: hrr.similarity(v, ctx) if ctx is not None else 0.0, limit))

    def _rank_by_ctx(self, rows: list, ctx, limit: int) -> list[dict]:
        return self._rank_by_vector(rows, lambda _f, v: hrr.similarity(v, ctx) if ctx is not None else 0.0, limit)

    def contradict(self, category: str | None = None, threshold: float = 0.3, limit: int = 10) -> list[dict]:
        with self.store._lock:  # retriever reads must not interleave with a store writer on the shared connection
            """Pairs of facts sharing entities (same subject) with low content-vector similarity (different claims). Empty without numpy."""
            if not hrr._HAS_NUMPY:
                return []
            rows = self._vector_rows(category, columns="fact_id, content, category, tags, trust_score, created_at, updated_at, hrr_vector")
            if len(rows) < 2:
                return []
            if len(rows) > 500:  # O(n²) guard: only compare the most recently updated facts
                rows = sorted(rows, key=lambda r: r["updated_at"] or r["created_at"], reverse=True)[:500]
            facts = []  # (public dict, lower-cased entity names, phase vector)
            # One entity lookup for all rows (a per-row query would be N round-trips).
            qmarks = ",".join("?" * len(rows))
            entities_by_fact: dict = {}
            for er in self.store._conn.execute(
                    "SELECT fe.fact_id, e.name FROM entities e JOIN fact_entities fe ON fe.entity_id = e.entity_id "
                    f"WHERE fe.fact_id IN ({qmarks})", [r["fact_id"] for r in rows]):
                entities_by_fact.setdefault(er["fact_id"], set()).add(er["name"].lower())
            for row in rows:
                fact = dict(row)
                vec = self._phases(fact.pop("hrr_vector"))
                if vec is None:
                    continue  # wrong-dim blob — skip rather than crash the whole pass
                facts.append((fact, entities_by_fact.get(fact["fact_id"], set()), vec))
            contradictions = []
            for i, (f1, ents1, vec1) in enumerate(facts):
                for f2, ents2, vec2 in facts[i + 1:]:
                    if not ents1 or not ents2:
                        continue
                    entity_overlap = len(ents1 & ents2) / len(ents1 | ents2)
                    if entity_overlap < 0.3:
                        continue  # not enough shared subject to be contradictory
                    content_sim = hrr.similarity(vec1, vec2)
                    contradiction_score = entity_overlap * (1.0 - _shift(content_sim))  # high overlap + low similarity
                    if contradiction_score >= threshold:
                        contradictions.append({
                            "fact_a": f1, "fact_b": f2,
                            "entity_overlap": round(entity_overlap, 3),
                            "content_similarity": round(content_sim, 3),
                            "contradiction_score": round(contradiction_score, 3),
                            "shared_entities": sorted(ents1 & ents2),
                        })
            return sorted(contradictions, key=lambda x: x["contradiction_score"], reverse=True)[:limit]

    def _vector_rows(self, category: str | None, columns: str = _FACT_COLUMNS + ", hrr_vector") -> list:
        """All facts that carry an HRR vector, optionally filtered by category."""
        where = "WHERE hrr_vector IS NOT NULL" + (" AND category = ?" if category else "")
        return self.store._conn.execute(f"SELECT {columns} FROM facts {where}", [category] if category else []).fetchall()

    def _rank_by_vector(self, rows: list, sim_fn: Callable[[dict, object], float], limit: int) -> list[dict]:
        """Score each row as (sim + 1) / 2 * trust_score (sim shifted to [0, 1]), sorted desc. Rows whose
        blob doesn't decode under the current dim (config drift) are skipped, not fatal."""
        scored = []
        for row in rows:
            vec = self._phases(row["hrr_vector"])
            if vec is None:
                continue
            fact = dict(row)
            fact.pop("hrr_vector", None)
            fact["score"] = _shift(sim_fn(fact, vec)) * fact["trust_score"]
            scored.append(fact)
        return sorted(scored, key=lambda x: x["score"], reverse=True)[:limit]

    def _expand_aliases(self, query: str) -> str:
        """Append the entity name set for every entity the query names (canonical name or alias,
        case-insensitive, word-boundary), so a query that names an entity by one name also
        matches facts that mention only the other ("KVStore ..." reaches a fact that says only
        "Redis ..."). Candidate generation only — Jaccard/HRR still score the original query.
        full entity scan per search — fine at single-user store sizes (dozens of
        entities, one small SELECT); widen to an index/limit if that ever shows in a profile."""
        q = query.lower()
        extra: list[str] = []
        seen: set = set()
        rows = self.store._conn.execute("SELECT name, aliases FROM entities").fetchall()
        for row in rows:
            names = [row["name"]] + [a.strip() for a in (row["aliases"] or "").split(",") if a.strip()]
            if not any(re.search(rf"(?<![a-z0-9]){re.escape(n.lower())}(?![a-z0-9])", q) for n in names):
                continue
            for n in names:
                key = n.lower()
                if key not in seen:
                    seen.add(key)
                    extra.append(n)
        return f"{query} {' '.join(extra)}".strip() if extra else query

    def _fts_candidates(self, query: str, category: str | None, min_trust: float, limit: int, prefix: bool = False) -> list[dict]:
        """Raw FTS5 MATCH candidates with rank normalized to [0, 1] as 'fts_rank'.
        prefix=True turns each token into a prefix match ("tok"*), used as the zero-candidate fallback."""
        category_clause = "AND f.category = ? " if category else ""
        params = [self._sanitize_fts_query(query, prefix=prefix)] + ([category] if category else []) + [min_trust, limit]
        sql = ("SELECT f.*, facts_fts.rank as fts_rank_raw FROM facts_fts JOIN facts f ON f.fact_id = facts_fts.rowid "
               f"WHERE facts_fts MATCH ? {category_clause}AND f.trust_score >= ? ORDER BY facts_fts.rank LIMIT ?")
        try:
            results = [dict(row) for row in self.store._conn.execute(sql, params).fetchall()]
        except Exception as exc:
            # FTS5 MATCH can fail on malformed queries; a persistent failure here
            # means a corrupt index — log it so it is not invisible.
            logger.warning("tiberius FTS5 search failed for %r: %s", query, exc)
            return []
        # FTS5 rank is negative (lower = better); normalize |rank| / max to [0, 1] (1e-6 floor avoids div by zero)
        max_rank = max([abs(f["fts_rank_raw"]) for f in results] + [1e-6])
        for fact in results:
            fact["fts_rank"] = abs(fact.pop("fts_rank_raw")) / max_rank
        return results

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        """Lowercase whitespace tokens with surrounding punctuation stripped (no stemming)."""
        return {c for c in (w.strip(_PUNCT) for w in text.lower().split()) if c} if text else set()

    @staticmethod
    def _sanitize_fts_query(query: str, prefix: bool = False) -> str:
        """Natural-language query -> FTS5-safe OR expression of quoted tokens. FTS5 AND-joins a multi-word
        MATCH by default, which tanks recall on prose: drop stopwords and <2-char tokens, strip FTS5 operator
        chars, phrase-quote each survivor. If nothing survives, return the raw query (zero results, not a SQL
        error). prefix=True appends FTS5's prefix operator to each token ("tok"*), so inflected stored
        tokens (fetch/fetches) still match — used only as the zero-candidate fallback."""
        if not query:
            return ""
        suffix = "*" if prefix else ""
        tokens = [f'"{c}"{suffix}' for c in (raw.strip(_PUNCT).translate(_FTS_OPERATORS) for raw in query.lower().split())
                  if len(c) >= 2 and c not in _FTS_STOPWORDS]
        return " OR ".join(tokens) if tokens else query

    @staticmethod
    def _jaccard_similarity(set_a: set, set_b: set) -> float:
        """Jaccard similarity coefficient: |A ∩ B| / |A ∪ B|."""
        return len(set_a & set_b) / len(set_a | set_b) if set_a and set_b else 0.0

    def _temporal_decay(self, timestamp_str: str | None) -> float:
        """0.5^(age_days / half_life); 1.0 if disabled, missing, unparseable, or in the future."""
        if not self.half_life or not timestamp_str:
            return 1.0
        try:
            ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00")) if isinstance(timestamp_str, str) else timestamp_str
            age_days = (datetime.now(timezone.utc) - (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc))).total_seconds() / 86400
            return 1.0 if age_days < 0 else math.pow(0.5, age_days / self.half_life)
        except (ValueError, TypeError):
            return 1.0
