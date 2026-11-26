"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { AlertTriangle, Check, ChevronDown, CircleDashed, Cloud, Columns3, LoaderCircle, Plus, RefreshCw } from "lucide-react";
import { useBuilds } from "@/components/builds/context";
import { Button } from "@/components/ui/button";
import { buttonVariants } from "@/components/ui/button-variants";
import {
  Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { COLUMN_DRAG, compareColsFrom } from "@/lib/compare";
import { variantState, type VariantState, type VariantTab, type VariantView, type VariantsAnswer } from "@/lib/variants/view";
import { cn } from "@/lib/utils";
import { DEFAULT_PIPELINE, variantKey, variantOf } from "@/pipelines";
import { compareUrl, inspectUrl, viewerUrl } from "@/lib/urls";

/** The questions an API run may ask at most: the cap of each assist step it turns on. */
const CAP = 25;
/** The most an API run may spend on one build. */
const BUILD_CAP = "$5";

const STATE_TEXT: Record<VariantState, string> = {
  built: "Built", stale: "Stale", queued: "Queued", running: "Building", failed: "Failed", "not built": "Not built",
};

function StateIcon({ state, className }: { state: VariantState; className?: string }) {
  const cls = cn("size-3.5 shrink-0", className);
  if (state === "built") return <Check aria-hidden="true" className={cn(cls, "text-success")} />;
  if (state === "stale") return <RefreshCw aria-hidden="true" className={cn(cls, "text-amber-600 dark:text-amber-400")} />;
  if (state === "queued" || state === "running") return <LoaderCircle aria-hidden="true" className={cn(cls, "motion-safe:animate-spin")} />;
  if (state === "failed") return <AlertTriangle aria-hidden="true" className={cn(cls, "text-destructive")} />;
  return <CircleDashed aria-hidden="true" className={cn(cls, "text-muted-foreground")} />;
}

/** The href of `key` in the same view, on the same page. */
const hrefOf = (key: string, view: "view" | "inspect", page: number | null): string =>
  (view === "view" ? viewerUrl : inspectUrl)({ slug: key, page });

/**
 * The variants of a document as tabs, and the build dropdown beside them. A tab is a built
 * variant, the variant shown, or one with a build in the queue (its tab is loading: the page shows
 * the build's progress and refreshes when it is published). The dropdown lists every pipeline with
 * its state; picking one opens its tab, and one that is not built is queued first (an API run after
 * a confirmation). Building is local only: with builds off the dropdown lists the built variants.
 */
export function VariantTabs({ slug, active, built, view, page }: {
  slug: string;
  /** The key of the variant shown. */
  active: string;
  built: VariantTab[];
  view: "view" | "inspect";
  page: number | null;
}) {
  const router = useRouter();
  const builds = useBuilds();
  const [menu, setMenu] = useState<VariantsAnswer | null>(null);
  const [loading, setLoading] = useState(false);
  const [confirm, setConfirm] = useState<VariantView | null>(null);
  // A tab being dragged: dropping it on the side opens the compare screen with it.
  const [dragging, setDragging] = useState<string | null>(null);

  // Tabs: the built ones, then those being built (from the builds poll), then the one shown.
  const tabs = new Map(built.map((t) => [t.key, t]));
  const jobs = builds?.data ? [...builds.data.active] : [];
  for (const job of jobs) {
    const v = variantOf(job.slug);
    if (v && v.doc.slug === slug && !tabs.has(job.slug)) tabs.set(job.slug, { name: v.pipeline.name, label: v.pipeline.label, key: job.slug });
  }
  // A variant published since this page rendered (a sibling's build finished while another tab
  // was open): the poll's publications have it before the page's built list does.
  const published = builds?.data?.published ?? {};
  for (const key of Object.keys(published)) {
    const v = published[key] ? variantOf(key) : null;
    if (v && v.doc.slug === slug && !tabs.has(key)) tabs.set(key, { name: v.pipeline.name, label: v.pipeline.label, key });
  }
  const shown = variantOf(active);
  if (shown && !tabs.has(active)) tabs.set(active, { name: shown.pipeline.name, label: shown.pipeline.label, key: active });
  // One document with no other variant, on a server that cannot build: no strip.
  if (!builds && tabs.size <= 1) return null;
  const stateOf = (key: string): VariantState | null => {
    const state = variantState(jobs.find((j) => j.slug === key), built.some((t) => t.key === key) || Boolean(published[key]));
    return state === "built" ? null : state;
  };

  const builtNames = built.map((t) => t.name);
  const variantKeyOf = (name: string) => variantKey(slug, name);
  const activeName = variantOf(active)?.pipeline.name ?? DEFAULT_PIPELINE;
  const compareHref = compareUrl(slug, compareColsFrom(activeName, builtNames), page);

  async function load() {
    setLoading(true);
    try {
      const r = await fetch(`/api/variants/${slug}?fresh=1`, { cache: "no-store" });
      if (r.ok) setMenu((await r.json()) as VariantsAnswer);
    } finally {
      setLoading(false);
    }
  }

  async function start(v: VariantView) {
    setConfirm(null);
    if (v.state === "not built" || v.state === "failed") await builds?.rebuild(v.key);
    router.push(hrefOf(v.key, view, page));
  }

  function pick(v: VariantView) {
    const building = v.state === "not built" || v.state === "failed";
    if (building && v.where === "api") setConfirm(v);
    else void start(v);
  }

  return (
    <div className="flex h-10 shrink-0 items-center gap-1 overflow-x-auto border-b bg-muted/30 px-2 sm:px-3">
      <nav aria-label="Variants of this document" className="flex min-w-0 items-center gap-1">
        <ul role="list" className="flex items-center gap-1">
          {[...tabs.values()].map((t) => {
            const current = t.key === active;
            const state = stateOf(t.key);
            return (
              <li key={t.key}>
                <Link
                  prefetch={false}
                  href={hrefOf(t.key, view, page)}
                  draggable={builtNames.includes(t.name)}
                  onDragStart={(e) => { e.dataTransfer.setData(COLUMN_DRAG, t.name); setDragging(t.name); }}
                  onDragEnd={() => setDragging(null)}
                  aria-current={current ? "page" : undefined}
                  data-variant-tab={t.name}
                  className={cn(
                    "inline-flex h-8 items-center gap-1.5 rounded-md px-2.5 type-small whitespace-nowrap",
                    current ? "bg-background font-medium text-foreground shadow-sm ring-1 ring-border" : "text-muted-foreground hover:bg-background/70 hover:text-foreground",
                  )}
                >
                  {state ? <StateIcon state={state} /> : null}
                  {t.label}
                  {state ? <span className="sr-only">({STATE_TEXT[state].toLowerCase()})</span> : null}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>
      {builtNames.length > 1 ? (
        <Link prefetch={false} href={compareHref} data-compare-link className={buttonVariants({ variant: "ghost", size: "sm" })}>
          <Columns3 aria-hidden="true" />
          <span className="max-sm:hidden">Compare</span>
        </Link>
      ) : null}
      {dragging ? (
        <div
          data-compare-drop
          onDragOver={(e) => { if (e.dataTransfer.types.includes(COLUMN_DRAG)) { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; } }}
          onDrop={(e) => {
            const name = e.dataTransfer.getData(COLUMN_DRAG);
            setDragging(null);
            if (!name) return;
            e.preventDefault();
            router.push(compareUrl(slug, [...new Set([activeName, name])].filter((n) => builtNames.includes(n)), page));
          }}
          className="fixed inset-y-0 right-0 z-50 flex w-1/3 items-center justify-center border-l-4 border-dashed border-primary bg-primary/10 p-6 text-center type-small font-medium backdrop-blur-sm"
        >
          Drop here to compare {tabs.get(variantKeyOf(dragging))?.label ?? dragging} side by side
        </div>
      ) : null}
      <DropdownMenu onOpenChange={(open) => { if (open) void load(); }}>
        <DropdownMenuTrigger render={<Button variant="ghost" size="sm" aria-label="Build or open a variant" />}>
          <Plus aria-hidden="true" />
          <span className="max-sm:hidden">Variants</span>
          <ChevronDown aria-hidden="true" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-80">
          <DropdownMenuGroup>
            <DropdownMenuLabel>{menu?.builds === false ? "Built variants" : "Pipelines"}</DropdownMenuLabel>
            {loading && !menu ? <DropdownMenuItem disabled>Checking what is built…</DropdownMenuItem> : null}
            {menu?.variants.map((v) => (
              <DropdownMenuItem key={v.key} onClick={() => pick(v)} data-pipeline={v.name} className="items-start">
                <StateIcon state={v.state} className="mt-0.5" />
                <span className="flex min-w-0 flex-1 flex-col">
                  <span className="flex items-center gap-1.5">
                    <span className="font-medium">{v.label}</span>
                    {v.where === "api" ? <Cloud aria-label="Anthropic API" className="size-3.5 text-muted-foreground" /> : null}
                    <span className="ml-auto type-caption text-muted-foreground">{STATE_TEXT[v.state]}</span>
                  </span>
                  <span className="type-caption text-muted-foreground">{v.steps.join(" · ")}</span>
                  {v.state === "failed" && v.job?.error ? <span className="type-caption text-destructive line-clamp-2">{v.job.error}</span> : null}
                </span>
              </DropdownMenuItem>
            ))}
          </DropdownMenuGroup>
          {menu?.builds === false ? (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuItem disabled>Building is available on a local server only.</DropdownMenuItem>
            </>
          ) : null}
        </DropdownMenuContent>
      </DropdownMenu>
      <Dialog open={confirm !== null} onOpenChange={(open) => { if (!open) setConfirm(null); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Build with {confirm?.label} through the Anthropic API?</DialogTitle>
            <DialogDescription>This run sends parts of the document to Anthropic, billed to the ANTHROPIC_API_KEY in the server&apos;s .env.</DialogDescription>
          </DialogHeader>
          {confirm?.sends === "pages" ? (
            <div className="space-y-2 type-small">
              <p className="font-medium">What leaves this machine</p>
              <ul className="list-disc space-y-1 pl-5">
                <li>A picture of every page, and the text the PDF holds for it, for the model to transcribe.</li>
              </ul>
              <p>
                <span className="font-medium">Cost:</span> one request per page, capped at {BUILD_CAP} for the build. Pages from an earlier
                build with the same model are reused at no cost.
              </p>
            </div>
          ) : confirm?.sends === "pages-and-transcript" ? (
            <div className="space-y-2 type-small">
              <p className="font-medium">What leaves this machine</p>
              <ul className="list-disc space-y-1 pl-5">
                <li>A picture of every page, and reflow&apos;s transcript of it, for the model to propose structural edits.</li>
              </ul>
              <p>
                <span className="font-medium">Cost:</span> one request per page, capped at {BUILD_CAP} for the build. Pages from an earlier
                build with the same model and transcript are reused at no cost.
              </p>
            </div>
          ) : (
          <div className="space-y-2 type-small">
            <p className="font-medium">What leaves this machine</p>
            <ul className="list-disc space-y-1 pl-5">
              <li>Crops of the page images around each form field, heading or scanned field that the rules are unsure of, with the text near them.</li>
              <li>Each figure that has no alt text, to describe it.</li>
            </ul>
            <p>
              <span className="font-medium">Questions:</span> up to {confirm?.asks ? 3 * CAP : 0} ({CAP} per step for field labels,
              headings and scanned-form fields), plus one per figure. Answers from an earlier build of this variant are reused.
            </p>
          </div>
          )}
          <DialogFooter>
            <DialogClose render={<Button variant="outline" />}>Cancel</DialogClose>
            <Button onClick={() => confirm && void start(confirm)} data-confirm-build>Build</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
