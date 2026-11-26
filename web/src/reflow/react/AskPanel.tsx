"use client";

import { type ReactNode, useEffect, useId, useImperativeHandle, useRef } from "react";
import { Button } from "@/components/ui/native-button";
import type { AnswerResult, Evidence } from "@/lib/ask-contract";
import { OCR_DOUBT, summaryBy } from "./ask-copy";

/**
 * An answer: an <article tabIndex={-1}> (`answerRef`) labelled by its "Answer" heading and described
 * by the answer. Quote first: each evidence's quote (in `lang`/`dir`) with its page and section,
 * and "Show on page" (only when canShowOnPage) and "Show in transcript" buttons; then the model's
 * answer under summaryBy(model); then "Clear answer". Moves focus to the article once per result
 * (`takeFocus`, which remembers it): a panel drawn again (the sidebar changed) does not take it
 * twice, and a result whose panel was hidden at first (its section collapsed) takes it as soon as
 * it shows. Text only, never HTML.
 */
/** The quote as it is, with each word OCR is unsure of marked as the transcript marks it. */
function quoteParts(ev: Evidence): ReactNode {
  if (!ev.doubt?.length) return ev.quote;
  const parts: ReactNode[] = [];
  let at = 0;
  for (const [a, z] of [...ev.doubt].sort((x, y) => x[0] - y[0])) {
    if (a < at) continue;
    if (a > at) parts.push(ev.quote.slice(at, a));
    parts.push(<span key={a} className="ocr-doubt cursor-help underline decoration-dotted decoration-2 underline-offset-4" title={OCR_DOUBT}>{ev.quote.slice(a, z)}</span>);
    at = z;
  }
  if (at < ev.quote.length) parts.push(ev.quote.slice(at));
  return parts;
}

export function AskPanel({ result, lang, dir, pageLabel, canShowOnPage, active, onShowOnPage, onShowInTranscript, onClear, answerRef, takeFocus }: {
  result: AnswerResult;
  lang: string;
  dir: "ltr" | "rtl";
  /** pageText(page, printed). */
  pageLabel: (page: number) => string;
  /** The page has an image to outline on. */
  canShowOnPage: (page: number) => boolean;
  /** Which evidence is shown right now, or null. */
  active: number | null;
  onShowOnPage: (i: number) => void;
  onShowInTranscript: (i: number) => void;
  onClear: () => void;
  /** The article, focused by Alt+Shift+R. */
  answerRef: React.Ref<HTMLElement>;
  /** Called after every render with the result and the article: focuses it if this result has not had focus yet. */
  takeFocus: (result: AnswerResult, article: HTMLElement) => void;
}): React.ReactElement | null {
  const id = useId();
  const own = useRef<HTMLElement>(null);
  useImperativeHandle(answerRef, () => own.current as HTMLElement);

  // A new answer takes focus: its heading and description are the answer, and the status region
  // says only where it was found.
  useEffect(() => { if (own.current) takeFocus(result, own.current); });

  const many = result.evidence.length > 1;
  const quoteIds = result.evidence.map((_, i) => `${id}-q${i}`).join(" ");
  return (
    <article ref={own} tabIndex={-1} aria-labelledby={`${id}-h`} aria-describedby={`${quoteIds} ${id}-a`}
             className="max-h-[50vh] overflow-y-auto rounded-lg border bg-card p-3 text-card-foreground focus:outline-none focus-visible:ring-3 focus-visible:ring-ring">
      <h3 id={`${id}-h`} className="type-label font-semibold">Answer</h3>
      <ol role="list" className="mt-2 flex flex-col gap-3">
        {result.evidence.map((ev, i) => {
          const page = pageLabel(ev.page);
          return (
            <li key={`${ev.page}-${ev.node}-${i}`} id={`${id}-q${i}`} className="flex flex-col gap-1.5">
              <p className="m-0 type-small text-muted-foreground">
                <span className="font-medium text-foreground">Page {page}</span>
                {ev.section ? <span lang={lang} dir="auto">, {ev.section}</span> : null}
              </p>
              <blockquote lang={lang} dir={dir} className="m-0 border-s-4 border-foreground/40 ps-3 text-base">{quoteParts(ev)}</blockquote>
              <div className="flex flex-wrap gap-2">
                {canShowOnPage(ev.page) ? (
                  <Button type="button" variant="outline" size="sm" data-ask-show-page={i} aria-pressed={active === i}
                          onClick={() => onShowOnPage(i)}>
                    Show on page{many ? <span className="sr-only">, passage {i + 1}</span> : null}
                  </Button>
                ) : null}
                <Button type="button" variant="outline" size="sm" data-ask-show-transcript={i} onClick={() => onShowInTranscript(i)}>
                  Show in transcript{many ? <span className="sr-only">, passage {i + 1}</span> : null}
                </Button>
              </div>
            </li>
          );
        })}
      </ol>
      <div id={`${id}-a`} className="mt-3 flex flex-col gap-0.5">
        <p className="m-0 type-small text-muted-foreground">{summaryBy(result.model)}</p>
        <p className="m-0">{result.answer}</p>
      </div>
      <div className="mt-3">
        <Button type="button" variant="ghost" size="sm" data-ask-clear="" onClick={onClear}>Clear answer</Button>
      </div>
    </article>
  );
}
