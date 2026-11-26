// How a page is named on screen. ?page=N and every "Page N" in the app use the PDF page index
// (1-based). When the document prints a different number on the page, that is shown too.
export function pageText(page: number, printed?: string | null): string {
  return printed && printed !== String(page) ? `${page} (printed ${printed})` : String(page);
}

// A page number typed by a reader or taken from a URL. Only a whole number of 1 or more counts:
// "1.5", "1e2", "0", "-3" and "" are null. A repeated query parameter means its first value.
export function parsePage(value: string | string[] | number | null | undefined): number | null {
  const raw = Array.isArray(value) ? value[0] : value;
  const n = typeof raw === "number" ? raw : typeof raw === "string" && /^\s*\d+\s*$/.test(raw) ? Number(raw) : NaN;
  return Number.isInteger(n) && n >= 1 ? n : null;
}

/** `page` kept within 1..`count`. */
export const clampPage = (page: number, count: number): number => Math.min(Math.max(page, 1), count);

// "1 page", "12 pages", "1,234 pages". Mirrors page_count_text in src/reflow/render/pagenames.py.
export function pageCountText(n: number): string {
  return `${n.toLocaleString("en-US")} ${n === 1 ? "page" : "pages"}`;
}

/** A page's file name stem in a document's published and working folders: 0007. */
export const pageStem = (page: number): string => String(page).padStart(4, "0");

/** A page image, relative to the document's base. */
export const pageImagePath = (page: number): string => `pages/${pageStem(page)}.webp`;
