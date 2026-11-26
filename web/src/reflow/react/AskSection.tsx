"use client";

import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Button } from "@/components/ui/native-button";
import { INPUT_CLASS } from "@/components/ui/input-class";
import { ASK_MAX_QUESTION_CHARS, type AnswerResult } from "@/lib/ask-contract";
import { messageFor } from "./ask-copy";
import { AskPanel } from "./AskPanel";
import { escapeAction, submitAction, useAsk } from "./useAsk";

/**
 * The question form of the "Ask this document" section: a role=search form (label "Question",
 * maxLength ASK_MAX_QUESTION_CHARS) followed by `children` (the AskPanel). `message` is shown under
 * the form and is the input's aria-describedby. While a question is in flight the Submit button is
 * aria-disabled, not disabled, so focus stays where it is: it does nothing when the box holds the
 * question already in flight (`repeat`), and a different question replaces the one in flight.
 * Escape in the box or on Submit cancels a question in flight; otherwise it clears a non-empty box;
 * in an empty box it asks the viewer to collapse the section (`onCollapse`), which focuses its header.
 */
function AskForm({ value, onChange, onSubmit, onCancel, onCollapse, busy, repeat, message, inputRef, children }: {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  /** Escape while `busy`: abandon the question in flight. */
  onCancel: () => void;
  /** Escape in an empty box: collapse the section and put focus on its header. */
  onCollapse: () => void;
  /** A question is in flight. */
  busy: boolean;
  /** The box holds the question in flight, so submitting would only repeat it. */
  repeat: boolean;
  /** messageFor(result) for a non-answer, else null. */
  message: string | null;
  inputRef: React.Ref<HTMLInputElement>;
  children?: React.ReactNode;
}): React.ReactElement {
  const id = useId();
  const messageId = `${id}-msg`;
  return (
    <div className="flex flex-col gap-2">
      <form role="search" aria-label="Ask a question about this document" className="flex flex-col gap-1.5"
            onSubmit={(e) => { e.preventDefault(); if (!repeat) onSubmit(); }}
            onKeyDown={(e) => {
              if (e.key !== "Escape") return;
              e.preventDefault();
              e.stopPropagation();
              const action = escapeAction(busy, value);
              if (action === "cancel") onCancel();
              else if (action === "clear-box") onChange("");
              else onCollapse();
            }}>
        <label htmlFor={`${id}-q`} className="type-small font-medium">Question</label>
        <div className="flex items-center gap-2">
          <input id={`${id}-q`} ref={inputRef} type="text" enterKeyHint="send" autoComplete="off"
                 maxLength={ASK_MAX_QUESTION_CHARS} aria-keyshortcuts="Alt+Shift+A"
                 aria-describedby={message ? messageId : undefined}
                 value={value} onChange={(e) => onChange(e.target.value)}
                 className={`${INPUT_CLASS} min-w-0 flex-1`} />
          <Button type="submit" aria-disabled={repeat || undefined} className="aria-disabled:opacity-50">Submit</Button>
        </div>
      </form>
      {message ? <p id={messageId} className="m-0 type-small">{message}</p> : null}
      {children}
    </div>
  );
}

/** What the viewer asks the controller to do with focus. `n` changes on every request. */
export type AskFocusRequest = { n: number; evidence: number | null };

/**
 * Everything the viewer needs of "Ask this document", in one lazily loaded piece: the question
 * state (useAsk), the form and the answer panel. The viewer keeps the section's header and open
 * state, and the highlights (overlays and marks); it is told of each answer through `onAnswer`.
 *
 * The form is drawn into `host`, an element in the Ask section of whichever sidebar shows (the
 * column on a wide screen, the Contents sheet on a narrow one), through a portal: the question and
 * the answer live here, and so survive the section collapsing, the sheet closing and the window
 * crossing the breakpoint. Without a host (the sheet is closed) nothing is drawn.
 */
