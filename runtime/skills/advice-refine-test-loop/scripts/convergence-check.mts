// Rabbit-hole trigger for advice-refine-test-loop (../references/rabbit-hole.md).
//
// Usage: node convergence-check.mts <ledger.tsv> <run>...
//
// Each <run> is one round of THIS close-out, as written in the ledger's run
// column (../references/ledger.md), in round order: round 1's label first.
// Rounds are numbered by that order, never by a label's r-suffix, and rows of
// any run not named are ignored, so a re-invoked close-out counts its own.
//
// c(N) is round N's new defect findings: rows with outcome `reproduced` or
// `unconfirmed`, except check `reframe`, one per row (ledger.md: one row per
// finding). Two findings on one `where` are two; only a row repeated verbatim,
// same `where` and claim, is one finding written twice and counts once.
//
// Every row of a named run carries one of ledger.md's outcomes (or `design`,
// on older rows). Anything else, such as the sweep's `confirmed` or a typo,
// exits 2 naming the row: it would otherwise drop out of c unseen.
//
// `where` must be the finding's verdict-schema file and line, `/abs/path:<int>`,
// on every row of a named run except `none` and `reframe` rows. Anything else
// exits 3 naming the row, with no verdict. Line 0 is a whole-file finding.
//
// Output, tab-separated:
//   round <N> <run> c=<c>    one line per round
//   recur <file> <rounds>    each file with findings in two or more rounds
//   fire|hold <reason>       the verdict, always the last line
// At the last round N it fires when N >= 2, c(N) > 0, and either
//   count  c(N) >= c(N-1), or
//   near   a round-N finding lies in the same file as a round N-1 finding
//          and within 50 lines of it; a line-0 finding matches its whole
//          file. The reason names the closest pair per file.
//
// Exit: 0 hold, 1 fire, 2 input error (usage, unreadable ledger, a repeated
// run, a run with no rows, a row of a named run without ten fields or with an
// unknown outcome),
// 3 a malformed `where` in a named run.

import { readFileSync } from "node:fs";
import process, { argv, exit, stderr, stdout } from "node:process";

const COUNTED: ReadonlySet<string> = new Set(["reproduced", "unconfirmed"]);
const OUTCOMES: ReadonlySet<string> = new Set([...COUNTED, "refuted", "advisory", "none", "design"]);
const NEAR = 50;

interface Finding {
  readonly where: string;
  readonly file: string;
  // 0: the whole file.
  readonly line: number;
}

interface Round {
  readonly run: string;
  rows: number;
  // One entry per finding, keyed by `where` and claim.
  readonly sites: Map<string, Finding>;
}

interface Pair {
  readonly now: Finding;
  readonly before: Finding;
  readonly apart: number | null;
}

function die(message: string, code = 2): never {
  stderr.write(`convergence-check: ${message}\n`);
  exit(code);
}

function parseWhere(where: string): Finding | null {
  const m = /^(\/.+):(\d+)$/.exec(where);
  if (m === null || m[1] === undefined || m[2] === undefined) return null;
  return { where, file: m[1], line: Number(m[2]) };
}

function readRounds(ledger: string, runs: readonly string[]): Round[] {
  const rounds = new Map<string, Round>();
  for (const run of runs) {
    if (rounds.has(run)) die(`run ${run} given twice`);
    rounds.set(run, { run, rows: 0, sites: new Map() });
  }
  let text: string;
  try {
    text = readFileSync(ledger, "utf8");
  } catch (e) {
    die(`cannot read ${ledger}: ${e instanceof Error ? e.message : String(e)}`);
  }
  text.split("\n").forEach((line, i) => {
    if (line === "") return;
    const f = line.split("\t");
    const round = rounds.get(f[2] ?? "");
    if (round === undefined) return;
    if (f.length !== 10) die(`${ledger}:${i + 1}: not ten fields`);
    round.rows++;
    const [, , , check, , where = "", claim = "", outcome = ""] = f;
    if (!OUTCOMES.has(outcome)) {
      die(`${ledger}:${i + 1}: outcome ${JSON.stringify(outcome)} is not one of ${[...OUTCOMES].join(", ")}`);
    }
    if (check === "reframe" || outcome === "none") return;
    const finding = parseWhere(where);
    if (finding === null) {
      die(`${ledger}:${i + 1}: where ${JSON.stringify(where)} is not /abs/path:<line>`, 3);
    }
    if (COUNTED.has(outcome)) round.sites.set(`${where}\t${claim}`, finding);
  });
  for (const round of rounds.values()) {
    if (round.rows === 0) die(`no ledger rows for run ${round.run}`);
  }
  return [...rounds.values()];
}

// The closest near pair per file, ordered by file; line pairs before
// whole-file ones.
function nearPairs(now: Round, before: Round): Pair[] {
  const best = new Map<string, Pair>();
  const rank = (p: Pair): number => p.apart ?? Number.POSITIVE_INFINITY;
  for (const a of now.sites.values()) {
    for (const b of before.sites.values()) {
      if (a.file !== b.file) continue;
      const apart = a.line === 0 || b.line === 0 ? null : Math.abs(a.line - b.line);
      if (apart !== null && apart > NEAR) continue;
      const pair = { now: a, before: b, apart };
      const held = best.get(a.file);
      if (held === undefined || rank(pair) < rank(held)) best.set(a.file, pair);
    }
  }
  return [...best.values()].sort((p, q) => p.now.file.localeCompare(q.now.file));
}

function main(args: readonly string[]): number {
  const [ledger, ...runs] = args;
  if (ledger === undefined || runs.length === 0) {
    die("usage: convergence-check.mts <ledger.tsv> <run>...");
  }
  const rounds = readRounds(ledger, runs);
  const out: string[] = rounds.map((r, i) => `round ${i + 1}\t${r.run}\tc=${r.sites.size}`);

  const seen = new Map<string, number[]>();
  rounds.forEach((r, i) => {
    for (const file of new Set([...r.sites.values()].map((f) => f.file))) {
      seen.set(file, [...(seen.get(file) ?? []), i + 1]);
    }
  });
  for (const [file, at] of [...seen].sort(([a], [b]) => a.localeCompare(b))) {
    if (at.length >= 2) out.push(`recur\t${file}\t${at.join(",")}`);
  }

  const n = rounds.length;
  const last = rounds[n - 1];
  const prev = rounds[n - 2];
  let verdict: string;
  if (last === undefined || prev === undefined) {
    verdict = "hold\tfewer than two rounds";
  } else {
    const c = last.sites.size;
    const cPrev = prev.sites.size;
    const reasons: string[] = [];
    if (c > 0 && c >= cPrev) reasons.push(`count: c(${n})=${c} >= c(${n - 1})=${cPrev}`);
    for (const p of nearPairs(last, prev)) {
      const how = p.apart === null ? "whole file" : `${p.apart} lines`;
      reasons.push(`near: ${p.now.where} (round ${n}) ~ ${p.before.where} (round ${n - 1}), ${how}`);
    }
    if (reasons.length > 0) {
      verdict = `fire\t${reasons.join("; ")}`;
    } else if (c === 0) {
      verdict = `hold\tc(${n})=0`;
    } else {
      verdict = `hold\tc(${n})=${c} < c(${n - 1})=${cPrev}, no finding within ${NEAR} lines of the last round's`;
    }
  }
  out.push(verdict);
  stdout.write(`${out.join("\n")}\n`);
  return verdict.startsWith("fire") ? 1 : 0;
}

process.exitCode = main(argv.slice(2));
