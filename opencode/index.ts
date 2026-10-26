/**
 * tiberius — local memory for OpenCode (native V2 plugin).
 *
 * Wraps the tiberius Python engine (this repo) in a local subprocess — one
 * implementation of the memory heuristics, not a port. No network: the engine
 * runs in this container against a local SQLite file.
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
 *
 * What it does:
 *   - tools: memory_search / memory_probe / memory_related / memory_reason / memory_add / memory_feedback
 *     (same operation surface as the Hermes provider — see skills/tiberius-usage/SKILL.md)
 *   - context hook: first model call per session pushes top relevant facts into the system prompt
 *   - event: on session.execution.succeeded, regex-extracts statements from user messages
 *
 * ponytail: one-shot context injection per session (memory added mid-session shows up in the
 * next session); upgrade = re-query per agent step. Memory must never break model dispatch —
 * every engine call is caught and swallowed.
 */
import { execFile } from "node:child_process";
import { Plugin } from "@opencode/plugin";

type Options = {
  db?: string;
  engine?: string[];
  pythonpath?: string;
  injectTop?: number;
};

type EngineResult = { results?: { fact_id: number; content: string; category?: string; trust_score?: number }[]; [k: string]: unknown };

function runEngine(engine: string[], baseArgs: string[], args: string[], input?: string, envOverride?: Record<string, string>): Promise<EngineResult | null> {
  return new Promise((resolveP, rejectP) => {
    execFile(engine[0], [...engine.slice(1), ...baseArgs, ...args], {
      env: envOverride ? { ...process.env, ...envOverride } : process.env,
      maxBuffer: 10 * 1024 * 1024,
    }, (err, stdout) => {
      if (err) return rejectP(err);
      try { resolveP(JSON.parse(stdout || "null")); } catch (e) { rejectP(e); }
    });
  });
}

function messageText(m: unknown): string {
  const msg = m as any;
  if (typeof msg?.text === "string") return msg.text;
  // V2 context hooks deliver provider-shaped messages: { role, content: [{ type: "text", text }] }
  // (ctx.session.context() uses { type, text } — both handled).
  const parts = Array.isArray(msg?.parts) ? msg.parts : Array.isArray(msg?.content) ? msg.content : undefined;
  if (Array.isArray(parts)) return parts.filter((p: any) => p?.type === "text").map((p: any) => p.text ?? "").join(" ");
  return "";
}