export function AskController(p: {
  url: string;
  version: string;
  say: (text: string) => void;
  pageLabel: (page: number) => string;
  lang: string;
  dir: "ltr" | "rtl";
  canShowOnPage: (page: number) => boolean;
  host: HTMLElement | null;
  /** Alt+Shift+A: changes to focus the box (the viewer has opened the section). */
  boxRequest: number;
  /** Alt+Shift+R and "Back to answer": focus the answer (or its Show on page button), else the box. */
  answerRequest: AskFocusRequest;
  /** Which evidence is shown on the page image right now, or null. */
  active: number | null;
  /** Escape in the empty box: collapse the section and focus its header. */
  onCollapse: () => void;
  onAnswer: (result: AnswerResult | null) => void;
  onShowOnPage: (result: AnswerResult, i: number) => void;
  onShowInTranscript: (result: AnswerResult, i: number) => void;
}): React.ReactElement | null {
  const ask = useAsk({ url: p.url, version: p.version, onAnnounce: p.say, pageLabel: p.pageLabel });
  const [value, setValue] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const answerRef = useRef<HTMLElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const pending = useRef<"box" | "answer" | null>(null);
  const lastBox = useRef(0);
  const lastAnswer = useRef(0);
  // The answer that already took focus: the panel is drawn again when the host changes, but only a
  // new answer moves focus.
  const focused = useRef<AnswerResult | null>(null);

  const takeFocus = (result: AnswerResult, article: HTMLElement) => {
    if (focused.current === result) return;
    article.focus();
    if (document.activeElement === article) focused.current = result;
  };

  const answer = ask.state.status === "done" && ask.state.result.kind === "answer" ? ask.state.result : null;
  const onAnswer = p.onAnswer;
  useEffect(() => { onAnswer(answer); }, [answer, onAnswer]);

  // Requests from the viewer. The section may not be showing yet (or the sheet not open), so they
  // wait for the render that has the form.
  useEffect(() => {
    if (p.boxRequest === lastBox.current) return;
    lastBox.current = p.boxRequest;
    pending.current = "box";
  }, [p.boxRequest]);
  useEffect(() => {
    if (p.answerRequest.n === lastAnswer.current) return;
    lastAnswer.current = p.answerRequest.n;
    pending.current = "answer";
  }, [p.answerRequest]);
  useEffect(() => {
    let frame = 0;
    const reveal = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const input = inputRef.current;
        if (input && document.activeElement === input) {
          input.scrollIntoView({ block: "nearest", inline: "nearest" });
        }
      });
    };
    const viewport = window.visualViewport;
    window.addEventListener("resize", reveal);
    viewport?.addEventListener("resize", reveal);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("resize", reveal);
      viewport?.removeEventListener("resize", reveal);
    };
  }, [p.host]);
  useEffect(() => {
    const want = pending.current;
    if (!want) return;
    if (want === "answer") {
      const i = p.answerRequest.evidence;
      const target = (i !== null ? panelRef.current?.querySelector<HTMLElement>(`[data-ask-show-page="${i}"]`) : null) ?? answerRef.current;
      if (target) {
        pending.current = null;
        target.focus();
        target.scrollIntoView({ block: "nearest", inline: "nearest" });
        return;
      }
    }
    if (inputRef.current) {
      pending.current = null;
      inputRef.current.focus();
      inputRef.current.scrollIntoView({ block: "nearest", inline: "nearest" });
      if (want === "box") inputRef.current.select();
    }
  });

  const result = ask.state.status === "done" ? ask.state.result : null;
  const message = result ? messageFor(result) : null;
  if (!p.host) return null;
  return createPortal(
    <AskForm value={value} onChange={setValue}
             onSubmit={() => { if (submitAction(ask.state, value) === "ask") ask.submit(value.trim()); }}
             onCancel={ask.cancel} onCollapse={p.onCollapse} busy={ask.state.status === "asking"}
             repeat={ask.state.status === "asking" && submitAction(ask.state, value) === "ignore"} message={message}
             inputRef={inputRef}>
      <div ref={panelRef}>
        {answer ? (
          <AskPanel result={answer} lang={p.lang} dir={p.dir} pageLabel={p.pageLabel}
                    canShowOnPage={p.canShowOnPage} active={p.active} answerRef={answerRef}
                    takeFocus={takeFocus}
                    onShowOnPage={(i) => p.onShowOnPage(answer, i)}
                    onShowInTranscript={(i) => p.onShowInTranscript(answer, i)}
                    onClear={() => { ask.clear(); pending.current = "box"; }} />
        ) : null}
      </div>
    </AskForm>,
    p.host,
  );
}
