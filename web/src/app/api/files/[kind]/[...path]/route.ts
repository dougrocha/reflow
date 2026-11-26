import { SAMPLE_DOCS } from "@/docs";
import { isVariantKey } from "@/pipelines";
import { publicRoot } from "@/lib/paths";
import { servePublicFile } from "@/lib/public-files";

// The files of a document that was built after the server started (next.config.ts rewrites the
// 404s of /docs/<slug>/... and /pdfs/<slug>.pdf here). See @/lib/public-files.
export async function GET(req: Request, ctx: RouteContext<"/api/files/[kind]/[...path]">) {
  const { kind, path } = await ctx.params;
  if (kind !== "docs" && kind !== "pdfs") return new Response("Not found", { status: 404 });
  // A variant's files are under its key (public/docs/<slug>~<pipeline>/); it shares its document's PDF.
  const variant = kind === "docs" && path[0] && isVariantKey(path[0]) ? [path[0]] : [];
  return servePublicFile(req, publicRoot(), kind, path, [...SAMPLE_DOCS.map((d) => d.slug), ...variant]);
}
