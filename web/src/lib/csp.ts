// The Content Security Policies the site sends. No imports: next.config.ts reads this file too.
//
//  - appCsp: every page of the app (next.config.ts, with X-Frame-Options: SAMEORIGIN).
//  - embedCsp: /embed/<id> (src/proxy.ts), the one page another site may frame. frame-ancestors
//    names the one parent origin the site record allows, or 'none'.
//  - RAW_FILE_CSP: a document's own published files under /docs/<slug>/ (index.html, the chunk
//    pages and the rest; next.config.ts and src/lib/public-files.ts). They are the converter's HTML
//    of an untrusted PDF, so they run in a sandbox: an opaque origin with no access to this
//    origin's storage or to its APIs as the same origin. The tokens are what the standalone
//    viewer (src/reflow/viewer/viewer.js) needs and no more:
//      allow-scripts      viewer.js loads chunks in place and follows deep links;
//      allow-popups, allow-popups-to-escape-sandbox
//                         a link opened in a new tab (Ctrl+click) opens, as a normal page;
//      allow-downloads    "Download the original PDF" where the browser saves PDFs;
//      allow-modals       printing (viewer.js marks what is not loaded before print).
//    Not allow-same-origin (it would undo the sandbox), not allow-forms (the transcripts have
//    no <form>; their controls work without it), not allow-top-navigation.

const BASE = [
  "default-src 'self'",
  "img-src 'self' data: blob:",
  // The transcript prints from a hidden frame holding a blob: copy of the document.
  "frame-src 'self' blob:",
  "style-src 'self' 'unsafe-inline'",
];

/** Next injects inline scripts for the RSC payload, so script-src keeps 'unsafe-inline';
 *  'unsafe-eval' is only for the dev server (React's debugging tools). */
const scriptSrc = (dev: boolean): string => `script-src 'self' 'unsafe-inline'${dev ? " 'unsafe-eval'" : ""}`;

export const isDev = (): boolean => process.env.NODE_ENV === "development";

export const appCsp = (dev = isDev()): string => [...BASE, scriptSrc(dev)].join("; ");

export const RAW_SANDBOX = "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox allow-downloads allow-modals";

export const rawFileCsp = (dev = isDev()): string => `${appCsp(dev)}; ${RAW_SANDBOX}`;

/** The headers every response gets besides its CSP (and, outside /embed, X-Frame-Options). */
export const COMMON_HEADERS: readonly { key: string; value: string }[] = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
];

/** `ancestors` is one exact origin or "'none'". */
export const embedCsp = (ancestors: string, dev = isDev()): string => `${appCsp(dev)}; frame-ancestors ${ancestors}`;
