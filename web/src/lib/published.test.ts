import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { repo } from "../../scripts/freshness";
import { FILE_NAMES } from "./published";

// The names a conversion writes are src/reflow/names.py's; FILE_NAMES is the web's copy of those it reads.
describe("FILE_NAMES", () => {
  const python = readFileSync(`${repo}/src/reflow/names.py`, "utf8");
  const constant = (name: string) => new RegExp(`^${name} = "([^"]+)"`, "m").exec(python)?.[1];
  const webOnly = ["audit", "buildInfo"];
  const names: Record<Exclude<keyof typeof FILE_NAMES, "audit" | "buildInfo">, string> = {
    manifest: "MANIFEST_JSON",
    review: "REVIEW_JSON",
    decisions: "DECISIONS_LEDGER",
    tree: "DOCUMENT_JSON",
    passages: "PASSAGES_JSONL",
    layoutDir: "LAYOUT_DIR",
    traceDir: "TRACE_DIR",
  };

  test.each(Object.entries(names))("%s is reflow.names.%s", (key, pythonName) => {
    expect<string>(FILE_NAMES[key as keyof typeof names]).toBe(constant(pythonName)!);
  });

  test("every name is checked here or web-only", () => {
    expect(Object.keys(FILE_NAMES).sort()).toEqual([...Object.keys(names), ...webOnly].sort());
  });
});
