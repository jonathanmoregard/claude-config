import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { findPackageJSON } from "node:module";
import { join } from "node:path";
import { test } from "node:test";
import { pathToFileURL } from "node:url";

const runtime = join(import.meta.dirname, "..", "..", "runtime");

function moduleType(file: string): string {
  const manifest = findPackageJSON(pathToFileURL(file));
  if (manifest === undefined) return "commonjs";
  const { type } = JSON.parse(readFileSync(manifest, "utf8")) as { type?: string };
  return type ?? "commonjs";
}

test("require()-based scripts under runtime/ stay in a commonjs scope", () => {
  const cjs = readdirSync(runtime, { recursive: true, encoding: "utf8" })
    .filter((rel) => rel.endsWith(".js"))
    .map((rel) => join(runtime, rel))
    .filter((file) => readFileSync(file, "utf8").includes("require("));
  assert.ok(cjs.length > 0, "found no require()-based .js under runtime/");
  for (const file of cjs) assert.equal(moduleType(file), "commonjs", file);
});
