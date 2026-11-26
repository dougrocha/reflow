// Every sentence "Ask this document" shows or speaks. The server sends kinds and reasons only.

import type { AskResult } from "@/lib/ask-contract";
import { plural } from "@/lib/format";

/** Fixed announcements: on submit, once after 8 s of waiting, on Clear and on cancelling a question. */
export const ASK_SAYS: { asking: string; stillWorking: string; cleared: string; cancelled: string } = {
  asking: "Searching the document for an answer…",
  stillWorking: "Still working. The model may be starting up.",
  cleared: "Answer cleared.",
  cancelled: "Question cancelled.",
};

/** The innermost part of a heading trail ("3. A > 3.1. B" gives "3.1. B"). */
function innermost(section: string): string {
  const parts = section.split(">");
  return parts[parts.length - 1].trim();
}

function pagesOf(pages: number[], pageLabel: (page: number) => string): string {
  const unique = [...new Set(pages)].sort((a, b) => a - b).map((p) => `page ${pageLabel(p)}`);
  if (unique.length <= 1) return unique[0] ?? "the document";
  return `${unique.slice(0, -1).join(", ")} and ${unique[unique.length - 1]}`;
}

/** What the status region says when a result arrives. An answer says only where it was found
 *  ("Answer found on page 5." / "... on page 5 and page 9."), since focus moves to the answer;
 *  every other kind says the same sentence as messageFor. */
export function announcementFor(result: AskResult, pageLabel: (page: number) => string): string {
  if (result.kind === "answer") {
    return `Answer found on ${pagesOf(result.evidence.map((e) => e.page), pageLabel)}.`;
  }
  return messageFor(result) ?? "";
}

/** The sentence shown under the question box (the input's aria-describedby) for every non-answer
 *  result: one per NoAnswerReason, UnavailableReason and ErrorReason, and busy with its seconds.
 *  Null for an answer. */
export function messageFor(result: AskResult): string | null {
  switch (result.kind) {
    case "answer":
      return null;
    case "none":
      switch (result.why) {
        case "no-match":
          return "No answer found. None of the words in your question appear in this document. Try other words.";
        case "not-stated":
          return "No answer found. The passages closest to your question do not say.";
        case "unverified":
          return "No answer found that could be checked against the document.";
      }
      break;
    case "unavailable":
      switch (result.why) {
        case "not-indexed":
          return "This document has not been prepared for questions.";
        case "stale":
          return "This document was updated after the page loaded. Reload the page to ask.";
        case "model-offline":
          return "The assistant that answers questions is not running right now. Try again later, or use Find.";
        case "no-credentials":
          return "Questions are not set up on this server.";
      }
      break;
    case "busy": {
      const n = Math.max(1, Math.ceil(result.retryAfterSeconds));
      return `Questions are coming in faster than they can be answered. Try again in ${plural(n, "second")}.`;
    }
    case "error":
      switch (result.why) {
        case "bad-request":
          return "The question could not be sent. Check that it is not empty or too long, then try again.";
        case "unknown-document":
          return "This document is not available for questions.";
        case "timeout":
          return "The assistant took too long. Try again.";
        case "unusable-reply":
          return "The assistant gave a reply that could not be used. Try asking in other words.";
        case "provider-failed":
          return "The assistant could not answer. Try again later.";
        case "network":
          return "The question could not be sent. Check your connection and try again.";
      }
  }
  return "The question could not be answered. Try again.";
}

/** The label above the model's answer text: "Summary by gemma4:e4b". */
export function summaryBy(model: string): string {
  return `Summary by ${model}`;
}

/** Said after "Show on page": "Showing <section or 'the passage'> on page <label>. Press Back to answer to return." */
export function shownOnPage(pageLabel: string, section: string): string {
  const what = innermost(section) || "the passage";
  return `Showing ${what} on page ${pageLabel}. Press Back to answer to return.`;
}

/** Said after "Show in transcript": "Highlighted on page <label>[, <section>]. Press Alt+Shift+R to return to the answer." */
export function highlightedInTranscript(pageLabel: string, section: string): string {
  const where = innermost(section);
  return `Highlighted on page ${pageLabel}${where ? `, ${where}` : ""}. Press Alt+Shift+R to return to the answer.`;
}

/** Said when "Show in transcript" cannot mark the passage (its page did not load, or the loaded page
 *  has no such block). `onPage` is what "Show on page" announced when the viewer fell back to it. */
export function notInTranscript(onPage: string | null): string {
  const sorry = "Could not find the passage in the transcript.";
  return onPage ? `${sorry} ${onPage}` : sorry;
}

/** The tooltip of a word OCR is unsure of, in a quote (the transcript's own wording). */
export const OCR_DOUBT = "OCR is unsure of this word";
