"use client";

import { useId, useState } from "react";
import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { SeverityBadge } from "@/components/app/severity-badge";
import { groupByType, suggestionText, type ReviewItem } from "@/lib/review";
import { pageText } from "@/lib/pages";
import { cn } from "@/lib/utils";
import type { PageImages } from "@/lib/docs-data";
import { DecidedBadge } from "@/components/builds/controls";
import { PagePreviewDialog } from "./page-preview-dialog";
import { itemUrl } from "@/lib/urls";

export function IssueList({ slug, title, items, images }: { slug: string; title: string; items: ReviewItem[]; images: PageImages }) {
  const [viewing, setViewing] = useState<number | null>(null);
  const baseId = useId();
  return (
    <div className="flex flex-col gap-8">
      {groupByType(items).map(([type, group], g) => (
        <section key={type} aria-labelledby={`${baseId}-${g}`} className="flex flex-col gap-3">
          <h3 id={`${baseId}-${g}`} className="flex items-center gap-2 type-h3">
            {type}
            <span className="sr-only">, </span>
            <Badge variant="secondary" className="h-6 px-2 type-caption tabular-nums">
              {group.length}
              <span className="sr-only"> {group.length === 1 ? "issue" : "issues"}</span>
            </Badge>
          </h3>
          <ul role="list" className="divide-y overflow-hidden rounded-xl ring-1 ring-foreground/10">
            {group.map((item) => {
              const where = item.page ? `page ${pageText(item.page, item.pageLabel)}` : "unknown page";
              const excerpt = item.description || item.alt || item.text;
              return (
                <li key={item.id} className="flex flex-col gap-3 p-4 sm:flex-row sm:items-start sm:justify-between sm:gap-6">
                  <div className="flex min-w-0 flex-col gap-1.5">
                    <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
                      <SeverityBadge severity={item.severity} />
                      <span className="font-medium">Page {item.page ? pageText(item.page, item.pageLabel) : "?"}</span>
                      <span className="type-small text-muted-foreground">
                        · {item.role} · {Math.round(item.confidence * 100)}% confidence
                      </span>
                      <DecidedBadge slug={slug} item={item} />
                    </p>
                    {excerpt ? <p className="line-clamp-2 type-small text-muted-foreground">{excerpt}</p> : null}
                    {item.suggestions?.map((s, i) => (
                      <p key={`${i}-${s.was}`} className="type-small">{suggestionText(s)}</p>
                    ))}
                  </div>
                  <div className="grid shrink-0 grid-cols-2 gap-2 sm:flex">
                    <Button variant="outline" disabled={!item.page} onClick={() => setViewing(items.indexOf(item))}>
                      Preview page<span className="sr-only">, {where}</span>
                    </Button>
                    <Link href={itemUrl(slug, item)} className={cn(buttonVariants({ variant: "outline" }))}>
                      Inspect<span className="sr-only">, {where}</span>
                    </Link>
                  </div>
                </li>
              );
            })}
          </ul>
        </section>
      ))}

      <Dialog open={viewing !== null} onOpenChange={(open) => { if (!open) setViewing(null); }}>
        {viewing !== null ? (
          <PagePreviewDialog slug={slug} title={title} items={items} index={viewing} images={images} onNavigate={setViewing} />
        ) : null}
      </Dialog>
    </div>
  );
}
