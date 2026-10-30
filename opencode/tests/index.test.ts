/**
 * Unit tests for the engine-subprocess layer (runEngine / callEngine /
 * messageText) and the skill-catalog wiring (Phase 2,
 * docs/skill-retrieval.md §3). The "engine" is `cat`, a shell one-liner, or
 * the fake Node engine (tests/fake-engine.mjs) standing in for
 * `python3 -m tiberius.cli` with the same output contract (JSON on stdout,
 * exit 1 with a JSON error on the failure path), so the interesting
 * behaviors — stdin flow, arg order, structured-error surfacing, timeout,
 * and the skill flow (sync → floor block → per-turn search → usage
 * feedback) — are exercised without Python and without network. No test may
 * call out (repo rule: local-memory, nothing phones home).
 *
 * Run: npm test  (node --test with type stripping; Node >= 22.6)
 */
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import tiberiusPlugin, {
  SKILL_BODY_MAX,
  callEngine,
  freshInjectionFacts,
  freshUserMsgs,
  messageText,
  renderBrief,
  renderDrift,
  renderSkillBlock,
  runEngine,
  skillDescription,
  skillDirOf,
  skillIdOf,
  skillLoadNames,
  skillSyncArgs,
  truncateText,
} from "../index.ts";

const tmp = mkdtempSync(join(tmpdir(), "tiberius-plugin-test-"));

test("runEngine feeds input to the engine's stdin (extract regression)", async () => {
  // The CLI's `extract` does stdin.read(): if runEngine drops `input`, this
  // hangs until the timeout instead of resolving — exactly the v0.1.x bug.
  const r = await runEngine(["cat"], [], [], JSON.stringify({ hello: "world" }), undefined, 5000);
  assert.deepEqual(r, { hello: "world" });
});

test("runEngine forwards baseArgs before command args", async () => {
  // The engine contract is JSON on stdout, so the fake echoes its argv as a
  // JSON array (no quotes/backslashes in these args, so no escaping needed).
  const script = join(tmp, "echo-args.sh");
  writeFileSync(
    script,
    "#!/bin/sh\nprintf '['\nsep=''\nfor a in \"$@\"; do printf '%s\"%s\"' \"$sep\" \"$a\"; sep=','; done\nprintf ']'\n",
    { mode: 0o755 },
  );
  const r = await runEngine([script], ["--db", "/tmp/m.db"], ["search", "q"], undefined, undefined, 5000);
  assert.deepEqual(r, ["--db", "/tmp/m.db", "search", "q"]);
});

test("runEngine surfaces the CLI's structured error (exit 1 + JSON on stdout)", async () => {
  const r = await runEngine(
    ["sh", "-c", `echo '{"error":"fact_id 999 not found"}'; exit 1`],
    [],
    [],
    undefined,
    undefined,
    5000,
  );
  assert.deepEqual(r, { error: "fact_id 999 not found" });
});

test("runEngine rejects on unstructured failure (no JSON on stdout)", async () => {
  await assert.rejects(
    runEngine(["sh", "-c", "echo oops 1>&2; exit 1"], [], [], undefined, undefined, 5000),
  );
});

test("runEngine kills and rejects a hung engine at the timeout", async () => {
  const t0 = Date.now();
  await assert.rejects(runEngine(["sh", "-c", "sleep 30"], [], [], undefined, undefined, 300));
  assert.ok(Date.now() - t0 < 10_000, "timeout must fire well before the sleep ends");
});

test("callEngine wraps engine failures as { error } instead of rejecting", async () => {
  const r = await callEngine(["sh", "-c", "echo broken 1>&2; exit 1"], [], [], undefined, undefined);
  assert.ok(r && typeof r.error === "string" && r.error.length > 0);
});

test("messageText handles the API shape ({ type, text })", () => {
  assert.equal(messageText({ type: "user", text: "hello" }), "hello");
});

test("messageText handles provider-shaped messages ({ role, content: [...] })", () => {
  assert.equal(
    messageText({
      role: "user",
      content: [{ type: "text", text: "part one " }, { type: "tool" }, { type: "text", text: "part two" }],
    }),
    "part one  part two",
  );
});

test("messageText handles the parts shape and rejects garbage", () => {
  assert.equal(messageText({ parts: [{ type: "text", text: "a" }, { type: "file" }] }), "a");
  assert.equal(messageText({}), "");
  assert.equal(messageText(null), "");
  assert.equal(messageText(undefined), "");
});

test("freshUserMsgs returns only unseen and id-less messages (extract delta)", () => {
  const seen = new Set<string>(["a"]);
  const out = freshUserMsgs(
    [{ id: "a", text: "seen" }, { id: "b", text: "new" }, { text: "id-less" }],
    seen,
  );
  assert.equal(out.length, 2);
  assert.equal(out[0].id, "b");
  assert.equal(out[1].text, "id-less"); // id-less messages can't be tracked — always fresh
});

