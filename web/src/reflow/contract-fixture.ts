// What Python publishes for the contract document (tests/test_viewer_contract.py renders it
// into schema/): the manifest and the pages the viewer should expand it to, and the published
// index.html and chunk pages. The viewer's tests read these instead of markup written by hand.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { loadChunkPages, type ManifestPage } from "./core";

const SCHEMA_DIR = join(import.meta.dir, "../../../schema");

type Contract = { manifest: Record<string, unknown>; pages: ManifestPage[]; anchors: [string, string][] };

export const contract = JSON.parse(readFileSync(join(SCHEMA_DIR, "viewer-contract.json"), "utf8")) as Contract;

/** The document base the tests serve it at. */
export const BASE = "/doc/";

/** A published file, as written: "index.html", "chunks/0002.html". */
export function published(name: string): string {
  return readFileSync(join(SCHEMA_DIR, "viewer-contract", name), "utf8");
}

/** A `fetch` that serves the published files under BASE, and answers 404 for anything else. */
export const serveContract = (async (input: RequestInfo | URL) => {
  const url = new URL(String(input), "http://x");
  const name = url.pathname.startsWith(BASE) ? url.pathname.slice(BASE.length) : "";
  try {
    return new Response(published(name));
  } catch {
    return new Response("not found", { status: 404 });
  }
}) as typeof fetch;

/** Every page's markup as the viewer holds it (core.loadChunkPages), by page number. */
export async function contractPages(): Promise<Map<number, string>> {
  const realFetch = globalThis.fetch;
  globalThis.fetch = serveContract;
  try {
    const pages = new Map<number, string>();
    for (const chunk of (contract.manifest.chunks as { href: string }[])) {
      for (const [n, html] of await loadChunkPages(BASE, chunk.href)) pages.set(n, html);
    }
    return pages;
  } finally {
    globalThis.fetch = realFetch;
  }
}
