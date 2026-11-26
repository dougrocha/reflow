import { isFiniteNumber, isRecord } from "@/lib/guards";
// The client's boundary with POST /api/ask/<slug>.

import {
  ASK_MAX_EVIDENCE,
  type AskRequest,
  type AskResult,
  type ErrorReason,
  type Evidence,
  type NoAnswerReason,
  type UnavailableReason,
} from "@/lib/ask-contract";

const NO_ANSWER: readonly string[] = ["no-match", "not-stated", "unverified"] satisfies NoAnswerReason[];
const UNAVAILABLE: readonly string[] = [
  "not-indexed",
  "stale",
  "model-offline",
  "no-credentials",
] satisfies UnavailableReason[];
const ERRORS: readonly string[] = [
  "bad-request",
  "unknown-document",
  "timeout",
  "unusable-reply",
  "provider-failed",
  "network",
] satisfies ErrorReason[];

const NETWORK: AskResult = { kind: "error", why: "network" };



function parseEvidence(v: unknown): Evidence | null {
  if (!isRecord(v)) return null;
  const { page, node, quote, section, region, doubt } = v;
  if (typeof page !== "number" || !Number.isInteger(page) || page < 1) return null;
  if (typeof node !== "string" || typeof quote !== "string" || typeof section !== "string") return null;
  if (!isRecord(region)) return null;
  const { bbox, width, height } = region;
  if (!Array.isArray(bbox) || bbox.length !== 4 || !bbox.every(isFiniteNumber)) return null;
  if (!isFiniteNumber(width) || !isFiniteNumber(height) || width <= 0 || height <= 0) return null;
  const [x0, top, x1, bottom] = bbox as number[];
  const ranges: [number, number][] = [];
  if (doubt !== undefined) {
    if (!Array.isArray(doubt)) return null;
    for (const r of doubt) {
      if (!Array.isArray(r) || r.length !== 2 || !r.every((n) => Number.isInteger(n))) return null;
      const [a, z] = r as number[];
      if (a < 0 || z <= a || z > quote.length) return null;
      ranges.push([a, z]);
    }
  }
  return { page, node, quote, section, region: { bbox: [x0, top, x1, bottom], width, height },
           ...(ranges.length ? { doubt: ranges } : {}) };
}

/** Unknown JSON from the server to an AskResult. Anything that does not fit becomes
 *  {kind: "error", why: "network"}, so the UI has one failure path. Re-checks the invariants the
 *  types state (evidence non-empty and at most ASK_MAX_EVIDENCE, page >= 1, region finite, known
 *  kinds and reasons). */
export function parseAskResult(json: unknown): AskResult {
  if (!isRecord(json)) return NETWORK;
  switch (json.kind) {
    case "answer": {
      const { answer, evidence, model } = json;
      if (typeof answer !== "string" || typeof model !== "string") return NETWORK;
      if (!Array.isArray(evidence) || evidence.length < 1 || evidence.length > ASK_MAX_EVIDENCE) return NETWORK;
      const parsed = evidence.map(parseEvidence);
      if (parsed.some((e) => e === null)) return NETWORK;
      return { kind: "answer", answer, evidence: parsed as [Evidence, ...Evidence[]], model };
    }
    case "none": {
      const { why, model } = json;
      if (typeof why !== "string" || !NO_ANSWER.includes(why)) return NETWORK;
      if (model !== null && typeof model !== "string") return NETWORK;
      return { kind: "none", why: why as NoAnswerReason, model };
    }
    case "unavailable": {
      const { why } = json;
      if (typeof why !== "string" || !UNAVAILABLE.includes(why)) return NETWORK;
      return { kind: "unavailable", why: why as UnavailableReason };
    }
    case "busy": {
      const { retryAfterSeconds } = json;
      if (!isFiniteNumber(retryAfterSeconds) || retryAfterSeconds < 0) return NETWORK;
      return { kind: "busy", retryAfterSeconds };
    }
    case "error": {
      const { why } = json;
      if (typeof why !== "string" || !ERRORS.includes(why)) return NETWORK;
      return { kind: "error", why: why as ErrorReason };
    }
    default:
      return NETWORK;
  }
}

/** POST `req` as JSON to `url` and parse the body whatever the HTTP status (the body is the result).
 *  Rejects only with the abort reason when `signal` aborts; any other failure resolves to
 *  {kind: "error", why: "network"}. */
export async function postAsk(url: string, req: AskRequest, signal: AbortSignal): Promise<AskResult> {
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(req),
      signal,
    });
    return parseAskResult(await res.json());
  } catch (err) {
    if (signal.aborted) throw signal.reason ?? err;
    return NETWORK;
  }
}
