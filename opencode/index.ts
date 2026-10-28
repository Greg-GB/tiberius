/**
 * tiberius — local memory for OpenCode (native V2 plugin).
 *
 * Wraps the tiberius Python engine (this repo) in a local subprocess — one
 * implementation of the memory heuristics, not a port. No network: the engine
 * runs in this container against a local SQLite file.
 *
 * Runtime-verified against live OpenCode 2.0.28 (2026-10-26): the recall and
 * store tools, context injection, and session-end extraction observed working
 * end-to-end (the update/remove/list tools were live-smoked 2026-10-27).
 *
 * opencode.json:
 *   { "plugins": [{ "package": "file:///opt/tiberius/opencode",
 *                   "options": { "db": "/opt/tiberius/memory.db" } }] }
 *
 * Options:
 *   db        SQLite path (default ~/.tiberius/memory.db inside the container)
 *   engine    argv for the engine (default ["python3", "-m", "tiberius.cli"])
 *   pythonpath  PYTHONPATH for the engine (default: none — the engine inherits
 *               the container's PYTHONPATH; set it to the repo parent so
 *               `python3 -m tiberius.cli` resolves)
 *   injectTop   facts injected into the system prompt at session start (default 4)
 *   injectPerTurn  per-turn injection (default true): each new user message is
 *               searched and newly relevant facts are injected, deduped per
 *               session — query-conditioned recall, matching the Hermes
 *               provider's per-turn prefetch. Set false for one-shot
 *               session-start injection.
 *   skillDirs   directories of <skill-name>/SKILL.md subdirs (skill catalog).
 *               If set: skill-sync at session start, a trust-floor skill block
 *               at session start, a per-turn ranked skill search, and two new
 *               tools (memory_skill_search / memory_skill_load). Unset: zero
 *               behavior change (no skill calls, no skill block, no tools).
 *   skillsTop   per-turn skill search top-k (default 3, when skillDirs set)
 *   skillsFloor  session-start skill block top-N by trust (default 2, when
 *               skillDirs set)
 *
 * What it does:
 *   - tools: memory_search / memory_probe / memory_related / memory_reason / memory_add / memory_feedback
 *     / memory_update / memory_remove / memory_list (same operation surface as
 *     the Hermes provider — see skills/tiberius-usage/SKILL.md), plus, with
 *     skillDirs: memory_skill_search (escape hatch past the per-turn top-k)
 *     and memory_skill_load (SKILL.md body for skills the built-in skill tool
 *     does not index — verified 2026-10-27: it rejects unindexed ids)
 *   - context hook: by default, every new user message is searched and only
 *     not-yet-shown facts are injected into the system prompt (query-conditioned
 *     recall, matching the Hermes provider); injectPerTurn: false reverts to a
 *     one-shot push on the session's first model call — recall that does not
 *     depend on the agent remembering to call the tools
 *   - skill catalog (skillDirs, docs/skill-retrieval.md §3): skills are stored
 *     as category-'skill' facts by skill-sync; the session-start block keeps
 *     proven workhorses (top-N by trust) visible, the per-turn block puts the
 *     skills relevant to THIS turn first, and usage feeds back into trust
 *   - event: on session.execution.succeeded, regex-extracts statements from user messages
 *     and, for skills loaded during the session (dedicated transcript skill
 *     messages for built-in loads; completed memory_skill_load tool parts),
 *     records `feedback helpful` once per skill — the learned-ranking signal
 *
 * Memory must never break model dispatch — every engine call is caught and
 * swallowed. (Per-turn injection is the default, so memory added mid-session
 * is picked up on the next turn; injectPerTurn: false reverts to the one-shot
 * session-start behavior.)
 */
import { execFile, type ChildProcess } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { Plugin } from "@opencode/plugin";

type Options = {
  db?: string;
  engine?: string[];
  pythonpath?: string;
  injectTop?: number;
  injectPerTurn?: boolean;
  skillDirs?: string[];
  skillsTop?: number;
  skillsFloor?: number;
};

type EngineResult = { results?: { fact_id: number; content: string; category?: string; trust_score?: number; tags?: string }[]; [k: string]: unknown; error?: string };

