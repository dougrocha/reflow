import type { Stamp } from "@/lib/docs-data";
import { lowest } from "@/lib/builds/view";
import { RenderedGenerations } from "./context";

/**
 * Tells the page's build tracking which publication of each document the page was rendered from:
 * pass every stamped read the page shows (readDocument, readPage, a summary). It sends only
 * `{slug: generation}`, the lowest when a document was read more than once, so a page never claims
 * to be newer than any of its data. Render it beside that data, inside the same Suspense boundary.
 * Renders nothing; with builds off nothing reads it.
 */
export function RenderedFrom({ reads }: { reads: Stamp[] }) {
  return <RenderedGenerations generations={lowest(reads.map((r) => ({ [r.slug]: r.generation })))} />;
}