test("freshInjectionFacts filters already-shown facts and caps the turn", () => {
  const seen = new Set<number>([1, 2]);
  const out = freshInjectionFacts(
    [{ fact_id: 1 }, { fact_id: 3 }, { fact_id: 4 }, { fact_id: 5 }],
    seen,
    2,
  );
  assert.deepEqual(out.map((f) => f.fact_id), [3, 4]); // 1,2 already shown; cap of 2 drops 5
});

test("freshInjectionFacts is empty when every result was already shown", () => {
  assert.deepEqual(freshInjectionFacts([{ fact_id: 1 }], new Set([1]), 3), []);
});

// --- skill catalog: pure helpers (Phase 2, docs/skill-retrieval.md §3) -------

test("skillSyncArgs expands one --dir flag per directory", () => {
  assert.deepEqual(skillSyncArgs(["/a", "/b"]), ["skill-sync", "--dir", "/a", "--dir", "/b"]);
});

test("skillIdOf prefers the skill-id tag, falls back to the content prefix", () => {
  assert.equal(skillIdOf({ content: "alpha: does things", tags: "skill-id:alpha, skill-dir:/x" }), "alpha");
  assert.equal(skillIdOf({ content: "legacy skill: no tags", tags: "other:x" }), "legacy skill");
  assert.equal(skillIdOf({ content: "no colon here" }), "no colon here");
});

test("skillDirOf and skillDescription read the tag and the content", () => {
  assert.equal(skillDirOf({ tags: "skill-id:a, skill-dir:/opt/skills/a" }), "/opt/skills/a");
  assert.equal(skillDirOf({ tags: "skill-id:a" }), undefined);
  assert.equal(skillDirOf({}), undefined);
  assert.equal(skillDescription("alpha: memory recall, probe and related lookups"), "memory recall, probe and related lookups");
  assert.equal(skillDescription("no colon here"), "no colon here");
});

test("truncateText keeps short text and cuts at a word boundary otherwise", () => {
  assert.equal(truncateText("12345", 5), "12345");
  assert.equal(truncateText("alpha beta gamma delta", 15), "alpha beta…");
  assert.equal(truncateText("abcdefghij", 5), "abcde…"); // no word boundary — hard cut
});

test("renderBrief renders the header marker, fact lines, and skill lane", () => {
  const block = renderBrief(
    {
      total: 5, entity_only: 1, tokens: 240,
      facts: [
        { fact_id: 15, content: "a fact" },
        { fact_id: 18, content: "another" },
        { fact_id: 19, content: "third" },
        { fact_id: 20, content: "fourth" },
      ],
      skills: [{ fact_id: 1, content: "alpha: memory recall, probe and related lookups", tags: "skill-id:alpha" }],
    } as any,
    "release process",
  );
  const lines = block.split("\n");
  assert.equal(lines[0], "brief: release process (4 facts, ~240 tokens — 3 of 5 match, search has the rest; +1 entity-linked)");
  assert.equal(lines[1], "- [15] a fact");
  assert.equal(lines[4], "- [20] fourth");
  assert.equal(lines[5], "skills:");
  assert.equal(lines[6], "- alpha — memory recall, probe and related lookups");
});

test("renderBrief omits markers when the window is the whole pool", () => {
  assert.equal(
    renderBrief({ total: 1, entity_only: 0, tokens: 8, facts: [{ fact_id: 7, content: "just one" }], skills: [] } as any, "q"),
    "brief: q (1 fact, ~8 tokens)\n- [7] just one",
  );
});

test("renderDrift renders the two-sided block with echo markers and the unbacked note", () => {
  const block = renderDrift(
    {
      anchor: "2026-01-01 00:00:00",
      tokens: 48,
      plan: {
        total: 2,
        count: 2,
        facts: [
          { fact_id: 2001, content: "AcmeWidget plan: crate loads happen at the dock before dawn", support: { count: 1, best: 0.75, echoes: [1001] } },
          { fact_id: 2002, content: "AcmeWidget policy: the ledger keeps status codes", support: null },
        ],
      },
      state: {
        total: 2,
        count: 2,
        facts: [
          { fact_id: 1001, content: "user prefers concise answers about memory work", created_at: "2026-01-01 00:00:00" },
          { fact_id: 1002, content: "project database is local SQLite", created_at: "2026-01-02 00:00:00" },
        ],
      },
      unbacked: [{ fact_id: 2002 }],
      note: "1 of 2 plan facts have no state echo >= 0.15 since 2026-01-01 00:00:00 — verify before treating it as drift",
    } as any,
    "AcmeWidget",
  );
  const lines = block.split("\n");
  assert.equal(lines[0], "drift: AcmeWidget (anchor 2026-01-01 00:00:00; 2 of 2 plan, 2 of 2 state, ~48 tokens)");
  assert.equal(lines[1], "plan:");
  assert.equal(lines[2], "- [2001] AcmeWidget plan: crate loads happen at the dock before dawn (backed ×1 @ 0.75)");
  assert.equal(lines[3], "- [2002] AcmeWidget policy: the ledger keeps status codes  [unbacked]");
  assert.equal(lines[4], "state:");
  assert.equal(lines[5], "- [1001] user prefers concise answers about memory work (2026-01-01 00:00:00)");
  assert.equal(lines[6], "- [1002] project database is local SQLite (2026-01-02 00:00:00)");
  assert.equal(lines[7], "unbacked: 1 of 2 plan facts have no state echo >= 0.15 since 2026-01-01 00:00:00 — verify before treating it as drift");
});

