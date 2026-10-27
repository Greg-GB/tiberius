/**
 * Unit tests for the engine-subprocess layer (runEngine / callEngine /
 * messageText). The "engine" is `cat` or a shell one-liner standing in for
 * `python3 -m tiberius.cli` with the same output contract (JSON on stdout,
 * exit 1 with a JSON error on the failure path), so the interesting
 * behaviors — stdin flow, arg order, structured-error surfacing, timeout —
 * are exercised without Python and without network. No test may call out
 * (repo rule: local-memory, nothing phones home).
 *
 * Run: npm test  (node --test with type stripping; Node >= 22.6)
 */
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { callEngine, freshUserMsgs, messageText, runEngine } from "../index.ts";

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
