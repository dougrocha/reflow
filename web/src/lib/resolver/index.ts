import { readFile, stat } from "node:fs/promises";
import { sampleDoc } from "@/docs";
import { exactOrigin } from "@/embed/protocol";
import { isRecord } from "@/lib/guards";
import { dataRoot, resolverFile } from "@/lib/paths";
import { DEMO_SITE_KEY, demoSite } from "./demo";
import { normalizeSourceUrl } from "./url";

// The resolver's index: which sites exist, which origins may frame the viewer for each, and which
// converted document each source URL of a site stands for. The operator writes it (`resolver.json`
// in DATA_ROOT, paths.ts); nothing the resolver receives ever adds to it.
//
//   { "sites": { "<key>": { "origins": ["https://example.org"],
//                           "documents": { "https://example.org/files/w9.pdf": "fw9" } } } }
//
// A site key is an identifier, not a secret: anyone who sees a page of the site sees it. So the
// only things behind it are public by intent, and each site sees only its own documents.
//
// The routes read it through ResolverStore, the one method they need. The backend is a file; a
// second one (a database) would implement the same method. `resolverStore()` is the one place
// the routes get their store from: the file, with the built-in demo site (./demo) behind it.

/** One site: where its pages are, and the documents its links may open. */
export type Site = {
  key: string;
  /** Exact origins (scheme, host, port) of the site's pages: the ones the resolver answers
   *  across origins, and the ones allowed to frame the viewer (./embed). */
  origins: readonly string[];
  /** Normalised source URL to the id of a registered document. */
  documents: ReadonlyMap<string, string>;
};

export interface ResolverStore {
  /** The site with this key, or null. */
  site(key: string): Promise<Site | null>;
}

export const SITE_KEY = /^[A-Za-z0-9_-]{1,64}$/;

/** The sites of an index file's JSON, and what was left out. Fails closed: an entry that is not
 *  right is dropped, never repaired, so a typo makes a link unknown and never a wrong answer. A
 *  document id is registered only when it is a catalogue document (src/docs.ts), not a variant
 *  and not a path. */
export function parseIndex(raw: unknown): { sites: Map<string, Site>; problems: string[] } {
  const sites = new Map<string, Site>();
  const problems: string[] = [];
  if (!isRecord(raw) || !isRecord(raw.sites)) return { sites, problems: ["no `sites` object"] };
  for (const [key, entry] of Object.entries(raw.sites)) {
    if (!SITE_KEY.test(key) || !isRecord(entry)) { problems.push(`site ${JSON.stringify(key)}: not a site`); continue; }
    const origins: string[] = [];
    for (const value of Array.isArray(entry.origins) ? entry.origins : []) {
      const origin = exactOrigin(value);
      if (origin) origins.push(origin);
      else problems.push(`site ${key}: origin ${JSON.stringify(value)} is not an exact http(s) origin`);
    }
    const documents = new Map<string, string>();
    for (const [url, id] of Object.entries(isRecord(entry.documents) ? entry.documents : {})) {
      const normal = normalizeSourceUrl(url);
      if (normal === null || typeof id !== "string" || !sampleDoc(id)) {
        problems.push(`site ${key}: ${JSON.stringify(url)} is not a source URL of a registered document`);
      } else documents.set(normal, id);
    }
    sites.set(key, { key, origins, documents });
  }
  return { sites, problems };
}

/** The store over an index file. A missing, unreadable or invalid file has no sites. The file is
 *  read again when its size or modification time changes, so an edit is live without a restart. */
export function fileStore(path: string, report: (problem: string) => void = (p) => console.error(`resolver index: ${p}`)): ResolverStore {
  let seen = "";
  let sites = new Map<string, Site>();
  return {
    async site(key) {
      let stamp: string;
      try {
        const info = await stat(path);
        stamp = `${info.size}:${info.mtimeMs}`;
      } catch {
        seen = "";
        sites = new Map();
        return null;
      }
      if (stamp !== seen) {
        let parsed: ReturnType<typeof parseIndex>;
        try {
          parsed = parseIndex(JSON.parse(await readFile(path, "utf8")));
        } catch {
          parsed = { sites: new Map(), problems: ["not JSON"] };
        }
        for (const problem of parsed.problems) report(problem);
        sites = parsed.sites;
        seen = stamp;
      }
      return sites.get(key) ?? null;
    },
  };
}

/** The store behind the file of sites: `demo` is the built-in demo site (./demo) when
 *  `demoOrigin` is an exact origin, unless the operator's file has a site `demo` of its own. */
export function withDemo(files: ResolverStore, demoOrigin: string | undefined): ResolverStore {
  return {
    async site(key) {
      const site = await files.site(key);
      if (site !== null || key !== DEMO_SITE_KEY) return site;
      return demoSite(demoOrigin);
    },
  };
}

// One file store per index file, made on first use, so a test can point REFLOW_DATA_ROOT elsewhere.
const fileStores = new Map<string, ResolverStore>();

/** The resolver's store, the one that both the resolver route and the embed proxy read. */
export function resolverStore(env: Record<string, string | undefined> = process.env): ResolverStore {
  const file = resolverFile(dataRoot(env));
  let files = fileStores.get(file);
  if (!files) fileStores.set(file, (files = fileStore(file)));
  return withDemo(files, env.REFLOW_DEMO_ORIGIN);
}