test("renderDrift omits the unbacked line when the field is absent", () => {
  const block = renderDrift(
    {
      anchor: "2026-01-01 00:00:00",
      tokens: 15,
      plan: { total: 1, count: 1, facts: [{ fact_id: 2001, content: "a plan claim", support: { count: 2, best: 0.3, echoes: [1001, 1002] } }] },
      state: { total: 0, count: 0, facts: [] },
    } as any,
    "AcmeWidget",
  );
  const lines = block.split("\n");
  assert.equal(lines.length, 4); // header, plan:, one plan line, state: — nothing else
  assert.ok(!lines.some((l) => l.startsWith("unbacked:")));
  assert.equal(lines[3], "state:");
});

test("renderSkillBlock renders '<id> — <description>' lines under a header", () => {
  const block = renderSkillBlock(
    [
      { content: "alpha: memory recall, probe and related lookups", tags: "skill-id:alpha" },
      { content: "beta: a very long description " + "word ".repeat(30) },
    ],
    "Skills available this session:",
  );
  const lines = block.split("\n");
  assert.equal(lines.length, 3);
  assert.equal(lines[0], "Skills available this session:");
  assert.equal(lines[1], "- alpha — memory recall, probe and related lookups");
  assert.ok(lines[2].startsWith("- beta — a very long description"));
  assert.ok(lines[2].endsWith("…"));
});

test("skillLoadNames reads both transcript skill-load signals; failed loads excluded", () => {
  const msgs = [
    { type: "user", id: "u1", text: "use the alpha skill" },
    { type: "skill", id: "ms1", skill: "skl-alpha", name: "alpha", text: "ALPHA BODY" },
    { type: "skill", id: "ms2", skill: "skl-solo" }, // no name — fall back to the skill id
    {
      type: "assistant",
      id: "a1",
      content: [
        { type: "tool", name: "memory_skill_load", state: { status: "completed", input: { name: "beta" } } },
        { type: "tool", name: "memory_skill_load", state: { status: "error", input: { name: "delta" } } },
        { type: "tool", name: "skill", state: { status: "completed", input: { id: "gamma" } } },
        { type: "tool", name: "memory_search", state: { status: "completed", input: { query: "x" } } },
        { type: "text", text: "thinking" },
      ],
    },
    null,
    42,
  ] as unknown[];
  assert.deepEqual(skillLoadNames(msgs), ["alpha", "skl-solo", "beta", "gamma"]);
});

// --- skill catalog: setup() wiring (Phase 2) ---------------------------------
// A fake Node engine (tests/fake-engine.mjs) stands in for
// `python3 -m tiberius.cli`: same JSON contract, deterministic skill/fact
// fixtures, and a JSON-line log of every invocation ($TIBERUS_FAKE_LOG) so the
// exact engine-call sequence is assertable. The mock ctx records registered
// tools, the context hook (driven directly with synthetic events), and a
// pushable event stream for the session-end loop.

const FAKE_ENGINE = fileURLToPath(new URL("./fake-engine.mjs", import.meta.url));
const DB = join(tmp, "mem.db");
const SKILLS_DIR = join(tmp, "skills");
const LOG_FILE = join(tmp, "fake-engine.jsonl");

process.env.TIBERUS_FAKE_LOG = LOG_FILE;
process.env.TIBERUS_FAKE_SKILLS_DIR = SKILLS_DIR;

// Fixture skill dirs (mirror the fake engine's catalog fixtures).
for (const [name, desc] of [
  ["alpha", "memory recall, probe and related lookups"],
  ["beta", "memory add, update and remove writes"],
  ["gamma", "formatting reports as markdown"],
  ["delta", "shell command execution"],
]) {
  mkdirSync(join(SKILLS_DIR, name), { recursive: true });
  writeFileSync(join(SKILLS_DIR, name, "SKILL.md"), `---\nname: ${name}\ndescription: "${desc}"\n---\n# ${name}\n\n${desc}\n`);
}

// The plugin's engine argv in the tests: `node <fake-engine> --db <DB> <cmd>…`.
const A = (...args: string[]) => ["--db", DB, ...args];

function logEntries(): { argv: string[] }[] {
  try {
    return readFileSync(LOG_FILE, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l));
  } catch {
    return [];
  }
}
function clearLog(): void {
  rmSync(LOG_FILE, { force: true });
}
// The session-end loop runs in the background, so waiting on it means polling
// the engine log (each engine call appends one line before the child exits).
async function waitForLog(min: number): Promise<{ argv: string[] }[]> {
  const deadline = Date.now() + 5000;
  for (;;) {
    const lines = logEntries();
    if (lines.length >= min) return lines;
    if (Date.now() > deadline) {
      throw new Error(`expected >= ${min} engine calls, got ${lines.length}: ${JSON.stringify(lines.map((l) => l.argv))}`);
    }
    await new Promise((r) => setTimeout(r, 25));
  }
}

