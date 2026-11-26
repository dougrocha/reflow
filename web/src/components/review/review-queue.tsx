import Link from "next/link";
import { ArrowRight } from "lucide-react";
import { DocumentCard } from "@/components/app/document-card";
import { IssueCounts } from "@/components/app/issue-counts";
import { buttonVariants } from "@/components/ui/button-variants";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import type { DocumentSummary } from "@/lib/documents";
import { pageCountText } from "@/lib/pages";
import { inspectUrl, overviewUrl } from "@/lib/urls";

const HEAD = "h-11 px-3 first:pl-4 last:pr-4 type-label text-muted-foreground";
const CELL = "px-3 py-3 align-top first:pl-4 last:pr-4 whitespace-normal";
const LINK = "font-medium text-link underline decoration-link/40 underline-offset-4 hover:decoration-link";

function startHref(d: DocumentSummary) {
  return d.firstIssueHref ?? inspectUrl({ slug: d.slug });
}

function mostCommon(d: DocumentSummary) {
  return d.topIssue ? `${d.topIssue.type} (${d.topIssue.count})` : "—";
}

export function ReviewQueue({ docs }: { docs: DocumentSummary[] }) {
  return (
    <>
      {/* Phones */}
      <ul role="list" className="grid gap-3 md:hidden">
        {docs.map((d) => (
          <li key={d.slug}>
            <DocumentCard
              doc={d}
              titleHref={overviewUrl(d.slug)}
              headingLevel={3}
              footer={<p className="type-small"><span className="text-muted-foreground">Most common: </span>{mostCommon(d)}</p>}
              actions={
                <>
                  <Link href={startHref(d)} className={buttonVariants({ size: "lg" })}>
                    Start review<span className="sr-only">, {d.title}</span>
                  </Link>
                  <Link href={`${overviewUrl(d.slug)}#issues`} className={buttonVariants({ variant: "outline", size: "lg" })}>
                    All issues<span className="sr-only">, {d.title}</span>
                  </Link>
                </>
              }
            />
          </li>
        ))}
      </ul>

      {/* md and up */}
      <div className="hidden overflow-hidden rounded-xl ring-1 ring-foreground/10 md:block">
        <Table>
          <TableCaption className="sr-only">Documents with review items, most errors first</TableCaption>
          <TableHeader>
            <TableRow>
              <TableHead className={HEAD}>Document</TableHead>
              <TableHead className={HEAD}>Issues</TableHead>
              <TableHead className={`${HEAD} hidden lg:table-cell`}>Most common</TableHead>
              <TableHead className={`${HEAD} text-right`}><span className="sr-only">Actions</span></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {docs.map((d) => (
              <TableRow key={d.slug}>
                <TableCell className={`${CELL} min-w-56`}>
                  <Link href={overviewUrl(d.slug)} className={LINK}>{d.title}</Link>
                  <p className="mt-1 type-small text-muted-foreground tabular-nums">{d.pages ? pageCountText(d.pages) : null}</p>
                </TableCell>
                <TableCell className={CELL}><IssueCounts errors={d.errors} warnings={d.warnings} /></TableCell>
                <TableCell className={`${CELL} hidden type-small lg:table-cell`}>{mostCommon(d)}</TableCell>
                <TableCell className={`${CELL} text-right`}>
                  <div className="flex justify-end gap-2">
                    <Link href={startHref(d)} className={buttonVariants()}>
                      Start review<span className="sr-only">, {d.title}</span>
                      <ArrowRight aria-hidden="true" data-icon="inline-end" />
                    </Link>
                    <Link href={`${overviewUrl(d.slug)}#issues`} className={buttonVariants({ variant: "outline" })}>
                      All issues<span className="sr-only">, {d.title}</span>
                    </Link>
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </>
  );
}
