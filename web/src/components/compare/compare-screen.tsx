"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useRef, useState, type DragEvent, type UIEvent } from "react";
import { ArrowLeft, ChevronDown, ChevronLeft, ChevronRight, Columns3, GripVertical, X } from "lucide-react";
import { BuildInfoDetails } from "@/components/app/build-info";
import { Button } from "@/components/ui/button";
import { buttonVariants } from "@/components/ui/button-variants";
import {
  DropdownMenu, DropdownMenuCheckboxItem, DropdownMenuContent, DropdownMenuGroup, DropdownMenuLabel, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { BuildInfo } from "@/lib/build-info";
import { COLUMN_DRAG, diffColumn, moveCol, pdfWordsOf, sameBlock, roleTag, summarize, type Block, type BlockDiff, type PageSummary, type Word } from "@/lib/compare";
import { pageText } from "@/lib/pages";

import { cn } from "@/lib/utils";
import { compareUrl, overviewUrl } from "@/lib/urls";

export type CompareColumn = {
  name: string;
  key: string;
  label: string;
  build: BuildInfo | null;
  /** The page's blocks, or null when this build has no layout for the page. */
  blocks: Block[] | null;
  /** Why this build could not be read (a manifest in a format since changed), or null. */
  error: string | null;
};

type Hover = { from: string; id: string | null; role: string; bbox: number[] } | null;

/**
 * The compare screen. The original page image is the first column; each variant is a column
 * of the same page's blocks, matched by where they sit. The first variant column is the reference:
 * the others mark what differs from it (words, a heading's level, a field's label, alt text), and
 * every column marks the words the PDF's own text does not have. Hovering or focusing a block
 * outlines its box on the original and the matching block in the other columns. The columns scroll
 * together. The URL holds the columns and the page.
 */
export function CompareScreen({ slug, title, page, pageCount, labels, image, pageSize, pdfLines, thumbs, columns, built }: {
  slug: string;
  title: string;
  page: number;
  pageCount: number;
  labels: string[];
  image: { src: string; srcSet?: string };
  pageSize: [number, number] | null;
  pdfLines: string[];
  thumbs: string[];
  columns: CompareColumn[];
  built: { name: string; label: string }[];
}) {
  const router = useRouter();
  const [hover, setHover] = useState<Hover>(null);
  const [dragging, setDragging] = useState<string | null>(null);
  const cols = columns.map((c) => c.name);
  const pdfWords = useMemo(() => pdfWordsOf(pdfLines.map((text) => ({ text }))), [pdfLines]);
  const reference = columns.find((c) => c.blocks)?.blocks ?? null;
  const diffs = useMemo(() => columns.map((c) => {
    if (!c.blocks) return null;
    const isRef = c.blocks === reference;
    const d = diffColumn(c.blocks, isRef ? null : reference, pdfWords);
    return { diffs: d, summary: summarize(d, isRef ? null : reference) };
  }), [columns, reference, pdfWords]);
  const referenceLabel = columns.find((c) => c.blocks === reference)?.label ?? null;

  const go = (next: string[], p = page) => router.replace(compareUrl(slug, next, p), { scroll: false });
  const pageHref = (p: number) => compareUrl(slug, cols, p);

  // The columns scroll together: one moves, the others follow to the same share of their height.
  const bodies = useRef(new Map<string, HTMLDivElement>());
  const syncing = useRef(false);
  function onScroll(from: string, e: UIEvent<HTMLDivElement>) {
    if (syncing.current) return;
    const el = e.currentTarget;
    const ratio = el.scrollTop / Math.max(1, el.scrollHeight - el.clientHeight);
    syncing.current = true;
    for (const [name, other] of bodies.current) {
      if (name !== from) other.scrollTop = ratio * (other.scrollHeight - other.clientHeight);
    }
    requestAnimationFrame(() => { syncing.current = false; });
  }
  const body = (name: string) => (el: HTMLDivElement | null) => {
    if (el) bodies.current.set(name, el);
    else bodies.current.delete(name);
  };

  function drop(e: DragEvent, index: number) {
    const name = e.dataTransfer.getData(COLUMN_DRAG);
    setDragging(null);
    if (!name || !built.some((b) => b.name === name)) return;
    e.preventDefault();
    go(moveCol(cols, name, index));
  }

  const label = (p: number) => `Page ${pageText(p, labels[p - 1])}`;
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <header className="flex h-12 shrink-0 items-center gap-2 border-b bg-background px-2 sm:px-3">
        <Link prefetch={false} href={overviewUrl(slug)} aria-label={`Back to ${title} overview`}
              className={buttonVariants({ variant: "ghost", size: "icon-lg" })}>
          <ArrowLeft aria-hidden="true" />
        </Link>
        <h1 className="min-w-0 truncate type-small font-medium">
          <span className="text-muted-foreground">Compare: </span>{title}
        </h1>
        <nav aria-label="Page" className="ml-auto flex items-center gap-1">
          <PageLink href={page > 1 ? pageHref(page - 1) : null} label="Previous page"><ChevronLeft aria-hidden="true" /></PageLink>
          <form
            className="flex items-center gap-1 type-small"
            onSubmit={(e) => {
              e.preventDefault();
              const n = Number(new FormData(e.currentTarget).get("page"));
              if (Number.isInteger(n) && n >= 1 && n <= pageCount) router.push(pageHref(n), { scroll: false });
            }}
          >
            <label htmlFor="compare-page" className="sr-only">Page number</label>
            <input id="compare-page" name="page" key={page} defaultValue={page} inputMode="numeric"
                   className="h-8 w-14 rounded-md border bg-background px-2 text-right tabular-nums" />
            <span className="whitespace-nowrap text-muted-foreground">of {pageCount}</span>
          </form>
          <PageLink href={page < pageCount ? pageHref(page + 1) : null} label="Next page"><ChevronRight aria-hidden="true" /></PageLink>
        </nav>
        <DropdownMenu>
          <DropdownMenuTrigger render={<Button variant="outline" size="sm" aria-label="Columns to compare" />}>
            <Columns3 aria-hidden="true" />
            <span className="max-sm:hidden">Variants</span>
            <ChevronDown aria-hidden="true" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-56">
            <DropdownMenuGroup>
              <DropdownMenuLabel>Built variants</DropdownMenuLabel>
              {built.map((b) => (
                <DropdownMenuCheckboxItem
                  key={b.name}
                  data-compare-option={b.name}
                  checked={cols.includes(b.name)}
                  disabled={cols.length === 1 && cols[0] === b.name}
                  onCheckedChange={(on) => go(on ? [...cols, b.name] : cols.filter((c) => c !== b.name))}
                >
                  {b.label}
                </DropdownMenuCheckboxItem>
              ))}
            </DropdownMenuGroup>
          </DropdownMenuContent>
        </DropdownMenu>
      </header>

      <nav aria-label="Pages" className="flex h-20 shrink-0 items-center gap-2 overflow-x-auto border-b bg-muted/30 px-3">
        {thumbs.map((src, i) => (
          <Link
            key={src}
            prefetch={false}
            scroll={false}
            href={pageHref(i + 1)}
            aria-current={i + 1 === page ? "page" : undefined}
            aria-label={label(i + 1)}
            className={cn("relative h-16 shrink-0 overflow-hidden rounded border bg-white",
              i + 1 === page ? "ring-2 ring-primary" : "opacity-80 hover:opacity-100")}
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={src} alt="" loading="lazy" className="h-full w-auto" />
            <span className="absolute right-0 bottom-0 rounded-tl bg-background/90 px-1 type-caption tabular-nums">{i + 1}</span>
          </Link>
        ))}
      </nav>

      <p className="shrink-0 border-b px-3 py-1.5 type-caption text-muted-foreground">
        {referenceLabel ? <>Marked against <span className="font-medium text-foreground">{referenceLabel}</span>: </> : null}
        <mark className="rounded-sm bg-amber-200/70 px-0.5 text-foreground dark:bg-amber-500/30">differs</mark>{" "}
        and <span className="underline decoration-sky-600 decoration-wavy underline-offset-2">not in the PDF&apos;s text</span>.
        {pdfWords.size === 0 ? " This page has no PDF text to check against." : null}
      </p>

      <div
        className="grid min-h-0 flex-1 overflow-x-auto"
        style={{ gridTemplateColumns: `repeat(${columns.length + 1}, minmax(18rem, 1fr))` }}
        onMouseLeave={() => setHover(null)}
      >
        <section aria-label={`Original, ${label(page)}`} className="flex min-h-0 flex-col border-r">
          <div className="flex h-10 shrink-0 items-center border-b px-3 type-small font-medium">Original</div>
          <div ref={body("original")} onScroll={(e) => onScroll("original", e)} className="min-h-0 flex-1 overflow-y-auto p-3">
            <div className="relative w-full bg-white shadow-sm" style={pageSize ? { aspectRatio: `${pageSize[0]} / ${pageSize[1]}` } : undefined}>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img {...image} sizes="(min-width: 1024px) 30vw, 90vw" alt={`${label(page)} of the PDF`} className="absolute inset-0 size-full" />
              {pageSize && hover ? (
                <svg aria-hidden="true" viewBox={`0 0 ${pageSize[0]} ${pageSize[1]}`} preserveAspectRatio="none" className="pointer-events-none absolute inset-0 size-full">
                  <rect data-original-outline x={hover.bbox[0]} y={hover.bbox[1]} width={hover.bbox[2] - hover.bbox[0]} height={hover.bbox[3] - hover.bbox[1]}
                        className="fill-sky-500/20 stroke-sky-600" strokeWidth={Math.max(1.5, pageSize[0] / 150)} />
                </svg>
              ) : null}
            </div>
          </div>
        </section>

        {columns.map((col, index) => {
          const d = diffs[index];
          return (
            <section
              key={col.name}
              aria-label={col.label}
              data-compare-column={col.name}
              className={cn("flex min-h-0 flex-col border-r", dragging && dragging !== col.name && "bg-muted/40")}
              onDragOver={(e) => { if (e.dataTransfer.types.includes(COLUMN_DRAG)) e.preventDefault(); }}
              onDrop={(e) => drop(e, index)}
            >
              <div className="shrink-0 border-b">
                <div className="flex h-10 items-center gap-1 px-1">
                  <span
                    draggable
                    onDragStart={(e) => { e.dataTransfer.setData(COLUMN_DRAG, col.name); e.dataTransfer.effectAllowed = "move"; setDragging(col.name); }}
                    onDragEnd={() => setDragging(null)}
                    title="Drag to reorder"
                    className="cursor-grab p-1 text-muted-foreground active:cursor-grabbing"
                  >
                    <GripVertical aria-hidden="true" className="size-4" />
                  </span>
                  <h2 className="min-w-0 flex-1 truncate type-small font-medium">{col.label}</h2>
                  <Button variant="ghost" size="icon-sm" aria-label={`Move ${col.label} left`} disabled={index === 0}
                          onClick={() => go(moveCol(cols, col.name, index - 1))}><ChevronLeft aria-hidden="true" /></Button>
                  <Button variant="ghost" size="icon-sm" aria-label={`Move ${col.label} right`} disabled={index === columns.length - 1}
                          onClick={() => go(moveCol(cols, col.name, index + 1))}><ChevronRight aria-hidden="true" /></Button>
                  <Button variant="ghost" size="icon-sm" aria-label={`Remove ${col.label}`} disabled={columns.length === 1}
                          onClick={() => go(cols.filter((c) => c !== col.name))}><X aria-hidden="true" /></Button>
                </div>
                {d ? <Summary summary={d.summary} reference={index > 0 || col.blocks !== reference ? referenceLabel : null} /> : null}
                <details className="group px-3 pb-2">
                  <summary className="cursor-pointer type-caption text-muted-foreground hover:text-foreground">How it was made</summary>
                  <div className="mt-2"><BuildInfoDetails build={col.build} slug={col.key} /></div>
                </details>
              </div>
              <div ref={body(col.name)} onScroll={(e) => onScroll(col.name, e)} className="min-h-0 flex-1 space-y-1.5 overflow-y-auto p-3">
                {col.error ? (
                  <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 p-3 type-small">
                    <p className="font-medium">This build could not be read.</p>
                    <p className="mt-1 text-muted-foreground">It was likely published by an older version of reflow. Build it again to compare it.</p>
                    <p className="mt-2 break-words font-mono type-caption text-muted-foreground">{col.error}</p>
                  </div>
                ) : null}
                {d && d.diffs.length === 0 ? <p className="type-small text-muted-foreground">No blocks on this page.</p> : null}
                {d ? d.diffs.map((x) => (
                  <BlockView
                    key={x.block.id}
                    diff={x}
                    referenceLabel={col.blocks === reference ? null : referenceLabel}
                    lit={!!hover && (hover.from === col.name ? hover.id === x.block.id : sameBlock(hover, x.block))}
                    onHover={(on) => setHover(on ? { from: col.name, id: x.block.id, role: x.block.role, bbox: x.block.bbox } : null)}
                  />
                )) : <p className="type-small text-muted-foreground">This build has no layout for {label(page).toLowerCase()}.</p>}
              </div>
            </section>
          );
        })}
      </div>
    </div>
  );
}

