# Skill retrieval via Tiberius — design & plan

Status: **Phase 1 complete** (2026-10-27): `skill-sync` CLI implemented as specified
in §2 + 11 tests — on local branch `skills/retrieval`, **not pushed** (WIP stays
local until the private dev repo exists). Phase 0 = GO (spike results in §6:
76% description-only → 100% with trigger tags). Phases 2–4 not started.

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

Render shape (per-turn block, next to the existing memory block):

```
Relevant skills (load with the skill tool, or memory_skill_load for unindexed ones):
- <id> — <description truncated to ~120 chars>
```

### New tools (plugin 9 → 11)

- `memory_skill_search <query>` — the escape hatch: `search <query> --category skill
  --top 5`. For skills that did not make the per-turn top-k. (The built-in `skill`
  tool has one built-in escape — "or a skill explicitly referenced by the user" —
  but the model cannot *discover* past the top-k without this.)
- `memory_skill_load <name>` — resolve the fact's `skill-dir` tag → return the
  `SKILL.md` body (size-capped). Makes unindexed dirs actually usable.

### Feedback loop (session end)

The session-end extraction pass already receives each turn's messages. Extend the
parser: detect `skill` and `memory_skill_load` tool calls with their id argument →
`feedback <fact_id> helpful` for each loaded skill (fact_id resolved via the
`skill-id` tag). v1 signal = "was loaded; per-turn tool-event detection is a v2 refinement
(§5, question 3).

### Implementation notes (from the current `opencode/index.ts`)

- Per-turn injection point: the `chat.message` handler — after the facts block,
  inside the same try/catch discipline ("memory must never break model dispatch").
- Session start: next to the `injectTop` handler.
- Subprocess cost: one extra engine spawn per turn (~200 ms, local) — acceptable;
  a combined search call is a possible later optimization.
- Tests: extend `opencode/tests/index.test.ts` (12 tests today): options unset →
  zero behavior change; per-turn dedupe; sync idempotency; `memory_skill_load` path.

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

### Questions for the Hermes agent

- **Q1.** What exactly does the Hermes core inject about skills? Names only, or
  names + descriptions? Which block — system prompt or per-turn? (Determines the
  token math and whether the "savings" story applies to Hermes at all.)
- **Q2.** Is that static index configurable or suppressible? If the provider can
  shape it, Hermes gets the token-savings version; otherwise Hermes is in
  coexistence mode like OpenCode.
- **Q3.** Where does Hermes discover skills (directory conventions; per-profile
  vs global)? → the `skill_dirs` values to document.
- **Q4.** When the model loads a skill, what do tool calls look like in the
  transcript that reaches `on_session_end(messages)`? (Tool name + argument shape —
  needed for load detection in the feedback loop.)
- **Q5.** Does the Hermes skill loader accept arbitrary IDs, or only skills the
  harness discovered?
- **Q6.** Are there length limits on what `prefetch` / `system_prompt_block` may
  return?

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
| 2 | OpenCode skill discovery paths (plugin-shipped vs project vs global) — how much does the BYO channel add? | OpenCode agent | Phase 2 scoping |
| 3 | Does the V2 event stream carry tool-completion events with name + args? | OpenCode agent | v2 feedback quality |
| 4 | Does the built-in `skill` tool accept ids from unindexed skills? (Tool description says "an available skill **or a skill explicitly referenced by the user**" — needs a live test.) If not → `memory_skill_load` is the only path for unindexed dirs. | OpenCode agent | Phase 2 (tool surface) |

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
| 1 | `skill-sync` CLI subcommand + tests (synthetic fixture skill dirs in tmp_path — fictional-content rule, same as the eval fixture) — **done 2026-10-27** | OpenCode (shared artifact — write it first; both harnesses depend on it) | Core suite green (58 passed); idempotency test present; golden eval unchanged (19/19 @1, @3) |
| 2 | OpenCode plugin: options, both blocks, two tools, feedback detection, tests | OpenCode | Plugin suite green; zero behavior change with options unset |
| 3 | Hermes provider: prefetch block, config, feedback detection | Hermes (after Q1–Q6 answered) | Provider tests in a Hermes env |
| 4 | Later: ranking refinements (body head in the retrieval surface, trigger-phrase frontmatter), per-turn feedback events, one shared skill registry across both harnesses | whichever | — |

**every** commit that touches the core runs the core suite + golden eval
(AGENTS.md #1). Phase 1 touches `cli.py` (not the core trio), but the gate
discipline still applies to anything retrieval-adjacent.

## 8. Shared branch protocol

Branch: `skills/retrieval` (off `main`).

- 2026-10-27: the branch was deleted from the public repo at the user's request
  and is **local only** until a private dev repo (e.g. `tiberius-dev`) exists.
  Until then the protocol below reduces to: work on the local branch, commit
  inside your area, never push.
- Areas: `cli.py` + `eval/skill_spike/` + this doc = **shared** (OpenCode writes
  first); `opencode/` = OpenCode agent; `provider.py` + Hermes test files =
  Hermes agent.
- Each agent: `git pull --rebase origin skills/retrieval` before every push;
  commit only inside your area; small commits.
- The feature lands via PR to `main` as usual (protect-main ruleset applies).
