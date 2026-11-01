"""tiberius memory plugin (MemoryProvider): structured fact storage with entity
resolution, trust scoring, and HRR-based compositional retrieval. Original plugin by dusterbloom (PR #2351).
Config in $HERMES_HOME/config.yaml under plugins.tiberius: db_path ($HERMES_HOME/tiberius_store.db),
auto_extract (false), default_trust (0.5), min_trust_threshold (0.3), temporal_decay_half_life (0),
hrr_dim (1024), hrr_weight (0.3)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error
from utils import is_truthy_value
from .brief import brief
from .drift import drift
from .store import MemoryStore, extract_statements
from .retrieval import FactRetriever
from hermes_cli.config import cfg_get

logger = logging.getLogger(__name__)

# Expanded by initialize() against the profile that opens it. A concrete path is copied by a profile clone
# and outlives a rename, so it keeps naming the old profile's DB.
_DEFAULT_DB_PATH = "$HERMES_HOME/tiberius_store.db"


FACT_STORE_SCHEMA = {
    "name": "fact_store",
    "description": (
        "Deep structured memory with algebraic reasoning. Use alongside the memory tool — memory for always-on "
        "context, fact_store for deep recall and compositional queries.\n\nACTIONS (simple → powerful):\n"
        "• add — Store a fact the user would expect you to remember. When a near-identical existing fact exists it is returned as possible_duplicates with a note — remove the older one if it's a true duplicate.\n• search — Keyword lookup ('editor config', 'deploy process'). 'search'/'brief' also take 'categories' (IN list) and 'exclude_category' to filter the candidate pool — e.g. drop 'skill' facts that dilute generic queries.\n"
        "• probe — Entity recall: ALL facts about a person/thing. Pass several via the 'entities' param for the union (facts about ANY of them); 'reason' is the ALL-of-them shape.\n• related — What connects to an entity? Structural adjacency.\n"
        "• brief — One-call context pack for a task: search + entity probe, token-budgeted (whole facts only, "
        "default 300) with a window marker (how many more matches exist). Use it when starting a task instead of "
        "several separate recall calls.\n"
        "• drift — Project drift check: the project's plan facts (tag 'plan': purpose, invariants) vs the state "
        "facts accumulated since the anchor (default: the oldest plan fact), with per-claim echo markers. Read both sides and "
        "judge drift yourself — the markers are hints, not verdicts. The 'unbacked' field (when present) lists "
        "plan claims with no state echo since the anchor: stale, unbuilt, or never recorded — verify before "
        "treating them as drift.\n"
        "• reason — Compositional: facts connected to MULTIPLE entities simultaneously.\n"
        "• stats — Store orientation: totals, category mix, trust buckets, stale hygiene count. Call once at the start of a session or before a hygiene sweep (contradict, bulk remove).\n"
        "• checkpoint — Session handoff: one active checkpoint per session (task/decisions/next as 1-8 short lines). "
        "op 'set' (default, takes 'facts' + 'session') replaces it; 'show' reads; 'clear' deletes. "
        "Set it at compaction or before ending a long task; the next session shows it instead of re-deriving state from prose.\n"
        "• supersede — Replace a stale fact: inserts a replacement (normal add semantics) and hides the old one "
        "from recall; the old row stays readable via 'show' with superseded_by/superseded_at. 'content' is the "
        "replacement statement (must differ from every existing fact); 'category'/'tags' optional.\n"
        "• contradict — Memory hygiene: find facts making conflicting claims.\n• update/remove/list — CRUD operations.\n\n"
        "IMPORTANT: Before answering anything about the user, the environment, or the project, "
        "ALWAYS recall first (probe for entities, search for topics, reason across both) — "
        "stored facts may not be in conversation context."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "add_batch", "search", "probe", "show", "entities", "brief", "drift", "related", "reason", "contradict", "update", "remove", "list", "stats", "checkpoint", "supersede"]},
            "content": {"type": "string", "description": "Fact content (required for 'add'; for 'supersede' the replacement statement)."},
            "query": {"type": "string", "description": "Search query (required for 'search'/'brief')."},
            "facts": {"type": "array", "minItems": 1,
                      "items": {"anyOf": [{"type": "string"},
                                          {"type": "object", "properties": {
                                               "content": {"type": "string"},
                                               "category": {"type": "string"},
                                               "tags": {"type": "string"}},
                                            "required": ["content"]}]},
                      "description": "Facts to add in one call (action 'add_batch'): strings or objects with content (plus optional category, tags). Validated up front, committed per item; a bad item errors with its index and nothing executes (docs/batch.md). For 'checkpoint' op 'set': 1-8 short handoff lines, one string each (task/decision/state/next)."},
            "project": {"type": "string", "description": "Project name for 'drift' (its plan facts are the 'plan'-tagged facts that name it)."},
            "budget": {"type": "integer", "description": "'brief'/'drift': token budget for the pack (default: 300)."},
            "support": {"type": "number", "description": "'drift': min overlap coefficient for a state fact to count as a plan-claim echo (default: 0.15)."},
            "entity": {"type": "string", "description": "Entity name for 'probe'/'related'. 'probe' also accepts several names via 'entities' (union: any)."},
            "entities": {"type": "array", "items": {"type": "string"}, "description": "Entity names for 'reason' (facts linked to ALL of them). For 'probe': union — facts linked to ANY of them (takes precedence over 'entity')."},
            "fact_id": {"type": "integer", "description": "Fact ID for 'show'/'update'/'remove'/'supersede'."},
            "category": {"type": "string", "enum": ["user_pref", "project", "tool", "general"]},
            "categories": {"type": "array", "items": {"type": "string"},
                           "description": "Category IN-list for 'search'/'brief' (e.g. [\"user_pref\", \"project\"]). Takes precedence over 'category'. Free-form — 'skill' is legal here even though it is not in the 'category' enum."},
            "exclude_category": {"type": "string",
                                 "description": "One category to drop from 'search'/'brief' results (e.g. 'skill' when the store carries many skill facts). Applies on top of 'categories'."},
            "session": {"type": "string", "description": "'checkpoint' (required): the session id the checkpoint belongs to (e.g. the harness session id)."},
            "op": {"type": "string", "enum": ["set", "show", "clear"],
                   "description": "'checkpoint' only: the operation (default 'set'). 'set' replaces the session's active checkpoint with 'facts'; 'show' reads it; 'clear' deletes it."},
            "tags": {"type": "string", "description": "Comma-separated tags."},
            "trust_delta": {"type": "number", "description": "Trust adjustment for 'update'."},
            "min_trust": {"type": "number", "description": "Minimum trust filter (default: 0.3)."},
            "since": {"type": "string", "description": "'list'/'search'/'brief': only facts created at or after the cutoff. 'drift': the state-window anchor. ISO (YYYY-MM-DD[ HH:MM:SS] UTC) or relative (30m/2h/7d/2w); 'drift' defaults to the oldest plan fact."},
            "order": {"type": "string", "enum": ["trust", "retrieval", "recent", "stale"],
                      "description": "'list': ordering — trust (default, trust_score desc), retrieval (most-recalled first), recent (newest first), stale (oldest-updated first)."},
            "limit": {"type": "integer", "description": "Max results (default: 10)."},
        },
        "required": ["action"],
    },
}

FACT_FEEDBACK_SCHEMA = {
    "name": "fact_feedback",
    "description": ("Rate a fact after using it. Mark 'helpful' if accurate, 'unhelpful' if outdated. "
                    "This trains the memory — good facts rise, bad facts sink. "
                    "Pass fact_ids (list) to rate several facts in one call; each bad id is reported, not raised."),
    "parameters": {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": ["helpful", "unhelpful"]},
                       "fact_id": {"type": "integer", "description": "The fact ID to rate."},
                       "fact_ids": {"type": "array", "items": {"type": "integer"},
                                    "description": "Fact IDs to rate in one call (same action). Mutually exclusive with fact_id."}},
        "required": ["action"],
    },
}

# Auto-extraction (on_session_end) patterns + statement cleanup live in the core
# (store.extract_statements) so the CLI and every other harness extract identically.


def _load_plugin_config() -> dict:
    try:
        from hermes_cli.config import load_config_readonly  # canonical: managed-scope overlay + ${VAR} expansion
        return cfg_get(load_config_readonly(), "plugins", "tiberius", default={}) or {}
    except Exception as exc:
        logger.warning("tiberius plugin config load failed (%s) — using defaults", exc)
        return {}


def _truncate_skill_desc(s: str, limit: int = 120) -> str:
    """Description for a skill block line: cut at *limit* chars on a word boundary (same
    render as the OpenCode plugin, so a skill reads identically in both harnesses)."""
    if len(s) <= limit:
        return s
    cut = s[:limit]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 0 else cut).rstrip() + "…"


def _skill_view_names(messages: list) -> list[str]:
    """Skill names the model loaded via skill_view, transcript order (may repeat; callers
    dedupe). Mirrors the core's own detector (agent/context_compressor_tail
    .skill_view_names_by_call_id): OpenAI-shaped assistant tool_calls with
    function.arguments as a JSON string; dict- and object-shaped rows both accepted."""
    names: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {}) if isinstance(tc, dict) else getattr(tc, "function", None)
            if not fn:
                continue
            if (fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", None)) != "skill_view":
                continue
            args = fn.get("arguments") if isinstance(fn, dict) else getattr(fn, "arguments", None)
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    continue
            skill = args.get("name") if isinstance(args, dict) else None
            if isinstance(skill, str) and skill:
                names.append(skill)
    return names


def _results(items: list, key: str = "results") -> str:
    return json.dumps({key: items, "count": len(items)})


def _limit(args: dict) -> int:
    # SQLite LIMIT -1 means unlimited; a negative limit from the model must
    # not dump the whole store.
    return max(1, int(args.get("limit", 10)))


def _tool_handler(actions: dict):
    """(self, args) handler dispatching on args["action"] over ``actions``; unknown action -> tool_error."""
    return lambda self, args: (actions[args["action"]](self, args) if args["action"] in actions
                               else tool_error(f"Unknown action: {args['action']}"))


class TiberiusMemoryProvider(MemoryProvider):
    """Tiberius memory with structured facts, entity resolution, and HRR retrieval."""

    def __init__(self, config: dict | None = None):
        self._config = config or _load_plugin_config()
        self._store = self._retriever = None
        self._min_trust = float(self._config.get("min_trust_threshold", 0.3))
        # Skill catalog (docs/skill-retrieval.md §4): everything skill-related is gated on
        # skill_dirs — unset means zero behavior change (no sync, no block, no feedback).
        raw_dirs = self._config.get("skill_dirs", [])
        if isinstance(raw_dirs, str):
            raw_dirs = [raw_dirs]
        self._skill_dirs = [str(d).strip() for d in raw_dirs if str(d).strip()]
        if self._skill_dirs:
            # Same manual expansion as db_path in initialize(): the config loader only resolves
            # ${VAR}, so a bare $HERMES_HOME placeholder must be replaced here.
            from hermes_constants import get_hermes_home
            _home = str(get_hermes_home())
            self._skill_dirs = [d.replace("$HERMES_HOME", _home).replace("${HERMES_HOME}", _home) for d in self._skill_dirs]
        self._skills_enabled = bool(self._skill_dirs)
        self._skills_top = max(1, int(self._config.get("skills_top", 3)))
        self._skills_floor = max(1, int(self._config.get("skills_floor", 2)))
        # Per-session skill state — reset in initialize() (the manager re-initializes the
        # one instance per profile for every new session).
        self._injected_skill_contents: set = set()
        self._floor_injected = False
        self._skill_feedback_sent: set = set()

    @property
    def name(self) -> str:
        return "tiberius"

    def is_available(self) -> bool:
        return True  # SQLite is always available, numpy is optional

    def save_config(self, values, hermes_home):
        """Write config to config.yaml under plugins.tiberius."""
        # The canonical writer: config lock, managed-mode refusal, default stripping, atomic replace.
        # ``merge_existing`` keeps every other section; *hermes_home* is the active profile already.
        from hermes_cli.config import save_config
        values = dict(values)
        # This profile's own DB spelled out (older setups wrote it; the dashboard form re-submits what it
        # read) pins every clone and rename of the profile to this file, so it is stored as the placeholder.
        db_path = values.get("db_path")
        if isinstance(db_path, str) and Path(db_path).expanduser() == Path(hermes_home) / "tiberius_store.db":
            values["db_path"] = _DEFAULT_DB_PATH
        save_config({"plugins": {"tiberius": values}}, merge_existing=True)

    def get_config_schema(self):
        return [
            {"key": "db_path", "description": "SQLite database path", "default": _DEFAULT_DB_PATH},
            {"key": "auto_extract", "description": "Auto-extract facts at session end", "default": "false", "choices": ["true", "false"]},
            {"key": "default_trust", "description": "Default trust score for new facts", "default": "0.5"},
            {"key": "hrr_dim", "description": "HRR vector dimensions", "default": "1024"},
        ]

    def initialize(self, session_id: str, **kwargs) -> None:
        from hermes_constants import get_hermes_home
        _hermes_home = str(get_hermes_home())
        db_path = self._config.get("db_path", _hermes_home + "/tiberius_store.db")
        if isinstance(db_path, str):  # expand $HERMES_HOME so paths resolve to the active profile
            db_path = db_path.replace("$HERMES_HOME", _hermes_home).replace("${HERMES_HOME}", _hermes_home)
        hrr_dim = int(self._config.get("hrr_dim", 1024))
        self._store = MemoryStore(db_path=db_path, default_trust=float(self._config.get("default_trust", 0.5)), hrr_dim=hrr_dim)
        self._retriever = FactRetriever(store=self._store, hrr_dim=hrr_dim, hrr_weight=float(self._config.get("hrr_weight", 0.3)),
                                        temporal_decay_half_life=int(self._config.get("temporal_decay_half_life", 0)))
        self._session_id = session_id
        # New session: skill state starts fresh (floor re-offered, seen-set and feedback
        # de-dupe cleared) — the provider instance is shared across a profile's sessions.
        self._injected_skill_contents = set()
        self._floor_injected = False
        self._skill_feedback_sent = set()
        if self._skills_enabled:
            self._sync_skills()

    def _sync_skills(self) -> None:
        """Upsert skill facts from the configured dirs at session start — in-process, the same
        code path as the CLI's skill-sync (no subprocess). Best-effort: a bad dir or a locked
        DB must never break session start. prune stays off: the configured list may be one
        harness's view of a shared store, and deleting another harness's facts is worse than
        stale ones (the CLI's --prune is the manual cleanup path)."""
        import argparse

        from .cli import _skill_sync
        if self._store is None:
            return
        try:
            report = _skill_sync(argparse.Namespace(dir=self._skill_dirs, prune=False), self._store)
            logger.info("tiberius skill-sync: %d skills (%d added, %d updated, %d unchanged, %d skipped)",
                        report["skills"], len(report["added"]), len(report["updated"]),
                        report["unchanged"], len(report["skipped"]))
        except Exception as exc:
            logger.warning("tiberius skill-sync failed (non-fatal): %s", exc)

    def system_prompt_block(self) -> str:
        if not self._store:
            return ""
        try:
            with self._store._lock:  # shared-connection rule: no connection access without the store lock
                total = self._store._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        except Exception:
            total = 0
        body = ("Active. Empty fact store — proactively add facts the user would expect you to remember.\n"
                "Use fact_store(action='add') to store durable structured facts about people, projects, preferences, decisions.\n"
                if total == 0 else
                f"Active. {total} facts stored with entity resolution and trust scoring.\n"
                "Use fact_store to search, probe entities, reason across entities, or add facts.\n")
        return "# Tiberius Memory\n" + body + "Use fact_feedback to rate facts after using them (trains trust scores)."

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._retriever or not query:
            return ""
        parts: list[str] = []
        try:
            results = self._retriever.search(query, min_trust=self._min_trust, limit=5)
            lines = [f"- [{r.get('trust_score', r.get('trust', 0)):.1f}] {r.get('content', '')}" for r in results]
            if results:
                # Coverage counter: an unmarked 5-fact block reads as the whole story, which is
                # what makes the agent answer from context instead of recalling. Show the window.
                header = "## Tiberius Memory"
                total = self._retriever.match_count(query, min_trust=self._min_trust)
                if total > len(results):
                    header += f" ({len(results)} of {total} match — fact_store search has the rest)"
                parts.append(header + "\n" + "\n".join(lines))
        except Exception as e:
            logger.debug("Tiberius prefetch failed: %s", e)
        if self._skills_enabled:
            try:
                parts.append(self._skills_block(query))
            except Exception as e:
                logger.debug("Tiberius skills block failed: %s", e)
        return "\n\n".join(p for p in parts if p)

    _SKILL_FLOOR_HEADER = "Skills available this session (trust-ranked; load with skill_view):"
    _SKILL_TURN_HEADER = "Skills relevant to this turn (load with skill_view):"

    def _skill_fact_line(self, row: dict) -> str:
        """One block line: '- <id> — <description 120-chars>'. The id prefers the skill-id tag
        (the frontmatter name) and falls back to the content's '<name>: ' prefix — the same
        derivation as the OpenCode plugin, so both harnesses name a skill identically."""
        content = row.get("content", "")
        name = ""
        for tag in row.get("tags", "").split(","):
            if tag.strip().startswith("skill-id:"):
                name = tag.strip()[len("skill-id:"):]
                break
        if name and content.startswith(name + ": "):
            desc = content[len(name) + 2:]  # the prefix may itself contain ': ' — never re-partition
        else:
            name, _, desc = content.partition(": ")
        return f"- {name} — {_truncate_skill_desc(desc)}"

    def _skills_block(self, query: str) -> str:
        """The skill retrieval surface (docs/skill-retrieval.md §4): a one-shot trust floor
        (top-N catalog skills, session start) plus a per-turn search top-k, deduped so a
        skill is injected at most once per session. Mirrors the OpenCode plugin's blocks."""
        if not self._store or not self._retriever:
            return ""
        chunks: list[str] = []
        if not self._floor_injected:
            floor = self._store.list_facts(category="skill", limit=self._skills_floor)
            fresh = [r for r in floor if r["content"] not in self._injected_skill_contents]
            if fresh:
                chunks.append(self._SKILL_FLOOR_HEADER + "\n" + "\n".join(self._skill_fact_line(r) for r in fresh))
            for r in floor:
                self._injected_skill_contents.add(r["content"])
            self._floor_injected = True
        results = self._retriever.search(query, category="skill", min_trust=self._min_trust, limit=self._skills_top)
        fresh = [r for r in results if r["content"] not in self._injected_skill_contents]
        if fresh:
            chunks.append(self._SKILL_TURN_HEADER + "\n" + "\n".join(self._skill_fact_line(r) for r in fresh))
        for r in results:
            self._injected_skill_contents.add(r["content"])
        return "\n\n".join(chunks)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [FACT_STORE_SCHEMA, FACT_FEEDBACK_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name not in self._TOOL_HANDLERS:
            return tool_error(f"Unknown tool: {tool_name}")
        try:
            return self._TOOL_HANDLERS[tool_name](self, args)
        except KeyError as exc:
            return tool_error(f"Missing required argument: {exc}")
        except Exception as exc:
            return tool_error(str(exc))

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if messages and self._skills_enabled:
            self._skill_feedback(messages)
        # is_truthy_value: auto_extract is a string enum ("false"/"true"); plain truthiness would treat "false" as on.
        if is_truthy_value(self._config.get("auto_extract", False)) and self._store and messages:
            self._auto_extract_facts(messages)

    def _skill_feedback(self, messages: list) -> None:
        """Usage feedback (docs/skill-retrieval.md §4): every distinct skill the model loaded
        this session (a skill_view tool call in the transcript) earns one 'helpful' rating,
        the same loop the OpenCode plugin runs. Loads of skills outside the catalog are
        skipped — nothing to train. Best-effort: never raises into session teardown."""
        if not self._store:
            return
        try:
            loaded = _skill_view_names(messages)
            if not loaded:
                return
            by_id: dict[str, int] = {}
            for row in self._store.list_facts(category="skill", limit=1000):
                for tag in row["tags"].split(","):
                    if tag.strip().startswith("skill-id:"):
                        by_id[tag.strip()[len("skill-id:"):]] = row["fact_id"]
                        break
            rated = 0
            for name in sorted(set(loaded)):
                # ponytail: exact skill-id first, then the basename of a path-qualified load
                # ("cat/skill" -> "skill"); a name collision across namespaces would mis-attribute
                # one ±0.05 — acceptable for a personal catalog.
                fid = by_id.get(name) or by_id.get(name.rsplit("/", 1)[-1])
                if fid is not None and fid not in self._skill_feedback_sent:
                    self._store.record_feedback(fid, helpful=True)
                    self._skill_feedback_sent.add(fid)
                    rated += 1
            if rated:
                logger.info("tiberius: skill usage feedback — %d loaded skill(s) rated helpful", rated)
        except Exception as exc:
            logger.debug("tiberius skill feedback failed: %s", exc)

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        """Mirror built-in memory writes as facts."""
        if action == "add" and self._store and content:
            try:
                self._store.add_fact(content, category="user_pref" if target == "user" else "general")
            except Exception as e:
                logger.debug("Tiberius memory_write mirror failed: %s", e)

    def shutdown(self) -> None:
        # Close on the caller's thread: leaving the shared connection (+ write lock) to GC keeps it alive on a gateway.
        if self._store is not None:
            try:
                self._store.close()
            except Exception as e:
                logger.debug("Tiberius shutdown close() failed: %s", e)
        self._store = self._retriever = None

    # Tool handlers (self, args) -> str. KeyError from args[...] / Exception -> tool_error in handle_tool_call;
    # argument coercion order (and therefore which error surfaces first) mirrors the underlying call order.

    def _entity_query(self, method: str, a: dict) -> str:
        """'related': single-entity retriever queries."""
        return _results(getattr(self._retriever, method)(a["entity"], category=a.get("category"), limit=_limit(a)))

    def _probe(self, a: dict) -> str:
        """"probe": one entity (`entity`) or N-entity union (`entities`: facts linked to any of them,
        the OR counterpart of `reason`). `entities` takes precedence when present."""
        names = a.get("entities") or a["entity"]
        return _results(self._retriever.probe(names, category=a.get("category"), limit=_limit(a)))

    def _add(self, a: dict) -> str:
        """'add': store the fact and report near-duplicate candidates (report-only — the caller removes if it agrees)."""
        fid = self._store.add_fact(a["content"], category=a.get("category", "general"), tags=a.get("tags", ""))
        out: dict = {"fact_id": fid}
        dups = self._retriever.find_duplicates(a["content"], fid)
        if dups:  # supplemental field: omitted when empty, note when present (see ARCHITECTURE.md)
            out["possible_duplicates"] = dups
            out["note"] = (f"{len(dups)} near-identical existing fact(s) — if one is a true duplicate, "
                           "remove the older fact_id and keep this one.")
        return json.dumps(out)

    def _show(self, a: dict) -> str:
        """'show': read one fact by id — content, entities, trust, counts. The read
        counterpart of update/remove (read before writing). A by-id lookup is not a
        recall, so no retrieval recording."""
        fact_id = a.get("fact_id")
        if fact_id is None:
            return tool_error("show requires 'fact_id'")
        fact = self._store.get_fact(int(fact_id))
        if fact is None:
            return json.dumps({"error": f"fact_id {fact_id} not found"})
        return json.dumps({"fact": fact})

    def _entities(self, a: dict) -> str:
        """'entities': list entities with linked-fact counts and aliases — entity discovery
        for probe/related/reason/brief/drift (they take an entity name but nothing lists the
        names). No required args; an optional 'limit' caps the list (the contract default is
        20, not the schema-wide 10). The output contract (docs/entities.md) is a bare JSON
        array — fact_count 0 is legal (orphaned entity rows)."""
        return json.dumps(self._store.list_entities(top=int(a.get("limit", 20))))

    _TOOL_HANDLERS = {
        "fact_store": _tool_handler({
            "add": lambda self, a: self._add(a),
            "search": lambda self, a: _results(self._retriever.search(
                a["query"], category=a.get("category"), min_trust=float(a.get("min_trust", self._min_trust)),
                limit=_limit(a), since=a.get("since"),
                categories=a.get("categories"), exclude_category=a.get("exclude_category"))),
            "probe": lambda self, a: self._probe(a),
            "show": lambda self, a: self._show(a),
            "entities": lambda self, a: self._entities(a),
            "brief": lambda self, a: json.dumps(brief(
                a["query"], self._retriever, self._store,
                budget=max(1, int(a.get("budget", 300))),
                category=a.get("category"), min_trust=float(a.get("min_trust", self._min_trust)),
                since=a.get("since"), categories=a.get("categories"),
                exclude_category=a.get("exclude_category"))),
            "drift": lambda self, a: json.dumps(drift(
                a["project"], self._retriever, self._store,
                since=a.get("since"), budget=max(1, int(a.get("budget", 300))),
                support=float(a.get("support", 0.15)))),
            "related": lambda self, a: self._entity_query("related", a),
            "reason": lambda self, a: _results(self._retriever.reason(a["entities"], category=a.get("category"), limit=_limit(a)))
            if a.get("entities") else tool_error("reason requires 'entities' list"),
            "contradict": lambda self, a: _results(self._retriever.contradict(category=a.get("category"), limit=_limit(a))),
            "update": lambda self, a: json.dumps({"updated": self._store.update_fact(
                int(a["fact_id"]), content=a.get("content"), trust_delta=float(a["trust_delta"]) if "trust_delta" in a else None,
                tags=a.get("tags"), category=a.get("category"))}),
            "remove": lambda self, a: json.dumps({"removed": self._store.remove_fact(int(a["fact_id"]))}),
            "list": lambda self, a: _results(self._store.list_facts(
                category=a.get("category"), min_trust=float(a.get("min_trust", 0.0)), limit=_limit(a), since=a.get("since"),
                order=a.get("order", "trust")), key="facts"),
            "stats": lambda self, a: json.dumps(self._store.stats()),
            "add_batch": lambda self, a: self._add_batch(a),
            "checkpoint": lambda self, a: self._checkpoint(a),
            "supersede": lambda self, a: self._supersede(a),
        }),
        "fact_feedback": lambda self, a: self._feedback(a),
    }

    def _add_batch(self, a: dict) -> str:
        if not self._store:
            return tool_error("store unavailable")
        facts = a.get("facts")
        if not isinstance(facts, list) or not facts:
            return tool_error("add_batch requires a non-empty 'facts' list (strings or {content, category?, tags?} objects)")
        try:
            return json.dumps(self._store.add_facts(facts))
        except ValueError as e:
            return tool_error(str(e))

    def _checkpoint(self, a: dict) -> str:
        """Session handoff (docs/checkpoint.md): one active checkpoint per session.
        op 'set' (default) replaces, 'show' reads, 'clear' deletes."""
        if not self._store:
            return tool_error("store unavailable")
        session = a.get("session")
        if not isinstance(session, str) or not session.strip():
            return tool_error("checkpoint requires a non-empty 'session' string")
        session = session.strip()
        op = a.get("op", "set")
        if op not in ("set", "show", "clear"):
            return tool_error("checkpoint op must be one of: set, show, clear")
        try:
            if op == "show":
                rows = self._store.list_by_tag(f"checkpoint:{session}")
                return json.dumps({"session": session, "results": rows, "count": len(rows)})
            if op == "clear":
                return json.dumps({"session": session,
                                   "cleared": self._store.delete_by_tag(f"checkpoint:{session}")})
            facts = a.get("facts")
            if (not isinstance(facts, list) or not facts
                    or not all(isinstance(f, str) and f.strip() for f in facts)):
                return tool_error("checkpoint set requires a non-empty 'facts' list of non-empty strings (1-8 lines)")
            if len(facts) > 8:
                return tool_error("checkpoint set takes at most 8 facts — a handoff, not a backup")
            return json.dumps(self._store.set_checkpoint(session, facts))
        except ValueError as e:
            return tool_error(str(e))

    def _supersede(self, a: dict) -> str:
        """Replace a fact: insert a replacement and point the old fact at it
        (docs/supersede.md). The old row stays readable via 'show'."""
        if not self._store:
            return tool_error("store unavailable")
        content = a.get("content")
        if not isinstance(content, str) or not content.strip():
            return tool_error("supersede requires a non-empty 'content' string (the replacement statement)")
        if not isinstance(a.get("fact_id"), int) or a["fact_id"] < 1:
            return tool_error("supersede requires an integer 'fact_id'")
        try:
            return json.dumps(self._store.supersede(a["fact_id"], content.strip(),
                                                    category=a.get("category"), tags=a.get("tags")))
        except KeyError as e:
            return tool_error(f"error: {e}")
        except ValueError as e:
            return tool_error(str(e))

    def _feedback(self, a: dict) -> str:
        """Single or batch feedback; the single-id path is byte-identical to the former inline lambda."""
        if not self._store:
            return tool_error("store unavailable")
        fid, fids = a.get("fact_id"), a.get("fact_ids")
        if fids:
            if fid is not None:
                return tool_error("provide fact_id OR fact_ids, not both")
            if not isinstance(fids, list) or not fids:
                return tool_error("fact_ids must be a non-empty list of integers")
            results = self._store.record_feedbacks(fids, helpful=a["action"] == "helpful")
            return json.dumps({"applied": sum(1 for r in results if r.get("ok")), "results": results})
        if fid is None:
            return tool_error("provide fact_id or a non-empty fact_ids list")
        return json.dumps(self._store.record_feedback(int(fid), helpful=a["action"] == "helpful"))

    def _auto_extract_facts(self, messages: list) -> None:
        # Compaction handoff summaries arrive as role="user" and match the decision patterns; never store the
        # compactor's own output as a fact. A merge-into-tail row holds genuine prior user text BEFORE
        # _MERGED_SUMMARY_DELIMITER (after the header) and the summary AFTER it — harvest only that segment.
        from agent.context_compressor import _MERGED_PRIOR_CONTEXT_HEADER, _MERGED_SUMMARY_DELIMITER, is_compaction_summary_message  # heavy; lazy
        extracted = 0
        for msg in messages:
            content = msg.get("content", "") if msg.get("role") == "user" else None
            pre = content.split(_MERGED_SUMMARY_DELIMITER, 1)[0].removeprefix(_MERGED_PRIOR_CONTEXT_HEADER).strip() \
                if isinstance(content, str) and _MERGED_SUMMARY_DELIMITER in content else ""
            if pre:
                content = pre
            elif content is None or is_compaction_summary_message(msg):
                continue
            if not isinstance(content, str) or len(content) < 10:
                continue
            for statement, category in extract_statements(content):
                try:
                    self._store.add_fact(statement, category=category)
                    extracted += 1
                except Exception:
                    pass
        if extracted:
            logger.info("Auto-extracted %d facts from conversation", extracted)

def register(ctx) -> None:
    """Register the tiberius memory provider with the plugin system."""
    ctx.register_memory_provider(TiberiusMemoryProvider(config=_load_plugin_config()))
