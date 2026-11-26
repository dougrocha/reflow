import { afterEach, beforeAll, describe, expect, test } from "bun:test";
import { BASE, contract, contractPages, published, serveContract } from "../contract-fixture";
import { normalizeManifest } from "../core";
import { buildTranscriptFile } from "./transcript-file";

const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });

let pages: Map<number, string>;
beforeAll(async () => { pages = await contractPages(); });

/** The `.pg` wrappers of a document's markup: each page's opening tag and label. */
function wrappers(markup: string): { open: string; label: string }[] {
  return [...markup.matchAll(/(<div class="pg"[^>]*>)<p class="pg-label"><span>([^<]*)<\/span>/g)]
    .map((m) => ({ open: m[1], label: m[2] }));
}

describe("buildTranscriptFile", () => {
  test("wraps each page as the chunk pages publish it", async () => {
    globalThis.fetch = serveContract;
    const manifest = normalizeManifest(contract.manifest);
    const readChunk = async (href: string) => {
      const first = manifest.chunks.find((c) => c.href === href)!;
      return new Map([...pages].filter(([n]) => n >= first.first && n <= first.last));
    };
    const file = await buildTranscriptFile(manifest, BASE, readChunk);
    const published_ = manifest.chunks.flatMap((c) => wrappers(published(c.href)));
    expect(wrappers(file)).toEqual(published_);
    expect(published_.length).toBe(manifest.pageCount);
    // The pages' own markup is not changed.
    expect(file).toContain("<style>");
    expect(file).toContain(pages.get(4)!);
  });
});
