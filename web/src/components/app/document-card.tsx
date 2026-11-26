import type { ReactNode } from "react";
import Link from "next/link";
import type { DocumentSummary } from "@/lib/documents";
import { IssueCounts } from "./issue-counts";
import { StatusBadge } from "./status-badge";
import { pageCountText } from "@/lib/pages";
import { overviewUrl } from "@/lib/urls";

/**
 * One document as a card, for phone lists. `plainTitleAnchor` renders the title as a plain <a>
 * so ReflowLinks can intercept it (the homepage); otherwise it is a Next Link.
 */
export function DocumentCard({
  doc, titleHref, headingLevel = 2, plainTitleAnchor = false, footer, actions,
}: {
  doc: Pick<DocumentSummary, "slug" | "title" | "description" | "pages" | "errors" | "warnings">;
  titleHref: string;
  headingLevel?: 2 | 3;
  plainTitleAnchor?: boolean;
  /** Extra content under the counts (e.g. "Most common: …"). */
  footer?: ReactNode;
  actions?: ReactNode;
}) {
  const H = headingLevel === 2 ? "h2" : "h3";
  // Links look like links: link colour and an underline (not colour alone, WCAG 1.4.1).
  const titleClass = "text-link underline decoration-link/40 underline-offset-4 hover:decoration-link";
  return (
    <article className="flex flex-col gap-3 rounded-xl p-4 ring-1 ring-foreground/10">
      <H className="type-h3">
        {plainTitleAnchor ? (
          <a href={titleHref} dir="auto" className={titleClass}>{doc.title}</a>
        ) : (
          <Link href={titleHref} dir="auto" className={titleClass}>{doc.title}</Link>
        )}
      </H>
      <p className="type-small text-muted-foreground">{doc.description}</p>
      <div className="flex flex-wrap items-center gap-2 type-small">
        {doc.pages ? <span className="tabular-nums">{pageCountText(doc.pages)}</span> : null}
        <StatusBadge converted={doc.pages !== null} />
        {doc.pages ? <IssueCounts errors={doc.errors} warnings={doc.warnings} href={`${overviewUrl(doc.slug)}#issues`} /> : null}
      </div>
      {footer}
      {actions ? <div className="grid grid-cols-2 gap-2">{actions}</div> : null}
    </article>
  );
}
