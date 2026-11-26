"use client";

import { useId, useState } from "react";
import { INPUT_CLASS } from "@/components/ui/input-class";
import { parsePage } from "@/lib/pages";

// The toolbar's page-number field: go to a PDF page by number.

// The page-number field: the ui Input's look at a fixed width, written without the class
// merger so the first load does not carry it.
const PAGE_INPUT = `${INPUT_CLASS} w-16 text-right tabular-nums`;
export function PageJump({ current, count, onGo, inputRef }: {
  current: number; count: number; onGo: (p: number) => void; inputRef: React.Ref<HTMLInputElement>;
}) {
  const [value, setValue] = useState("");
  const id = useId();
  return (
    <form className="flex items-center gap-1.5 whitespace-nowrap"
          onSubmit={(e) => { e.preventDefault(); const n = parsePage(value); if (n) onGo(n); setValue(""); }}>
      <label htmlFor={id} className="sr-only">Go to PDF page number</label>
      <input ref={inputRef} id={id} aria-keyshortcuts="Alt+Shift+G" inputMode="numeric" pattern="[0-9]*" placeholder={String(current)}
             value={value} onChange={(e) => setValue(e.target.value)}
             className={PAGE_INPUT} />
      <span aria-hidden="true" className="type-small text-muted-foreground tabular-nums">of {count}</span>
      <span className="sr-only">of {count}. Current PDF page {current}.</span>
    </form>
  );
}
