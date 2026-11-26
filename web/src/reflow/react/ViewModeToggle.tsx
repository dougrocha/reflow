"use client";

import { useRef, useState, type ReactNode } from "react";

// One segment of the group. Written out whole, not composed from the ui toggle's variants and
// merged: nothing overrides anything, and the first load carries no class merger.
const SEGMENT = "group/toggle inline-flex h-8 min-w-8 shrink-0 items-center justify-center gap-1 rounded-none border border-l-0 border-input bg-transparent px-2 text-sm font-medium whitespace-nowrap transition-all outline-none first:rounded-l-lg first:border-l last:rounded-r-lg hover:bg-muted hover:text-foreground focus:z-10 focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50 aria-pressed:bg-muted [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4";

type ViewModeOption<T extends string> = {
  value: T;
  label: string;
  shortLabel?: string;
  icon?: ReactNode;
};

/** A single-select toolbar with roving focus. Arrow keys move focus; Space or Enter picks. */
export function ViewModeToggle<T extends string>({
  value,
  onChange,
  options,
  label = "View",
}: {
  value: T;
  onChange: (next: T) => void;
  options: ViewModeOption<T>[];
  label?: string;
}) {
  const [focused, setFocused] = useState(value);
  const buttons = useRef(new Map<T, HTMLButtonElement>());
  return (
    <div role="group" aria-label={label} className="flex w-fit rounded-lg"
         onKeyDown={(event) => {
           const keys = ["ArrowLeft", "ArrowRight", "Home", "End"];
           if (!keys.includes(event.key)) return;
           event.preventDefault();
           const index = options.findIndex((option) => buttons.current.get(option.value) === document.activeElement);
           const rtl = getComputedStyle(event.currentTarget).direction === "rtl";
           const forward = event.key === (rtl ? "ArrowLeft" : "ArrowRight");
           const next = event.key === "Home" ? 0 : event.key === "End" ? options.length - 1
             : (index + (forward ? 1 : -1) + options.length) % options.length;
           const option = options[next];
           if (option) buttons.current.get(option.value)?.focus();
         }}>
      {options.map((o) => (
        <button key={o.value} type="button" aria-label={o.label} aria-pressed={o.value === value}
                ref={(element) => { if (element) buttons.current.set(o.value, element); else buttons.current.delete(o.value); }}
                tabIndex={o.value === focused ? 0 : -1}
                onFocus={() => setFocused(o.value)} onClick={() => onChange(o.value)}
                className={SEGMENT}>
          {o.icon}
          {o.shortLabel ? (
            <>
              <span aria-hidden="true" className="sm:hidden">{o.shortLabel}</span>
              <span aria-hidden="true" className="hidden sm:inline">{o.label}</span>
            </>
          ) : o.label}
        </button>
      ))}
    </div>
  );
}