export default Plugin.define({
  id: "tiberius-memory",
  async setup(ctx) {
    const opts = (ctx.options ?? {}) as Options;
    const engine = opts.engine ?? ["python3", "-m", "tiberius.cli"];
    const baseArgs = opts.db ? ["--db", opts.db] : [];
    const env = opts.pythonpath ? { PYTHONPATH: opts.pythonpath } : undefined; // else: container's baked-in ENV PYTHONPATH
    const injected = new Set<string>();

    // --- tools ---------------------------------------------------------------
    ctx.tool.transform((e) => {
      e.add({
        name: "memory_search",
        description: "Search long-term local memory (facts about the user, projects, decisions, preferences). Use before asking the user something that might already be known.",
        input: { type: "object", properties: { query: { type: "string", description: "Keywords for the fact you need" }, top: { type: "integer", description: "Max results (default 5)" } }, required: ["query"] },
        execute: async (input: any) => {
          const r = await runEngine(engine, baseArgs, ["search", String(input.query), "--top", String(input.top ?? 5)], undefined, env);
          return { content: JSON.stringify((r?.results ?? []).map((f) => ({ id: f.fact_id, content: f.content, category: f.category, trust: f.trust_score }))) };
        },
      });
      e.add({
        name: "memory_probe",
        description: "Recall ALL stored facts linked to one entity (a person, service, or project name).",
        input: { type: "object", properties: { entity: { type: "string" } }, required: ["entity"] },
        execute: async (input: any) => {
          const r = await runEngine(engine, baseArgs, ["probe", String(input.entity)], undefined, env);
          return { content: JSON.stringify((r?.results ?? []).map((f) => ({ id: f.fact_id, content: f.content, category: f.category }))) };
        },
      });
      e.add({
        name: "memory_related",
        description: "One hop out: facts that share entities with the given entity (what is connected to it).",
        input: { type: "object", properties: { entity: { type: "string" } }, required: ["entity"] },
        execute: async (input: any) => {
          const r = await runEngine(engine, baseArgs, ["related", String(input.entity)], undefined, env);
          return { content: JSON.stringify((r?.results ?? []).map((f) => ({ id: f.fact_id, content: f.content, category: f.category }))) };
        },
      });
      e.add({
        name: "memory_reason",
        description: "Compositional recall: facts linked to ALL of the given entities. Use for questions that span two or more.",
        input: { type: "object", properties: { entities: { type: "array", items: { type: "string" }, description: "Entity names the fact must relate to all of" }, top: { type: "integer", description: "Max results (default 5)" } }, required: ["entities"] },
        execute: async (input: any) => {
          const ents = (input.entities as string[]).map(String);
          const r = await runEngine(engine, baseArgs, ["reason", ...ents, "--top", String(input.top ?? 5)], undefined, env);
          return { content: JSON.stringify((r?.results ?? []).map((f) => ({ id: f.fact_id, content: f.content, category: f.category, trust: f.trust_score }))) };
        },
      });
      e.add({
        name: "memory_feedback",
        description: "Rate a recalled fact after using it: helpful if it was accurate, unhelpful if outdated. Trains trust scores — call this after acting on a fact.",
        input: { type: "object", properties: { fact_id: { type: "integer", description: "The id returned by a recall tool" }, rating: { type: "string", enum: ["helpful", "unhelpful"] } }, required: ["fact_id", "rating"] },
        execute: async (input: any) => {
          const r = await runEngine(engine, baseArgs, ["feedback", String(input.fact_id), String(input.rating)], undefined, env);
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
          const r = await runEngine(engine, baseArgs, args, undefined, env);
          return { content: JSON.stringify(r ?? {}) };
        },
      });
    });

    // --- context injection: top relevant facts once per session --------------
    // Verified against live OpenCode 2.0.28 (2026-10): event.messages carries
    // provider-shaped messages ({ role, content: [{ type: "text", text }] }),
    // and event.system is snapshotted when the hook's promise settles — the
    // search must be AWAITED, a fire-and-forget .then() lands after dispatch.
    // Cost: ~one engine start (100-300ms locally) on the first model call per
    // session. Errors stay swallowed: memory must never break model dispatch.
    ctx.session.hook("context", async (event) => {
      if (injected.has(event.sessionID)) return;
      const lastUser = [...event.messages]
        .reverse()
        .filter((m: any) => (m?.role ?? m?.type) === "user")
        .map(messageText)
        .filter(Boolean)
        .pop();
      if (!lastUser) return;
      injected.add(event.sessionID);
      try {
        const r = await runEngine(engine, baseArgs, ["search", lastUser.slice(0, 200), "--top", String(opts.injectTop ?? 4)], undefined, env);
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
        // V2 events wrap the payload in `data` (verified against live
        // OpenCode 2.0.28): { id, created, type, durable, data: { sessionID } }.
        const sessionID: string | undefined = ev?.data?.sessionID ?? ev?.properties?.sessionID ?? ev?.sessionID;
        if (!sessionID) continue;
        try {
          const raw: any[] = await ctx.session.context({ sessionID });
          const msgs = raw
            .filter((m) => m?.type === "user" && typeof m.text === "string")
            .map((m) => ({ role: "user", content: m.text as string }))
            .filter((m) => m.content.length >= 10);
          if (!msgs.length) continue;
          await runEngine(engine, baseArgs, ["extract"], JSON.stringify(msgs), env);
        } catch { /* best-effort */ }
      }
    })().catch(() => {});

    return () => { ac.abort(); };
  },
});
