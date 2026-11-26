// The documents list's rows and its sort orders. Plain comparators, shared by the server
// (the default order of the home page) and the table's own controls: no table library.

export type DocumentRow = {
  slug: string;
  title: string;
  description: string;
  /** Where the title goes: the document's transcript page, /view/<slug>. */
  href: string;
  /** Page count of the converted document, null when it has not been converted. */
  pages: number | null;
  errors: number;
  warnings: number;
  /** Inspector link: the first issue, or page 1. */
  inspectHref: string;
};

/** A sortable column of the table. */
export type SortColumn = "pages" | "issues";
type SortDirection = "asc" | "desc";
/** The order the rows are shown in; null is the server's order (the sample list). */
export type DocumentSort = { column: SortColumn; direction: SortDirection } | null;

// Ascending comparators. Pages: unconverted documents (null) go last whichever way the
// column is sorted. Issues: errors first, then warnings.
const COMPARE: Record<SortColumn, (a: DocumentRow, b: DocumentRow) => number> = {
  pages: (a, b) => {
    if (a.pages === null || b.pages === null) return a.pages === b.pages ? 0 : a.pages === null ? 1 : -1;
    return a.pages - b.pages;
  },
  issues: (a, b) => a.errors - b.errors || a.warnings - b.warnings,
};

/** The rows in `sort` order: a new array, stable, with the null-page rule kept for both directions. */
export function sortDocuments(rows: DocumentRow[], sort: DocumentSort): DocumentRow[] {
  if (!sort) return rows;
  const compare = COMPARE[sort.column];
  const sign = sort.direction === "desc" ? -1 : 1;
  return [...rows].sort((a, b) => {
    // Unconverted rows stay last even when the order is reversed.
    if (sort.column === "pages" && (a.pages === null || b.pages === null)) return compare(a, b);
    return sign * compare(a, b);
  });
}
