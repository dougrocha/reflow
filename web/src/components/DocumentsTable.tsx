"use client";

import { useId, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
import { DocumentCard } from "@/components/app/document-card";
import { IssueCounts } from "@/components/app/issue-counts";
import { StatusBadge } from "@/components/app/status-badge";
import { useBuilds } from "@/components/builds/context";
import { BuildStatus, BuildsLink, RebuildButton } from "@/components/builds/controls";
import { Button, buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";
import { sortDocuments, type DocumentRow, type DocumentSort, type SortColumn } from "./documents-sort";
import { overviewUrl } from "@/lib/urls";

/** One section of the list: a collection (docs.ts) and its rows, in the server's order. */
export type DocumentSection = { id: string; name: string; rows: DocumentRow[] };

/** The id of a section's heading, the target of the jump links. */
const sectionAnchor = (id: string) => `collection-${id}`;

// Column-specific cell and header classes.
const HEAD_BASE = "h-11 px-3 first:pl-4 last:pr-4 type-label text-muted-foreground";
const CELL_BASE = "px-3 py-3 align-top first:pl-4 last:pr-4";

// The phone sort select and the table header buttons share one `sort` state. The select
// offers the orders worth naming; any other order shows as the default.
type SortValue = "default" | "pages-desc" | "pages-asc" | "issues-desc";
const SORT_SELECTION: Record<SortValue, DocumentSort> = {
  default: null,
  "pages-desc": { column: "pages", direction: "desc" },
  "pages-asc": { column: "pages", direction: "asc" },
  "issues-desc": { column: "issues", direction: "desc" },
};

function sortValueOf(sort: DocumentSort): SortValue {
  const match = (Object.keys(SORT_SELECTION) as SortValue[]).find((key) => {
    const wanted = SORT_SELECTION[key];
    return wanted === null ? sort === null : sort?.column === wanted.column && sort.direction === wanted.direction;
  });
  return match ?? "default";
}

// A header press cycles ascending, descending, then the default order.
function toggled(sort: DocumentSort, column: SortColumn): DocumentSort {
  if (sort?.column !== column) return { column, direction: "asc" };
  return sort.direction === "asc" ? { column, direction: "desc" } : null;
}

function SortHeader({ column, sort, onSort, label, className }: {
  column: SortColumn;
  sort: DocumentSort;
  onSort: (next: DocumentSort) => void;
  label: string;
  className?: string;
}) {
  const direction = sort?.column === column ? sort.direction : null;
  const Icon = direction === "asc" ? ArrowUp : direction === "desc" ? ArrowDown : ArrowUpDown;
  return (
    <TableHead
      className={cn(HEAD_BASE, className)}
      aria-sort={direction === "asc" ? "ascending" : direction === "desc" ? "descending" : "none"}
    >
      <Button
        variant="ghost"
        size="sm"
        className={cn("-mx-2.5 font-medium", column === "pages" && "ml-auto")}
        onClick={() => onSort(toggled(sort, column))}
      >
        {label}
        <Icon aria-hidden="true" className="size-3.5 text-muted-foreground" />
      </Button>
    </TableHead>
  );
}

function BuildCommand({ slug, className }: { slug: string; className?: string }) {
  return (
    <code className={cn("rounded-md bg-muted px-1.5 py-0.5 type-mono", className)}>
      bun run docs:build {slug}
    </code>
  );
}

function RowActions({ row, size }: { row: DocumentRow; size?: "lg" }) {
  return (
    <>
      <Link href={overviewUrl(row.slug)} className={buttonVariants({ variant: "outline", size })}>
        Overview<span className="sr-only">, {row.title}</span>
      </Link>
      {row.pages ? (
        <Link href={row.inspectHref} className={buttonVariants({ variant: "outline", size })}>
          Inspect<span className="sr-only">, {row.title}</span>
        </Link>
      ) : null}
      <RebuildButton slug={row.slug} title={row.title} size={size} built={row.pages !== null} />
    </>
  );
}

/** The documents list, in sections: rows arrive in the server's order; one filter and sort,
 *  local, apply to every section. */
export function DocumentsTable({ sections }: { sections: DocumentSection[] }) {
  // Set when the server has builds on (a BuildsProvider above): then each row shows its build
  // status and a Rebuild button, in place of the command that builds it.
  const builds = useBuilds();
  const filterId = useId();
  const sortId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<DocumentSort>(null);

  const total = sections.reduce((n, section) => n + section.rows.length, 0);
  const filtering = query.trim() !== "";
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return sections.map((section) => ({
      ...section,
      body: sortDocuments(
        q ? section.rows.filter((row) => `${row.title} ${row.description}`.toLowerCase().includes(q)) : section.rows,
        sort,
      ),
    }));
  }, [sections, query, sort]);
  const matches = shown.reduce((n, section) => n + section.body.length, 0);
  // While filtering, a section with no matches is left out, from the list and from the jump links.
  const visible = shown.filter((section) => !filtering || section.body.length > 0);
  const count = (section: (typeof shown)[number]) =>
    filtering ? `${section.body.length} of ${section.rows.length}` : `${section.rows.length}`;

  const emptyFilter = (
    <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed p-8 text-center">
      <p>No documents match &ldquo;{query.trim()}&rdquo;.</p>
      <Button
        variant="outline"
        onClick={() => {
          setQuery("");
          inputRef.current?.focus();
        }}
      >
        Clear filter
      </Button>
    </div>
  );

  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          <Label htmlFor={filterId} className="sr-only">
            Filter documents by title or description
          </Label>
          <Input
            ref={inputRef}
            id={filterId}
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Filter by title or description"
            autoComplete="off"
            className="h-9 sm:w-80"
          />
          <div className="flex items-center gap-2 md:hidden">
            <Label htmlFor={sortId} className="type-label">
              Sort
            </Label>
            <select
              id={sortId}
              value={sortValueOf(sort)}
              onChange={(event) => setSort(SORT_SELECTION[event.target.value as SortValue])}
              className="h-9 flex-1 rounded-lg border border-input bg-transparent px-2 text-sm dark:bg-input/30"
            >
              <option value="default">Default order</option>
              <option value="pages-desc">Pages, most first</option>
              <option value="pages-asc">Pages, fewest first</option>
              <option value="issues-desc">Issues, most first</option>
            </select>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <p aria-live="polite" className="type-small text-muted-foreground">
            {filtering ? `${matches} of ${total} documents` : `${total} documents`}
          </p>
          <BuildsLink />
        </div>
      </div>

      {visible.length > 0 ? (
        <nav aria-label="Jump to section">
          <ul role="list" className="flex flex-wrap gap-x-4 gap-y-2 type-small">
            {visible.map((section) => (
              <li key={section.id}>
                <a
                  href={`#${sectionAnchor(section.id)}`}
                  className="text-link underline decoration-link/40 underline-offset-4 hover:decoration-link"
                >
                  {section.name} ({count(section)})
                </a>
              </li>
            ))}
          </ul>
        </nav>
      ) : (
        emptyFilter
      )}

      {visible.map((section) => (
        <section key={section.id} aria-labelledby={sectionAnchor(section.id)} className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-3">
          <h2 id={sectionAnchor(section.id)} className="mt-4 scroll-mt-20 type-h2">
            {section.name} ({count(section)})
          </h2>

          {/* Phones: cards. The same rows, filter and sort as the table. */}
          <ul role="list" className="grid gap-3 md:hidden">
            {section.body.map((row) => (
              <li key={row.slug}>
                <DocumentCard
                  doc={row}
                  titleHref={row.href}
                  headingLevel={3}
                  footer={builds ? <BuildStatus slug={row.slug} /> : row.pages ? undefined : <BuildCommand slug={row.slug} className="self-start" />}
                  actions={<RowActions row={row} size="lg" />}
                />
              </li>
            ))}
          </ul>

          {/* md and up: the table. */}
          <div className="hidden overflow-hidden rounded-xl ring-1 ring-foreground/10 md:block">
            <Table>
              <TableCaption className="sr-only">
                {section.name} documents, with page counts, review issues and links to the overview and inspector
              </TableCaption>
              <TableHeader>
                <TableRow>
                  <TableHead className={HEAD_BASE}>Document</TableHead>
                  <SortHeader column="pages" sort={sort} onSort={setSort} label="Pages" className="text-right" />
                  <TableHead className={cn(HEAD_BASE, "hidden lg:table-cell")}>Status</TableHead>
                  <SortHeader column="issues" sort={sort} onSort={setSort} label="Issues" />
                  <TableHead className={cn(HEAD_BASE, "text-right")}>
                    <span className="sr-only">Actions</span>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {section.body.map((row) => (
                  <TableRow key={row.slug}>
                    <TableCell className={cn(CELL_BASE, "min-w-64 whitespace-normal")}>
                      <Link
                        href={row.href}
                        className="type-body font-medium text-link underline decoration-link/40 underline-offset-4 hover:decoration-link"
                      >
                        {row.title}
                      </Link>
                      <p className="mt-1 type-small text-muted-foreground">{row.description}</p>
                      {builds ? <BuildStatus slug={row.slug} className="mt-2 lg:hidden" /> : null}
                    </TableCell>
                    <TableCell className={cn(CELL_BASE, "text-right tabular-nums")}>
                      {row.pages?.toLocaleString("en-US") ?? "—"}
                    </TableCell>
                    <TableCell className={cn(CELL_BASE, "hidden lg:table-cell")}>
                      {row.pages ? (
                        <div className="flex flex-col items-start gap-1.5">
                          <StatusBadge converted />
                          {builds ? <BuildStatus slug={row.slug} /> : null}
                        </div>
                      ) : (
                        <div className="flex flex-col items-start gap-1.5">
                          <StatusBadge converted={false} />
                          {builds ? <BuildStatus slug={row.slug} /> : <BuildCommand slug={row.slug} className="text-foreground" />}
                        </div>
                      )}
                    </TableCell>
                    <TableCell className={cn(CELL_BASE, "whitespace-normal")}>
                      {row.pages ? (
                        <IssueCounts errors={row.errors} warnings={row.warnings} href={`${overviewUrl(row.slug)}#issues`} />
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </TableCell>
                    <TableCell className={cn(CELL_BASE, "whitespace-normal text-right")}>
                      <div className="flex flex-wrap justify-end gap-2">
                        <RowActions row={row} />
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </section>
      ))}
    </div>
  );
}
