// The message protocol between a page that frames reflow's viewer (the parent: a customer's page,
// through the loader) and the viewer in the frame (/embed/<id>). docs/embed.md.
//
// No imports and no DOM: the loader is built to one dependency-free file, and the viewer's side
// and the parent's side (./viewer, ./parent) both read messages through `parseMessage` here, so
// the two cannot disagree about what a message is.
//
// Every message is one flat object:
//
//   { protocol: "reflow-embed", version: 1, instance: "<id>", type: "<type>", ...fields }
//
//   viewer → parent   ready  {}                  the viewer is listening; the parent may send now
//                     title  { title: string }   the document's title, for the dialog's name
//                     page   { page: number }    the page at the reading line changed
//                     close  {}                  the reader asked to close (Close button, Escape)
//   parent → viewer   page   { page: number }    go to this page
//
// `instance` is a random id the parent makes for each frame it opens and puts in the frame's URL.
// A message of another instance, another version, another type or with any other field is not a
// message of this protocol and is ignored. The origin and the source window are checked by the
// side that receives (./viewer, ./parent); this module checks the shape.

export const PROTOCOL = "reflow-embed";
export const VERSION = 1;

/** A per-frame id: the parent makes it (newInstanceId), the frame's URL carries it. */
export const INSTANCE_ID = /^[A-Za-z0-9_-]{16,64}$/;

/** The longest title a message carries; a longer one is not a message. */
export const MAX_TITLE_CHARS = 500;

export type ViewerMessage =
  | { type: "ready" }
  | { type: "title"; title: string }
  | { type: "page"; page: number }
  | { type: "close" };

export type ParentMessage = { type: "page"; page: number };

/** Which fields each type has, besides the envelope's, per direction. */
const FIELDS = {
  viewer: { ready: [], title: ["title"], page: ["page"], close: [] },
  parent: { page: ["page"] },
} as const satisfies Record<string, Record<string, readonly string[]>>;

const ENVELOPE = ["protocol", "version", "instance", "type"];

const isPage = (v: unknown): v is number => typeof v === "number" && Number.isInteger(v) && v >= 1 && v <= 1_000_000;
const isTitle = (v: unknown): v is string => typeof v === "string" && v.length <= MAX_TITLE_CHARS;

/** The message in `data` when it is one of this protocol, from `from`, for `instance`; else null. */
export function parseMessage(data: unknown, from: "viewer", instance: string): ViewerMessage | null;
export function parseMessage(data: unknown, from: "parent", instance: string): ParentMessage | null;
export function parseMessage(data: unknown, from: "viewer" | "parent", instance: string): ViewerMessage | ParentMessage | null {
  if (typeof data !== "object" || data === null || Array.isArray(data)) return null;
  const m = data as Record<string, unknown>;
  if (m.protocol !== PROTOCOL || m.version !== VERSION || m.instance !== instance || !INSTANCE_ID.test(instance)) return null;
  const types: Record<string, readonly string[]> = FIELDS[from];
  if (typeof m.type !== "string" || !Object.hasOwn(types, m.type)) return null;
  const fields = types[m.type];
  const keys = Object.keys(m);
  if (keys.length !== ENVELOPE.length + fields.length || !keys.every((k) => ENVELOPE.includes(k) || fields.includes(k))) return null;
  if (fields.includes("page") && !isPage(m.page)) return null;
  if (fields.includes("title") && !isTitle(m.title)) return null;
  return m as ViewerMessage | ParentMessage;
}

/** The message on the wire: the envelope around `body`. */
export function envelope(instance: string, body: ViewerMessage | ParentMessage): Record<string, unknown> {
  return { protocol: PROTOCOL, version: VERSION, instance, ...body };
}

/** An origin as `URL.origin` writes it, http or https: no path, no wildcard, no `null`. */
export function exactOrigin(value: unknown): string | null {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value);
    // A wildcard host ("https://*.example.org") survives URL parsing, and in frame-ancestors it
    // would let every matching site frame the viewer.
    return (url.protocol === "http:" || url.protocol === "https:") && url.origin === value && !value.includes("*")
      ? value : null;
  } catch {
    return null;
  }
}

/** A new instance id: 128 random bits, hex. */
export function newInstanceId(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** The frame's URL: the resolver's viewer URL with the parent's origin and the instance id added.
 *  The server frames the viewer only for a parent origin the site lists (docs/embed.md). */
export function embedSrc(viewer: string, { parent, instance, page }: { parent: string; instance: string; page?: number }): string {
  const url = new URL(viewer);
  url.searchParams.set("parent", parent);
  url.searchParams.set("instance", instance);
  if (page !== undefined && isPage(page)) url.searchParams.set("page", String(page));
  return url.href;
}
