import { sampleDoc } from "@/docs";
import { buildsEnabled } from "@/lib/builds/gate";
import type { JobRecord } from "@/lib/builds/view";
import { getBuilds } from "@/lib/builds/system";
import { crossSite, fail, json, notFound, perRequest } from "@/lib/http";
import { docsRoot } from "@/lib/paths";
import { isKeyOf } from "@/pipelines";
import { builtKeys } from "./built";
import { staleKeys } from "./stale";
import { variantViews, type VariantsAnswer } from "./view";

// GET /api/variants/<slug>. Building is local only: without the builds gate the answer lists only
// the built variants, and no pipelines to build.

/** GET /api/variants/<slug>; with ?fresh=1 and builds on, built variants that are stale say so (it
 *  runs the provenance check, about a second; concurrent ones share one run). */
export async function getVariants(req: Request, slug: string): Promise<Response> {
  await perRequest();
  if (!sampleDoc(slug)) return notFound();
  const refused = crossSite(req);
  if (refused) return fail(403, refused);
  const builds = buildsEnabled();
  const b = builds ? getBuilds() : null;
  const built = await builtKeys(b?.docsRoot ?? docsRoot(), slug);
  let jobs: JobRecord[] = [];
  let models: string[] = [];
  if (b) {
    await b.runner.pump();
    const { active, recent } = await b.store.snapshot(200);
    jobs = [...active, ...recent].filter((j) => isKeyOf(slug, j.slug));
    models = await b.models();
  }
  const variants = variantViews(slug, built, jobs, builds, models);
  // The check spawns a process, so it is a build-side action: with builds off, ?fresh=1 answers as without it.
  if (builds && new URL(req.url).searchParams.get("fresh") === "1") {
    const stale = await staleKeys(variants.filter((v) => v.state === "built").map((v) => v.key));
    for (const v of variants) if (stale.has(v.key)) v.state = "stale";
  }
  const answer: VariantsAnswer = { slug, builds, variants };
  return json(answer);
}
