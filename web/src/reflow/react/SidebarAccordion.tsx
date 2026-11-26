import { ChevronDown } from "lucide-react";

/**
 * One section of the viewer's sidebar accordion (the disclosure pattern): a heading holding a real
 * button, with aria-expanded and aria-controls, over a panel that is hidden while closed. The
 * panel stays in the DOM, so aria-controls always names an element and what the reader typed
 * survives a collapse. Sections do not know each other: any number can be open. The heading is an
 * h2, the level the sidebar's old "Contents" title had. The chevron turns only when motion is allowed; the
 * panel itself opens and closes at once, so there is no animation to turn off.
 */
export function AccordionSection({ id, title, open, onToggle, buttonRef, keyShortcuts, panelClassName = "", children }: {
  /** Unique on the page: the button is `<id>-trigger`, the panel `<id>-panel`. */
  id: string;
  title: string;
  open: boolean;
  onToggle: () => void;
  buttonRef?: React.Ref<HTMLButtonElement>;
  /** The section's keyboard shortcut, for aria-keyshortcuts on its button. */
  keyShortcuts?: string;
  panelClassName?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="border-b last:border-b-0">
      <h2 className="m-0 type-h4">
        <button ref={buttonRef} type="button" id={`${id}-trigger`} aria-expanded={open} aria-controls={`${id}-panel`} aria-keyshortcuts={keyShortcuts}
                onClick={onToggle}
                className="flex w-full items-center justify-between gap-2 px-5 py-3 text-start type-h4 outline-offset-[-3px] hover:bg-muted">
          {title}
          <ChevronDown aria-hidden="true" className={`size-4 shrink-0 motion-safe:transition-transform ${open ? "rotate-180" : ""}`} />
        </button>
      </h2>
      <div id={`${id}-panel`} hidden={!open} className={`px-5 pb-5 ${panelClassName}`}>{children}</div>
    </div>
  );
}
