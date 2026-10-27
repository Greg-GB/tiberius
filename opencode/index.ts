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
 *
 * What it does:
 *   - tools: memory_search / memory_probe / memory_related / memory_reason / memory_add / memory_feedback
 *     / memory_update / memory_remove / memory_list (same operation surface as
 *     the Hermes provider — see skills/tiberius-usage/SKILL.md)
 *   - context hook: by default, every new user message is searched and only
 *     not-yet-shown facts are injected into the system prompt (query-conditioned
 *     recall, matching the Hermes provider); injectPerTurn: false reverts to a
 *     one-shot push on the session's first model call — recall that does not
 *     depend on the agent remembering to call the tools
 *   - event: on session.execution.succeeded, regex-extracts statements from user messages
 *
 * Memory must never break model dispatch — every engine call is caught and
 * swallowed. (Per-turn injection is the default, so memory added mid-session
 * is picked up on the next turn; injectPerTurn: false reverts to the one-shot
 * session-start behavior.)
 */
import { execFile, type ChildProcess } from "node:child_process";
import { Plugin } from "@opencode/plugin";

type Options = {
  db?: string;
  engine?: string[];
  pythonpath?: string;
  injectTop?: number;
  injectPerTurn?: boolean;
};

type EngineResult = { results?: { fact_id: number; content: string; category?: string; trust_score?: number }[]; [k: string]: unknown; error?: string };

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
            event.system.push({ type: "text", text: `Relevant stored memory (search with the memory_search tool for more):\n${fresh.map((f) => `- [${f.fact_id}] ${f.content}`).join("\n")}` });
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
          event.system.push({ type: "text", text: `Relevant stored memory (search with the memory_search tool for more):\n${facts.join("\n")}` });
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
          if (!msgs.length) continue;
          await runEngine(engine, baseArgs, ["extract"], JSON.stringify(msgs), env);
          // Mark AFTER the engine call: a failed turn retries its messages next turn.
          for (const m of fresh) if (typeof m.id === "string") seen.add(m.id);
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
