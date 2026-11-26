"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useId, useState, type FormEvent } from "react";
import { ChevronDown, ChevronLeft, ChevronRight, Ellipsis } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Label } from "@/components/ui/label";
import { Popover, PopoverContent, PopoverDescription, PopoverTitle, PopoverTrigger } from "@/components/ui/popover";
import { pageText as formatPage, parsePage } from "@/lib/pages";

import type { ReviewBar } from "@/lib/inspect";
import type { PanelId } from "./panels";
import { PANEL_LABELS } from "./panels";
import { inspectUrl } from "@/lib/urls";
import { plural } from "@/lib/format";

type Pages = { page: number; count: number; label: string | null }[];

// The panels in the order the menus list them, which is also the order they sit on desktop.
const MENU_ORDER: PanelId[] = ["page", "details", "transcript"];

// The toolbar under the workspace header: the issue navigator and the page picker. From md up
// it is one 44px row; on phones it is two short rows' worth of controls in one 44px row.
export function InspectorToolbar({
  slug,
  page,
  pageText,
  pageCount,
  prevPageHref,
  nextPageHref,
  pagesWithIssues,
  review,
  visible,
  onTogglePanel,
  onResetLayout,
}: {
  slug: string;
  page: number;
  pageText: string;
  pageCount: number;
  prevPageHref: string | null;
  nextPageHref: string | null;
  pagesWithIssues: Pages;
  review: ReviewBar;
  visible: PanelId[];
  onTogglePanel: (id: PanelId) => void;
  onResetLayout: () => void;
}) {
  const picker = (withSelect: boolean) => (
    <PagePicker
      slug={slug}
      page={page}
      pageText={pageText}
      pageCount={pageCount}
      prevPageHref={prevPageHref}
      nextPageHref={nextPageHref}
      pagesWithIssues={withSelect ? pagesWithIssues : []}
    />
  );

  return (
    <>
      {/* Phones */}
      <div className="flex h-11 shrink-0 items-center justify-between gap-2 border-b bg-background px-2 md:hidden">
        <IssueNav review={review} compact />
        {picker(false)}
      </div>
      {/* md and up */}
      <div className="hidden h-11 shrink-0 items-center gap-3 border-b bg-background px-3 md:flex">
        <IssueNav review={review} />
        <div className="ml-auto flex items-center gap-2">
          {picker(true)}
          <PanelsMenu visible={visible} onToggle={onTogglePanel} onReset={onResetLayout} />
        </div>
      </div>
    </>
  );
}

// "Issue 2 / 7" with the previous and next links and, on wide screens, the issue's type. Compact
// (‹ 2/7 ›) on phones, where the type is in the menu's label instead.
function IssueNav({ review, compact = false }: { review: ReviewBar; compact?: boolean }) {
  if (review.total === 0) {
    return compact ? null : <p className="type-small text-muted-foreground">No issues in this document.</p>;
  }
  const count = review.position
    ? compact ? `${review.position}/${review.total}` : `Issue ${review.position} / ${review.total}`
    : `${review.total} issues`;
  return (
    <nav aria-label="Issues" className={compact ? "flex shrink-0 items-center gap-0.5 text-sm" : "flex min-w-0 items-center gap-2 text-sm"}>
      {review.prevIssueHref ? (
        <Link href={review.prevIssueHref} className={buttonVariants({ variant: "outline", size: "icon" })} aria-label="Previous issue">
          <ChevronLeft aria-hidden="true" />
        </Link>
      ) : (
        <Button variant="outline" size="icon" disabled aria-label="Previous issue"><ChevronLeft aria-hidden="true" /></Button>
      )}
      <span className="tabular-nums" aria-live="polite">{count}</span>
      {review.nextIssueHref ? (
        <Link href={review.nextIssueHref} className={buttonVariants({ variant: "outline", size: "icon" })} aria-label="Next issue">
          <ChevronRight aria-hidden="true" />
        </Link>
      ) : (
        <Button variant="outline" size="icon" disabled aria-label="Next issue"><ChevronRight aria-hidden="true" /></Button>
      )}
      {review.current && !compact ? (
        <Badge variant="secondary" className="hidden max-w-[12rem] truncate lg:inline-flex xl:max-w-[16rem]">{review.current.type}</Badge>
      ) : null}
    </nav>
  );
}

