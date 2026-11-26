import Link from "next/link";
import { CircleCheck } from "lucide-react";
import { PageHeader } from "@/components/app/page-header";
import { Section } from "@/components/app/section";
import { Stat, StatGrid } from "@/components/app/stat";
import { EmptyState } from "@/components/app/empty-state";
import { ReviewQueue } from "@/components/review/review-queue";
import { RenderedFrom } from "@/components/builds/rendered-from";
import { documentSummaries } from "@/lib/documents";
import { plural } from "@/lib/format";
import { overviewUrl } from "@/lib/urls";

export const metadata = { title: "Review" };

export default async function ReviewPage() {
  const docs = await documentSummaries();
  const converted = docs.filter((d) => d.pages !== null);
  const withIssues = converted
    .filter((d) => d.errors + d.warnings > 0)
    .sort((a, b) => b.errors - a.errors || b.warnings - a.warnings || a.title.localeCompare(b.title));
  const clean = converted.filter((d) => d.errors + d.warnings === 0);
  const notConverted = docs.filter((d) => d.pages === null);
  const errors = converted.reduce((n, d) => n + d.errors, 0);
  const warnings = converted.reduce((n, d) => n + d.warnings, 0);

  return (
    <>
      <RenderedFrom reads={docs} />
      <PageHeader
        title="Review"
        lead="Items reflow could not finish on its own, across every converted document. Errors need fixing before a transcript is fully accessible; warnings are suggestions to check."
      />
      <div className="flex flex-col gap-10 lg:gap-12">
        <Section title="Summary" srOnlyTitle>
          <StatGrid>
            <Stat label="Converted" value={`${converted.length} of ${docs.length}`} />
            <Stat label="Errors" value={errors} tone={errors ? "error" : "success"} />
            <Stat label="Warnings" value={warnings} tone={warnings ? "warning" : "success"} />
            <Stat label="Need review" value={`${withIssues.length} of ${converted.length}`} hint="documents" />
          </StatGrid>
          <p role="note" className="mt-4 type-small text-muted-foreground">
            Automatic review checks figure alt text, document language, table shape, and heading fragments. Reading order is not checked.
          </p>
        </Section>

        <Section title="Documents with issues" description="Most errors first. Start review opens the inspector at the first item.">
          {withIssues.length ? (
            <ReviewQueue docs={withIssues} />
          ) : (
            <EmptyState
              icon={CircleCheck}
              headingLevel={3}
              title="Nothing to review"
              description="No review items were detected in the converted documents."
            />
          )}
        </Section>

        {clean.length ? (
          <Section title="No issues" description="reflow finished these documents on its own.">
            <ul role="list" className="grid gap-2 sm:grid-cols-2">
              {clean.map((d) => (
                <li key={d.slug} className="flex items-baseline justify-between gap-3 rounded-lg px-4 py-3 ring-1 ring-foreground/10">
                  <Link href={overviewUrl(d.slug)} className="font-medium text-link underline decoration-link/40 underline-offset-4 hover:decoration-link">
                    {d.title}
                  </Link>
                  <span className="shrink-0 type-small text-muted-foreground tabular-nums">{plural(d.pages ?? 0, "page")}</span>
                </li>
              ))}
            </ul>
          </Section>
        ) : null}

        {notConverted.length ? (
          <Section title="Not converted" description="These PDFs have no transcript yet. Convert one with the command shown, or read what the source PDF check found.">
            <ul role="list" className="grid gap-2">
              {notConverted.map((d) => (
                <li key={d.slug} className="flex flex-col gap-2 rounded-lg px-4 py-3 ring-1 ring-foreground/10 sm:flex-row sm:items-center sm:justify-between">
                  <span className="flex flex-wrap items-baseline gap-x-3">
                    <Link href={overviewUrl(d.slug)} className="font-medium text-link underline decoration-link/40 underline-offset-4 hover:decoration-link">
                      {d.title}
                    </Link>
                    <Link href={`${overviewUrl(d.slug)}#audit`} className="type-small text-link underline decoration-link/40 underline-offset-4 hover:decoration-link">
                      Source audit<span className="sr-only"> for {d.title}</span>
                    </Link>
                  </span>
                  <code className="self-start rounded-md bg-muted px-1.5 py-0.5 type-mono sm:self-auto">bun run docs:build {d.slug}</code>
                </li>
              ))}
            </ul>
          </Section>
        ) : null}
      </div>
    </>
  );
}