interface CtxHandle {
  ctx: any;
  tools: any[];
  contextHandler: (event: any) => Promise<void>;
  pushEvent: (ev: any) => void;
  endEvents: () => void;
  setTranscript: (sessionID: string, msgs: any[]) => void;
}

function makeCtx(options: Record<string, unknown>): CtxHandle {
  const tools: any[] = [];
  const transcripts = new Map<string, any[]>();
  let contextHandler: (event: any) => Promise<void>;
  const queue: any[] = [];
  let resolveWait: (() => void) | null = null;
  let ended = false;
  const wake = () => {
    const r = resolveWait;
    resolveWait = null;
    r?.();
  };
  const ctx = {
    options,
    tool: { transform: (fn: (e: any) => void) => fn({ add: (t: any) => tools.push(t) }) },
    session: {
      hook: (_name: string, handler: any) => {
        contextHandler = handler;
      },
      context: async ({ sessionID }: { sessionID: string }) => transcripts.get(sessionID) ?? [],
    },
    event: {
      subscribe: () => ({
        [Symbol.asyncIterator]: () => ({
          async next(): Promise<IteratorResult<any>> {
            if (queue.length) return { value: queue.shift(), done: false };
            if (ended) return { value: undefined, done: true };
            await new Promise<void>((r) => {
              resolveWait = r;
            });
            if (queue.length) return { value: queue.shift(), done: false };
            return { value: undefined, done: true };
          },
          async return(): Promise<IteratorResult<any>> {
            ended = true;
            wake();
            return { value: undefined, done: true };
          },
        }),
      }),
    },
  };
  return {
    ctx,
    tools,
    contextHandler: (event: any) => contextHandler(event),
    pushEvent: (ev: any) => {
      queue.push(ev);
      wake();
    },
    endEvents: () => {
      ended = true;
      wake();
    },
    setTranscript: (sessionID: string, msgs: any[]) => {
      transcripts.set(sessionID, msgs);
    },
  };
}

// Run the real setup() against the mock ctx, clear the engine log, run the
// body, then tear the plugin down (end the event stream + cleanup) so each
// test starts from a stopped background loop.
async function withSetup(options: Record<string, unknown>, body: (h: CtxHandle) => Promise<void>): Promise<void> {
  const h = makeCtx(options);
  const cleanupFn = await tiberiusPlugin.setup(h.ctx as never);
  clearLog();
  try {
    await body(h);
  } finally {
    h.endEvents();
    await cleanupFn?.();
    await new Promise((r) => setTimeout(r, 20)); // let the for-await loop exit
  }
}

// A context-hook event: provider-shaped user messages (the shape verified
// against live OpenCode 2.0.28) and the pushable system-part array.
function ctxEvent(sessionID: string, texts: string[]): any {
  return {
    sessionID,
    system: [] as { type: string; text: string }[],
    messages: texts.map((t) => ({ role: "user", content: [{ type: "text", text: t }] })),
  };
}

test("skill options unset: zero skill behavior (no skill calls, no skill tools, no skill block)", async () => {
  await withSetup({ db: DB, engine: ["node", FAKE_ENGINE] }, async (h) => {
    const ev = ctxEvent("ses1", ["help me with memory recall"]);
    await h.contextHandler(ev);
    // Only the pre-existing per-turn fact search — nothing skill-related.
    assert.deepEqual(logEntries().map((e) => e.argv), [A("search", "help me with memory recall", "--top", "10")]);
    assert.equal(ev.system.length, 1);
    assert.ok(ev.system[0].text.startsWith("Relevant stored memory"));
    assert.ok(!h.tools.some((t) => t.name.startsWith("memory_skill_")));
    assert.equal(h.tools.length, 14);
  });
});

test("session start: skill-sync once, trust-floor block, per-turn skill search, same-turn dedupe", async () => {
  await withSetup({ db: DB, engine: ["node", FAKE_ENGINE], skillDirs: [SKILLS_DIR] }, async (h) => {
    const ev = ctxEvent("ses1", ["help me with memory recall"]);
    await h.contextHandler(ev);
    assert.deepEqual(
      logEntries().map((e) => e.argv),
      [
        A("skill-sync", "--dir", SKILLS_DIR),
        A("list", "--category", "skill", "--top", "2"),
        A("search", "help me with memory recall", "--category", "skill", "--top", "3"),
        A("search", "help me with memory recall", "--top", "10"),
      ],
    );
    // Floor block (top-2 by trust: alpha, beta) + the fact block. The per-turn
    // skill search found nothing beyond the floor, so no per-turn block.
    assert.equal(ev.system.length, 2);
    assert.ok(ev.system[0].text.startsWith("Skills available this session"));
    assert.ok(ev.system[0].text.includes("- alpha — memory recall, probe and related lookups"));
    assert.ok(ev.system[0].text.includes("- beta — memory add, update and remove writes"));
    assert.ok(!ev.system[0].text.includes("gamma"));
    assert.ok(ev.system[1].text.startsWith("Relevant stored memory"));
    assert.ok(ev.system[1].text.includes("[1001]"));

    // Same user message re-dispatched within the turn: zero engine calls.
    const ev1b = ctxEvent("ses1", ["help me with memory recall"]);
    await h.contextHandler(ev1b);
    assert.equal(logEntries().length, 4);
    assert.equal(ev1b.system.length, 0);
  });
});

