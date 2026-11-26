import { PageHeader } from "@/components/app/page-header";
import { RenderedFrom } from "@/components/builds/rendered-from";
import { DocumentsTable, type DocumentSection } from "@/components/DocumentsTable";
import type { DocumentRow } from "@/components/documents-sort";
import { HashRedirect } from "@/components/home/hash-redirect";
import { COLLECTIONS } from "@/docs";
import { documentSummaries, type DocumentSummary } from "@/lib/documents";
import { inspectUrl, viewerUrl } from "@/lib/urls";

export const metadata = { title: "Documents" };

export default async function Home() {
  const docs = await documentSummaries();
  const row = (d: DocumentSummary): DocumentRow => ({
    slug: d.slug,
    title: d.title,
    description: d.description,
    // The title opens the transcript page: its URL keeps the reader's page (?page=N), so a
    // session survives a reload and can be shared. The PDF is linked from the document pages.
    href: viewerUrl({ slug: d.slug }),
    pages: d.pages,
    errors: d.errors,
    warnings: d.warnings,
    inspectHref: d.firstIssueHref ?? inspectUrl({ slug: d.slug }),
  });
  // One section per collection, in COLLECTIONS order; each in the sample list's order.
  const sections: DocumentSection[] = COLLECTIONS.map((c) => ({
    id: c.id,
    name: c.name,
    rows: docs.filter((d) => d.collection === c.id).map(row),
  }));

  return (
    <>
      <RenderedFrom reads={docs} />
      <HashRedirect slugs={docs.map((d) => d.slug)} />
      <PageHeader
        display
        title="Public documents"
        lead="Public documents, listed the way a city or agency site lists them. Select a title to read its accessible transcript. These are publicly posted third-party documents, shown as conversion samples; reflow is not affiliated with or endorsed by their publishers."
      />
      {/* With builds on (a dev server, or REFLOW_BUILDS=1) the table also shows each build and has Rebuild. */}
      <DocumentsTable sections={sections} />
    </>
  );
}
