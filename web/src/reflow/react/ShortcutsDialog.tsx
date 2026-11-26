"use client";

import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Kbd } from "@/components/ui/kbd";
import { VIEWER_SHORTCUTS, shortcutKeys } from "./shortcuts";

/**
 * The viewer's keyboard shortcuts as a real table. A Base UI dialog: focus is trapped inside,
 * Escape closes it, and focus returns to where it was.
 */
export function ShortcutsDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="type-h4">Keyboard shortcuts</DialogTitle>
          <DialogDescription>
            In the document viewer. Each is Alt and Shift pressed together with a letter (Option and Shift on a Mac), so none of them gets in the way of typing.
          </DialogDescription>
        </DialogHeader>
        <table className="w-full text-left type-small">
          <caption className="sr-only">Document viewer shortcuts</caption>
          <thead>
            <tr className="border-b">
              <th scope="col" className="py-2 pr-4 type-label">Keys</th>
              <th scope="col" className="py-2 type-label">Action</th>
            </tr>
          </thead>
          <tbody>
            {VIEWER_SHORTCUTS.map((s) => (
              <tr key={s.id} className="border-b last:border-0">
                <td className="py-2 pr-4 align-top whitespace-nowrap">
                  <span className="inline-flex items-center gap-1">
                    {shortcutKeys(s).map((k, i) => (
                      <span key={k} className="inline-flex items-center gap-1">
                        {i > 0 ? <span aria-hidden="true">+</span> : null}
                        <Kbd>{k}</Kbd>
                      </span>
                    ))}
                  </span>
                </td>
                <td className="py-2 align-top">{s.action}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="type-small text-muted-foreground">
          Escape closes a dialog or the contents panel. Your browser&rsquo;s own find (Ctrl+F or Cmd+F) is left alone.{" "}
          <a href="/about#keyboard" className="underline underline-offset-4">All keyboard shortcuts</a>
        </p>
      </DialogContent>
    </Dialog>
  );
}
