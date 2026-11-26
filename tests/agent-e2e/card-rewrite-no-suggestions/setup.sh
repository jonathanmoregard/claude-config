#!/usr/bin/env bash
# A TypeScript flashcard tool: suggest.ts asks `claude -p --output-format json`
# for three rewrites of a card (forward, reverse, cloze) as a JSON object in
# the reply text, then digs the object out of `.result` and maps keys and
# fields through alias tables that two earlier commits already widened. The
# model has drifted again (a new wrapper key, a capitalised strategy, new field
# names), so every rewrite is silently dropped. All repo-owned.
set -euo pipefail
WORK="${E2E_WORK:?}"
git -C "$WORK" init -q -b main
git -C "$WORK" config user.email e2e@example.invalid
git -C "$WORK" config user.name e2e
mkdir -p "$WORK/src" "$WORK/test" "$WORK/debug"

cat > "$WORK/package.json" <<'JSON'
{
  "name": "card-polish",
  "version": "0.3.0",
  "type": "module",
  "scripts": { "test": "node --test test/*.test.ts" }
}
JSON

cat > "$WORK/src/prompt.ts" <<'TS'
export type Strategy = "forward" | "reverse" | "cloze";
export const STRATEGIES: Strategy[] = ["forward", "reverse", "cloze"];

export function buildPrompt(front: string, back: string): string {
  return `Rewrite this flashcard three ways: "forward" (same direction, clearer),
"reverse" (ask for the front given the back) and "cloze" (fill in the blank).

<front>${front}</front>
<back>${back}</back>

Answer with a JSON object keyed by strategy, each value {"question": "...", "answer": "..."}.`;
}
TS

cat > "$WORK/src/parse.ts" <<'TS'
import { STRATEGIES, type Strategy } from "./prompt.ts";

export interface Suggestion { strategy: Strategy; question: string; answer: string }

// First JSON object anywhere in the text: survives prose and ``` fences.
export function extractJsonObject(text: string): Record<string, unknown> | null {
  for (let i = text.indexOf("{"); i !== -1; i = text.indexOf("{", i + 1)) {
    for (let j = text.lastIndexOf("}"); j > i; j = text.lastIndexOf("}", j - 1)) {
      try {
        const v = JSON.parse(text.slice(i, j + 1));
        if (v && typeof v === "object" && !Array.isArray(v)) return v;
      } catch { /* try a shorter span */ }
    }
  }
  return null;
}

const WRAPPERS = ["candidates", "rewrites"];
const QUESTION_KEYS = ["question", "front", "prompt"];
const ANSWER_KEYS = ["answer", "back"];

function field(entry: Record<string, unknown>, names: string[]): string | null {
  for (const n of names) {
    const v = entry[n];
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  return null;
}

export function parseSuggestions(text: string): Suggestion[] {
  let obj: unknown = extractJsonObject(text);
  if (!obj) return [];
  for (const w of WRAPPERS) {
    if (w in (obj as Record<string, unknown>)) { obj = (obj as Record<string, unknown>)[w]; break; }
  }
  const out: Suggestion[] = [];
  for (const [key, entry] of Object.entries(obj as Record<string, unknown>)) {
    const strategy = key.trim().toLowerCase() as Strategy;
    if (!STRATEGIES.includes(strategy) || !entry || typeof entry !== "object") continue;
    const question = field(entry as Record<string, unknown>, QUESTION_KEYS);
    const answer = field(entry as Record<string, unknown>, ANSWER_KEYS);
    if (question && answer) out.push({ strategy, question, answer });
  }
  return out;
}
TS

cat > "$WORK/src/suggest.ts" <<'TS'
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { writeFile } from "node:fs/promises";
import { buildPrompt } from "./prompt.ts";
import { parseSuggestions, type Suggestion } from "./parse.ts";

const run = promisify(execFile);

export async function suggest(front: string, back: string): Promise<Suggestion[]> {
  const { stdout } = await run("claude", [
    "-p", buildPrompt(front, back),
    "--output-format", "json",
    "--model", "haiku",
    "--tools", "",
    "--no-session-persistence",
  ], { timeout: 120_000 });
  const envelope = JSON.parse(stdout) as { result: string; is_error: boolean };
  if (envelope.is_error) throw new Error("claude reported an error");
  if (process.env.CARD_POLISH_DEBUG) await writeFile("debug/last-response.txt", envelope.result);
  return parseSuggestions(envelope.result);
}
TS

cat > "$WORK/test/parse.test.ts" <<'TS'
import { test } from "node:test";
import assert from "node:assert/strict";
import { parseSuggestions } from "../src/parse.ts";

test("documented shape", () => {
  const text = JSON.stringify({
    forward: { question: "Capital of France?", answer: "Paris" },
    reverse: { question: "Paris is the capital of?", answer: "France" },
    cloze: { question: "___ is the capital of France.", answer: "Paris" },
  });
  assert.equal(parseSuggestions(text).length, 3);
});
TS

# History: the reader started bare, then two fixes each widened it.
FULL_FIELDS='const QUESTION_KEYS = ["question", "front", "prompt"];'
FULL_ANSWERS='const ANSWER_KEYS = ["answer", "back"];'
FULL_WRAP='const WRAPPERS = ["candidates", "rewrites"];'
swap() { python3 -c 'import sys; p,a,b=sys.argv[1:]; s=open(p).read(); open(p,"w").write(s.replace(a,b))' "$WORK/src/parse.ts" "$1" "$2"; }
commit() { GIT_AUTHOR_DATE="$1" GIT_COMMITTER_DATE="$1" git -C "$WORK" commit -qam "$2"; }
swap "$FULL_FIELDS" 'const QUESTION_KEYS = ["question"];'
swap "$FULL_ANSWERS" 'const ANSWER_KEYS = ["answer"];'
swap "$FULL_WRAP" 'const WRAPPERS: string[] = [];'
git -C "$WORK" add -A
commit 2026-10-02T10:00:00 "feat: three rewrite suggestions per card via claude -p"
swap 'const QUESTION_KEYS = ["question"];' "$FULL_FIELDS"
swap 'const ANSWER_KEYS = ["answer"];' "$FULL_ANSWERS"
commit 2026-10-19T10:00:00 "fix(parse): accept front/back field names, the model sometimes answers in the card's terms"
swap 'const WRAPPERS: string[] = [];' "$FULL_WRAP"
commit 2026-11-07T10:00:00 'fix(parse): unwrap {"candidates": ...} / {"rewrites": ...}, haiku started wrapping its answer'

cat > "$WORK/debug/last-response.txt" <<'TXT'
Here are three rewrites of your card:

```json
{
  "suggestions": [
    {"strategy": "Forward", "q": "What enzyme unwinds DNA at the replication fork?", "a": "Helicase"},
    {"strategy": "Reverse", "q": "Helicase unwinds what at the replication fork?", "a": "DNA"},
    {"strategy": "Cloze", "q": "___ unwinds DNA at the replication fork.", "a": "Helicase"}
  ]
}
```
TXT