function PageLink({ href, label, children }: { href: string | null; label: string; children: React.ReactNode }) {
  if (!href) return <Button variant="ghost" size="icon-sm" aria-label={label} disabled>{children}</Button>;
  return <Link prefetch={false} scroll={false} href={href} aria-label={label} className={buttonVariants({ variant: "ghost", size: "icon-sm" })}>{children}</Link>;
}

function Summary({ summary: s, reference }: { summary: PageSummary; reference: string | null }) {
  const parts = [`${s.blocks} blocks`, `${s.headings} headings`];
  if (s.fields) parts.push(`${s.fields} fields`);
  if (s.figures) parts.push(`${s.described} of ${s.figures} figures described`);
  return (
    <p data-compare-summary className="min-h-[3.25rem] px-3 pb-1 type-caption text-muted-foreground">
      {parts.join(" · ")}
      {reference ? <><br /><span className={s.differ || s.missing ? "text-amber-700 dark:text-amber-400" : undefined}>
        {s.differ} differ from {reference}{s.missing ? `, ${s.missing} of its blocks not here` : ""}
      </span></> : null}
      {s.notInPdf ? <><br />{s.notInPdf} words not in the PDF&apos;s text</> : null}
    </p>
  );
}

function Words({ words }: { words: Word[] }) {
  return words.map((w, i) => {
    const cls = cn(w.notInPdf && "underline decoration-sky-600 decoration-wavy underline-offset-2");
    const text = i < words.length - 1 ? `${w.text} ` : w.text;
    return w.changed
      ? <mark key={i} className={cn("rounded-sm bg-amber-200/70 text-foreground dark:bg-amber-500/30", cls)}>{text}</mark>
      : <span key={i} className={cls || undefined}>{text}</span>;
  });
}

