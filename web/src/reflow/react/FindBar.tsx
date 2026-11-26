"use client";

import { useId } from "react";
import { ChevronDown, ChevronUp, Search } from "lucide-react";
import { Button } from "@/components/ui/native-button";
import { INPUT_CLASS } from "@/components/ui/input-class";
import type { FindState } from "./useFind";

/**
 * The find box in the toolbar: a search field, "n of m", and previous and next buttons. Enter
 * steps to the next match, Shift+Enter to the previous one, Escape clears. On a narrow screen
 * the field folds away behind a search button. The current match is spoken by the viewer's
 * status region, so the count here is plain text, not a live region.
 */
export function FindBar({ value, onChange, find, open, onOpenChange, inputRef }: {
  value: string;
  onChange: (value: string) => void;
  find: FindState;
  /** Narrow screens only: whether the field is showing. */
  open: boolean;
  onOpenChange: (open: boolean) => void;
  inputRef: React.Ref<HTMLInputElement>;
}) {
  const id = useId();
  const has = find.query !== "";
  const count = !value.trim() || !has
    ? ""
    : find.total === 0
      ? find.searching ? "Searching…" : "No matches"
      : `${find.rank} of ${find.total.toLocaleString("en-US")}${find.searching ? "+" : ""}`;
  return (
    <>
      <Button type="button" variant="outline" size="icon-lg" className="sm:hidden"
              aria-label="Find in document" aria-expanded={open} aria-controls={id}
              onClick={() => onOpenChange(!open)}>
        <Search aria-hidden="true" />
      </Button>
      <form id={id} role="search" aria-label="Find in document"
            className={`items-center gap-1.5 ${open ? "order-last flex basis-full sm:order-none sm:basis-auto" : "hidden sm:flex"}`}
            onSubmit={(e) => { e.preventDefault(); find.next(); }}>
        <label htmlFor={`${id}-q`} className="sr-only">Find in document</label>
        <input id={`${id}-q`} ref={inputRef} type="search" enterKeyHint="search" autoComplete="off"
               placeholder="Find in document" aria-keyshortcuts="Alt+Shift+F"
               value={value} onChange={(e) => onChange(e.target.value)}
               onFocus={find.warm}
               onKeyDown={(e) => {
                 if (e.key === "Enter" && e.shiftKey) { e.preventDefault(); find.prev(); }
                 else if (e.key === "Escape" && value) { e.preventDefault(); e.stopPropagation(); onChange(""); }
               }}
               className={`${INPUT_CLASS} flex-1 sm:w-48 sm:flex-none`} />
        <span className="min-w-[4.5rem] whitespace-nowrap type-small text-muted-foreground tabular-nums">{count}</span>
        <Button type="button" variant="outline" size="icon" disabled={find.total === 0} onClick={find.prev}>
          <ChevronUp aria-hidden="true" /><span className="sr-only">Previous match</span>
        </Button>
        <Button type="button" variant="outline" size="icon" disabled={find.total === 0} onClick={find.next}>
          <ChevronDown aria-hidden="true" /><span className="sr-only">Next match</span>
        </Button>
        {find.failed > 0 && find.query ? <span className="type-small text-destructive">Some pages could not be searched.</span> : null}
      </form>
    </>
  );
}
