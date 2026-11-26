import { exactOrigin, INSTANCE_ID } from "@/embed/protocol";
import { SITE_KEY, type ResolverStore } from "./index";

// Who may frame /embed/<id>: the answer is the page's `frame-ancestors` (src/lib/csp.ts, sent by
// src/proxy.ts). One exact origin, or 'none'. docs/embed.md.
//
// The frame's URL is the resolver's viewer URL (`/embed/<id>?site=<key>`) with the loader's
// `parent` (the page's own origin) and `instance` added. The page may be framed by `parent` only
// when all of these hold, and by nobody otherwise:
//  - `site` names a site of the index, and that site registered document `id`;
//  - `parent` is an exact origin the site lists (its `origins`: the parent sites allowed to frame
//    the viewer, not the domains PDFs come from);
//  - `instance` is an instance id, so the frame can speak the protocol (src/embed/protocol.ts).
// Each parameter must be there once. `site=` names a record; it proves nothing about who asks.
// What proves the parent is the browser, which enforces frame-ancestors against the real parent.
// The page with no `parent` is the shareable hosted page: readable by anyone, framed by no one.

export const NO_FRAMING = "'none'";

const single = (params: URLSearchParams, name: string): string | null => {
  const all = params.getAll(name);
  return all.length === 1 ? all[0] : null;
};

/** The one origin that may frame the viewer of `id` for this query, or NO_FRAMING. */
export async function frameAncestor(store: ResolverStore, id: string, params: URLSearchParams): Promise<string> {
  const key = single(params, "site");
  const parent = exactOrigin(single(params, "parent"));
  const instance = single(params, "instance");
  if (key === null || !SITE_KEY.test(key) || parent === null || instance === null || !INSTANCE_ID.test(instance)) return NO_FRAMING;
  const site = await store.site(key);
  if (!site || !site.origins.includes(parent)) return NO_FRAMING;
  return [...site.documents.values()].includes(id) ? parent : NO_FRAMING;
}
