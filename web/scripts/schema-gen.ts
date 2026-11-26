// Writes the TypeScript types of the published formats from their schemas (schema/ at the
// repository root): `bun run schema:gen`. src/reflow/contract.test.ts fails while a generated
// file differs from what this would write.
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { generateTypes, type Schema } from "../src/reflow/schema";

const REPO = join(import.meta.dir, "..", "..");

const FILES = [
  { schema: "schema/manifest.schema.json", out: "web/src/reflow/manifest.gen.ts", constName: "MANIFEST_SCHEMA" },
];

/** Each generated file: its path from the repository root, and what it should hold. */
export function generated(): { out: string; path: string; text: string }[] {
  return FILES.map(({ schema, out, constName }) => {
    const parsed = JSON.parse(readFileSync(join(REPO, schema), "utf8")) as Schema;
    return { out, path: join(REPO, out), text: generateTypes(parsed, schema, constName) };
  });
}

if (import.meta.main) {
  for (const { out, path, text } of generated()) {
    writeFileSync(path, text);
    console.log(`wrote ${out}`);
  }
}
