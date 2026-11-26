import { join } from "node:path";
import { assertSlug } from "../pipelines";

// Where things are on disk. No framework imports: the Next server and scripts/build-docs.ts both
// use this file, so the two cannot disagree about where a document is published or a build is
// recorded. Imports in it are relative for the same reason. `root` is DATA_ROOT, `dir` a builds
// folder.

/** public/: what the site serves. */
export const publicRoot = (cwd = process.cwd()): string => join(cwd, "public");

/** public/docs: the published documents, one folder per key (src/pipelines.ts). */
export const docsRoot = (cwd = process.cwd()): string => join(publicRoot(cwd), "docs");

/** public/pdfs: one `<slug>.pdf` per document, what the test page links to. */
export const pdfsRoot = (cwd = process.cwd()): string => join(publicRoot(cwd), "pdfs");

/** DATA_ROOT: where whole conversions go (gitignored, never served). */
export function dataRoot(env: Record<string, string | undefined> = process.env, cwd = process.cwd()): string {
  return env.REFLOW_DATA_ROOT ?? join(cwd, ".reflow");
}

/** The build record's folder, inside DATA_ROOT. */
export const buildsDir = (root: string): string => join(root, ".builds");

/** The jobs of a build record: `<id>.json`, `<id>.cancel`, `<id>.log`. */
export const jobsDir = (dir: string): string => join(dir, "jobs");

/** The claims of a build record: `<key>.json`, the job that holds a document's build. */
export const claimsDir = (dir: string): string => join(dir, "claims");

/** What each document last published. */
export const publicationsFile = (dir: string): string => join(dir, "publications.json");

/** The decision stores' folder, inside DATA_ROOT. It holds `coordination.lock` too. */
export const decisionsDir = (root: string): string => join(root, ".decisions");

/** `<data root>/.decisions/<slug>.jsonl`. Throws on a slug that is not a plain document name, so
 *  no path is ever built from anything else. */
export function storePath(root: string, slug: string): string {
  assertSlug(slug);
  return join(decisionsDir(root), `${slug}.jsonl`);
}

/** The resolver's index (src/lib/resolver): the operator's file of sites and their documents, inside DATA_ROOT. */
export const resolverFile = (root: string): string => join(root, "resolver.json");