function Changed({ on, children }: { on: boolean; children: React.ReactNode }) {
  return on ? <mark className="rounded-sm bg-amber-200/70 text-foreground dark:bg-amber-500/30">{children}</mark> : <>{children}</>;
}

function BlockView({ diff, referenceLabel, lit, onHover }: {
  diff: BlockDiff;
  referenceLabel: string | null;
  lit: boolean;
  onHover: (on: boolean) => void;
}) {
  const { block: b, match } = diff;
  const heading = /^h[1-6]$/.test(b.role);
  return (
    <div
      data-block={b.id}
      data-lit={lit || undefined}
      tabIndex={0}
      onMouseEnter={() => onHover(true)}
      onFocus={() => onHover(true)}
      onBlur={() => onHover(false)}
      className={cn(
        "rounded-md border px-2 py-1.5 outline-none focus-visible:ring-2 focus-visible:ring-ring",
        diff.differs ? "border-l-4 border-l-amber-500" : "border-transparent",
        lit && "bg-sky-500/10 ring-2 ring-sky-600",
      )}
    >
      <div className="mb-0.5 flex flex-wrap items-center gap-1.5 type-caption text-muted-foreground">
        <Changed on={diff.roleChanged}><span className="font-medium">{roleTag(b.role)}</span></Changed>
        {diff.roleChanged && match ? <span>({roleTag(match.role)} in {referenceLabel})</span> : null}
        {referenceLabel && !match ? <span>Not in {referenceLabel}</span> : null}
      </div>
      {b.role === "field" ? (
        <p className="type-small">
          <span className="text-muted-foreground">Label: </span>
          <Changed on={diff.labelChanged}>{b.label || "none"}</Changed>
          {diff.labelChanged && match ? <span className="text-muted-foreground"> ({match.label || "none"} in {referenceLabel})</span> : null}
        </p>
      ) : null}
      {b.shape ? (
        <p className="type-small">
          <Changed on={diff.shapeChanged}>{b.shape}</Changed>
          {diff.shapeChanged && match ? <span className="text-muted-foreground"> ({match.shape ?? "no shape"} in {referenceLabel})</span> : null}
        </p>
      ) : null}
      {b.role === "figure" ? (
        <p className="type-small">
          <span className="text-muted-foreground">Alt text: </span>
          <Changed on={diff.altChanged}>{b.alt || "none"}</Changed>
        </p>
      ) : null}
      {diff.words.length ? <p className={cn("type-small", heading && "font-semibold")}><Words words={diff.words} /></p> : null}
      {diff.removed.length ? (
        <p className="type-caption text-muted-foreground">
          Leaves out from {referenceLabel}: <del className="rounded-sm bg-amber-200/70 text-foreground dark:bg-amber-500/30">{diff.removed.join(" ")}</del>
        </p>
      ) : null}
    </div>
  );
}
