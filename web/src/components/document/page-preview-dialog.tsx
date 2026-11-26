"use client";

import Link from "next/link";
import { Button, buttonVariants } from "@/components/ui/button";
import { DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { SeverityBadge } from "@/components/app/severity-badge";
import { issueType, reviewBoxes, type ReviewItem } from "@/lib/review";
import { pageImagePath, pageText } from "@/lib/pages";
import { pageImageSet } from "@/reflow/core";
import type { PageImages } from "@/lib/docs-data";
import { boxPercent } from "@/lib/source-region";
import { cn } from "@/lib/utils";
import { docBase, itemUrl, viewerUrl } from "@/lib/urls";

// The dialog is at most 56rem wide (sm:max-w-4xl) and 2rem narrower than the window, minus padding.
const DIALOG_IMAGE_SIZES = "min(52rem, calc(100vw - 4rem))";

// The page image with every flagged box on that page drawn over it (each item's targets, or its
// bbox when it has none). The selected item's boxes are solid and red, the others on the same
// page are dashed. The page is always white, so the severity colours are pinned to their light
// values here; dark mode would otherwise lighten them.
export function PagePreviewDialog({
  slug,
  title,
  items,
  index,
  images,
  onNavigate,
}: {
  slug: string;
  title: string;
  /** The items in walk order (least trustworthy page first). Previous and Next move through all of them. */
  items: ReviewItem[];
  index: number;
  /** Widths and digest of the page images, from the manifest. */
  images: PageImages;
  onNavigate: (index: number) => void;
}) {
  const item = items[index];
  const page = item.page ?? 1;
  const [width, height] = item.pageSize ?? [];
  const samePage = items.filter((i) => i.page === item.page);
  const image = pageImageSet(docBase(slug), pageImagePath(page), images.widths, images.version);
  const excerpt = item.description || item.alt || item.text;

  return (
    <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-4xl">
      <DialogHeader>
        <DialogTitle>
          {title}, page {pageText(page, item.pageLabel)}
        </DialogTitle>
        <DialogDescription>
          {issueType(item)} · item {index + 1} of {items.length}
        </DialogDescription>
      </DialogHeader>

      <div className="relative overflow-hidden rounded-md border bg-white [--severity-error:#b42318] [--severity-warning:#93370d]">
        {/* eslint-disable-next-line @next/next/no-img-element -- static page images in public/ */}
        <img {...image} sizes={DIALOG_IMAGE_SIZES} alt={`Page ${page} of ${title}`} className="block h-auto w-full" />
        {width && height
          ? samePage.flatMap((flag) => reviewBoxes(flag).map((box, n) => {
              const selected = flag.id === item.id;
              return (
                <div
                  key={`${flag.id}:${n}`}
                  aria-hidden="true"
                  className={
                    selected
                      ? item.severity === "error"
                        ? "absolute border-[3px] border-severity-error bg-severity-error/15"
                        : "absolute border-[3px] border-severity-warning bg-severity-warning/15"
                      : "absolute border-2 border-dashed border-[#525252]"
                  }
                  style={boxPercent(box, width, height)}
                />
              );
            }))
          : null}
      </div>

      <div className="flex flex-col gap-2">
        <p className="flex flex-wrap items-center gap-2 type-small">
          <SeverityBadge severity={item.severity} />
          <span>
            Page {item.page ? pageText(item.page, item.pageLabel) : "?"} · {item.role} · {Math.round(item.confidence * 100)}%
            confidence · {item.source}
          </span>
        </p>
        {excerpt ? <p className="type-small text-muted-foreground">{excerpt}</p> : null}
      </div>

      <DialogFooter className="flex-col-reverse gap-2 sm:flex-row sm:justify-between">
        <div className="grid grid-cols-2 gap-2 sm:flex">
          <Button variant="outline" disabled={index === 0} onClick={() => onNavigate(index - 1)}>
            Previous
          </Button>
          <Button variant="outline" disabled={index === items.length - 1} onClick={() => onNavigate(index + 1)}>
            Next
          </Button>
        </div>
        <div className="grid grid-cols-2 gap-2 sm:flex">
          <Link href={viewerUrl({ slug, page })} className={cn(buttonVariants({ variant: "outline" }))}>
            Read transcript
          </Link>
          <Link href={itemUrl(slug, { ...item, page })} className={cn(buttonVariants({ variant: "outline" }))}>
            Open inspector
          </Link>
        </div>
      </DialogFooter>
    </DialogContent>
  );
}