// Engine calls are local and fast (~100-300ms for the Python start). The
// timeout exists so a hung subprocess (locked DB, wedged filesystem) can
// never hang the model: the context hook AWAITS one of these calls before
// every session's first dispatch, and tool calls await one per invocation.
const ENGINE_TIMEOUT_MS = 30_000;

// Engine children in flight, so plugin teardown (hot reload / server shutdown)
// can kill them instead of leaking orphaned python processes mid-write.
const activeChildren = new Set<ChildProcess>();

export function runEngine(engine: string[], baseArgs: string[], args: string[], input?: string, envOverride?: Record<string, string>, timeoutMs: number = ENGINE_TIMEOUT_MS): Promise<EngineResult | null> {
  return new Promise((resolveP, rejectP) => {
    const child = execFile(engine[0], [...engine.slice(1), ...baseArgs, ...args], {
      env: envOverride ? { ...process.env, ...envOverride } : process.env,
      maxBuffer: 10 * 1024 * 1024,
      timeout: timeoutMs,
    }, (err, stdout) => {
      activeChildren.delete(child);
      if (err) {
        // The CLI's error contract is JSON on stdout with exit 1 (e.g.
        // feedback on an unknown fact_id → {"error": "fact_id 999 not found"}).
        // When the engine reported structure, surface it instead of the raw
        // "Command failed" error.
        try {
          const reported: unknown = JSON.parse(stdout || "null");
          if (reported !== null && typeof reported === "object") { resolveP(reported as EngineResult); return; }
        } catch { /* not structured — report the spawn/exit error as-is */ }
        rejectP(err);
        return;
      }
      try { resolveP(JSON.parse(stdout || "null")); } catch (e) { rejectP(e); }
    });
    activeChildren.add(child);
    // `extract` feeds its message list on stdin — without writing it and
    // closing the stream the CLI blocks forever on stdin.read() and the
    // event loop wedges. (Not via the `input` option: @types/node doesn't
    // declare it on ExecFileOptions.)
    if (input !== undefined && child.stdin) {
      child.stdin.on("error", () => {}); // EPIPE if the engine exits before reading (e.g. argparse error)
      child.stdin.write(input);
      child.stdin.end();
    }
  });
}

