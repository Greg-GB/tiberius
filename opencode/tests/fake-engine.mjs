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
  { fact_id: 4, content: "delta: shell command execution", trust_score: 0.4, tags: `skill-id:delta, skill-dir:${skillsDir}/delta` },
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
  case "brief": {
    // Emulates cli.py brief (docs/brief.md): pool of non-skill matches, skills
    // lane capped by --skills, char/4 whole-fact budget fill (facts lane first,
    // skills the remainder — the real CLI's tiered fill), window marker via
    // total (the full matching pool). No entity graph in the fixtures, so
    // entity_only is always 0 here.
    const query = rest[0] ?? "";
    const budget = flag("--budget") ? parseInt(flag("--budget"), 10) : 300;
    const skillsCap = flag("--skills") ? parseInt(flag("--skills"), 10) : 2;
    const pool = ranked(FACTS, query, 20);
    const skillMatches = ranked(SKILLS, query, 20).slice(0, skillsCap);
    const facts = [];
    const skills = [];
    let used = 0;
    for (const f of pool) {
      const t = Math.ceil(f.content.length / 4);
      if (used + t > budget) continue; // whole-fact drop
      facts.push(row(f));
      used += t;
    }
    for (const s of skillMatches) {
      const t = Math.ceil(s.content.length / 4);
      if (used + t > budget) continue;
      skills.push(row(s));
      used += t;
    }
    console.log(JSON.stringify({ query, total: pool.length, entity_only: 0, count: facts.length, tokens: used, budget, facts, skills }));
    break;
  }
  case "drift": {
    if (process.env.TIBERUS_FAKE_FAIL === "drift") fail("drift");
    // Emulates cli.py drift (docs/drift.md): two-sided pack — plan facts with
    // echo markers (2001 backed, 2002 unbacked), state facts (the FACTS
    // fixtures + created_at), split budget fill (budget//2 per side), and the
    // unbacked supplemental (omitted when empty — the fixtures always have an
    // unbacked claim, so it is always present here).
    const project = rest[0] ?? "";
    const budget = flag("--budget") ? parseInt(flag("--budget"), 10) : 300;
    const anchor = flag("--since") ?? "2025-12-31 00:00:00";
    const planAll = [
      { fact_id: 2001, content: "AcmeWidget plan: crate loads happen at the dock before dawn", category: "project", trust_score: 0.9, support: { count: 1, best: 0.75, echoes: [1001] } },
      { fact_id: 2002, content: "AcmeWidget policy: the ledger keeps status codes", category: "project", trust_score: 0.8, support: null },
    ];
    const stateAll = FACTS.map((f) => ({ ...f, created_at: "2026-01-01 00:00:00" }));
    const fill = (facts, b) => {
      const out = [];
      let used = 0;
      for (const f of facts) {
        const t = Math.ceil(f.content.length / 4);
        if (used + t > b) continue; // whole-fact drop
        out.push(f);
        used += t;
      }
      return { out, used };
    };
    const half = Math.floor(budget / 2);
    const p = fill(planAll, half);
    const s = fill(stateAll, budget - half);
    const out = {
      project,
      anchor,
      budget,
      tokens: p.used + s.used,
      plan: { total: planAll.length, count: p.out.length, facts: p.out },
      state: { total: stateAll.length, count: s.out.length, facts: s.out },
    };
    const unbacked = planAll.filter((f) => f.support === null);
    if (unbacked.length) {
      out.unbacked = unbacked;
      out.note = `${unbacked.length} of ${planAll.length} plan facts have no state echo >= 0.15 since ${anchor} — verify before treating it as drift`;
    }
    console.log(JSON.stringify(out));
    break;
  }
  case "feedback": {
    if (process.env.TIBERUS_FAKE_FAIL === "feedback") fail("feedback");
    const [fid, rating] = rest;
    console.log(JSON.stringify({ fact_id: Number(fid), rating, trust_delta: rating === "helpful" ? 0.1 : -0.1 }));
    break;
  }
  case "show": {
    if (process.env.TIBERUS_FAKE_FAIL === "show") fail("show");
    // Emulates cli.py show (docs/show.md): one fact by id — the full row plus
    // entities; unknown id is the CLI's JSON error + exit 1.
    const arg = rest[0] ?? "";
    const hit = FACTS.find((f) => f.fact_id === Number(arg));
    if (!hit) {
      console.log(JSON.stringify({ error: `fact_id ${arg} not found` }));
      process.exit(1);
    }
    console.log(JSON.stringify({ fact: { fact_id: hit.fact_id, content: hit.content, category: "general", tags: hit.tags, trust_score: hit.trust_score, retrieval_count: 0, helpful_count: 0, created_at: "2026-01-01 00:00:00", updated_at: "2026-01-01 00:00:00", entities: [{ name: "User", aliases: [] }] } }));
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