test("second turn: only skills new to the turn are pushed (floor and per-turn share the seen set)", async () => {
  await withSetup({ db: DB, engine: ["node", FAKE_ENGINE], skillDirs: [SKILLS_DIR] }, async (h) => {
    await h.contextHandler(ctxEvent("ses1", ["help me with memory recall"]));
    const ev2 = ctxEvent("ses1", ["help me with memory recall", "formatting a report please"]);
    await h.contextHandler(ev2);
    // Turn 2 adds only the per-turn skill search + fact search (no re-sync,
    // no floor re-list).
    assert.deepEqual(logEntries().map((e) => e.argv).slice(4), [
      A("search", "formatting a report please", "--category", "skill", "--top", "3"),
      A("search", "formatting a report please", "--top", "10"),
    ]);
    // gamma is new to the turn → per-turn block; the floor's alpha/beta are
    // not re-pushed, and nothing fact-side matched the turn query matched no facts.
    assert.equal(ev2.system.length, 1);
    assert.ok(ev2.system[0].text.startsWith("Skills relevant to this turn"));
    assert.ok(ev2.system[0].text.includes("- gamma — formatting reports as markdown"));
    assert.ok(!ev2.system[0].text.includes("alpha"));
  });
});

test("injectPerTurn: false keeps the session-start floor only (no per-turn skill search)", async () => {
  await withSetup({ db: DB, engine: ["node", FAKE_ENGINE], skillDirs: [SKILLS_DIR], injectPerTurn: false }, async (h) => {
    const ev1 = ctxEvent("ses1", ["help me with memory recall"]);
    await h.contextHandler(ev1);
    assert.deepEqual(
      logEntries().map((e) => e.argv),
      [
        A("skill-sync", "--dir", SKILLS_DIR),
        A("list", "--category", "skill", "--top", "2"),
        A("search", "help me with memory recall", "--top", "4"), // one-shot fact injection (injectTop default 4)
      ],
    );
    assert.equal(ev1.system.length, 2); // floor + one-shot fact block
    // Second turn: nothing (one-shot already done, per-turn skill search off).
    const ev2 = ctxEvent("ses1", ["help me with memory recall", "formatting a report please"]);
    await h.contextHandler(ev2);
    assert.equal(logEntries().length, 3);
    assert.equal(ev2.system.length, 0);
  });
});

test("skill tools are registered only when skillDirs is set", async () => {
  await withSetup({ db: DB, engine: ["node", FAKE_ENGINE], skillDirs: [SKILLS_DIR] }, async (h) => {
    const names = h.tools.map((t) => t.name);
    assert.ok(names.includes("memory_skill_search"));
    assert.ok(names.includes("memory_skill_load"));
    assert.equal(h.tools.length, 16);
  });
});

test("memory_skill_search searches the skill category and returns skill ids", async () => {
  await withSetup({ db: DB, engine: ["node", FAKE_ENGINE], skillDirs: [SKILLS_DIR] }, async (h) => {
    const tool = h.tools.find((t) => t.name === "memory_skill_search");
    const out = await tool.execute({ query: "memory recall", top: 2 });
    assert.deepEqual(logEntries().map((e) => e.argv), [A("search", "memory recall", "--category", "skill", "--top", "2")]);
    assert.deepEqual(JSON.parse(out.content), [
      { id: "alpha", fact_id: 1, description: "memory recall, probe and related lookups" },
      { id: "beta", fact_id: 2, description: "memory add, update and remove writes" },
    ]);
  });
});

test("memory_skill_load resolves the skill-dir tag and reads SKILL.md", async () => {
  await withSetup({ db: DB, engine: ["node", FAKE_ENGINE], skillDirs: [SKILLS_DIR] }, async (h) => {
    const tool = h.tools.find((t) => t.name === "memory_skill_load");
    const ok = await tool.execute({ name: "gamma" });
    assert.equal(ok.content, readFileSync(join(SKILLS_DIR, "gamma", "SKILL.md"), "utf8"));
    assert.deepEqual(logEntries().map((e) => e.argv), [A("list", "--category", "skill", "--top", "1000")]);
    const missing = await tool.execute({ name: "nope" });
    assert.deepEqual(JSON.parse(missing.content), { error: "skill nope not found in the catalog" });
    const undir = await tool.execute({ name: "orphan" });
    assert.deepEqual(JSON.parse(undir.content), { error: "skill orphan has no skill-dir tag (was it added by skill-sync?)" });
  });
});

