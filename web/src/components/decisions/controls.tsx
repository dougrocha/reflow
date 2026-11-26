"use client";

import { useId, useRef, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { bodies, type Body, type Sent } from "@/lib/decisions/client";
import { ALT_MAX_CHARS, NOW_MAX_CHARS, NOW_MAX_GROWTH } from "@/lib/decisions/limits";

// The pieces the inspector's decision panel, its decided-node view and the document page's
// decisions list share: one way to send a choice and say what happened, and the two small forms
// (another reading, an alt text). Real buttons and labelled fields; every result goes to a
// polite live region.

export type Outcome = { message: string; reload: boolean } | null;

/** Run choices one at a time; `outcome` is what the live region says. */
export function useAction(post: (body: Body) => Promise<Sent>) {
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<Outcome>(null);
  const run = async (body: Body): Promise<boolean> => {
    if (busy) return false;
    setBusy(true);
    setOutcome({ message: "Saving…", reload: false });
    const sent = await post(body);
    setOutcome({ message: sent.message, reload: !sent.ok && sent.reload });
    setBusy(false);
    return sent.ok;
  };
  return { busy, outcome, run };
}

/** The polite live region. It is always in the page, so a screen reader hears what is put in it.
 *  A 409 adds a Reload button beside its sentence. */
export function Announcer({ outcome, className }: { outcome: Outcome; className?: string }) {
  return (
    <div className={className}>
      <p aria-live="polite" data-decision-status className="type-small">{outcome?.message ?? ""}</p>
      {outcome?.reload ? (
        <Button type="button" variant="outline" size="sm" className="mt-1" onClick={() => window.location.reload()}>
          Reload
        </Button>
      ) : null}
    </div>
  );
}

/** A button that stays focusable and does nothing while a choice is being sent. */
export function ActionButton({ busy, onClick, children, variant = "outline", ...rest }: {
  busy: boolean; onClick: () => void; children: React.ReactNode; variant?: "outline" | "default" | "ghost"; "aria-label"?: string;
}) {
  return (
    <Button type="button" variant={variant} size="sm" aria-disabled={busy || undefined} className={busy ? "cursor-default opacity-70" : undefined}
      onClick={() => { if (!busy) onClick(); }} {...rest}>
      {children}
    </Button>
  );
}

/** One labelled field for another reading of an OCR word. */
export function ReadingForm({ was, busy, onSave, onCancel }: {
  was: string; busy: boolean; onSave: (now: string) => void; onCancel: () => void;
}) {
  const id = useId();
  const [now, setNow] = useState(was);
  const limit = Math.min(NOW_MAX_CHARS, NOW_MAX_GROWTH * [...was].length);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!busy && now.trim() !== "") onSave(now);
  };
  return (
    <form onSubmit={submit} className="flex flex-col gap-1.5">
      <label htmlFor={id} className="type-small font-medium">Another reading for ‘{was}’</label>
      <Input id={id} value={now} onChange={(e) => setNow(e.target.value)} maxLength={limit} autoFocus autoComplete="off" spellCheck={false}
        aria-describedby={`${id}-hint`} />
      <p id={`${id}-hint`} className="type-caption text-muted-foreground">One line, at most {limit} characters.</p>
      <div className="flex gap-2">
        <Button type="submit" size="sm" aria-disabled={busy || undefined}>Save reading</Button>
        <Button type="button" variant="outline" size="sm" onClick={onCancel}>Cancel</Button>
      </div>
    </form>
  );
}

/** The alt text of a figure: a labelled textarea, a Decorative checkbox and Save. */
export function AltForm({ initial, decorativeInitial = false, busy, onSave, onCancel, extra }: {
  initial: string; decorativeInitial?: boolean; busy: boolean; onSave: (alt: { text: string; decorative: boolean }) => void; onCancel?: () => void;
  /** Controls placed under the field (such as "Use the previous alt text"). */
  extra?: (set: (text: string) => void) => React.ReactNode;
}) {
  const id = useId();
  const [text, setText] = useState(initial);
  const [decorative, setDecorative] = useState(decorativeInitial);
  const field = useRef<HTMLTextAreaElement>(null);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (busy) return;
    if (!decorative && text.trim() === "") { field.current?.focus(); return; }
    onSave({ text: decorative ? "" : text, decorative });
  };
  return (
    <form onSubmit={submit} className="flex flex-col gap-2">
      <label htmlFor={id} className="type-small font-medium">Alt text</label>
      <Textarea id={id} ref={field} value={text} onChange={(e) => setText(e.target.value)} maxLength={ALT_MAX_CHARS} rows={3}
        disabled={decorative} aria-describedby={`${id}-hint`} />
      <p id={`${id}-hint`} className="type-caption text-muted-foreground">
        Say what the figure shows, in at most {ALT_MAX_CHARS.toLocaleString("en-US")} characters.
      </p>
      <label className="flex items-center gap-2 type-small">
        <input type="checkbox" checked={decorative} onChange={(e) => setDecorative(e.target.checked)} className="size-4" />
        Decorative: screen readers skip it
      </label>
      {extra?.(setText)}
      <div className="flex gap-2">
        <Button type="submit" size="sm" aria-disabled={busy || undefined}>Save alt text</Button>
        {onCancel ? <Button type="button" variant="outline" size="sm" onClick={onCancel}>Cancel</Button> : null}
      </div>
    </form>
  );
}

/** What Change needs to know about an earlier decision. */
export type Handle =
  | { id: string; kind: "correct" | "keep"; was: string }
  | { id: string; kind: "alt"; text: string; decorative: boolean }
  | { id: string; kind: "dismiss" };

/** Change and Undo for a decision that is in the store (an applied one has no queue item). */
export function ChangeControls({ handle, canChange, post, label }: {
  handle: Handle; canChange: boolean; post: (body: Body) => Promise<Sent>;
  /** What the decision is, for the accessible names of its buttons ("Undo: Corrected ‘a’ to ‘b’"). */
  label?: string;
}) {
  const { busy, outcome, run } = useAction(post);
  const [changing, setChanging] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const done = async (body: Body) => { if (await run(body)) setChanging(false); };
  const close = () => { setChanging(false); queueMicrotask(() => trigger.current?.focus()); };
  return (
    <div className="flex flex-col gap-2">
      {changing && handle.kind !== "dismiss" ? (
        handle.kind === "alt" ? (
          <AltForm initial={handle.text} decorativeInitial={handle.decorative} busy={busy} onCancel={close}
            onSave={(alt) => void done(bodies.changeAlt(handle.id, alt))} />
        ) : (
          <div className="flex flex-col gap-2">
            <ActionButton busy={busy} onClick={() => void done(bodies.changeKeep(handle.id))}>
              Keep OCR’s reading ‘{handle.was}’
            </ActionButton>
            <ReadingForm was={handle.was} busy={busy} onCancel={close} onSave={(now) => void done(bodies.changeOther(handle.id, now))} />
          </div>
        )
      ) : (
        <div className="flex flex-wrap gap-2">
          {canChange ? (
            <Button ref={trigger} type="button" variant="outline" size="sm" aria-label={label ? `Change: ${label}` : undefined} onClick={() => setChanging(true)}>Change</Button>
          ) : null}
          <ActionButton busy={busy} aria-label={label ? `Undo: ${label}` : undefined} onClick={() => void run(bodies.undo(handle.id))}>Undo</ActionButton>
        </div>
      )}
      <Announcer outcome={outcome} />
    </div>
  );
}
