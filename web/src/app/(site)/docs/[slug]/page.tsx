import { notFound } from "next/navigation";
import { ArrowRight, BookOpenText, CircleCheck, Download, ScanSearch } from "lucide-react";
import Link from "next/link";
import { sampleDoc, slugParams } from "@/docs";
import { PageHeader } from "@/components/app/page-header";
import { Section } from "@/components/app/section";
import { StatusBadge } from "@/components/app/status-badge";
import { IssueCounts } from "@/components/app/issue-counts";
import { EmptyState } from "@/components/app/empty-state";
import { NotConvertedNotice } from "@/components/app/not-converted";
import { buttonVariants } from "@/components/ui/button-variants";
import { IssueList } from "@/components/document/issue-list";
import { DocumentFacts } from "@/components/document/document-facts";
import { DecisionsSection } from "@/components/builds/controls";
import { RenderedFrom } from "@/components/builds/rendered-from";
import { readDocument } from "@/lib/docs-data";
import { summarize } from "@/lib/documents";
import { plural } from "@/lib/format";
import { cn } from "@/lib/utils";
import { inspectUrl, viewerUrl } from "@/lib/urls";

export const generateStaticParams = slugParams;

export async function generateMetadata(props: PageProps<"/docs/[slug]">) {
  const { slug } = await props.params;
  return { title: sampleDoc(slug)?.title ?? "Document not found" };
}

export default async function DocumentPage(props: PageProps<"/docs/[slug]">) {
  const { slug } = await props.params;
  if (!sampleDoc(slug)) notFound();
  // Everything below comes from this one read, so the page shows one publication of the document.
  const data = await readDocument(slug);
  const doc = summarize(data);
  const converted = doc.pages !== null;
  const items = doc.items ?? [];
  const audit = data.audit;

  return (
    <>
      <RenderedFrom reads={[data]} />
      <PageHeader
        crumbs={[{ label: "Documents", href: "/" }, { label: doc.title }]}
        title={doc.title}
        lead={doc.description}
        meta={
          <>
            <StatusBadge converted={converted} />
            {converted ? <span className="tabular-nums">{plural(doc.pages!, "page")}</span> : null}
            {converted ? <IssueCounts errors={doc.errors} warnings={doc.warnings} /> : null}
          </>
        }
        actions={
          <>
            {converted ? (
              <>
                <Link href={viewerUrl({ slug })} className={buttonVariants({ size: "lg" })}>
                  <BookOpenText aria-hidden="true" />
                  Read transcript
                </Link>
                <Link href={doc.firstIssueHref ?? inspectUrl({ slug })} className={cn(buttonVariants({ variant: "outline", size: "lg" }))}>
                  <ScanSearch aria-hidden="true" />
                  Open inspector
                </Link>
              </>
            ) : null}
            <a href={doc.pdfHref} download className={buttonVariants({ variant: "ghost", size: "lg" })}>
              <Download aria-hidden="true" />
              Download PDF
            </a>
          </>
        }
      />

      <div className="grid gap-10 lg:grid-cols-[minmax(0,1fr)_18rem] lg:gap-12">
        <div className="min-w-0">
          {converted ? (
            <Section
              id="issues"
              title="Issues"
              description={
                `${items.length ? `${plural(items.length, "item")} to check. ` : ""}Automatic review checks figure alt text, document language, table shape, and heading fragments. Reading order is not checked.`
              }
              actions={
                doc.firstIssueHref ? (
                  <Link href={doc.firstIssueHref} className={buttonVariants()}>
                    Start review
                    <ArrowRight aria-hidden="true" data-icon="inline-end" />
                  </Link>
                ) : undefined
              }
            >
              {items.length ? (
                <IssueList slug={slug} title={doc.title} items={items} images={data.images} />
              ) : (
                <EmptyState
                  icon={CircleCheck}
                  headingLevel={3}
                  title="No issues to review"
                  description="No review items were detected in this document."
                  action={<Link href={viewerUrl({ slug })} className={cn(buttonVariants({ variant: "outline" }))}>Read transcript</Link>}
                />
              )}
            </Section>
          ) : (
            <Section id="issues" title="Not converted yet">
              <NotConvertedNotice slug={slug} pdfHref={doc.pdfHref} title={doc.title} outdated={data.outdated} />
            </Section>
          )}
          {converted ? <DecisionsSection slug={slug} title={doc.title} /> : null}
          {audit ? (
            <div className="mt-10">
              <Section
                id="audit"
                title="Source PDF audit"
                description={`${audit.passed ? "Passed" : "Not passed"}. Checks of the original PDF for the structure a screen reader needs.`}
              >
                <ul className="flex flex-col gap-3">
                  {audit.findings.map((f) => (
                    <li key={f.id} className="flex flex-col gap-1">
                      <p className="type-small font-medium">
                        {f.title} <span className="text-muted-foreground">({f.status})</span>
                      </p>
                      <p className="type-small text-muted-foreground">{f.detail}</p>
                    </li>
                  ))}
                </ul>
              </Section>
            </div>
          ) : null}
        </div>
        <DocumentFacts doc={doc} />
      </div>
    </>
  );
}
