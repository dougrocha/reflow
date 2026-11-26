import { builtAt } from "../published";
import { buildsEnabled } from "./gate";
import { fail, json, localOnly, notFound, perRequest, readJsonObject } from "../http";
import { isFinished, isJobId } from "./record";
import { getBuilds, type Builds } from "./system";
import { parseBehind, type JobKind, type JobRecord } from "./view";
import { isFixedPipeline, ollamaPipeline, variantOf } from "../../pipelines";

// The /api/builds routes, as functions of a Request. The route files are one line each. Every
// handler but the probe first checks the gate (404 when off), then the loopback Host and that the
// request is from this site (a write must carry its Origin): the decisions routes' checks.

type Handler = (req: Request, b: Builds) => Promise<Response>;

async function guarded(req: Request, handler: Handler): Promise<Response> {
  await perRequest();
  if (!buildsEnabled()) return notFound();
  const refused = localOnly(req);
  if (refused) return refused;
  return handler(req, getBuilds());
}

/** When each document was last published (null: not built). */
async function builtTimes(b: Builds): Promise<Record<string, number | null>> {
  const entries = await Promise.all(b.slugs().map(async (slug) => [slug, await builtAt(b.docsRoot, slug)] as const));
  return Object.fromEntries(entries);
}

/** GET /api/builds/enabled: whether builds are on, for the pages' client code to ask once. 200 either
 *  way (a 404 would be logged in the browser's console on every page load); it reads nothing else,
 *  starts nothing and says nothing about the jobs. */
export async function buildsProbe(): Promise<Response> {
  await perRequest();
  return json({ enabled: buildsEnabled() });
}

export function listBuilds(req: Request): Promise<Response> {
  return guarded(req, async (_req, b) => {
    // A page that has stayed behind a publication says which documents it rendered, and at which
    // generation (`?behind=fw9:154`, Freshness.report); unknown slugs and junk are dropped.
    const slugs = new Set(b.slugs());
    const behind = parseBehind(new URL(req.url).searchParams.get("behind"), (slug) => slugs.has(slug));
    await b.runner.pump();
    const { active, recent } = await b.store.snapshot();
    const built = await builtTimes(b);
    // Every publication since this server last looked (a page build, a command-line build, one
    // cancelled or interrupted after it published, or all of them after a restart) has its
    // document's cached reads marked stale, because that needs a request. Last, with nothing after
    // it that can throw: Next applies the mark only after this handler returns, and drops it if the
    // handler throws. The answer may reach the page before the mark is applied, and the first
    // request after it is served the stale entry; a page compares `published` with the generation
    // it rendered, so a refresh that comes back with old data is retried, and one that stays behind
    // is reported with the next poll, which marks the document again.
    const published = await b.invalidatePublished(behind);
    return json({ concurrency: b.runner.concurrency, active, recent, built, published });
  });
}

/** Whether the variants list would offer `key`: a fixed pipeline, or one made for an installed Ollama model. */
async function offered(b: Builds, key: string): Promise<boolean> {
  const v = variantOf(key);
  if (!v) return false;
  return isFixedPipeline(v.pipeline) || (await b.models()).some((m) => ollamaPipeline(m)?.name === v.pipeline.name);
}

async function enqueueSlug(b: Builds, slug: string, kind: JobKind = "build"): Promise<Response> {
  // A document, one already built (a folder in public/docs), or a variant of a document that the
  // variants list offers, also one not built yet (src/pipelines.ts): the job and claim keys are
  // a bounded set, not whatever name a request makes up.
  if (!b.slugs().includes(slug) && !(await offered(b, slug))) return fail(400, "That is not a document.");
  const { job, created } = await b.runner.enqueue(slug, kind);
  return json({ job, created }, created ? 202 : 200);
}

export function enqueueBuild(req: Request): Promise<Response> {
  return guarded(req, async (r, b) => {
    const read = await readJsonObject(r);
    if ("error" in read) return fail(read.status, read.error);
    if (typeof read.body.slug !== "string") return fail(400, "Give the document as {\"slug\": \"...\"}.");
    // A full build is the default; the one other job is applying the recorded decisions.
    if (read.body.kind !== undefined && read.body.kind !== "rerender") return fail(400, "The kind can only be \"rerender\".");
    return enqueueSlug(b, read.body.slug, read.body.kind ?? "build");
  });
}

async function jobOr404(b: Builds, id: string): Promise<JobRecord | Response> {
  const job = isJobId(id) ? await b.store.get(id) : null;
  return job ?? fail(404, "No such build.");
}

export function cancelBuild(req: Request, id: string): Promise<Response> {
  return guarded(req, async (_r, b) => {
    const job = await jobOr404(b, id);
    if (job instanceof Response) return job;
    if (isFinished(job)) return fail(409, "That build has already finished.");
    const { job: after } = await b.runner.cancel(id);
    await b.runner.pump();
    return json({ job: after ?? job });
  });
}

export function retryBuild(req: Request, id: string): Promise<Response> {
  return guarded(req, async (_r, b) => {
    const job = await jobOr404(b, id);
    if (job instanceof Response) return job;
    if (job.state === "done" || !isFinished(job)) return fail(409, "Only a build that failed, was cancelled or was interrupted can be retried.");
    return enqueueSlug(b, job.slug, job.kind);
  });
}
