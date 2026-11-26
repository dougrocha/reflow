// The form of a source URL the index is keyed by. A link on a customer's page and the entry the
// operator wrote name the same PDF when this gives the same string for both. It never makes two
// different resources equal: it keeps the path and the query exactly as written (a `/download?id=3`
// link is a different document from `?id=4`), and a link it cannot read is not a link at all.

/** The longest URL the resolver looks at; a longer one is refused before it is parsed. */
export const MAX_URL_CHARS = 2048;

/** The URL in its normal form, or null when it is not an absolute http or https URL, has
 *  credentials, or is too long. Scheme and host are lower case (URL does that), the default port,
 *  the fragment and the dot segments are gone. */
export function normalizeSourceUrl(raw: string): string | null {
  if (raw.length > MAX_URL_CHARS) return null;
  let url: URL;
  try {
    url = new URL(raw.trim());
  } catch {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  if (url.username !== "" || url.password !== "" || url.hostname === "") return null;
  url.hash = "";
  return url.href;
}
