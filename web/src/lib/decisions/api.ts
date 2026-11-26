import { buildsEnabled } from "../builds/gate";
import { fail, json, localOnly, notFound, perRequest, readJsonObject } from "../http";
import { SLUG_RE } from "../../pipelines";
import { getBuilds, type Builds } from "../builds/system";
import { BODY_CAP } from "./http";
import { fold, pending } from "./fold";
import { append, isRefused, readStore } from "./store";
import { readLedger, readPublished, type Conflict } from "./snapshot";
import { decisionViews } from "./status";
import { buildRecords, mintId, needsPublished, parseRequest, stamp } from "./validate";

// The /api/decisions routes, as functions of a Request. The route file is one line. Every handler
// checks, in this order: the gate (an empty 404 when off), the loopback Host, the same-site rules
// (a stricter one for a write), and the slug (a document this server knows). Nothing is read or
// written before all of those pass. Every answer is `Cache-Control: no-store`.

type Handler = (req: Request, b: Builds, slug: string) => Promise<Response>;

async function guarded(req: Request, slug: string, handler: Handler): Promise<Response> {
  await perRequest();
  // Decisions are on exactly when builds are: a decision only takes effect through a build, so a
  // server that cannot build has no use for a recorder.
  if (!buildsEnabled()) return notFound();
  const refused = localOnly(req);
  if (refused) return refused;
  const b = getBuilds();
  if (!SLUG_RE.test(slug) || !b.slugs().includes(slug)) return notFound();
  return handler(req, b, slug);
}

const refusal = (c: Conflict): Response => json(c.reload ? { error: c.error, reload: true } : { error: c.error }, c.status);

/** GET /api/decisions/<slug>: the folded store with each decision's status for the published build. */
export function getDecisions(req: Request, slug: string): Promise<Response> {
  return guarded(req, slug, async (_req, b) => {
    const read = await readPublished(slug, { docsRoot: b.docsRoot, publications: () => b.store.publications() });
    if (!read.ok) return refusal(read);
    const { published } = read;
    let snapshot;
    try {
      snapshot = await readStore(slug);
    } catch (e) {
      console.error(`The decision store of ${slug} could not be read.`, e);
      return fail(500, "The decisions could not be read.");
    }
    const { decisions, pending: waiting } = decisionViews(snapshot, published.ledger);
    const { ledger } = published;
    return json({
      slug, rev: published.rev, generation: published.generation,
      source: ledger ? { sha256: ledger.source.sha256 } : null,
      ledger: ledger ? { store: ledger.store } : null,
      store: {
        id: snapshot.store, bytes: snapshot.bytes, lines: snapshot.lines, torn: snapshot.torn, unreadable: snapshot.unreadable.length,
        duplicate: fold(snapshot).duplicate, error: snapshot.error,
      },
      pending: waiting,
      decisions,
    });
  });
}

/** POST /api/decisions/<slug>: one choice (docs/review-decisions.md, "Recording decisions from the
 *  web"). 201 {decision, pending} only after the record is durable. */
export function postDecision(req: Request, slug: string): Promise<Response> {
  return guarded(req, slug, async (r, b) => {
    const read = await readJsonObject(r, BODY_CAP);
    if ("error" in read) return fail(read.status, read.error);
    const parsed = parseRequest(read.body);
    if (!parsed.ok) return refusal(parsed);
    const request = parsed.request;
    let published = null;
    if (needsPublished(request)) {
      const got = await readPublished(slug, { docsRoot: b.docsRoot, publications: () => b.store.publications() });
      if (!got.ok) return refusal(got);
      published = got.published;
    }
    const at = stamp();
    let refused: Conflict | null = null;
    let written;
    try {
      // The checks that read the store run under its lock, in the same step as the write.
      written = await append(slug, (store) => {
        const built = buildRecords(request, { published, store, id: () => mintId(), at });
        if (built.ok) return built.records;
        refused = built;
        return { refused: built.error, status: 409 };
      });
    } catch (e) {
      console.error(`A decision for ${slug} could not be recorded.`, e);
      return fail(500, "The decision could not be recorded.");
    }
    if (isRefused(written)) return refusal(refused ?? { ok: false, status: 409, error: written.refused });
    const snapshot = await readStore(slug).catch(() => null);
    const ledger = published?.ledger ?? (await readLedger(b.docsRoot, slug));
    const waiting = snapshot
      ? pending(snapshot, ledger ? { store: ledger.store.id, bytes: ledger.store.bytes } : { store: null, bytes: 0 })
        .filter((e) => e.parsed.type === "record").length
      : written.length;
    return json({ decision: written[0]!.id, pending: waiting }, 201);
  });
}
