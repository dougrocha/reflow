import { cancelBuild } from "@/lib/builds/api";

export async function POST(req: Request, ctx: RouteContext<"/api/builds/[id]/cancel">) {
  return cancelBuild(req, (await ctx.params).id);
}