test("memory_skill_load truncates pathological bodies at SKILL_BODY_MAX", async () => {
  await withSetup({ db: DB, engine: ["node", FAKE_ENGINE], skillDirs: [SKILLS_DIR] }, async (h) => {
    const file = join(SKILLS_DIR, "delta", "SKILL.md");
    const original = readFileSync(file, "utf8");
    writeFileSync(file, "x".repeat(SKILL_BODY_MAX + 8_000));
    try {
      const tool = h.tools.find((t) => t.name === "memory_skill_load");
      const out = await tool.execute({ name: "delta" });
      assert.equal(out.content, "x".repeat(SKILL_BODY_MAX) + "\n[truncated]");
    } finally {
      writeFileSync(file, original);
    }
  });
});

test("memory_brief calls the engine, renders the block, and marks shown facts as seen", async () => {
  await withSetup({ db: DB, engine: ["node", FAKE_ENGINE] }, async (h) => {
    const tool = h.tools.find((t) => t.name === "memory_brief");
    const out = await tool.execute({ query: "project memory", budget: 50 }, { sessionID: "ses1" });
    assert.deepEqual(logEntries().map((e) => e.argv), [A("brief", "project memory", "--budget", "50")]);
    const lines = out.content.split("\n");
    assert.equal(lines[0], "brief: project memory (2 facts, ~43 tokens)");
    assert.ok(lines.includes("- [1001] user prefers concise answers about memory work"));
    assert.ok(lines.includes("- [1002] project database is local SQLite"));
    assert.ok(lines.includes("skills:"));
    assert.ok(lines.includes("- alpha — memory recall, probe and related lookups"));
    assert.ok(lines.includes("- beta — memory add, update and remove writes"));

    // A brief IS a recall: its shown facts are marked seen for the session, so
    // the per-turn injector must not re-push them.
    const ev = ctxEvent("ses1", ["user memory preferences"]);
    await h.contextHandler(ev);
    assert.deepEqual(logEntries().map((e) => e.argv).slice(1), [A("search", "user memory preferences", "--top", "10")]);
    assert.equal(ev.system.length, 0); // 1001 was already shown by the brief
  });
});

test("memory_brief renders the window marker when the budget truncates the pool", async () => {
  await withSetup({ db: DB, engine: ["node", FAKE_ENGINE] }, async (h) => {
    const tool = h.tools.find((t) => t.name === "memory_brief");
    const out = await tool.execute({ query: "project memory", budget: 15 });
    const lines = out.content.split("\n");
    assert.equal(lines[0], "brief: project memory (1 fact, ~12 tokens — 1 of 2 match, search has the rest)");
    assert.ok(lines.includes("- [1001] user prefers concise answers about memory work"));
    assert.ok(!lines.some((l) => l.includes("[1002]"))); // whole-fact drop: 12 + 8 = 20 > 15
    assert.ok(!lines.includes("skills:")); // skills lane gets no remaining budget
  });
});

test("memory_search passes since to the engine when set, omits it when unset", async () => {
  await withSetup({ db: DB, engine: ["node", FAKE_ENGINE] }, async (h) => {
    const tool = h.tools.find((t) => t.name === "memory_search");
    await tool.execute({ query: "project memory", since: "24h" });
    assert.deepEqual(logEntries().map((e) => e.argv), [A("search", "project memory", "--since", "24h", "--top", "5")]);
  });
});

test("memory_brief passes since to the engine", async () => {
  await withSetup({ db: DB, engine: ["node", FAKE_ENGINE] }, async (h) => {
    const tool = h.tools.find((t) => t.name === "memory_brief");
    await tool.execute({ query: "project memory", budget: 50, since: "2026-01-01" }, { sessionID: "ses1" });
    assert.deepEqual(logEntries().map((e) => e.argv), [A("brief", "project memory", "--since", "2026-01-01", "--budget", "50")]);
  });
});

test("memory_show calls the engine, passes the fact id, and marks the fact seen", async () => {
  await withSetup({ db: DB, engine: ["node", FAKE_ENGINE] }, async (h) => {
    const tool = h.tools.find((t) => t.name === "memory_show");
    const out = await tool.execute({ fact_id: 1001 }, { sessionID: "ses1" });
    assert.deepEqual(logEntries().map((e) => e.argv), [A("show", "1001")]);
    const f = JSON.parse(out.content);
    assert.equal(f.fact_id, 1001);
    assert.equal(f.content, "user prefers concise answers about memory work");
    assert.ok(Array.isArray(f.entities));
    assert.ok(!("hrr_vector" in f));

    // The full fact content lands in the conversation: the shown fact is
    // marked seen for the session, so the per-turn injector does not
    // re-surface it. The fake search for this query returns only 1001 —
    // shown by the show call — so nothing injects.
    const ev = ctxEvent("ses1", ["user memory preferences"]);
    await h.contextHandler(ev);
    assert.deepEqual(logEntries().map((e) => e.argv).slice(1), [A("search", "user memory preferences", "--top", "10")]);
    assert.equal(ev.system.length, 0); // 1001 was already shown by memory_show
  });
});

test("memory_show surfaces the unknown-id error", async () => {
  await withSetup({ db: DB, engine: ["node", FAKE_ENGINE] }, async (h) => {
    const tool = h.tools.find((t) => t.name === "memory_show");
    const out = await tool.execute({ fact_id: 99999 }, { sessionID: "ses1" });
    // The CLI's JSON error passes through un-rendered (the model reads it).
    assert.deepEqual(JSON.parse(out.content), { error: "fact_id 99999 not found" });
  });
});

