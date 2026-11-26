// Builds the loader (src/embed/loader.ts) to the one file the app serves at /embed/loader.js:
// `bun run loader:build`. It runs before `next dev` and `next build` (the predev and prebuild
// scripts), so the file is always the current source. public/embed/loader.js is generated and
// not committed, like public/docs. src/embed/loader-build.test.ts holds the file to its limits.
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const WEB = join(import.meta.dir, "..");
export const LOADER_ENTRY = join(WEB, "src/embed/loader.ts");
export const LOADER_OUT = join(WEB, "public/embed/loader.js");

/** The loader as one minified, dependency-free script that defines no global. */
export async function buildLoader(): Promise<string> {
  const result = await Bun.build({
    entrypoints: [LOADER_ENTRY],
    target: "browser",
    format: "iife",
    minify: true,
    define: { "process.env.NODE_ENV": '"production"' },
  });
  if (!result.success || result.outputs.length !== 1) {
    throw new Error(`loader build failed:\n${result.logs.map(String).join("\n")}`);
  }
  return result.outputs[0].text();
}

if (import.meta.main) {
  const code = await buildLoader();
  mkdirSync(join(WEB, "public/embed"), { recursive: true });
  writeFileSync(LOADER_OUT, code);
  console.log(`wrote public/embed/loader.js (${code.length} bytes)`);
}
