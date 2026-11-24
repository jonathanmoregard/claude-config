// Rabbit-hole trigger for advice-refine-test-loop (../references/rabbit-hole.md).
//
// Usage: node convergence-check.mts <ledger.tsv> <token>/r1 ... <token>/rN
//
// The labels are this close-out's rounds as the ledger's run column holds them
// (../references/ledger.md, column 3): one token, `<commit>-<HHMM>`, picked
// once at the Entry Gate, then r1..rN in order. Anything else exits 11: two
// tokens, a gap, a round out of place, a token without its minute. Rows of any
// other run are ignored, so a re-invoked close-out, under its own token, counts
// its own rounds.
//
// Every row of a named run is checked against ledger.md's ten columns in one
// pass, and every problem is listed on stderr (`<ledger>:<line>: ...`) before
// the exit: 12 when the only problems are in `where`, 11 otherwise.
//   date       YYYY-MM-DD
//   source     artl
//   check      A, a class id (D3, G12), variant or reframe
//   trigger    set
//   where      /abs/path:<int> (0 = whole file); `-` on a none row; set on a
//              reframe row (copied from its thread, not parsed)
//   claim      set
//   outcome    reproduced, unconfirmed, refuted, advisory or none
//   fixed      yes, no, deferred, open or -
//   fp_reason  set on a refuted row, `-` on any other
//
// c(N) is round N's new defect findings: rows with outcome `reproduced` or
// `unconfirmed`, except check `reframe`, one per row. Two findings on one
// `where` are two; only a row repeated verbatim, same `where` and claim, counts
// once. A re-report of an earlier finding gets no row at all (ledger.md rule
// 8), so nothing here matches findings by meaning.
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
// Exit: 0 hold, 10 fire, 11 input error (usage, unreadable ledger, the
// labels, a run with no rows, a row of a named run without ten fields or with
// a bad column, anything thrown), 12 a malformed `where` and nothing else
// wrong. None of 10-12 is a code node returns on its own: node exits 1 when
// it cannot load this file (no type stripping, a syntax error) or on an
// uncaught throw, so 1 is never read as a verdict.

import { readFileSync } from "node:fs";
import process, { argv, exit, stderr, stdout } from "node:process";

const HOLD = 0;
const FIRE = 10;
const INPUT = 11;
const WHERE = 12;

const COUNTED: ReadonlySet<string> = new Set(["reproduced", "unconfirmed"]);
const OUTCOMES: ReadonlySet<string> = new Set([...COUNTED, "refuted", "advisory", "none"]);
const FIXED: ReadonlySet<string> = new Set(["yes", "no", "deferred", "open", "-"]);
const LABEL = /^([0-9a-f]{7,40}-\d{4})\/r([1-9]\d*)$/;
const CHECK = /^(A|[A-Z]\d+|variant|reframe)$/;
const DATE = /^\d{4}-\d{2}-\d{2}$/;
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

interface Problem {
  readonly column: string;
  readonly text: string;
}

function die(lines: readonly string[], code = INPUT): never {
  stderr.write(lines.map((l) => `convergence-check: ${l}\n`).join(""));
  exit(code);
}

function parseWhere(where: string): Finding | null {
  const m = /^(\/.+):(\d+)$/.exec(where);
  if (m === null || m[1] === undefined || m[2] === undefined) return null;
  return { where, file: m[1], line: Number(m[2]) };
}

function checkLabels(runs: readonly string[]): void {
  const problems: string[] = [];
  let token: string | undefined;
  runs.forEach((run, i) => {
    const m = LABEL.exec(run);
    if (m === null || m[1] === undefined) {
      problems.push(`${run}: not <commit>-<HHMM>/r<N> (ledger.md, column 3)`);
      return;
    }
    token ??= m[1];
    if (m[1] !== token) problems.push(`${run}: token ${m[1]} is not ${token}; one close-out, one token`);
    if (Number(m[2]) !== i + 1) problems.push(`${run}: given as round ${i + 1}; pass r1..rN in order`);
  });
  if (problems.length > 0) die(problems);
}

