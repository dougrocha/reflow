import { expect, test } from "bun:test";
import { buildLoader } from "../../scripts/build-loader";

// The built loader (scripts/build-loader.ts) is what customers run, so what matters is the file,
// not the source: small, one file with no import left, and nothing a strict CSP would block.

const code = await buildLoader();

test("is a few KB", () => {
  expect(code.length).toBeGreaterThan(1000);
  expect(code.length).toBeLessThan(8 * 1024);
});

test("is one self-contained script that defines no global", () => {
  expect(code.startsWith("(()=>{")).toBe(true);
  expect(code).not.toMatch(/\b(?:import|export)\b[^"']*\bfrom\b/);
  expect(code).not.toMatch(/\brequire\s*\(/);
});

test("uses nothing a CSP without 'unsafe-inline' or 'unsafe-eval' blocks", () => {
  expect(code).not.toMatch(/\beval\s*\(/);
  expect(code).not.toMatch(/\bnew Function\b|\bFunction\s*\(/);
  expect(code).not.toMatch(/\bset(?:Timeout|Interval)\s*\(\s*["'`]/);
  expect(code).not.toMatch(/\.cssText\b/);
  expect(code).not.toMatch(/\.(?:innerHTML|outerHTML)\b|insertAdjacentHTML|document\.write|srcdoc/);
  expect(code).not.toMatch(/createElement\(["']style["']\)/);
  expect(code).not.toMatch(/setAttribute\(["']style["']/);
});
