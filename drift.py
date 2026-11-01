"""drift: two-sided plan/state comparison pack (design: docs/drift.md).

Composition above the core trio: an exact tag+entity query for the plan side
(no top-N, so no plan claim is ever crowded out by ranking), search ∪ probe
∪ related with a created_at window for the state side, and per-claim echo
markers (overlap coefficient on cleaned token sets). Read-only apart from
retrieval recording (a drift IS a recall — three underlying ones).

Harness-free: the CLI subcommand, the OpenCode plugin tool, and the Hermes
provider action all wrap drift() — one implementation, three consumers.

The judgment is never made here: drift() assembles the evidence (which plan
claims have state echoes since the anchor, which don't) and the agent that
reads the pack decides whether that is drift.
"""
from __future__ import annotations

from . import retrieval as _retrieval
from .brief import _fill, _rank
from .retrieval import FactRetriever
from .store import MemoryStore, _parse_since

_PLAN_TAG = "plan"
_PLAN_TAG_SQL = "(',' || f.tags || ',') LIKE '%,plan,%'"  # exact tag in the comma list


def _plan_facts(project: str, conn) -> list[dict]:
    """Facts linked to the project's entity AND carrying the 'plan' tag — an exact
    query (caller holds MemoryStore._lock). No top-N and no ranking cutoff: the
    spike found a centrality top-N could crowd plan facts out of the window once a
    project accumulates enough state facts."""
    sql = ("SELECT f.fact_id, f.content, f.category, f.tags, f.trust_score, f.retrieval_count, "
           "f.helpful_count, f.created_at, f.updated_at FROM facts f "
           "JOIN fact_entities fe ON fe.fact_id = f.fact_id "
           "JOIN entities e ON e.entity_id = fe.entity_id "
           f"WHERE e.name = ? COLLATE NOCASE AND {_PLAN_TAG_SQL} AND f.superseded_by IS NULL ORDER BY f.fact_id")
    return [dict(r) for r in conn.execute(sql, (project,)).fetchall()]


def _tokens(f: dict, project: str) -> set[str]:
    """A fact's claim signature: the house FTS cleaning (stopwords stripped,
    >=2-char tokens, content plus tags) with the project token excluded — the
    hub token is in every fact on both sides and carries no echo weight."""
    toks = FactRetriever._tokenize(f["content"]) | FactRetriever._tokenize(f.get("tags") or "")
    return {t for t in toks if len(t) >= 2 and t not in _retrieval._FTS_STOPWORDS and t != project.lower()}


def _overlap(a: set[str], b: set[str]) -> float:
    """Overlap coefficient |A∩B| / min(|A|,|B|): how much of the smaller set (the plan
    claim) is echoed by the state fact. Length-robust where raw Jaccard is not — a
    long state fact must not dilute the score (spike finding 2/3, docs/drift.md §6)."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _rank_by_recency(facts: list[dict]) -> list[dict]:
    """State side: recency is the one scale comparable across the three sources
    (search relevance and entity centrality are not — the same reason brief tiers),
    and drift is a recency question: 'what changed since the anchor'.
    Deterministic: created_at desc, trust desc, fact_id asc."""
    facts = sorted(facts, key=lambda f: f["fact_id"])
    facts = sorted(facts, key=lambda f: f.get("trust_score", 0.0), reverse=True)
    return sorted(facts, key=lambda f: f.get("created_at") or "", reverse=True)


def drift(project: str, ret: FactRetriever, store: MemoryStore, *,
          since: str | None = None, budget: int = 300, top: int = 20,
          support: float = 0.15) -> dict:
    """Two-sided comparison pack for a project: its plan facts (tag 'plan' linked to
    the project's entity) against the state facts accumulated since the anchor, with
    per-claim echo markers. The agent reads the pack and judges — drift() never scores drift.

    Anchor: since (ISO or relative, the store._parse_since forms) or, by default, the
    oldest plan fact's created_at — 'since the plan was declared' (update never moves
    created_at, so the anchor is stable). An existing project checked for the first
    time wants an explicit since (its history predates the plan facts).

    min_trust is 0.0 on the state-side search: the window should be complete (what
    happened is what happened); each fact's trust_score ships in its shape so the
    reader can discount."""
    project = ret._canonical(project)  # alias -> canonical, so either AKA name works
    with store._lock:
        plan = _plan_facts(project, store._conn)
    if not plan:
        raise ValueError(
            f"no plan facts for {project!r}: store the plan first "
            f"(add facts tagged {_PLAN_TAG!r} that name the project), then drift again")
    anchor = min(f["created_at"] for f in plan) if since is None else _parse_since(since)

    pool: dict[int, dict] = {}
    for f in ret.search(project, min_trust=0.0, limit=top):
        pool[f["fact_id"]] = f
    for f in ret.probe(project, limit=top):
        pool[f["fact_id"]] = f
    for f in ret.related(project, limit=top):
        pool[f["fact_id"]] = f
    plan_ids = {f["fact_id"] for f in plan}
    state = _rank_by_recency([f for f in pool.values()
                              if f["fact_id"] not in plan_ids and f["created_at"] >= anchor])

    state_sigs = {f["fact_id"]: _tokens(f, project) for f in state}
    echoes: dict[int, list[tuple[float, int]]] = {}
    for f in plan:
        sig = _tokens(f, project)
        scored = sorted(((_overlap(sig, s), fid) for fid, s in state_sigs.items()), reverse=True)
        echoes[f["fact_id"]] = [(j, fid) for j, fid in scored if j >= support]

    # Split fill, not tiered: the pack is two-sided (docs/drift.md §1), so each side
    # gets its own half of the budget. Plan-first was retired by the first live run,
    # where 7 plan claims consumed the whole budget and 0 of 16 state facts showed.
    # The window totals say how much of each side was cut, and a plan fact with no
    # echo is still reported in 'unbacked' below regardless of the fill.
    plan_shown, used = _fill(_rank(plan), budget // 2)
    state_shown, used2 = _fill(state, budget - budget // 2)
    used += used2

    unbacked = [f for f in plan if not echoes[f["fact_id"]]]

    def shape_plan(f: dict) -> dict:
        e = echoes[f["fact_id"]]
        row = {"fact_id": f["fact_id"], "content": f["content"],
               "category": f.get("category"), "trust_score": f.get("trust_score")}
        row["support"] = ({"count": len(e), "best": round(e[0][0], 3),
                           "echoes": [fid for _, fid in e[:5]]} if e else None)
        return row

    def shape_state(f: dict) -> dict:
        return {"fact_id": f["fact_id"], "content": f["content"],
                "category": f.get("category"), "trust_score": f.get("trust_score"),
                "created_at": f.get("created_at")}

    out = {
        "project": project,
        "anchor": anchor,
        "budget": budget,
        "tokens": used,  # the pack (plan + state shown); 'unbacked' is a supplemental list
        "plan": {"total": len(plan), "count": len(plan_shown),
                 "facts": [shape_plan(f) for f in plan_shown]},
        "state": {"total": len(state), "count": len(state_shown),
                  "facts": [shape_state(f) for f in state_shown]},
    }
    if unbacked:  # supplemental field: omitted when empty, note when present (ARCHITECTURE.md)
        out["unbacked"] = [shape_plan(f) for f in unbacked]
        out["note"] = (f"{len(unbacked)} of {len(plan)} plan facts have no state echo >= {support} "
                       f"since {anchor} — no state evidence means stale or unbuilt (or code-level "
                       f"and never recorded); verify before treating it as drift")
    return out