function rowProblems(f: readonly string[]): Problem[] {
  const [date = "", source = "", , check = "", trigger = "", where = "", claim = "", outcome = "",
    fixed = "", fp = ""] = f;
  const out: Problem[] = [];
  const bad = (column: string, value: string, want: string): void => {
    out.push({ column, text: `${column} ${JSON.stringify(value)} is not ${want}` });
  };
  if (!DATE.test(date)) bad("date", date, "YYYY-MM-DD");
  if (source !== "artl") bad("source", source, "artl");
  if (!CHECK.test(check)) bad("check", check, "A, a class id, variant or reframe");
  if (trigger === "") bad("trigger", trigger, "set");
  if (claim === "") bad("claim", claim, "set");
  if (!OUTCOMES.has(outcome)) bad("outcome", outcome, `one of ${[...OUTCOMES].join(", ")}`);
  if (!FIXED.has(fixed)) bad("fixed", fixed, `one of ${[...FIXED].join(", ")}`);
  if (outcome === "refuted" && (fp === "" || fp === "-")) bad("fp_reason", fp, "a reason, on a refuted row");
  if (outcome !== "refuted" && fp !== "-") bad("fp_reason", fp, "-, on a row that is not refuted");
  if (check === "reframe") {
    if (where === "") bad("where", where, "set");
  } else if (outcome === "none") {
    if (where !== "-") bad("where", where, "-, on a none row");
  } else if (parseWhere(where) === null) {
    bad("where", where, "/abs/path:<line>");
  }
  return out;
}

function readRounds(ledger: string, runs: readonly string[]): Round[] {
  const rounds = new Map<string, Round>(runs.map((run) => [run, { run, rows: 0, sites: new Map() }]));
  const tokens = new Set(runs.map((run) => run.split("/")[0]));
  let text: string;
  try {
    text = readFileSync(ledger, "utf8");
  } catch (e) {
    die([`cannot read ${ledger}: ${e instanceof Error ? e.message : String(e)}`]);
  }
  const problems: Problem[] = [];
  text.split("\n").forEach((line, i) => {
    if (line === "") return;
    const f = line.split("\t");
    const round = rounds.get(f[2] ?? "");
    if (round === undefined) {
      // This close-out's token with a malformed round (T/R2, T/r02): a typo that would drop the row.
      const [tok = "", rnd = ""] = (f[2] ?? "").split("/");
      if (tokens.has(tok) && !/^r[1-9][0-9]*$/.test(rnd)) {
        problems.push({ column: "run", text: `${ledger}:${i + 1}: run ${f[2]} is not <token>/r<N>` });
      }
      return;
    }
    round.rows++;
    const at = `${ledger}:${i + 1}`;
    if (f.length !== 10) {
      problems.push({ column: "row", text: `${at}: not ten fields` });
      return;
    }
    const found = rowProblems(f);
    problems.push(...found.map((p) => ({ column: p.column, text: `${at}: ${p.text}` })));
    const [, , , check, , where = "", claim = "", outcome = ""] = f;
    const finding = parseWhere(where);
    if (found.length === 0 && check !== "reframe" && COUNTED.has(outcome) && finding !== null) {
      round.sites.set(`${where}\t${claim}`, finding);
    }
  });
  if (problems.length > 0) {
    die(problems.map((p) => p.text), problems.every((p) => p.column === "where") ? WHERE : INPUT);
  }
  for (const round of rounds.values()) {
    if (round.rows === 0) die([`no ledger rows for run ${round.run}`]);
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
    die(["usage: convergence-check.mts <ledger.tsv> <token>/r1 ... <token>/rN"]);
  }
  checkLabels(runs);
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
  return verdict.startsWith("fire") ? FIRE : HOLD;
}

try {
  process.exitCode = main(argv.slice(2));
} catch (e) {
  die([`unexpected error: ${e instanceof Error ? e.message : String(e)}`]);
}
