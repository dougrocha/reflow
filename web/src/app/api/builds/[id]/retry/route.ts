import { retryBuild } from "@/lib/builds/api";

export async function POST(req: Request, ctx: RouteContext<"/api/builds/[id]/retry">) {
  return retryBuild(req, (await ctx.params).id);
}