test("memory_add_batch sends the facts JSON on stdin and passes the result through", async () => {
  await withSetup({ db: DB, engine: ["node", FAKE_ENGINE] }, async (h) => {
    const tool = h.tools.find((t) => t.name === "memory_add_batch");
    const out = await tool.execute({ facts: ["fact one", "fact two"] }, { sessionID: "ses1" });
    // Output contract (docs/batch.md): {added, duplicates, fact_ids} — passed through un-rendered.
    assert.deepEqual(JSON.parse(out.content), { added: 2, duplicates: 0, fact_ids: [9000, 9001] });
    assert.deepEqual(logEntries().map((e) => e.argv), [A("add-batch")]);
    // Empty facts is a client-side error: no engine call at all.
    const empty = await tool.execute({ facts: [] }, { sessionID: "ses1" });
    assert.deepEqual(JSON.parse(empty.content), { error: "facts must be a non-empty array" });
    assert.equal(logEntries().length, 1);
  });
});

test("memory_feedback passes a comma-joined fact_ids batch to the engine", async () => {
  await withSetup({ db: DB, engine: ["node", FAKE_ENGINE] }, async (h) => {
    const tool = h.tools.find((t) => t.name === "memory_feedback");
    const out = await tool.execute({ fact_ids: [1, 2, 3], rating: "helpful" }, { sessionID: "ses1" });
    assert.deepEqual(logEntries().map((e) => e.argv), [A("feedback", "1,2,3", "helpful")]);
    assert.deepEqual(JSON.parse(out.content), { applied: 3, results: [1, 2, 3].map((n) => ({ fact_id: n, rating: "helpful", old_trust: 0.5, new_trust: 0.6, ok: true })) });
    // Single fact_id stays on the original argv shape.
    await tool.execute({ fact_id: 7, rating: "unhelpful" }, { sessionID: "ses1" });
    assert.deepEqual(logEntries().map((e) => e.argv).slice(1), [A("feedback", "7", "unhelpful")]);
  });
});

test("memory_entities passes --top and returns the engine's bare array", async () => {
  await withSetup({ db: DB, engine: ["node", FAKE_ENGINE] }, async (h) => {
    const tool = h.tools.find((t) => t.name === "memory_entities");
    const out = await tool.execute({});
    // Output contract (docs/entities.md): a bare JSON array — one object per entity.
    assert.deepEqual(JSON.parse(out.content), [
      { name: "AcmeWidget", aliases: ["AW"], fact_count: 2 },
      { name: "User", aliases: [], fact_count: 1 },
    ]);
    assert.deepEqual(logEntries().map((e) => e.argv), [A("entities", "--top", "20")]);
    const one = await tool.execute({ top: 1 });
    assert.deepEqual(logEntries().map((e) => e.argv).slice(1), [A("entities", "--top", "1")]);
    assert.deepEqual(JSON.parse(one.content), [{ name: "AcmeWidget", aliases: ["AW"], fact_count: 2 }]);
  });
});

test("memory_drift calls the engine, renders the block, and marks shown facts as seen", async () => {
  await withSetup({ db: DB, engine: ["node", FAKE_ENGINE] }, async (h) => {
    const tool = h.tools.find((t) => t.name === "memory_drift");
    const out = await tool.execute({ project: "AcmeWidget", since: "2026-01-01", budget: 200 }, { sessionID: "ses1" });
    assert.deepEqual(logEntries().map((e) => e.argv), [A("drift", "AcmeWidget", "--since", "2026-01-01", "--budget", "200")]);
    const lines = out.content.split("\n");
    assert.equal(lines[0], "drift: AcmeWidget (anchor 2026-01-01; 2 of 2 plan, 2 of 2 state, ~47 tokens)");
    assert.equal(lines[1], "plan:");
    assert.ok(lines.includes("- [2001] AcmeWidget plan: crate loads happen at the dock before dawn (backed ×1 @ 0.75)"));
    assert.ok(lines.includes("- [2002] AcmeWidget policy: the ledger keeps status codes  [unbacked]"));
    assert.ok(lines.includes("state:"));
    assert.ok(lines.includes("- [1001] user prefers concise answers about memory work (2026-01-01 00:00:00)"));
    assert.ok(lines.some((l) => l.startsWith("unbacked: 1 of 2 plan facts")));

    // A drift IS a recall: its shown facts are marked seen for the session, so
    // the per-turn injector must not re-push them. The fake search for this
    // query returns only 1001 — shown by the drift pack — so nothing injects.
    const ev = ctxEvent("ses1", ["user memory preferences"]);
    await h.contextHandler(ev);
    assert.deepEqual(logEntries().map((e) => e.argv).slice(1), [A("search", "user memory preferences", "--top", "10")]);
    assert.equal(ev.system.length, 0); // 1001 was already shown by the drift
  });
});

