"use client";

import { useCallback, useEffect, useReducer, useRef } from "react";
import type { AskResult } from "@/lib/ask-contract";
import { ASK_SAYS, announcementFor } from "./ask-copy";
import { postAsk } from "./ask-wire";

export type AskState =
  | { status: "idle" }
  | { status: "asking"; question: string }
  | { status: "done"; question: string; result: AskResult };

type AskAction =
  | { type: "submit"; question: string }
  | { type: "result"; result: AskResult }
  | { type: "clear" };

/** The state transitions, apart from React. A result only lands while asking. */
export function askReducer(state: AskState, action: AskAction): AskState {
  switch (action.type) {
    case "submit":
      return { status: "asking", question: action.question };
    case "result":
      return state.status === "asking"
        ? { status: "done", question: state.question, result: action.result }
        : state;
    case "clear":
      return { status: "idle" };
  }
}

/** What Enter or Submit does with the box's text: nothing for an empty box or for the question
 *  already in flight, else ask (a different question replaces the one in flight). */
export function submitAction(state: AskState, value: string): "ask" | "ignore" {
  const question = value.trim();
  if (!question) return "ignore";
  return state.status === "asking" && state.question === question ? "ignore" : "ask";
}

/** What Escape in the box does: cancel the question in flight, else clear a non-empty box, else
 *  close the band. */
export function escapeAction(busy: boolean, value: string): "cancel" | "clear-box" | "close" {
  if (busy) return "cancel";
  return value ? "clear-box" : "close";
}

const STILL_WORKING_MS = 8000;

/** The question's state machine around postAsk. `enabled` is false without both `url` and
 *  `version` (the viewer then renders no Ask button). `submit` aborts any request in flight,
 *  announces ASK_SAYS.asking, then ASK_SAYS.stillWorking once after 8 s, then announcementFor(result).
 *  `clear` aborts, returns to idle and announces ASK_SAYS.cleared; `cancel` does the same and
 *  announces ASK_SAYS.cancelled. Unmount aborts. */
export function useAsk(o: {
  url?: string;
  version?: string;
  onAnnounce: (text: string) => void;
  pageLabel: (page: number) => string;
}): {
  state: AskState;
  enabled: boolean;
  submit: (question: string) => void;
  clear: () => void;
  cancel: () => void;
} {
  const [state, dispatch] = useReducer(askReducer, { status: "idle" } as AskState);
  const { url, version } = o;
  const enabled = Boolean(url && version);
  const live = useRef(o);
  useEffect(() => {
    live.current = o;
  });
  const controller = useRef<AbortController | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const stop = useCallback(() => {
    controller.current?.abort();
    controller.current = null;
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = null;
  }, []);

  useEffect(() => stop, [stop]);

  const submit = useCallback(
    (raw: string) => {
      const question = raw.trim();
      if (!question || !url || !version) return;
      stop();
      const ctl = new AbortController();
      controller.current = ctl;
      dispatch({ type: "submit", question });
      live.current.onAnnounce(ASK_SAYS.asking);
      timer.current = setTimeout(() => {
        timer.current = null;
        if (!ctl.signal.aborted) live.current.onAnnounce(ASK_SAYS.stillWorking);
      }, STILL_WORKING_MS);
      postAsk(url, { question, version }, ctl.signal).then(
        (result) => {
          if (ctl.signal.aborted) return;
          if (timer.current !== null) clearTimeout(timer.current);
          timer.current = null;
          controller.current = null;
          dispatch({ type: "result", result });
          live.current.onAnnounce(announcementFor(result, live.current.pageLabel));
        },
        () => {
          /* aborted: a newer submit, Clear or unmount owns the state now */
        },
      );
    },
    [url, version, stop],
  );

  const clear = useCallback(() => {
    stop();
    dispatch({ type: "clear" });
    live.current.onAnnounce(ASK_SAYS.cleared);
  }, [stop]);

  const cancel = useCallback(() => {
    stop();
    dispatch({ type: "clear" });
    live.current.onAnnounce(ASK_SAYS.cancelled);
  }, [stop]);

  return { state, enabled, submit, clear, cancel };
}