// ‹ Page [input] of N › and, on wide screens, the pages with issues.
function PagePicker({
  slug,
  page,
  pageText,
  pageCount,
  prevPageHref,
  nextPageHref,
  pagesWithIssues,
}: {
  slug: string;
  page: number;
  pageText: string;
  pageCount: number;
  prevPageHref: string | null;
  nextPageHref: string | null;
  pagesWithIssues: Pages;
}) {
  const router = useRouter();
  const id = useId();
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const onList = pagesWithIssues.some((p) => p.page === page);

  const submit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const n = parsePage(value);
    if (n === null || n > pageCount) {
      setError(`Enter a page from 1 to ${pageCount}.`);
      return;
    }
    setError(null);
    setValue("");
    router.push(inspectUrl({ slug, page: n }));
  };

  return (
    <div className="flex items-center gap-2">
      <nav aria-label="Pages" className="flex items-center gap-1">
        {prevPageHref ? (
          <Link href={prevPageHref} className={buttonVariants({ variant: "outline", size: "icon" })} aria-label="Previous page">
            <ChevronLeft aria-hidden="true" />
          </Link>
        ) : (
          <Button variant="outline" size="icon" disabled aria-label="Previous page"><ChevronLeft aria-hidden="true" /></Button>
        )}
        <form onSubmit={submit} noValidate className="flex items-start gap-1.5 text-sm">
          <Label htmlFor={`${id}-page`} className="sr-only">Go to PDF page number</Label>
          <div className="flex flex-col gap-0.5">
            <input
              id={`${id}-page`}
              inputMode="numeric"
              placeholder={String(page)}
              value={value}
              onChange={(e) => { setValue(e.target.value); if (error) setError(null); }}
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? `${id}-error` : undefined}
              className="h-8 w-14 rounded-lg border border-input bg-transparent px-2 text-right tabular-nums outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring aria-invalid:border-destructive dark:bg-input/30"
            />
            {error ? (
              <span id={`${id}-error`} role="alert" className="type-caption text-destructive">{error}</span>
            ) : null}
          </div>
          <span className="h-8 leading-8 whitespace-nowrap text-muted-foreground tabular-nums">of {pageCount}</span>
          <span className="sr-only">Current page {pageText}.</span>
        </form>
        {nextPageHref ? (
          <Link href={nextPageHref} className={buttonVariants({ variant: "outline", size: "icon" })} aria-label="Next page">
            <ChevronRight aria-hidden="true" />
          </Link>
        ) : (
          <Button variant="outline" size="icon" disabled aria-label="Next page"><ChevronRight aria-hidden="true" /></Button>
        )}
      </nav>

      {pagesWithIssues.length > 0 ? (
        <>
          <Label htmlFor={`${id}-issues`} className="sr-only">Pages with issues</Label>
          <select
            id={`${id}-issues`}
            value={onList ? String(page) : ""}
            onChange={(e) => {
              const n = parsePage(e.target.value);
              if (n !== null && n <= pageCount) router.push(inspectUrl({ slug, page: n }));
            }}
            className="h-8 max-w-[10rem] rounded-lg border border-input bg-transparent px-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring lg:max-w-[14rem] dark:bg-input/30"
          >
            {!onList ? <option value="">Pages with issues</option> : null}
            {pagesWithIssues.map((p) => (
              <option key={p.page} value={String(p.page)}>
                {`Page ${formatPage(p.page, p.label)}: ${plural(p.count, "issue")}`}
              </option>
            ))}
          </select>
        </>
      ) : null}
    </div>
  );
}

