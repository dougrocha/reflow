import { readdirSync } from "node:fs";
import { readdir } from "node:fs/promises";
import { isKeyOf, variantKeysIn } from "@/pipelines";
import { docsRoot } from "@/lib/paths";
import { builtAt, readManifest } from "@/lib/published";
import { normalizeManifest } from "@/reflow/core";
import { variantViews, type VariantTab } from "./view";

// Which variants of a document are published: the folders of public/docs with a manifest.

/** The keys of the variants published in `root` (public/docs/<slug>~<pipeline>/), the default's not
 *  included: it is the document's own folder. */
export function variantFolders(root: string): string[] {
  try {
    return variantKeysIn(readdirSync(root));
  } catch {
    return [];
  }
}

// Whether a variant's manifest reads, by root, key and the manifest's modification time: a manifest
// is read once until it is published again.
const readable = new Map<string, boolean>();

async function reads(root: string, key: string, at: number): Promise<boolean> {
  const memo = `${root}\0${key}\0${at}`;
  const known = readable.get(memo);
  if (known !== undefined) return known;
  let ok = false;
  try {
    const raw = await readManifest(root, key);
    ok = raw !== null && Boolean(normalizeManifest(raw));
  } catch {
    ok = false;
  }
  readable.set(memo, ok);
  return ok;
}

/** The keys of the variants of `slug` published in `root`, the default's included when built. */
export async function builtKeys(root: string, slug: string): Promise<Set<string>> {
  const names = await readdir(root).catch(() => [] as string[]);
  const keys = [slug, ...variantKeysIn(names).filter((n) => isKeyOf(slug, n))];
  const built = await Promise.all(keys.map((k) => builtAt(root, k).then((at) => (at === null ? null : k))));
  return new Set(built.filter((k): k is string => k !== null));
}

/** The built variants of a document, for its tab strip: the default first. A variant whose manifest
 *  no longer reads (published by an older build, in a format since changed) is left out until it is
 *  built again; the default is kept, so its pages show their error. */
export async function builtVariants(slug: string, root = docsRoot()): Promise<VariantTab[]> {
  const built = await builtKeys(root, slug);
  const kept = await Promise.all([...built].map(async (k) => {
    if (k === slug) return k;
    const at = await builtAt(root, k);
    return at !== null && (await reads(root, k, at)) ? k : null;
  }));
  const shown = new Set(kept.filter((k): k is string => k !== null));
  return variantViews(slug, shown, [], false).map(({ name, label, key }) => ({ name, label, key }));
}