// Safe wrapper for tool executes: an engine failure becomes a consistent
// {"error": ...} result the model can read, never a thrown raw Node error.
// (Memory outages must degrade the answer, not crash the turn.)
export async function callEngine(engine: string[], baseArgs: string[], args: string[], input?: string, envOverride?: Record<string, string>): Promise<EngineResult | null> {
  try {
    return await runEngine(engine, baseArgs, args, input, envOverride);
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

export function messageText(m: unknown): string {
  const msg = m as any;
  if (typeof msg?.text === "string") return msg.text;
  // V2 context hooks deliver provider-shaped messages: { role, content: [{ type: "text", text }] }
  // (ctx.session.context() uses { type, text } — both handled).
  const parts = Array.isArray(msg?.parts) ? msg.parts : Array.isArray(msg?.content) ? msg.content : undefined;
  if (Array.isArray(parts)) return parts.filter((p: any) => p?.type === "text").map((p: any) => p.text ?? "").join(" ");
  return "";
}

export function freshUserMsgs(userMsgs: any[], seen: Set<string>): any[] {
  // Id-less messages can't be tracked, so they stay "fresh" (old behavior).
  return userMsgs.filter((m) => typeof m.id !== "string" || !seen.has(m.id));
}

// Per-turn injection dedupe: facts already shown to this session are not
// re-pushed, and at most `cap` new facts go in per turn (keeps the system
// prompt lean).
export function freshInjectionFacts<T extends { fact_id: number }>(results: T[], seen: Set<number>, cap: number): T[] {
  return results.filter((f) => !seen.has(f.fact_id)).slice(0, cap);
}

// --- skill catalog (Phase 2, docs/skill-retrieval.md §3) ---------------------
// Skill facts are category-'skill' rows written by `skill-sync`: content
// '<name>: <description>', tags 'skill-id:<name>, skill-dir:<abs path>, ...'.

// Display cap for skill descriptions in injected blocks (~120 chars per the
// design doc) and for memory_skill_load bodies (SKILL.md files are small;
// 32KB is far beyond any real one — the cap exists so a pathological body
// can never blow up the system prompt).
export const SKILL_DESC_LIMIT = 120;
export const SKILL_BODY_MAX = 32_000;

export function skillSyncArgs(dirs: string[]): string[] {
  const args = ["skill-sync"];
  for (const d of dirs) args.push("--dir", d);
  return args;
}

// The skill id from a skill fact: the skill-id tag is authoritative (it is
// what skill-sync wrote and what the built-in skill tool / memory_skill_load
// need); fall back to the content prefix.
export function skillIdOf(f: { content: string; tags?: string }): string {
  const tagged = (f.tags ?? "").split(",").map((t) => t.trim()).find((t) => t.startsWith("skill-id:"));
  if (tagged) return tagged.slice("skill-id:".length);
  const i = f.content.indexOf(":");
  return (i > 0 ? f.content.slice(0, i) : f.content).trim();
}

export function skillDirOf(f: { tags?: string }): string | undefined {
  const tagged = (f.tags ?? "").split(",").map((t) => t.trim()).find((t) => t.startsWith("skill-dir:"));
  return tagged ? tagged.slice("skill-dir:".length) : undefined;
}

// '<name>: <description>' -> '<description>' (whole content if no prefix).
export function skillDescription(content: string): string {
  const i = content.indexOf(":");
  return (i > 0 ? content.slice(i + 1) : content).trim();
}

export function truncateText(s: string, limit: number): string {
  if (s.length <= limit) return s;
  const cut = s.slice(0, limit);
  const sp = cut.lastIndexOf(" ");
  return (sp > 0 ? cut.slice(0, sp) : cut).trimEnd() + "…";
}

export function renderSkillBlock(rows: { content: string; tags?: string }[], header: string): string {
  return `${header}\n${rows
    .map((f) => `- ${skillIdOf(f)} — ${truncateText(skillDescription(f.content), SKILL_DESC_LIMIT)}`)
    .join("\n")}`;
}

// Skill-load signals in a session transcript (for the usage-feedback loop).
// Two shapes, verified against @opencode/client types (SessionMessageSkill,
// SessionMessageAssistantTool):
//   1. Built-in skill tool: a successful load is recorded as its own transcript
//      message { type: "skill", skill, name, text } — success-only, so it is
//      exactly the "was loaded" signal (a failed load leaves no such message).
//   2. memory_skill_load (BYO catalog): an assistant content part
//      { type: "tool", name: "memory_skill_load", state: { status, input } };
//      completed loads only — a failed read was not useful.
// Returns skill ids, possibly duplicated (the caller dedupes).
export function skillLoadNames(msgs: unknown[]): string[] {
  const names: string[] = [];
  for (const m of msgs) {
    const msg = m as any;
    if (!msg || typeof msg !== "object") continue;
    if (msg.type === "skill") {
      const n = typeof msg.name === "string" ? msg.name : typeof msg.skill === "string" ? msg.skill : "";
      if (n) names.push(n);
      continue;
    }
    const parts = Array.isArray(msg.content) ? msg.content : Array.isArray(msg.parts) ? msg.parts : undefined;
    if (!Array.isArray(parts)) continue;
    for (const p of parts) {
      const part = p as any;
      if (part?.type !== "tool" || part?.state?.status !== "completed") continue;
      const n = part.name === "memory_skill_load" ? part.state.input?.name : part.name === "skill" ? part.state.input?.id : undefined;
      if (typeof n === "string" && n) names.push(n);
    }
  }
  return names;
}

export default Plugin.define({
  id: "tiberius-memory",
  async setup(ctx) {
    const opts = (ctx.options ?? {}) as Options;
    const engine = opts.engine ?? ["python3", "-m", "tiberius.cli"];
    const baseArgs = opts.db ? ["--db", opts.db] : [];
    const env = opts.pythonpath ? { PYTHONPATH: opts.pythonpath } : undefined; // else: container's baked-in ENV PYTHONPATH
    // Per-turn (query-conditioned) injection is the default — matching the
    // Hermes provider's per-turn prefetch(), where recall tracks the question
    // rather than a session-start snapshot. injectPerTurn: false reverts to
    // the one-shot session-start push.
    const injectPerTurn = opts.injectPerTurn ?? true;
    // Skill catalog (docs/skill-retrieval.md §3): everything skill-related is
    // gated on skillDirs — unset means zero behavior change (no engine calls,
    // no system-prompt blocks, no tools).
    const skillDirs = Array.isArray(opts.skillDirs) ? opts.skillDirs.map(String).filter(Boolean) : [];
    const skillsEnabled = skillDirs.length > 0;
    const skillsTop = Math.max(1, opts.skillsTop ?? 3);
    const skillsFloor = Math.max(1, opts.skillsFloor ?? 2);
    const injected = new Set<string>();
    // User-message ids already fed to extract, per session: each turn sends only
    // the delta, so extract cost stays O(new messages) instead of O(session).
    // A hot-reload loses this map and re-extracts a session's history once —
    // idempotent under UNIQUE(content).
    const extractedUserMsgs = new Map<string, Set<string>>();
    // Per-turn injection state (only used when per-turn injection is on): the last user
    // message text searched per session (model calls within the same turn
    // re-dispatch the same messages — no engine call needed) and the
    // fact_ids already injected per session (each turn injects only what is
    // new to that turn).
    const lastSearched = new Map<string, string>();
    const injectedFacts = new Map<string, Set<number>>();
    // Skill-injection state (skillDirs only): sessions that already ran
    // skill-sync at session start, the skill fact_ids already shown per
    // session (floor + per-turn blocks share it — a skill shown in the floor
    // is not re-pushed by the per-turn search), and the last user message
    // text skill-searched per session (same per-turn dedupe as facts).
    const syncedSessions = new Set<string>();
    const injectedSkills = new Map<string, Set<number>>();
    const lastSkillSearch = new Map<string, string>();
    // Skill usage feedback (session end): skills that already got
    // `feedback helpful` per session — the handler fires once per COMPLETED
    // TURN with the full transcript, so re-loads from earlier turns must not
    // re-train trust. (Hot-reload loses this map and can re-send a session's
    // feedback once — bounded, and trust is clamped to [0, 1].)
    const skillFeedbackSent = new Map<string, Set<string>>();

    // --- tools ---------------------------------------------------------------
    ctx.tool.transform((e) => {
      e.add({
        name: "memory_search",
        description: "Search long-term local memory (facts about the user, the environment, projects, decisions, preferences). Call it before answering anything about the environment, the project, or user preferences, and before starting work a past decision may affect. Conversation context is not a substitute — stored facts may not be in it.",
        input: { type: "object", properties: { query: { type: "string", description: "Keywords for the fact you need" }, top: { type: "integer", description: "Max results (default 5)" } }, required: ["query"] },
        execute: async (input: any) => {
          const r = await callEngine(engine, baseArgs, ["search", String(input.query), "--top", String(input.top ?? 5)], undefined, env);
          return { content: JSON.stringify((r?.results ?? []).map((f) => ({ id: f.fact_id, content: f.content, category: f.category, trust: f.trust_score }))) };
        },
      });
      e.add({
        name: "memory_probe",
        description: "Recall ALL stored facts linked to one entity (a person, service, or project name).",
        input: { type: "object", properties: { entity: { type: "string" } }, required: ["entity"] },
        execute: async (input: any) => {
          const r = await callEngine(engine, baseArgs, ["probe", String(input.entity)], undefined, env);
          return { content: JSON.stringify((r?.results ?? []).map((f) => ({ id: f.fact_id, content: f.content, category: f.category }))) };
        },
      });
      e.add({
        name: "memory_related",
        description: "One hop out: facts that share entities with the given entity (what is connected to it).",
        input: { type: "object", properties: { entity: { type: "string" } }, required: ["entity"] },
        execute: async (input: any) => {
          const r = await callEngine(engine, baseArgs, ["related", String(input.entity)], undefined, env);
          return { content: JSON.stringify((r?.results ?? []).map((f) => ({ id: f.fact_id, content: f.content, category: f.category }))) };
        },
      });
      e.add({
        name: "memory_reason",
        description: "Compositional recall: facts linked to ALL of the given entities. Use for questions that span two or more.",
        input: { type: "object", properties: { entities: { type: "array", items: { type: "string" }, description: "Entity names the fact must relate to all of" }, top: { type: "integer", description: "Max results (default 5)" } }, required: ["entities"] },
        execute: async (input: any) => {
          const ents = (input.entities as string[]).map(String);
          const r = await callEngine(engine, baseArgs, ["reason", ...ents, "--top", String(input.top ?? 5)], undefined, env);
          return { content: JSON.stringify((r?.results ?? []).map((f) => ({ id: f.fact_id, content: f.content, category: f.category, trust: f.trust_score }))) };
        },
      });
      e.add({
        name: "memory_feedback",
        description: "Rate a recalled fact after using it: helpful if it was accurate, unhelpful if outdated. Trains trust scores — call this after acting on a fact.",
        input: { type: "object", properties: { fact_id: { type: "integer", description: "The id returned by a recall tool" }, rating: { type: "string", enum: ["helpful", "unhelpful"] } }, required: ["fact_id", "rating"] },
        execute: async (input: any) => {
          const r = await callEngine(engine, baseArgs, ["feedback", String(input.fact_id), String(input.rating)], undefined, env);
          return { content: JSON.stringify(r ?? {}) };
        },
      });
      e.add({
        name: "memory_add",
        description: "Store one durable fact (a preference, decision, or project fact the user would expect to have you remember). One statement, present tense, no fluff.",
        input: { type: "object", properties: { content: { type: "string" }, category: { type: "string", description: "user_pref | project | general (default general)" }, tags: { type: "string" } }, required: ["content"] },
        execute: async (input: any) => {
          const args = ["add", String(input.content)];
          if (input.category) args.push("--category", String(input.category));
          if (input.tags) args.push("--tags", String(input.tags));
          const r = await callEngine(engine, baseArgs, args, undefined, env);
          return { content: JSON.stringify(r ?? {}) };
        },
      });
      e.add({
        name: "memory_update",
        description: "Update an existing fact in place: new content, tags, category, and/or a trust nudge. Prefer this over remove+add — it keeps the fact_id, entity links, and retrieval history.",
        input: { type: "object", properties: { fact_id: { type: "integer", description: "The id of the fact to update" }, content: { type: "string", description: "New full content (one statement, present tense)" }, tags: { type: "string", description: "Comma-separated tags (replaces the current tags)" }, category: { type: "string", description: "user_pref | project | tool | general" }, trust_delta: { type: "number", description: "Trust adjustment, e.g. 0.1 (clamped to 0..1)" } }, required: ["fact_id"] },
        execute: async (input: any) => {
          const args = ["update", String(input.fact_id)];
          if (input.content) args.push("--content", String(input.content));
          if (input.tags) args.push("--tags", String(input.tags));
          if (input.category) args.push("--category", String(input.category));
          if (typeof input.trust_delta === "number") args.push("--trust-delta", String(input.trust_delta));
          if (args.length === 2) return { content: JSON.stringify({ error: "nothing to update: pass content, tags, category, or trust_delta" }) };
          const r = await callEngine(engine, baseArgs, args, undefined, env);
          return { content: JSON.stringify(r ?? {}) };
        },
      });
      e.add({
        name: "memory_remove",
        description: "Delete a fact by id — for facts that are wrong or obsolete. Prefer memory_update when only the content is outdated.",
        input: { type: "object", properties: { fact_id: { type: "integer", description: "The id to delete" } }, required: ["fact_id"] },
        execute: async (input: any) => {
          const r = await callEngine(engine, baseArgs, ["remove", String(input.fact_id)], undefined, env);
          return { content: JSON.stringify(r ?? {}) };
        },
      });
      e.add({
        name: "memory_list",
        description: "Browse stored facts ordered by trust (find fact_ids to update or remove).",
        input: { type: "object", properties: { category: { type: "string", description: "user_pref | project | tool | general" }, since: { type: "string", description: "Only facts created at or after — ISO (YYYY-MM-DD[ HH:MM:SS] UTC) or relative (30m/2h/7d/2w)" }, top: { type: "integer", description: "Max results (default 50)" } } },
        execute: async (input: any) => {
          const args = ["list"];
          if (input.category) args.push("--category", String(input.category));
          if (input.since) args.push("--since", String(input.since));
          args.push("--top", String(input.top ?? 50));
          const r = await callEngine(engine, baseArgs, args, undefined, env);
          return { content: JSON.stringify((r?.results ?? []).map((f) => ({ id: f.fact_id, content: f.content, category: f.category, trust: f.trust_score }))) };
        },
      });
      if (skillsEnabled) {
        // Skill-catalog tools (skillDirs set): the escape hatch past the
        // per-turn top-k, and the body loader for skills the built-in skill
        // tool does not index (it rejects unindexed ids — verified 2026-10-27).
        e.add({
          name: "memory_skill_search",
          description: "Search the skill catalog (task-specific instruction documents) by keyword, for skills not in the injected skill block. Returns skill ids — load one with the skill tool (if indexed) or memory_skill_load.",
          input: { type: "object", properties: { query: { type: "string", description: "Keywords for the relevant skill should match" }, top: { type: "integer", description: "Max results (default 5)" } }, required: ["query"] },
          execute: async (input: any) => {
            const r = await callEngine(engine, baseArgs, ["search", String(input.query), "--category", "skill", "--top", String(input.top ?? 5)], undefined, env);
            return { content: JSON.stringify((r?.results ?? []).map((f) => ({ id: skillIdOf(f), fact_id: f.fact_id, description: skillDescription(f.content) }))) };
          },
        });
        e.add({
          name: "memory_skill_load",
          description: "Load the full SKILL.md body of a catalog skill by its id (from the injected skill block or memory_skill_search). Use it for skills the built-in skill tool does not index.",
          input: { type: "object", properties: { name: { type: "string", description: "Skill id" } }, required: ["name"] },
          execute: async (input: any) => {
            const name = String(input.name);
            // Skill catalogs are small: one list call resolves the skill-dir
            // tag (the store has no by-tag lookup — list is the public surface).
            const r = await callEngine(engine, baseArgs, ["list", "--category", "skill", "--top", "1000"], undefined, env);
            const f = (r?.results ?? []).find((x) => skillIdOf(x) === name);
            if (!f) return { content: JSON.stringify({ error: `skill ${name} not found in the catalog` }) };
            const dir = skillDirOf(f);
            if (!dir) return { content: JSON.stringify({ error: `skill ${name} has no skill-dir tag (was it added by skill-sync?)` }) };
            const file = join(dir, "SKILL.md");
            try {
              const body = readFileSync(file, "utf8");
              return { content: body.length > SKILL_BODY_MAX ? body.slice(0, SKILL_BODY_MAX) + "\n[truncated]" : body };
            } catch (e) {
              return { content: JSON.stringify({ error: `cannot read ${file}: ${e instanceof Error ? e.message : String(e)}` }) };
            }
          },
        });
      }
    });

    // --- context injection: per-turn by default (query-conditioned) ----------
    // Verified against live OpenCode 2.0.28 (2026-10): event.messages carries
    // provider-shaped messages ({ role, content: [{ type: "text", text }] }),
    // and event.system is snapshotted when the hook's promise settles — the
    // search must be AWAITED, a fire-and-forget .then() lands after dispatch.
    // Cost: ~one engine start (100-300ms locally) on the first model call per
    // session. Errors stay swallowed: memory must never break model dispatch.
    ctx.session.hook("context", async (event) => {
      // The LAST user message. event.messages is chronological and
      // provider-shaped ({ role, content: [...] }); a compacted session leads
      // with its summary block AS a user message, so .reverse() would pick the
      // summary instead of the current question (latent in the original
      // one-shot code, where first == last on a session's first dispatch).
      const lastUser = [...event.messages]
        .filter((m: any) => (m?.role ?? m?.type) === "user")
        .map(messageText)
        .filter(Boolean)
        .pop();
      if (!lastUser) return;
      const query = lastUser.slice(0, 200);
      if (skillsEnabled) {
        // Skill catalog (docs/skill-retrieval.md §3). Same outage discipline
        // as the facts path: mark the session before the engine call (a down
        // engine skips the rest of the session rather than re-delaying every
        // model call), and swallow everything — memory must never break
        // dispatch.
        if (!syncedSessions.has(event.sessionID)) {
          syncedSessions.add(event.sessionID);
          try {
            await runEngine(engine, baseArgs, skillSyncArgs(skillDirs), undefined, env);
          } catch { /* best-effort: no catalog this session rather than a stuck dispatch */ }
          let seen = injectedSkills.get(event.sessionID);
          if (!seen) {
            seen = new Set<number>();
            injectedSkills.set(event.sessionID, seen);
          }
          // Session-start trust floor: proven workhorses stay visible every
          // session (top-N by trust — list orders by trust_score DESC).
          try {
            const r = await runEngine(engine, baseArgs, ["list", "--category", "skill", "--top", String(skillsFloor)], undefined, env);
            const fresh = freshInjectionFacts(r?.results ?? [], seen, skillsFloor);
            for (const f of fresh) seen.add(f.fact_id);
            if (fresh.length) {
              event.system.push({
                type: "text",
                text: renderSkillBlock(fresh, "Skills available this session (load with the skill tool if indexed, or memory_skill_load otherwise):"),
              });
            }
          } catch { /* memory must never break model dispatch */ }
        }
        // Per-turn ranked search, beside the facts search: the skills relevant
        // to THIS turn. The lastSkillSearch dedupe mirrors lastSearched — model
        // calls within the same turn re-dispatch the same message and cost
        // nothing. injectPerTurn: false keeps the session-start floor only.
        if (injectPerTurn && lastSkillSearch.get(event.sessionID) !== query) {
          lastSkillSearch.set(event.sessionID, query);
          let seen = injectedSkills.get(event.sessionID);
          if (!seen) {
            seen = new Set<number>();
            injectedSkills.set(event.sessionID, seen);
          }
          try {
            const r = await runEngine(engine, baseArgs, ["search", query, "--category", "skill", "--top", String(skillsTop)], undefined, env);
            const fresh = freshInjectionFacts(r?.results ?? [], seen, skillsTop);
            for (const f of fresh) seen.add(f.fact_id);
            if (fresh.length) {
              event.system.push({
                type: "text",
                text: renderSkillBlock(fresh, "Skills relevant to this turn (load with the skill tool if indexed, or memory_skill_load otherwise):"),
              });
            }
          } catch { /* memory must never break model dispatch */ }
        }
      }
      if (injectPerTurn) {
        // Per-turn: every new user message gets its own search; facts already
        // shown this session are not re-pushed (each turn injects only what is
        // new to that turn). Later model calls within the same turn re-dispatch
        // the same last user message — the lastSearched check skips them, so
        // the cost is one engine start per turn, not per model call.
        if (lastSearched.get(event.sessionID) === query) return;
        // Mark BEFORE the search: if the engine is down, we skip rather than
        // re-delaying dispatch on every later model call this turn.
        lastSearched.set(event.sessionID, query);
        let seen = injectedFacts.get(event.sessionID);
        if (!seen) {
          seen = new Set<number>();
          injectedFacts.set(event.sessionID, seen);
        }
        try {
          const r = await runEngine(engine, baseArgs, ["search", query, "--top", "10"], undefined, env);
          const fresh = freshInjectionFacts(r?.results ?? [], seen, 3);
          for (const f of fresh) seen.add(f.fact_id);
          if (fresh.length) {
            const more = typeof r?.total === "number" && r.total > fresh.length ? ` (store has ${r.total} matches)` : "";
            event.system.push({ type: "text", text: `Relevant stored memory${more} (search with the memory_search tool for more):\n${fresh.map((f) => `- [${f.fact_id}] ${f.content}`).join("\n")}` });
          }
        } catch { /* memory must never break model dispatch */ }
        return;
      }
      if (injected.has(event.sessionID)) return;
      // Mark injected BEFORE the search: if the engine is down, we skip
      // rather than retrying (and re-delaying) dispatch on every later model
      // call this session. The trade is one missed injection per outage.
      injected.add(event.sessionID);
      try {
        const r = await runEngine(engine, baseArgs, ["search", query, "--top", String(opts.injectTop ?? 4)], undefined, env);
        const facts = (r?.results ?? []).map((f) => `- [${f.fact_id}] ${f.content}`);
        if (facts.length) {
          const more = typeof r?.total === "number" && r.total > facts.length ? ` (store has ${r.total} matches)` : "";
          event.system.push({ type: "text", text: `Relevant stored memory${more} (search with the memory_search tool for more):\n${facts.join("\n")}` });
        }
      } catch { /* memory must never break model dispatch */ }
    });

    // --- session end: regex-extract statements from user messages ------------
    const ac = new AbortController();
    (async () => {
      const events = await ctx.event.subscribe({ signal: ac.signal });
      for await (const ev of events as AsyncIterable<any>) {
        const type: string = ev?.type ?? ev?.properties?.type ?? "";
        if (type !== "session.execution.succeeded" && type !== "session.execution.interrupted") continue;
        // Fires once per completed turn. Only messages not yet extracted are
        // sent (per-session id tracking); re-sends after a failed turn are
        // safe — add_fact dedupes on UNIQUE(content).
        // V2 events wrap the payload in `data` (verified against live
        // OpenCode 2.0.28): { id, created, type, durable, data: { sessionID } }.
        const sessionID: string | undefined = ev?.data?.sessionID ?? ev?.properties?.sessionID ?? ev?.sessionID;
        if (!sessionID) continue;
        try {
          const raw: any[] = await ctx.session.context({ sessionID });
          // 1) Statement extraction (user messages, delta only).
          const userMsgs = raw.filter((m) => m?.type === "user" && typeof m.text === "string");
          let seen = extractedUserMsgs.get(sessionID);
          if (!seen) {
            seen = new Set<string>();
            extractedUserMsgs.set(sessionID, seen);
          }
          const fresh = freshUserMsgs(userMsgs, seen);
          const msgs = fresh
            .map((m) => ({ role: "user", content: m.text as string }))
            .filter((m) => m.content.length >= 10);
          if (msgs.length) {
            await runEngine(engine, baseArgs, ["extract"], JSON.stringify(msgs), env);
            // Mark AFTER the engine call: a failed turn retries its messages next turn.
            for (const m of fresh) if (typeof m.id === "string") seen.add(m.id);
          }
          // 2) Skill usage feedback (docs/skill-retrieval.md §3): a skill that
          // was loaded gets `feedback helpful` ONCE per session — the learned-
          // ranking signal. Gated on skillDirs like the rest of the skill
          // feature: with options unset, a built-in skill load in the
          // transcript must not trigger skill-catalog engine calls or trust
          // changes (zero behavior change is the contract). The handler fires
          // once per completed turn with the full transcript, so dedupe
          // against what was already sent.
          if (skillsEnabled) {
            const loads = skillLoadNames(raw);
            if (loads.length) {
              let sent = skillFeedbackSent.get(sessionID);
              if (!sent) {
                sent = new Set<string>();
                skillFeedbackSent.set(sessionID, sent);
              }
              const freshLoads = [...new Set(loads)].filter((n) => !sent.has(n));
              if (freshLoads.length) {
                try {
                  const list = await runEngine(engine, baseArgs, ["list", "--category", "skill", "--top", "1000"], undefined, env);
                  const byId = new Map<string, number>((list?.results ?? []).map((f) => [skillIdOf(f), f.fact_id as number]));
                  for (const n of freshLoads) {
                    const fid = byId.get(n);
                    if (fid === undefined) continue; // load of an out-of-catalog skill — nothing to train
                    await runEngine(engine, baseArgs, ["feedback", String(fid), "helpful"], undefined, env);
                    // Mark only after a successful call: a failed turn retries next turn.
                    sent.add(n);
                  }
                } catch { /* best-effort */ }
              }
            }
          }
        } catch { /* best-effort */ }
      }
    })().catch(() => {});

    return () => {
      ac.abort();
      // Don't leave in-flight engines behind on hot reload / server shutdown —
      // an orphaned python process could keep the DB write-locked.
      for (const child of activeChildren) child.kill("SIGTERM");
      activeChildren.clear();
    };
  },
});
