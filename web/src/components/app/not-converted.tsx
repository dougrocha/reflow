import { CircleDashed, Download, History } from "lucide-react";
import { ConvertNow } from "@/components/builds/controls";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { buttonVariants } from "@/components/ui/button-variants";
import { RenderedFrom } from "@/components/builds/rendered-from";
import type { SampleDoc } from "@/docs";
import type { DocumentData } from "@/lib/docs-data";
import { inspectUrl, pdfHref as pdfUrl, viewerUrl } from "@/lib/urls";
import type { VariantTab } from "@/lib/variants/view";
import { DEFAULT_PIPELINE, buildCommand, variantOf } from "@/pipelines";
import { WorkspaceHeader } from "./workspace-header";
import type { WorkspaceView } from "./view-switch";

export function NotConvertedNotice({ slug, pdfHref, title, outdated = null }: {
  slug: string;
  pdfHref: string;
  title: string;
  /** Why the build that is published no longer reads (DocumentData.outdated), or null. */
  outdated?: string | null;
}) {
  // A variant: its tab loads here while its build is queued or running.
  const pipeline = variantOf(slug)?.pipeline;
  const variant = pipeline && pipeline.name !== DEFAULT_PIPELINE ? pipeline : null;
  const name = variant ? `The ${variant.label} variant` : "This transcript";
  return (
    <Alert>
      {outdated ? <History aria-hidden="true" /> : <CircleDashed aria-hidden="true" />}
      <AlertTitle>
        {outdated ? `${name} was built by an older version of reflow`
          : variant ? `The ${variant.label} variant is not built yet` : "Not converted yet"}
      </AlertTitle>
      <AlertDescription className="flex flex-col items-start gap-3">
        <p>
          {outdated
            ? "Its files are in a format this app no longer reads, so it can't be opened. Build it again: while the build runs, its progress shows here, and the transcript opens when it is done. To build it from a terminal, run:"
            : variant
              ? `There is no ${variant.label} transcript of this PDF yet. While its build runs, its progress shows here, and the transcript opens when it is done. To build it from a terminal, run:`
              : "reflow has not converted this PDF, so there is no transcript or review to show. To convert it, run:"}
        </p>
        <code className="rounded-md bg-muted px-2 py-1 type-mono text-foreground">{buildCommand(slug)}</code>
        {/* With builds on, the page can convert it: the button, then the status; when the build is published the page refreshes. */}
        <ConvertNow slug={slug} title={title} />
        <a href={pdfHref} download className={buttonVariants({ variant: "outline" })}>
          <Download aria-hidden="true" />
          Download PDF
        </a>
        {outdated ? (
          <details className="type-caption text-muted-foreground">
            <summary className="cursor-pointer hover:text-foreground">What didn&apos;t read</summary>
            <p className="mt-1 break-words font-mono">{outdated}</p>
          </details>
        ) : null}
      </AlertDescription>
    </Alert>
  );
}

/** A workspace page (the transcript, the inspector) of a document with no transcript: its header
 *  and the notice, where the build's progress shows. `slug` is the variant's key, `page` the
 *  reader's page kept for the tabs. */
export function NotConvertedWorkspace({ data, slug, doc, current, page, variants }: {
  data: DocumentData;
  slug: string;
  doc: SampleDoc;
  current: WorkspaceView;
  page: number | null;
  variants: VariantTab[];
}) {
  return (
    <>
      <RenderedFrom reads={[data]} />
      <WorkspaceHeader slug={slug} title={doc.title} current={current} viewHref={viewerUrl({ slug, page })} inspectHref={inspectUrl({ slug, page })} variants={variants} page={page} />
      <main id="main" tabIndex={-1} className="min-h-0 flex-1 overflow-y-auto p-4 focus:outline-none sm:p-8">
        <div className="mx-auto max-w-2xl">
          <h2 className="sr-only">{data.outdated ? "Built by an older version" : "Not converted yet"}</h2>
          <NotConvertedNotice slug={slug} pdfHref={pdfUrl(doc.slug)} title={doc.title} outdated={data.outdated} />
        </div>
      </main>
    </>
  );
}