test("memory_drift omits the optional flags when unset", async () => {
  await withSetup({ db: DB, engine: ["node", FAKE_ENGINE] }, async (h) => {
    const tool = h.tools.find((t) => t.name === "memory_drift");
    await tool.execute({ project: "AcmeWidget" }, { sessionID: "ses1" });
    // Default budget only — no since/top/support flags.
    assert.deepEqual(logEntries().map((e) => e.argv), [A("drift", "AcmeWidget", "--budget", "300")]);
  });
});

test("memory_drift surfaces the engine's structured error", async () => {
  process.env.TIBERUS_FAKE_FAIL = "drift";
  try {
    await withSetup({ db: DB, engine: ["node", FAKE_ENGINE] }, async (h) => {
      const tool = h.tools.find((t) => t.name === "memory_drift");
      const out = await tool.execute({ project: "AcmeWidget" }, { sessionID: "ses1" });
      // The CLI's JSON error passes through un-rendered (the model reads it).
      assert.deepEqual(JSON.parse(out.content), { error: "fake engine failure: drift" });
    });
  } finally {
    delete process.env.TIBERUS_FAKE_FAIL;
  }
});

test("session end: feedback helpful once per loaded skill (failed loads excluded, no re-send)", async () => {
  await withSetup({ db: DB, engine: ["node", FAKE_ENGINE], skillDirs: [SKILLS_DIR] }, async (h) => {
    const transcript = [
      { type: "user", id: "u1", text: "please use the alpha skill for this memory task" },
      { type: "skill", id: "ms1", skill: "skl-alpha", name: "alpha", text: "ALPHA BODY" },
      {
        type: "assistant",
        id: "a1",
        content: [
          { type: "tool", name: "memory_skill_load", state: { status: "completed", input: { name: "beta" } } },
          { type: "tool", name: "memory_skill_load", state: { status: "error", input: { name: "delta" } } },
          { type: "tool", name: "skill", state: { status: "completed", input: { id: "gamma" } } },
        ],
      },
    ];
    h.setTranscript("ses1", transcript);
    const turn = (id: string) => ({ id, created: 0, type: "session.execution.succeeded", durable: {}, data: { sessionID: "ses1" } });
    h.pushEvent(turn("e1"));
    const entries = await waitForLog(5);
    assert.deepEqual(
      entries.map((e) => e.argv),
      [
        A("extract"),
        A("list", "--category", "skill", "--top", "1000"),
        A("feedback", "1", "helpful"), // alpha — dedicated transcript skill message
        A("feedback", "2", "helpful"), // beta — completed memory_skill_load part
        A("feedback", "3", "helpful"), // gamma — completed built-in skill part
      ],
    );
    // Second completed turn: new user message re-extracts, but no feedback
    // re-send for the already-trained skills (delta, the failed load, stays out).
    h.setTranscript("ses1", [...transcript, { type: "user", id: "u2", text: "thanks, that memory skill worked great" }]);
    h.pushEvent(turn("e2"));
    const entries2 = await waitForLog(6);
    assert.deepEqual(entries2[5].argv, A("extract"));
    assert.ok(!entries2.slice(5).some((e) => e.argv[2] === "feedback" || e.argv[2] === "list"));
  });
});

test("session end, options unset: a built-in skill load triggers no skill-catalog calls", async () => {
  // Regression: the feedback loop must be gated on skillDirs — otherwise an
  // off-feature plugin still lists the catalog and trains trust on skill
  // loads from the harness's own (unindexed-here) skills.
  await withSetup({ db: DB, engine: ["node", FAKE_ENGINE] }, async (h) => {
    h.setTranscript("ses1", [
      { type: "user", id: "u1", text: "please use the opencode skill for this task" },
      { type: "skill", id: "ms1", skill: "skl-opencode", name: "opencode", text: "BODY" },
    ]);
    h.pushEvent({ id: "e1", created: 0, type: "session.execution.succeeded", durable: {}, data: { sessionID: "ses1" } });
    const entries = await waitForLog(1);
    assert.deepEqual(entries.map((e) => e.argv), [A("extract")]);
    // The turn handler is done once the extract landed; no list/feedback may follow.
    await new Promise((r) => setTimeout(r, 200));
    assert.ok(!logEntries().some((e) => e.argv[2] === "list" || e.argv[2] === "feedback"));
  });
});

test("skill-sync failure is swallowed: dispatch continues and the floor still lands", async () => {
  await withSetup({ db: DB, engine: ["node", FAKE_ENGINE], skillDirs: [SKILLS_DIR] }, async (h) => {
    process.env.TIBERUS_FAKE_FAIL = "skill-sync";
    try {
      const ev = ctxEvent("ses1", ["help me with memory recall"]);
      await h.contextHandler(ev); // must not throw — memory never breaks dispatch
      assert.ok(ev.system.some((s) => s.text.startsWith("Skills available this session")));
      const argvs = logEntries().map((e) => e.argv);
      assert.deepEqual(argvs[0], A("skill-sync", "--dir", SKILLS_DIR)); // the attempted (failed) sync
      assert.ok(argvs.some((a) => a[2] === "list" && a[4] === "skill")); // floor list still ran
    } finally {
      delete process.env.TIBERUS_FAKE_FAIL;
    }
  });
});
