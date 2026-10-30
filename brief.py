"""brief: token-budgeted context pack (design: docs/brief.md).

Composition above the core trio: search + probe (existing Retrieval methods),
dedupe, floor, tiered rank, budget fill. Read-only apart from retrieval
recording (a brief IS a recall — search and probe are both the recorded
variants, so a fact in both sets counts once per underlying recall).
ponytail: the overlap double-count; if it shows in trust tuning, dedupe before recording.

Harness-free: the CLI subcommand, the OpenCode plugin tool, and the Hermes
provider's fact_store(action='brief') all wrap brief() — one implementation,
three consumers.
"""
from __future__ import annotations

import re
import sqlite3

from .retrieval import FactRetriever
from .store import MemoryStore, _parse_since


def _named_entities(text: str, conn: sqlite3.Connection) -> list[str]:
    """Canonical names of entities that appear in *text* at word boundaries —
    the same convention as FactRetriever._expand_aliases (which inlines the
    same scan for FTS alias expansion). These are brief's probe targets."""
    q = text.lower()
    out: list[str] = []
    seen: set[str] = set()
    for row in conn.execute("SELECT name, aliases FROM entities"):
        names = [row["name"]] + [a.strip() for a in (row["aliases"] or "").split(",") if a.strip()]
        if not any(re.search(rf"(?<![a-z0-9]){re.escape(n.lower())}(?![a-z0-9])", q) for n in names):
            continue
        if row["name"].lower() not in seen:  # an alias hit resolves to the canonical name
            seen.add(row["name"].lower())
            out.append(row["name"])
    return out


def _rank(facts: list[dict]) -> list[dict]:
    """Deterministic rank within one tier: score desc, then trust_score desc,
    then updated_at desc (ISO strings), then fact_id asc. Stable multi-pass sort."""
    facts = sorted(facts, key=lambda f: f["fact_id"])
    facts = sorted(facts, key=lambda f: f.get("updated_at") or f.get("created_at") or "", reverse=True)
    facts = sorted(facts, key=lambda f: f.get("trust_score", 0.0), reverse=True)
    return sorted(facts, key=lambda f: f.get("score", 0.0), reverse=True)


def _fill(facts: list[dict], budget: int) -> tuple[list[dict], int]:
    """Budget fill in rank order at the char/4 token estimate (ceil). A fact
    that does not fit is dropped whole (a partial fact is worse than no
    fact); the scan continues past a miss, so a smaller fact later in rank
    order may still fit."""
    kept: list[dict] = []
    used = 0
    for f in facts:
        toks = -(-len(f["content"]) // 4)
        if used + toks <= budget:
            kept.append(f)
            used += toks
    return kept, used


def brief(query: str, ret: FactRetriever, store: MemoryStore, *,
          budget: int = 300, floor: float = 0.10, top: int = 20,
          category: str | None = None, min_trust: float = 0.3,
          since: str | None = None,
          skills: int = 2) -> dict:
    """Token-budgeted context pack for a query: search ∪ probe → dedupe →
    floor → tiered rank → budget fill → optional skill lane.
    Tier 1 is the query-scored pool facts (the floor applies); tier 2 is entity-only
    facts (probe's structural path is not trust-gated and its centrality
    score is on a different scale, so it bypasses the floor and ranks as its
    own tier). 'total' is the query-match pool via match_count() — the FTS
    pool, no rerank, no recording — so a rendered block can state it is a
    window, not the whole (same marker as the prefetch counter). Both take
    the same since window (created_at at or after), so the marker describes
    the window, not the whole store."""
    pool = ret.search(query, category=category, min_trust=min_trust, limit=top, since=since)
    total = ret.match_count(query, category=category, min_trust=min_trust, since=since)
    pool_ids = {f["fact_id"] for f in pool}

    with store._lock:  # shared-connection rule: the entities scan touches the connection directly
        entities = _named_entities(query, store._conn)
    since_cut = _parse_since(since) if since is not None else None
    entity_only: list[dict] = []
    seen_entity: set[int] = set()
    for entity in entities:
        for f in ret.probe(entity, category=category, limit=top):
            if since_cut is not None and (f.get("created_at") or "") < since_cut:
                # The window applies to the probe tier too — otherwise a stale
                # entity-linked fact re-enters the pack search left it out.
                # ponytail: post-filter after probe's top-N pick — a windowed brief
                # can under-fill the entity tier when the newest linked facts rank
                # past top-N; widen top if it shows.
                continue
            if f["fact_id"] not in pool_ids and f["fact_id"] not in seen_entity:
                seen_entity.add(f["fact_id"])
                entity_only.append(f)

    skill_pool = [f for f in pool if f.get("category") == "skill" and f["score"] >= floor]
    fact_pool = [f for f in pool if f.get("category") != "skill" and f["score"] >= floor]
    entity_facts = [f for f in entity_only if f.get("category") != "skill"]
    entity_skills = [f for f in entity_only if f.get("category") == "skill"]

    # Tiered fill: the query-scored pool consumes the budget first (tier 1),
    # entity-only facts the remainder (tier 2) — their score scales differ,
    # so they are ranked and filled separately, never interleaved.
    tier1, used = _fill(_rank(fact_pool), budget)
    tier2, used2 = _fill(_rank(entity_facts), budget - used)
    facts = tier1 + tier2
    used += used2
    skill_rows, skill_toks = _fill(_rank(skill_pool + entity_skills), budget - used)
    skill_rows = skill_rows[: max(0, skills)]
    skill_toks = sum(-(-len(s["content"]) // 4) for s in skill_rows)

    def shape(f: dict) -> dict:
        return {"fact_id": f["fact_id"], "content": f["content"],
                "category": f.get("category"), "trust_score": f.get("trust_score"),
                "score": round(f.get("score", 0.0), 3)}

    return {
        "query": query,
        "total": total,  # query-match pool (like search's 'total') — the window's "of N"
        "entity_only": len([f for f in facts + skill_rows if f["fact_id"] in seen_entity]),
        "count": len(facts),
        "tokens": used + skill_toks,  # char/4 estimate of shown content (facts + skills)
        "budget": budget,
        "facts": [shape(f) for f in facts],
        "skills": [shape(f) for f in skill_rows],
    }
