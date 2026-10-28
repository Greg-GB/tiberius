# Skill retrieval via Tiberius — design & plan

Status: **Phase 3 complete** (2026-10-28): Hermes provider wiring implemented
as designed in §4 (in-process skill-sync at `initialize`, prefetch skills block
with trust floor + per-turn top-k, session-end usage feedback; Q1–Q6 answered
in §4). Gates: core suite 101 tests, golden eval 19/19 @1 and @3, OpenCode
plugin 29 tests green, typecheck clean. Phase 2 complete (2026-10-28): OpenCode
plugin wiring implemented as designed in §3 (commit `d942209`), 29 plugin tests
green, typecheck clean. Phase 1 (`skill-sync` + 11 tests, commit `c0f9ba5`)
landed 2026-10-27. Everything is on branch `skills/retrieval`, pushed to the
public repo 2026-10-28 at the user's decision (`main` untouched; the feature
lands via PR — see §8). Phase 0 = GO (spike results in §6: 76% description-only
→ 100% with trigger tags). Phase 4 remains open (§5, note under the table).

Branch: `skills/retrieval` — shared by the OpenCode agent and the Hermes agent.
See [Shared branch protocol](#8-shared-branch-protocol).

## 1. Background

Tiberius stores facts about the user/project and injects the relevant ones into
context per turn (Hermes `provider.py` `prefetch`; OpenCode plugin `injectPerTurn`).
Agent harnesses also ship **skills** — instruction documents (`SKILL.md`: frontmatter
`name` + `description`, markdown body) that the model loads on demand. Harnesses
expose skills today with a **static index** in the system prompt (all names +
descriptions, unranked) plus a `skill` tool that loads the body by ID.

The static index has two failure modes that scale with catalog size:

1. **Token cost** — every skill's description is paid for on every turn, relevant or not.
2. **Unranked attention** — the model must scan the whole list; selection quality
   degrades as the list grows.

**The idea:** treat skills as a second Tiberius corpus. Store each skill's
`name: description` as a fact in category `skill`, retrieve the top-k per turn
alongside memory facts, and inject that ranked list. Bodies stay out of the store —
the harness's existing skill loader fetches them by ID.

Bonus over a static index or a flat embedding index:

- **Learned ranking** — Tiberius trust scoring: a skill that actually gets used
  gets `feedback helpful` and rises.
- **Compositional discovery** — the entity graph: `reason git deploy` finds skills
  linked to *both* entities.

Prior art: Voyager's skill library (retrieval over a growing skill corpus). What is
new here: trust-scored usage feedback, compositional entity retrieval, and a
bring-your-own-directory channel for skills the harness does not index.

## 2. Core design (harness-agnostic)

**Zero changes to `store.py` / `retrieval.py` / `holographic.py`.** The category
filter is already wired through `search` / `list` / `reason` (verified against
`cli.py` at the commit this doc was written). Everything below is CLI + adapter
surface, per the harness-free invariant (AGENTS.md #2).

### Storage shape (one fact per skill)

| Field    | Value                                        | Why                                                                                   |
|----------|----------------------------------------------|---------------------------------------------------------------------------------------|
| content  | `<name>: <description>`                      | Retrieval surface. Name in content puts the skill name on the FTS text and in the entity graph — this is where the compositional wins come from. |
| category | `skill`                                      | All existing filters work unchanged.                                                  |
| tags     | `skill-id:<id>, skill-dir:<path>`            | `id` = what the harness `skill` tool needs; `dir` = where the body lives (for the plugin's own loader). |
| trust    | 0.5 initial                                  | Trained by usage feedback.                                                            |
| entities | auto-extracted from content                  | Skills join the graph for `related` / `reason`.                                       |

### Sync: new CLI subcommand `skill-sync` (Phase 1)

- `skill-sync --dir DIR [--dir ...] [--prune]`
- Reads `<skill-name>/SKILL.md` frontmatter (the convention this repo already ships:
  `skills/tiberius-usage/SKILL.md`).
- Optional frontmatter key `triggers:` (comma-separated) — extra trigger
  vocabulary folded into the fact's tags. Rationale: the Phase 0 spike showed
  description-only surfaces miss 24% of natural queries on vocabulary mismatch
  (§6); tags are already FTS-indexed, so this costs nothing in the core.
- Upsert by `skill-id` tag: missing → `add`; description changed → `update --content`;
  unchanged → no-op.
- `--prune` (explicit, **off** by default): remove skill facts whose id no longer
  appears in any synced dir. Never delete on a partial sync by accident.
- Idempotent; cheap enough to re-run at every session start (no caching in v1).
- One command feeds both harnesses from different dir lists.
- **Implemented (Phase 1, 2026-10-27):** frontmatter is parsed with a stdlib-only
  subset parser (single-line `key: value` with optional quotes, `>`/`|` block
  scalars — no YAML dependency); skill name falls back to the directory name when
  the frontmatter has no `name:`; a skill without a description is reported in
  `skipped` and the sync continues (best-effort, same discipline as `extract`).
  Output is the JSON report `{dirs, skills, added, updated, unchanged, pruned,
  skipped}`. `--prune` only ever touches facts carrying a `skill-id` tag (hand-
  added skill facts are never deleted); a missing `--dir` is a JSON error (exit 1)
  so a typo cannot prune the whole catalog; the same id in multiple `--dir`s
  resolves last-dir-wins. Verified end-to-end against the repo's own
  `skills/tiberius-usage/SKILL.md`, including the `triggers:` path from §6
  (a Phase 0 miss query hits once the trigger words are in the frontmatter).

## 3. OpenCode side

Owner: **OpenCode agent**. Code: `opencode/index.ts`, `opencode/tests/`.

**The honest trade-off:** OpenCode owns its own `<available_skills>` block — the
plugin cannot remove it. So v1 on OpenCode *adds* a ranked block on top (small
per-turn cost) in exchange for three things OpenCode does not have today:

1. **Relevance ranking + trust floor** — the static index is unranked; the per-turn
   block puts the skills that matter *this turn* first, and the session-start block
   keeps proven workhorses (top-N by trust) always visible.
2. **Bring-your-own-skill-directory** — the real new capability: skills in a dir
   that OpenCode's discovery does not index cannot be loaded by the built-in
   `skill` tool. Tiberius + the plugin loader becomes a second skill registry that
   works for arbitrary directories (team skill repos, per-project skills).
3. **Usage feedback loop** — a learned ranking of what actually gets used.

### New plugin options (all off by default — zero behavior change until configured)

| Option           | Default           | Effect                                                                                                                        |
|------------------|-------------------|-------------------------------------------------------------------------------------------------------------------------------|
| `skillDirs?: string[]` | unset          | If set: `skill-sync` at session start (best-effort; must never break model dispatch — same discipline as existing injection). |
| `skillsTop?: number`   | 3 (if `skillDirs` set) | Per-turn: second `search <query> --category skill --top N` beside the existing facts search; same `lastSearched` dedupe; inject only skills not yet shown this session; description rendered truncated to ~120 chars. |
| `skillsFloor?: number` | 2 (if `skillDirs` set) | Session-start block: top-N skill facts by trust via `list --category skill --top N` (flag already exists). |

Render shape (implemented headers; descriptions truncated to 120 chars at a
word boundary):

```
Skills available this session (load with the skill tool if indexed, or memory_skill_load otherwise):
- <id> — <description>

Skills relevant to this turn (load with the skill tool if indexed, or memory_skill_load otherwise):
- <id> — <description>
```

### New tools (plugin 9 → 11)

- `memory_skill_search <query>` — the escape hatch: `search <query> --category skill
  --top 5`. For skills that did not make the per-turn top-k. (The built-in `skill`
  tool has one built-in escape — "or a skill explicitly referenced by the user" —
  but the model cannot *discover* past the top-k without this.)
- `memory_skill_load <name>` — resolve the fact's `skill-dir` tag → return the
  `SKILL.md` body (size-capped). Makes unindexed dirs actually usable.

### Feedback loop (session end) — implemented 2026-10-28

The session-end handler (fires once per completed turn with the full
transcript from `ctx.session.context()`) now detects skill loads in the
transcript — verified shapes against `@opencode/client` types
(`SessionMessageSkill`, `SessionMessageAssistantTool`):

- **Built-in `skill` tool:** a successful load is recorded as its own
  transcript message `{ type: "skill", skill, name, text }` — success-only,
  so it is exactly the "was loaded" signal (a failed load leaves no message).
- **`memory_skill_load` (BYO catalog):** an assistant content part
  `{ type: "tool", name: "memory_skill_load", state: { status: "completed", input: { name } } }`;
  only `completed` parts count — a failed read was not useful.

Each distinct loaded skill gets `feedback <fact_id> helpful` **once per
session** (fact_id resolved from the catalog's `list --category skill`);
dedupe is against the already-sent set, not the per-turn delta, because the
handler sees the whole transcript every turn. Out-of-catalog loads
(no catalog fact for that id) are skipped — nothing to train. Like the rest
of the skill feature, the loop is **gated on `skillDirs`** — with options
unset, a built-in skill load in the transcript must trigger no
skill-catalog engine calls and no trust changes (zero behavior change is
the contract; caught on branch review 2026-10-28, regression-tested).

### Implementation notes (as built, 2026-10-28)

- Both blocks live in the `ctx.session.hook("context", …)` handler — skill-sync +
  floor on the session's first dispatch (guarded by a per-session `syncedSessions`
  set), per-turn skill search beside the facts search (its own `lastSkillSearch`
  dedupe, mirroring `lastSearched`). Floor and per-turn blocks share one
  per-session seen set of skill fact_ids, so a skill is injected at most once.
- Subprocess cost: +1 engine spawn at session start (sync) + 1 per turn (skill
  search), ~200 ms local each — acceptable; a combined search call is a later
  optimization.
- Outage discipline unchanged: every engine call is caught and swallowed;
  a failed skill-sync skips the floor for that session rather than re-delaying
  every model call.
- `EngineResult` row type gained `tags?: string` (the CLI row contract already
  carried it — the plugin just hadn't typed it).
- Tests: `opencode/tests/index.test.ts` grew 12 → 27. New: pure-helper tests
  (skillSyncArgs / skillIdOf / skillDirOf / skillDescription / truncateText /
  renderSkillBlock / skillLoadNames) and full `setup()` wiring tests driven by a
  mock ctx + a fake Node engine (`tests/fake-engine.mjs` — same JSON contract as
  `python3 -m tiberius.cli`, deterministic skill/fact fixtures, JSON-line log of
  every invocation). Covers: options-unset zero-change (asserts the exact
  engine-call sequence and tool count), session-start sync-once + floor +
  same-turn dedupe, per-turn only-new injection, `injectPerTurn: false`,
  both tools (incl. not-found / missing-dir-tag errors), session-end feedback
  once-per-skill with failed-load exclusion and no re-send, and the
  skill-sync-failure-is-swallowed path. No Python, no network.

## 4. Hermes side — for the Hermes agent

Owner: **Hermes agent**. Code: `provider.py` + Hermes provider tests.
The OpenCode agent has **no checkout of hermes-agent in this container** (the core
suite's `tests/conftest.py` expects `HERMES_AGENT_REPO`, default
`~/.hermes/hermes-agent` — it is not present here), so everything marked Q* below
needs your answers before the Hermes adapter is written.

### Design sketch (mirrors the OpenCode side)

- `skill-sync --dir <hermes skill dirs>` at `initialize` (best-effort).
- `prefetch`: skills block next to `## Tiberius Memory` — retrieved top-k
  (`search <query> --category skill --top N`) plus the trust floor.
- Config: `plugins.tiberius` in `config.yaml`: `skill_dirs`, `skills_top`,
  `skills_floor`; `save_config` must round-trip them (see
  `tests/test_tiberius_save_config.py`).
- Feedback: `on_session_end(messages)` — detect skill loads in the transcript,
  send `feedback helpful` (same pattern as the OpenCode session-end pass).

### Q1–Q6 (answered 2026-10-28, hermes-agent at `/opt/hermes`)

- **Q1.** Names **and** descriptions: `prompt_builder._render_skills_index`
  renders each skill's name + description, grouped by category, in the system
  prompt (static per session). The skills block is additive, not a replacement —
  coexistence mode (see concerns below).
- **Q2.** Not suppressible from a plugin: the core renders its own index from
  the discovered catalog. The provider shapes **retrieval** (ranking, trust
  floor, search channel) but not the static index — same position as OpenCode.
- **Q3.** `$HERMES_HOME/skills/` (per-profile; this profile has 120 `SKILL.md`
  files, all category-nested, e.g. `creative/ascii-art/SKILL.md`, plus
  `skills/.archive/` — hidden dirs are excluded by the walk). `skill_dirs`
  therefore accepts the profile skills root and the walker recurses.
- **Q4.** OpenAI-shaped assistant `tool_calls`: `function.name == "skill_view"`
  with `function.arguments` a JSON string (`{"name": "..."}`); the detector
  (`_skill_view_names`) also tolerates dict-shaped args and object-shaped rows,
  mirroring the core's own `skill_view_names_by_call_id` pattern.
- **Q5.** Catalog-only: `skill_view` resolves against the harness catalog,
  same as the OpenCode built-in skill tool (which rejects unindexed ids).
- **Q6.** No hard limit; the provider controls block size (description
  truncation at 120 chars, `skills_top`/`skills_floor` caps). External timeout
  protects against hangs — no cap added.

### Implementation record (Phase 3, 2026-10-28)

- `skill-sync` at `initialize()`, **in-process** (same `_skill_sync` code path
  as the CLI — no subprocess; the provider already imports the core directly).
  Best-effort: any failure is logged and never breaks session start. `prune`
  stays off: the configured dirs may be one harness's view of a shared store —
  deleting another harness's facts is worse than stale ones (the CLI's `--prune`
  is the manual cleanup path).
- **Recursive discovery (Phase 1 follow-up, this commit):** the original
  1-level scan found 0 of this profile's 120 nested skills. `_skill_sync` now
  walks arbitrarily; a partially-bad `--dir` list syncs the good dirs and
  reports the bad ones in `skipped` (an entirely missing list is still a hard
  error — the CLI JSON-error contract).
- `prefetch`: skills block next to `## Tiberius Memory` — one-shot trust floor
  (`list_facts(category="skill")` top `skills_floor`, first turn) + per-turn
  `search(query, category="skill")` top `skills_top`, deduped by fact content
  so a skill is injected at most once per session. Same line rendering as
  OpenCode (`- <id> — <desc 120-chars>`).
- Config under `plugins.tiberius`: `skill_dirs` (list or string), `skills_top`
  (default 3), `skills_floor` (default 2). **Everything is gated on
  `skill_dirs` — unset means zero behavior change** (no sync, no block, no
  feedback; asserted by test). `$HERMES_HOME` placeholders in `skill_dirs` are
  expanded in the provider (same manual expansion as `db_path` — the config
  loader only resolves `${VAR}`, not bare `$VAR`). `save_config` round-trips
  the new keys (test).
- Feedback in `on_session_end(messages)`: each distinct skill loaded via
  `skill_view` this session earns one `feedback helpful` (identical semantics
  to the OpenCode session-end pass — no unhelpful for not-loaded skills).
  Load names match the `skill-id` tag exactly, falling back to the basename of
  a path-qualified load. Once per session per fact (per-session dedupe set,
  reset in `initialize`).
- Per-session state (`seen` set, floor flag, feedback dedupe) resets in
  `initialize()` — the manager re-initializes the one provider instance per
  profile for every new session.
- Tests: `tests/test_tiberius_hermes_skills.py` (14 tests: config normalization
  + `$HERMES_HOME` expansion, initialize sync incl. nested + partially-bad
  dirs, floor/turn/dedupe/re-offer, line rendering, feedback incl. re-session
  and uncatalog, detector shapes, zero-behavior-change) + 2 recursive-scan
  tests in `tests/test_tiberius_skill_sync.py`.
- **Live gate (still open):** restart the Hermes gateway with
  `skill_dirs: [$HERMES_HOME/skills]` configured, then check the log for the
  `tiberius skill-sync: N skills` line at session start and the skills block
  in a turn's prefetch. Verified against real data pre-restart: 114 of 120
  `SKILL.md` sync (5 in hidden `.archive/`, 1 skipped — no frontmatter
  description).

### Concerns / notes

- If Q2 = "no", treat Hermes exactly like OpenCode (coexistence): the value prop
  is ranking + trust + the search channel, not token savings.
- The trust floor uses `list --category skill --top N` (verified working today).
- **No skill logic in the core** — adapter + CLI only (AGENTS.md invariant #2).
- Phase 1 (`skill-sync`) is a shared artifact: the OpenCode agent writes it first;
  the Hermes side only consumes it. Do not fork a second sync path.

## 5. Open questions (tracked)

| # | Question | Owner | Blocks |
|---|----------|-------|--------|
| 1 | Hermes Q1–Q6 (§4) | Hermes agent | Phase 3 design only |
| 2 | OpenCode skill discovery paths (plugin-shipped vs project vs global) — how much does the BYO channel add over the native index? | OpenCode agent | Phase 4 scoping only (Phase 2 shipped the BYO channel) |
| 3 | **Resolved 2026-10-28.** No event stream needed: `ctx.session.context()` returns the full transcript, where tool calls are assistant content parts `{ type: "tool", name, state: { status, input } }` and built-in skill loads are dedicated `{ type: "skill", skill, name, text }` messages (success-only). Feedback detection was built on that (§3). | — | — |
| 4 | **Resolved 2026-10-27 (live test).** The built-in `skill` tool **rejects unindexed ids** (observed: `Unable to load skill no-such-skill-xyz`), despite the "explicitly referenced by the user" wording. `memory_skill_load` is therefore the only body path for unindexed/BYO dirs — the block wording ("load with the skill tool if indexed, or memory_skill_load otherwise") holds. | — | — |

**Phase 4 lead (2026-10-28):** `@opencode/plugin` 2.1.4 exposes
`ctx.skill` — a `SkillEditor` transform (`list` / `get` / `add` / `update` /
`remove` + `reload()`) over `Skill.Info` (`{ id, name, description,
autoinvoke, path, content }`), with a `DirectorySource` (`{ type: "directory",
path }`) in `@opencode/schema/skill`. A plugin may be able to *shape the
native skill index itself* (e.g. add BYO-dir skills to it), which would make
the built-in `skill` tool work for unindexed dirs and shrink the
`memory_skill_load` surface. Not verified live; v1 deliberately stays additive
(approved design). Investigating this is the natural Phase 4 entry point.

## 6. Phase 0: recall spike

**Hypothesis:** the existing FTS5 + entity-expansion pipeline retrieves skill
descriptions from natural user messages well enough, with zero core changes.

**Method** (`eval/skill_spike/run_skill_spike.py` — committed, reproducible):

- Catalog: `eval/skill_spike/catalog.json` — 17 skills: 3 real (tiberius-usage,
  report, opencode — OpenCode's own public skills, **not** user data) + 14
  synthetic realistic skills spanning git/deploy/sql/docker/pytest/csv/pdf/
  review/api/logs/refactoring/research/kubernetes/typescript.
- Each skill is `add`ed as `<id>: <description>` with `--category skill
  --tags skill-id:<id>` into a fresh throwaway DB.
- 17 queries shaped like real user messages, one target skill each;
  `search <query> --category skill --top 5`.
- Metrics: recall@5 (target in top-5), recall@1 (target is the top hit).
- Compositional probes: for each skill, take two of its extracted entities and run
  `reason <e1> <e2>` — does the target skill come back?
- **Go threshold:** recall@5 ≥ 80%. Below that → study the miss patterns
  (vocab mismatch? entity expansion? min_trust?) before any adapter code.
- Caveat: the spike queries are 1:1-constructed (one clear target each). A harder
  follow-up (multi-target and no-target queries) belongs in Phase 1+.

**Results** (2026-10-27, OpenCode agent, commit of this doc):

Run 1 — description-only surface (`--no-triggers`):

```
recall@5 = 13/17 (76%)   recall@1 = 12/17 (71%)   ->  NO-GO
```

Misses (all one failure mode — **vocabulary mismatch at FTS candidate
generation**: the query shares zero tokens with the description, so the
candidate stage never sees the skill and HRR rerank never gets a chance):

| Target | Query (user's words) | Description's words |
|---|---|---|
| tiberius-usage | "what do you **remember** about how I like my deploys done" | search/probe/reason/… "Recall before answering" |
| api-integration | "the payment **endpoint** returns **429s** when we poll it" | "HTTP APIs … rate limiting … error responses" |
| log-analysis | "… piece together what happened from the **logs**" | "application **log** files … timestamps" |
| refactoring | "… I would like to **split** it into smaller **pieces**" | "refactoring … **restructure**ing" (the user never says the category name) |

Run 2 — same catalog, plus trigger-vocabulary tags on the four missed skills only
(tags are already FTS-indexed: `facts_fts` covers the `tags` column — **zero core
changes**):

```
recall@5 = 17/17 (100%)   recall@1 = 17/17 (100%)   ->  GO
```

Precision check: no query was hijacked by a trigger tag — all 17 targets still
rank #1, so the conservative trigger sets cost nothing.

Compositional probes (`reason <e1> <e2>`): 6/6 attempted hit top-5, but most
skills were skipped because entity extraction over descriptions is mediocre —
sentence-initial junk ("Covers", "Collect", "Recall") dominates and many
descriptions yield only one entity. Usable for `reason`-style discovery today;
entity-quality on skill surfaces is a Phase 4 refinement (extending the core
stopword list is a core change and needs the eval gate — deliberately not v1).

**Conclusions:**

1. The premise holds: the existing pipeline retrieves skills from natural
   messages. 100% on a 1:1-constructed benchmark is "mechanism validated", not
   an operational expectation — the harder multi-target/no-target follow-up is
   Phase 1+.
2. **Surface quality is the lever.** v1 therefore ships two authoring aids:
   - guideline: descriptions must contain the words a user would actually type
     (symptoms, not just category names);
   - optional SKILL.md frontmatter key `triggers:` (comma-separated), which
     `skill-sync` folds into the fact's tags — already FTS-indexed, zero core
     changes.
3. A true semantic fallback (embedding similarity when FTS finds nothing)
   would be a core change — explicitly out of v1 scope.

## 7. Phases, gates, ownership

| Phase | Work | Owner | Gate |
|-------|------|-------|------|
| 0 | Spike (§6) | OpenCode | recall@5 ≥ 80% |
| 1 | `skill-sync` CLI subcommand + tests (synthetic fixture skill dirs in tmp_path — fictional-content rule, same as the eval fixture) — **done 2026-10-27** (`c0f9ba5`) | OpenCode (shared artifact — write it first; both harnesses depend on it) | Core suite green (58 passed); idempotency test present; golden eval unchanged (19/19 @1, @3) |
| 2 | OpenCode plugin: options, both blocks, two tools, feedback detection, tests — **done 2026-10-28** (`d942209`) | OpenCode | Plugin suite green (27 passed, typecheck clean); zero behavior change with options unset (asserted by test — exact engine-call sequence + tool count); core gates unchanged (58 tests, 19/19 @1 and @3) |
| 3 | Hermes provider: prefetch block, config, feedback detection — **done 2026-10-28** (recursive-discovery fix to Phase 1 included; live-gateway log check still open, see §4) | Hermes (after Q1–Q6 answered) | Provider tests in a Hermes env (14 tests); core suite 101 passed; golden eval 19/19 @1, @3 |
| 4 | Later: ranking refinements (body head in the retrieval surface, trigger-phrase frontmatter), per-turn feedback events, one shared skill registry across both harnesses, native index shaping via `ctx.skill` (lead in §5) | whichever | — |

**every** commit that touches the core runs the core suite + golden eval
(AGENTS.md #1). Phase 1 touches `cli.py` (not the core trio), but the gate
discipline still applies to anything retrieval-adjacent.

**Release note (when this feature lands):** feature PRs do not bump the
version (CONTRIBUTING.md — bump in all places at release time). At release
time, also update `skills/tiberius-usage/SKILL.md`: this branch
deliberately leaves that cross-harness usage skill untouched, because the
skill-catalog tools are conditional (`skillDirs`) on OpenCode and the Hermes
provider exposes skill retrieval through its existing prefetch/feedback path
(no new tools).

## 8. Shared branch protocol

Branch: `skills/retrieval` (off `main`).

- 2026-10-28: the user confirmed the branch is shared via the **public repo** —
  pushed to `github.com/Greg-GB/tiberius` as `skills/retrieval`. `main` is
  untouched; the feature lands via PR (protect-main ruleset). History: the
  branch was deleted from the public repo 2026-10-27 at the user's request while
  awaiting a private dev repo (e.g. `tiberius-dev`); that repo was never
  created, and the local-only period ended with the 2026-10-28 push.
- Areas: `cli.py` + `eval/skill_spike/` + this doc = **shared** (OpenCode writes
  first); `opencode/` = OpenCode agent; `provider.py` + Hermes test files =
  Hermes agent.
- Each agent: `git pull --rebase origin skills/retrieval` before every push;
  commit only inside your area; small commits.
- The feature lands via PR to `main` as usual (protect-main ruleset applies).
