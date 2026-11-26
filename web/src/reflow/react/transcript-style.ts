// Shared look of reflow transcript HTML, used by the viewer and the inspector. Plain
// constants, so both client components can import them.

// A white-on-muted card, used for page images and transcript pages alike.
export const CARD_CLASS = "rounded-xl bg-card text-card-foreground shadow-sm ring-1 ring-foreground/10";

// An answer's quote in the transcript (ask-highlight.ts): <mark data-ask> gets a yellow background
// and a 2px underline, so it is not colour alone; when the quote cannot be matched in the DOM the
// whole block ([data-ask-block]) gets an outline and a tint instead. In forced-colors mode the
// system Highlight colours replace the yellow and the outline keeps its line. Nothing animates.
const ASK_MARK_CLASS = [
  "[&_mark[data-ask]]:rounded-sm [&_mark[data-ask]]:bg-[#fde047] [&_mark[data-ask]]:text-[#111827]",
  "[&_mark[data-ask]]:underline [&_mark[data-ask]]:decoration-2 [&_mark[data-ask]]:underline-offset-2",
  "forced-colors:[&_mark[data-ask]]:[background-color:Highlight] forced-colors:[&_mark[data-ask]]:[color:HighlightText]",
  "[&_[data-ask-block]]:rounded-sm [&_[data-ask-block]]:bg-[#fde047]/25 [&_[data-ask-block]]:outline-3 [&_[data-ask-block]]:outline-offset-4 [&_[data-ask-block]]:outline-[#111827]",
  "forced-colors:[&_[data-ask-block]]:[outline-color:Highlight]",
].join(" ");

// Transcript HTML from reflow, styled by the stylesheet the standalone HTML uses too
// (transcript.css): its type scale, rhythm and the classes reflow writes. Colours come from the
// theme, so they follow the OS scheme.
export const TRANSCRIPT_CLASS = "reflow-transcript break-words [overflow-wrap:anywhere] " + ASK_MARK_CLASS;

/**
 * The id of a node's element in transcript HTML, as reflow writes it (render/html.py,
 * anchor()). Only headings, tables and figures carry one; paragraphs, lists and captions
 * have no element to match.
 */
export function transcriptAnchor(nodeId: string): string {
  return "n" + nodeId.replace(/[^0-9a-zA-Z_-]/g, "-");
}
