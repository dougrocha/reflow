import { readFile, stat } from "node:fs/promises";
import { join } from "node:path";
import { pageStem } from "./pages";

// The files a conversion writes of one document, and the one place that names them. A document's
// key (src/pipelines.ts) is its folder in two roots:
//  - public/docs/<key>/ (docsRoot): what the site serves, the files reflow's PUBLIC_NAMES lists;
//  - DATA_ROOT/<key>/ (dataRoot): the whole conversion, including the files that are not served.
// A read answers null for a file that is missing or is not JSON.

/** The names of those files. All but `audit` and `buildInfo` are reflow's own (src/reflow/names.py);
 *  published.test.ts holds the two lists to each other. */
export const FILE_NAMES = {
  manifest: "manifest.json",
  review: "review.json",
  decisions: "decisions.json",
  tree: "document.json",
  passages: "passages.jsonl",
  layoutDir: "layout",
  traceDir: "trace",
  audit: "audit.json",
  buildInfo: "build-info.json",
} as const;

/** The JSON in a file, or null when it is missing or not JSON. */
async function readJson(path: string): Promise<unknown> {
  try {
    return JSON.parse(await readFile(path, "utf8"));
  } catch {
    return null;
  }
}

const manifestPath = (docsRoot: string, key: string): string => join(docsRoot, key, FILE_NAMES.manifest);
export const readManifest = (docsRoot: string, key: string) => readJson(manifestPath(docsRoot, key));
/** When a document was last published, as the modification time of its manifest.json (ms); null
 *  when it has none, which is what "not built" means. The publication generation (publications.json)
 *  says whether a read is current, not whether a document is built: a document published by an
 *  older build has a manifest and no record. */
export async function builtAt(docsRoot: string, key: string): Promise<number | null> {
  try {
    return Math.round((await stat(manifestPath(docsRoot, key))).mtimeMs);
  } catch {
    return null;
  }
}
export const readReview = (docsRoot: string, key: string) => readJson(join(docsRoot, key, FILE_NAMES.review));
export const readBuildInfo = (docsRoot: string, key: string) => readJson(join(docsRoot, key, FILE_NAMES.buildInfo));
export const readDecisions = (docsRoot: string, key: string) => readJson(join(docsRoot, key, FILE_NAMES.decisions));

/** `<key>/audit.json`: the source PDF's audit (written by build-docs, not by reflow convert). */
export const auditPath = (dataRoot: string, key: string): string => join(dataRoot, key, FILE_NAMES.audit);
export const readAudit = (dataRoot: string, key: string) => readJson(auditPath(dataRoot, key));
/** `<key>/passages.jsonl`: what Ask reads. */
export const passagesPath = (dataRoot: string, key: string): string => join(dataRoot, key, FILE_NAMES.passages);
/** A page's layout sidecar (a few KB). */
export const readSidecar = (dataRoot: string, key: string, page: number) =>
  readJson(join(dataRoot, key, FILE_NAMES.layoutDir, `${pageStem(page)}.json`));
/** A page's trace, the heuristic engine's record. */
export const readTrace = (dataRoot: string, key: string, page: number) =>
  readJson(join(dataRoot, key, FILE_NAMES.traceDir, `${pageStem(page)}.json`));
/** The document's whole tree: the largest is over 100 MB, so read it only for what one page needs. */
export const readTree = (dataRoot: string, key: string) => readJson(join(dataRoot, key, FILE_NAMES.tree));
