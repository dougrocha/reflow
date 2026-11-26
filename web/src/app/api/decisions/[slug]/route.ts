import { getDecisions, postDecision } from "@/lib/decisions/api";

// A document's review decisions: the folded store with each decision's status, and a new choice.
// See docs/review-decisions.md.
export async function GET(req: Request, ctx: RouteContext<"/api/decisions/[slug]">) {
  return getDecisions(req, (await ctx.params).slug);
}
export async function POST(req: Request, ctx: RouteContext<"/api/decisions/[slug]">) {
  return postDecision(req, (await ctx.params).slug);
}
