import { getVariants } from "@/lib/variants/api";

// A document's variants: built, building, or (with builds on) which pipelines can build it. See
// @/lib/variants/api and src/pipelines.ts.
export async function GET(req: Request, ctx: RouteContext<"/api/variants/[slug]">) {
  return getVariants(req, (await ctx.params).slug);
}
