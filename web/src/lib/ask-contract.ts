// "Ask this document": what crosses HTTP between POST /api/ask/<slug> and the viewer. A plain
// module, shared by the server (lib/ask) and the client (reflow/react/ask-wire.ts). The server
// sends kinds and reasons only; every sentence the reader meets is in reflow/react/ask-copy.ts.

import type { SourceRegion } from "@/lib/source-region";

export const ASK_MAX_QUESTION_CHARS = 300;
export const ASK_MAX_EVIDENCE = 3;
/** Longest quote an Evidence carries; longer slices are clipped at a word boundary. */
export const ASK_QUOTE_CHARS = 240;
/** Longest summary the model may give (the prompt asks for three sentences); a longer one is refused. */
export const ASK_ANSWER_CHARS = 600;

/** What the client posts. `version` is the manifest's "v" of the document the reader is looking at. */
export type AskRequest = { question: string; version: string };

/** One highlighted place. Everything the client needs to draw it; nothing it needs to look up. */
export type Evidence = {
  /** PDF page, 1-based. */
  page: number;
  /** The transcript's data-node of the block holding the quote. */
  node: string;
  /** The document's own words (never the model's), verbatim, at most ASK_QUOTE_CHARS. */
  quote: string;
  /** Heading trail of the block, innermost last, e.g. "3. Deep Residual Learning > 3.1. Residual Learning". May be "". */
  section: string;
  /** Page-image box in PDF points with the page size: boxPercent() draws it. */
  region: SourceRegion;
  /** [start, end) ranges of `quote` that OCR is unsure of; absent when there are none. */
  doubt?: [number, number][];
};

export type NoAnswerReason =
  | "no-match"     // no passage shares a word with the question (no model call was made)
  | "not-stated"   // the model read the candidates and says the document does not state it
  | "unverified";  // the model answered but the citation or a number could not be checked

export type UnavailableReason =
  | "not-indexed"      // converted before passages existed, or the data is not on this server
  | "stale"            // the request's version differs from the converted data: reload
  | "model-offline"    // Ollama unreachable, or its model is not pulled
  | "no-credentials";  // provider is anthropic and ANTHROPIC_API_KEY is missing or rejected

export type ErrorReason =
  | "bad-request"       // not a JSON {question, version}, question empty or too long, or cross-site
  | "unknown-document"  // the slug is not a sample document
  | "timeout"           // the model did not reply in time
  | "unusable-reply"    // the model's reply was not JSON of the expected shape
  | "provider-failed"   // the provider answered with an HTTP error or an unreadable body
  | "network";          // client side only: the server was unreachable or its reply did not parse

/** The reply of POST /api/ask/<slug>. Reasons only; the reader's sentences come from ask-copy.ts. */
export type AskResult =
  | { kind: "answer"; answer: string; evidence: [Evidence, ...Evidence[]]; model: string }
  | { kind: "none"; why: NoAnswerReason; model: string | null }
  | { kind: "unavailable"; why: UnavailableReason }
  | { kind: "busy"; retryAfterSeconds: number }
  | { kind: "error"; why: ErrorReason };

export type AnswerResult = Extract<AskResult, { kind: "answer" }>;
