import { ASK_MAX_QUESTION_CHARS, type AskRequest, type AskResult } from "@/lib/ask-contract";
import { ask, cleanQuestion, clientOf, statusOf } from "@/lib/ask";
import { isRecord } from "@/lib/guards";
import { NO_STORE, readCapped } from "@/lib/http";

// "Ask this document": one question in, the answer and where to highlight it out. A thin shell
// over ask() in @/lib/ask.

/**
 * Seconds the platform lets this handler run. A request can wait in the model gate for up to
 * QUEUE_WAIT_MS (130 s, the queue-wait cap in ask.ts) and then spend the model's 120 s limit, so
 * 250 s at worst; 270 leaves headroom. The queue wait is capped rather than maxDuration sized for
 * the uncapped worst case (360 s) so that a reader behind a long queue is told "busy" after
 * about two minutes instead of staring at a spinner for six. A literal, as Next reads it statically.
 */
export const maxDuration = 270;

/** A body bigger than this is not a question. */
const MAX_BODY_CHARS = 2_000;
/** The same cap in bytes (UTF-8 is at most 4 per character), checked while the body streams in:
 *  a route handler has no body limit of its own, so reading it whole first would hold any size. */
const MAX_BODY_BYTES = 4 * MAX_BODY_CHARS;

function reply(result: AskResult): Response {
  const headers: Record<string, string> = { ...NO_STORE };
  if (result.kind === "busy") headers["Retry-After"] = String(result.retryAfterSeconds);
  return Response.json(result, { status: statusOf(result), headers });
}

/** Parse the body, call ask(slug, body, req.signal, deps, client), reply with the AskResult at statusOf(result)
 *  and Cache-Control: no-store. An invalid request is {kind: "error", why: "bad-request"}. */
export async function POST(req: Request, ctx: RouteContext<"/api/ask/[slug]">): Promise<Response> {
  const { slug } = await ctx.params;
  const request = await parseAskRequest(req);
  if (!request) return reply({ kind: "error", why: "bad-request" });
  return reply(await ask(slug, request, req.signal, undefined, clientOf(req.headers)));
}

/** Body must be application/json {question: string, version: string}, question non-empty after trim
 *  and at most ASK_MAX_QUESTION_CHARS; Sec-Fetch-Site, when present, must be "same-origin" (a
 *  cross-site page must not spend the operator's model). Null when invalid. */
async function parseAskRequest(req: Request): Promise<AskRequest | null> {
  const site = req.headers.get("sec-fetch-site");
  if (site !== null && site !== "same-origin") return null;
  const type = req.headers.get("content-type") ?? "";
  if (!/^application\/json\s*(;|$)/i.test(type)) return null;
  let body: unknown;
  try {
    const bytes = await readCapped(req, MAX_BODY_BYTES);
    if (bytes === null) return null;
    const text = new TextDecoder().decode(bytes);
    if (text.length > MAX_BODY_CHARS) return null;
    body = JSON.parse(text);
  } catch {
    return null;
  }
  if (!isRecord(body)) return null;
  const { question, version } = body as Record<string, unknown>;
  if (typeof question !== "string" || typeof version !== "string") return null;
  const cleaned = cleanQuestion(question);
  if (cleaned === "" || cleaned.length > ASK_MAX_QUESTION_CHARS) return null;
  return { question: cleaned, version };
}
