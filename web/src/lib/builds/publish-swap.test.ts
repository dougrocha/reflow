import { afterEach, beforeEach, expect, test } from "bun:test";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { recover, swapIn } from "../../../scripts/lib/swap";
import { removeDir, tempDir } from "./testkit";

// How build-docs.ts puts a new document in place of the published one (scripts/lib/swap.ts).

let root: string;
beforeEach(async () => (root = await tempDir()));
afterEach(() => removeDir(root));

function doc(dir: string, v: string) {
  mkdirSync(join(dir, "pages"), { recursive: true });
  writeFileSync(join(dir, "manifest.json"), JSON.stringify({ v }));
  writeFileSync(join(dir, "pages", "1.png"), v);
}
const version = (dir: string) => JSON.parse(readFileSync(join(dir, "manifest.json"), "utf8")).v;

// A reader in another process reads the manifest in a loop and counts the reads that found none.
const READER = `
const [file, stop] = process.argv.slice(1);
const fs = require("node:fs");
let reads = 0, misses = 0;
fs.writeSync(1, "ready\\n");
while (!fs.existsSync(stop)) {
  reads++;
  try { JSON.parse(fs.readFileSync(file, "utf8")); } catch { misses++; }
}
fs.writeSync(1, JSON.stringify({ reads, misses }) + "\\n");
`;

async function publishUnderReader(atomic: boolean) {
  const to = join(root, "docs", "fw9");
  doc(to, "v0");
  const stop = join(root, "stop");
  const reader = Bun.spawn(["bun", "-e", READER, join(to, "manifest.json"), stop], { stdout: "pipe" });
  const out = reader.stdout.getReader();
  await out.read(); // "ready"
  for (let i = 1; i <= 200; i++) {
    doc(`${to}.new`, `v${i}`);
    await swapIn(`${to}.new`, to, { atomic });
  }
  writeFileSync(stop, "");
  let text = "";
  for (let r = await out.read(); !r.done; r = await out.read()) text += new TextDecoder().decode(r.value);
  await reader.exited;
  return { to, ...(JSON.parse(text.trim().split("\n").pop()!) as { reads: number; misses: number }) };
}

test.if(process.platform === "linux")("a publish over a document never leaves a reader without it", async () => {
  const { to, reads, misses } = await publishUnderReader(true);
  expect(reads).toBeGreaterThan(0);
  expect(misses).toBe(0);
  expect(version(to)).toBe("v200");
  expect(existsSync(`${to}.new`)).toBe(false);
  expect(existsSync(`${to}.old`)).toBe(false);
}, 60_000);

test("the fallback swap replaces the document and leaves nothing beside it", async () => {
  const to = join(root, "docs", "fw9");
  doc(to, "v0");
  doc(`${to}.new`, "v1");
  await swapIn(`${to}.new`, to, { atomic: false });
  expect(version(to)).toBe("v1");
  expect(existsSync(`${to}.new`)).toBe(false);
  expect(existsSync(`${to}.old`)).toBe(false);
});

test("a fallback swap killed between its renames is undone by the next publish's recover", async () => {
  const to = join(root, "docs", "fw9");
  // What the kill leaves: the old document moved aside, the new one still staged.
  doc(`${to}.old`, "v0");
  doc(`${to}.new`, "v1");
  await recover(to);
  expect(version(to)).toBe("v0");
  expect(existsSync(`${to}.old`)).toBe(false);
  // A kill after the new one went in, before the old one was deleted: the old one is removed.
  doc(`${to}.old`, "v0");
  await recover(to);
  expect(version(to)).toBe("v0");
  expect(existsSync(`${to}.old`)).toBe(false);
});

test("a first publish renames the staged folder in", async () => {
  const to = join(root, "docs", "fw9");
  doc(`${to}.new`, "v1");
  await swapIn(`${to}.new`, to);
  expect(version(to)).toBe("v1");
});