// Phones: the things that do not fit the bar, in one menu. Pages with issues is a radio group,
// so the current page is the checked one.
export function MoreMenu({
  slug,
  page,
  pageCount,
  pagesWithIssues,
  visible,
  onTogglePanel,
  onResetLayout,
  className,
}: {
  slug: string;
  page: number;
  pageCount: number;
  pagesWithIssues: Pages;
  visible: PanelId[];
  onTogglePanel: (id: PanelId) => void;
  onResetLayout: () => void;
  className?: string;
}) {
  const router = useRouter();
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button variant="outline" size="icon-lg" aria-label="More" className={className} />}>
        <Ellipsis aria-hidden="true" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-56">
        {pagesWithIssues.length > 0 ? (
          <>
            <DropdownMenuGroup>
              <DropdownMenuLabel>Pages with issues</DropdownMenuLabel>
              <DropdownMenuRadioGroup
                value={String(page)}
                onValueChange={(v) => {
                  const n = parsePage(v);
                  if (n !== null && n <= pageCount && n !== page) router.push(inspectUrl({ slug, page: n }));
                }}
              >
                {pagesWithIssues.map((p) => (
                  <DropdownMenuRadioItem key={p.page} value={String(p.page)}>
                    {`Page ${formatPage(p.page, p.label)}: ${p.count}`}
                  </DropdownMenuRadioItem>
                ))}
              </DropdownMenuRadioGroup>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
          </>
        ) : null}
        <DropdownMenuGroup>
          <DropdownMenuLabel>Panels</DropdownMenuLabel>
          {MENU_ORDER.map((id) => {
            const on = visible.includes(id);
            return (
              <DropdownMenuCheckboxItem key={id} checked={on} disabled={on && visible.length === 1} onCheckedChange={() => onTogglePanel(id)}>
                {PANEL_LABELS[id]}
              </DropdownMenuCheckboxItem>
            );
          })}
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={onResetLayout}>Reset layout</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

// md and up: the panels, and a reset back to the default split.
function PanelsMenu({ visible, onToggle, onReset }: { visible: PanelId[]; onToggle: (id: PanelId) => void; onReset: () => void }) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button variant="outline" />}>
        Panels
        <ChevronDown data-icon="inline-end" aria-hidden="true" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-48">
        <DropdownMenuGroup>
          <DropdownMenuLabel>Show</DropdownMenuLabel>
          {MENU_ORDER.map((id) => {
            const on = visible.includes(id);
            return (
              <DropdownMenuCheckboxItem
                key={id}
                checked={on}
                // At least one panel stays on.
                disabled={on && visible.length === 1}
                onCheckedChange={() => onToggle(id)}
              >
                {PANEL_LABELS[id]}
              </DropdownMenuCheckboxItem>
            );
          })}
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={onReset}>Reset layout</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

// Help and the keyboard shortcuts, in the header at every width.
export function Help() {
  return (
    <Popover>
      <PopoverTrigger render={<Button variant="outline" size="icon-lg" aria-label="Help and keyboard shortcuts" />}>?</PopoverTrigger>
      <PopoverContent align="end" className="w-80 space-y-3">
        <PopoverTitle>Reading the inspector</PopoverTitle>
        <PopoverDescription>
          Each box is a piece of the page as reflow read it. Colours are roles. Select a box or a block of
          the transcript to see how it was read; the details show the engine, confidence and the reasons.
        </PopoverDescription>
        <ul className="space-y-1.5 text-sm">
          <li className="flex items-center justify-between gap-2"><span>Next / previous issue</span><span className="flex gap-1"><Kbd>j</Kbd><Kbd>k</Kbd></span></li>
          <li className="flex items-center justify-between gap-2"><span>Next / previous page</span><span className="flex gap-1"><Kbd>]</Kbd><Kbd>[</Kbd></span></li>
          <li className="flex items-center justify-between gap-2"><span>Zoom the page in / out</span><span className="flex gap-1"><Kbd>+</Kbd><Kbd>-</Kbd></span></li>
          <li className="flex items-center justify-between gap-2"><span>Zoom at the cursor</span><span className="flex gap-1"><Kbd>Ctrl</Kbd><span>+ wheel</span></span></li>
        </ul>
        <p className="type-caption text-muted-foreground">Shortcuts do nothing while you type in a field.</p>
        <Link href="/about#keyboard" className="type-small underline underline-offset-4">All keyboard shortcuts</Link>
      </PopoverContent>
    </Popover>
  );
}
