"use client";

import type { ReactNode } from "react";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";

/**
 * The sidebar on narrow screens: a sheet from the left holding the same accordion as the column
 * (table of contents and Ask a question when the document can answer, then the download buttons). Its title is for the dialog's
 * name only; the accordion's own headings are what is seen. The sheet's close button sits in the
 * top strip above them. It is loaded on demand (the
 * viewer imports it with React.lazy the first time it opens), so Base UI's dialog code is
 * not part of the viewer's first load. The trigger is the viewer's own button, not a
 * SheetTrigger, so it exists before this module does; `finalFocus` says where focus goes on
 * close (Base UI's default, true, is the previously focused element: that button).
 */
export function ContentsSheet({ open, onOpenChange, finalFocus, title, children }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  finalFocus: () => boolean | HTMLElement | null;
  /** The dialog's name. */
  title: string;
  children: ReactNode;
}) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="left" className="!w-[min(22rem,90vw)] gap-0 overflow-y-auto pt-12" finalFocus={finalFocus}>
        <SheetTitle className="sr-only">{title}</SheetTitle>
        {children}
      </SheetContent>
    </Sheet>
  );
}
