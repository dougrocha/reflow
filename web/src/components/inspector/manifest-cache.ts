import { loadManifest, type Manifest } from "@/reflow/core";

const manifests = new Map<string, { version: string; pending: Promise<Manifest> }>();

/**
 * The manifest of the document at `base`, shared by every page the inspector shows. `version` is
 * the digest of the build the page was rendered from (the manifest's `v`): a rebuild changes it,
 * so a manifest read before the rebuild is not served after it, and the new build's chunk list is
 * read. Without a version (a converter that wrote none) nothing is kept.
 */
export function manifestFor(base: string, version: string | null): Promise<Manifest> {
  if (version === null) return loadManifest(base);
  const kept = manifests.get(base);
  if (kept?.version === version) return kept.pending;
  const pending = loadManifest(base);
  manifests.set(base, { version, pending });
  pending.catch(() => {
    if (manifests.get(base)?.pending === pending) manifests.delete(base); // allow a retry
  });
  return pending;
}
