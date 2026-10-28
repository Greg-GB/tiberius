#!/usr/bin/env node
/**
 * Fake tiberius CLI engine for unit tests. Mimics the `python3 -m tiberius.cli`
 * contract (JSON on stdout; exit 1 with a JSON error on the failure path)
 * using deterministic in-memory fixtures — no Python, no DB, no network.
 *
 * Every invocation is appended as one JSON line to $TIBERUS_FAKE_LOG so tests
 * can assert the exact engine calls the plugin wiring makes.
 *
 *   TIBERUS_FAKE_LOG         log file for invocations (JSON lines)
 *   TIBERUS_FAKE_SKILLS_DIR  fixture dir placed in skill-dir tags
 *   TIBERUS_FAKE_FAIL        if set to a command name, that command fails with
 *                            {"error": ...} + exit 1 (failure-swallowing tests)
 *
 * Fixtures mirror the SKILL.md dirs the test writes under TIBERUS_FAKE_SKILLS_DIR
 * (alpha/beta/gamma/delta), plus an "orphan" catalog row with no skill-dir tag
 * (exercises memory_skill_load's error path). list/search order by
 * (keyword-hit count, trust_score DESC); `list` by trust_score DESC — the CLI
 * contract the plugin relies on.
 */
import { appendFileSync } from "node:fs";

const argv = process.argv.slice(2);
if (process.env.TIBERUS_FAKE_LOG) {
  appendFileSync(process.env.TIBERUS_FAKE_LOG ?? "", JSON.stringify({ argv }) + "\n");
}

const skillsDir = process.env.TIBERUS_FAKE_SKILLS_DIR ?? "";
const SKILLS = [
  { fact_id: 1, content: "alpha: memory recall, probe and related lookups", trust_score: 0.95, tags: `skill-id:alpha, skill-dir:${skillsDir}/alpha` },
  { fact_id: 2, content: "beta: memory add, update and remove writes", trust_score: 0.85, tags: `skill-id:beta, skill-dir:${skillsDir}/beta` },
  { fact_id: 3, content: "gamma: formatting reports as markdown", trust_score: 0.6, tags: `skill-id:gamma, skill-dir:${skillsDir}/gamma` },
  { fact_id: 4, content: "delta: shell command execution", trust_score: 0.4, tags: "skill-id:delta, skill-dir:${skillsDir}/delta" },
  { fact_id: 5, content: "orphan: catalog row lacking a dir tag", trust_score: 0.2, tags: "skill-id:orphan" },
];
const FACTS = [
  { fact_id: 1001, content: "user prefers concise answers about memory work", trust_score: 0.9, tags: "" },
  { fact_id: 1002, content: "project database is local SQLite", trust_score: 0.7, tags: "" },
];

function fail(cmd) {
  console.log(JSON.stringify({ error: `fake engine failure: ${cmd}` }));
  process.exit(1);
}

// Single-letter tokens would substring-match everything; the real FTS
// stopwording is irrelevant here — just keep the fixtures deterministic.
function tokens(q) {
  return q.toLowerCase().split(/\s+/).filter((t) => t.length >= 3);
}
function hitCount(row, toks) {
  const c = row.content.toLowerCase();
  return toks.reduce((n, t) => (c.includes(t) ? n + 1 : n), 0);
}
function ranked(rows, query, top) {
  const toks = tokens(query);
  return rows
    .map((r) => [r, hitCount(r, toks)])
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1] || b[0].trust_score - a[0].trust_score)
    .slice(0, top)
    .map(([r]) => r);
}
function row(r) {
  return { fact_id: r.fact_id, content: r.content, category: r.category, trust_score: r.trust_score, tags: r.tags };
}

// argv: [--db PATH] <cmd> <args...>
const dbIdx = argv.indexOf("--db");
if (dbIdx >= 0) argv.splice(dbIdx, 2);
const [cmd, ...rest] = argv;
const flag = (name) => {
  const j = rest.indexOf(name);
  return j >= 0 ? rest[j + 1] : undefined;
};
const takeTop = () => {
  const t = flag("--top");
  return t ? parseInt(t, 10) : 5;
};

switch (cmd) {
  case "skill-sync": {
    if (process.env.TIBERUS_FAKE_FAIL === "skill-sync") fail("skill-sync");
    const dirs = [];
    for (let k = 0; k < rest.length; k++) if (rest[k] === "--dir") dirs.push(rest[k + 1]);
    // Same report shape as the real CLI (cli.py _skill_sync).
    console.log(JSON.stringify({ dirs, skills: SKILLS.length - 1, added: [], updated: [], unchanged: [], pruned: [], skipped: [] }));
    break;
  }
  case "list": {
    const isSkill = flag("--category") === "skill";
    const rows = (isSkill ? SKILLS : FACTS).slice(0, takeTop()); // fixtures are trust-sorted
    console.log(JSON.stringify({ results: rows.map(row), count: rows.length }));
    break;
  }
  case "search": {
    const isSkill = flag("--category") === "skill";
    const rows = ranked(isSkill ? SKILLS : FACTS, rest[0] ?? "", takeTop());
    console.log(JSON.stringify({ results: rows.map(row), count: rows.length }));
    break;
  }
  case "feedback": {
    if (process.env.TIBERUS_FAKE_FAIL === "feedback") fail("feedback");
    const [fid, rating] = rest;
    console.log(JSON.stringify({ fact_id: Number(fid), rating, trust_delta: rating === "helpful" ? 0.1 : -0.1 }));
    break;
  }
  case "extract": {
    let input = "";
    process.stdin.setEncoding("utf8");
    for await (const chunk of process.stdin) input += chunk;
    const msgs = input.trim() ? JSON.parse(input) : [];
    console.log(JSON.stringify({ extracted: msgs.length }));
    break;
  }
  default:
    fail(cmd ?? "(no command)");
}
