import { readPage } from "@/lib/docs-data";
import { parsePage } from "@/lib/pages";
import type { PageSource } from "@/lib/source-region";

// One page's layout, for the viewer's "Show on page": the page size and the boxes of its
// sections, as the conversion wrote them. It comes from the page's layout sidecar (a few KB),
// never from document.json, which can be over 100 MB.

export async function GET(_req: Request, ctx: RouteContext<"/api/source/[slug]/[page]">) {
  const { slug, page } = await ctx.params;
  const n = parsePage(page);
  if (n === null) return Response.json({ error: "bad page" }, { status: 400 });
  let layout;
  try {
    layout = (await readPage(slug, n)).sidecar;
  } catch {
    return Response.json({ error: "unknown document" }, { status: 404 });
  }
  if (!layout) return Response.json({ error: "no such page" }, { status: 404 });
  const body: PageSource = { width: layout.width, height: layout.height, sections: layout.sections };
  return Response.json(body, { headers: { "Cache-Control": "public, max-age=300" } });
}
