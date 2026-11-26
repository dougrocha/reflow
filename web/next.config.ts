import path from "node:path";
import type { NextConfig } from "next";
import { LOADER_PATH } from "./src/embed/loader-rules";
import { appCsp, COMMON_HEADERS, rawFileCsp } from "./src/lib/csp";

// The policies are in src/lib/csp.ts, which src/proxy.ts and src/lib/public-files.ts read too.
const SECURITY_HEADERS = [
  ...COMMON_HEADERS,
  { key: "X-Frame-Options", value: "SAMEORIGIN" },
  { key: "Content-Security-Policy", value: appCsp() },
];

const nextConfig: NextConfig = {
  // The repository, not web/: the transcript styles import the stylesheet the Python renderer
  // also ships (src/reflow/viewer/transcript.css), and Turbopack reads no file outside its root.
  turbopack: { root: path.join(__dirname, "..") },
  // Extra hostnames (e.g. a LAN IP for testing on a phone) that may load dev assets:
  // REFLOW_DEV_ORIGINS=192.168.0.10,my-host.local bun run dev
  allowedDevOrigins: process.env.REFLOW_DEV_ORIGINS?.split(",").filter(Boolean) ?? [],
  cacheComponents: true,
  partialPrefetching: true,
  // A document built after `next start` booted has files Next does not know (it indexes public/ at
  // boot). Fallback rewrites run only after the static files were checked, so files that existed at
  // boot are still served by Next, and the rest by a route that reads them from disk
  // (src/lib/public-files.ts). The headers() rules below match the original URL, so they apply to both.
  async rewrites() {
    return {
      beforeFiles: [],
      afterFiles: [],
      fallback: [
        { source: "/docs/:slug/:path+", destination: "/api/files/docs/:slug/:path+" },
        { source: "/pdfs/:file", destination: "/api/files/pdfs/:file" },
      ],
    };
  },
  async headers() {
    return [
      // Pages, figures, chunks and assets are immutable only at a versioned URL: the viewer adds
      // "?v=<digest>" (the manifest's "v", a digest of the published output) to every one it
      // loads, so a reconversion that changes any of them changes the URL and nothing stale is kept. The same files at a bare URL
      // (the standalone index.html, older links) are revalidated on every load instead.
      {
        source: "/docs/:slug/:dir(pages|figures|chunks|assets)/:path*",
        has: [{ type: "query", key: "v" }],
        headers: [{ key: "Cache-Control", value: "public, max-age=31536000, immutable" }],
      },
      // (Every matching rule applies, in order, so this one must exclude versioned URLs or it
      // would override the rule above.)
      {
        source: "/docs/:slug/:dir(pages|figures|chunks|assets)/:path*",
        missing: [{ type: "query", key: "v" }],
        headers: [{ key: "Cache-Control", value: "public, max-age=0, must-revalidate" }],
      },
      // The entry points change in place when a document is converted again.
      {
        source: "/docs/:slug/:file(manifest|toc|review)\\.json",
        headers: [{ key: "Cache-Control", value: "public, max-age=0, must-revalidate" }],
      },
      // The loader script (docs/embed.md) is included by other sites, so it is public JavaScript with
      // nothing in it that varies. Its path is not versioned, so a copy is kept for an hour. The
      // rule below leaves /embed/* out, so it gets only these headers.
      {
        source: LOADER_PATH,
        headers: [
          ...COMMON_HEADERS,
          { key: "Cache-Control", value: "public, max-age=3600" },
          // For a page that includes it with crossorigin=anonymous.
          { key: "Access-Control-Allow-Origin", value: "*" },
        ],
      },
      // Every path but /embed/<id>, whose headers src/proxy.ts sets per request: it is the one page
      // another site may frame, so it has frame-ancestors from the resolver's index and no
      // X-Frame-Options. And but /demo-site, the demo customer page (src/demo-site/server.ts), which
      // sends a customer's own policy, as a site of its own would.
      {
        source: "/:path((?!embed/[^/]+/?$|demo-site(?:/|$)).*)",
        headers: SECURITY_HEADERS,
      },
      // A document's own published pages (index.html, the chunk pages, and any SVG, the files a
      // browser runs as a document), and not the app's /docs/<slug> and /docs/<slug>/compare pages
      // or the PDF (Chrome shows no PDF in a sandbox): the converter's HTML of an untrusted PDF runs
      // sandboxed, in an opaque origin (src/lib/csp.ts). Also at /api/files/docs/..., the route
      // that serves files built after boot: these rules win over a route's own headers. After the
      // rule above, so this CSP is the one sent.
      ...["/docs", "/api/files/docs"].map((prefix) => ({
        source: `${prefix}/:slug/:path(.+\\.(?:html|svg))`,
        headers: [{ key: "Content-Security-Policy", value: rawFileCsp() }],
      })),
      // The standalone viewer, in that opaque origin, fetches its chunk pages and toc.json across
      // origins. They are public by intent and read without credentials.
      {
        source: "/docs/:slug/:path(chunks/.+|toc\\.json)",
        headers: [{ key: "Access-Control-Allow-Origin", value: "*" }],
      },
    ];
  },
};

export default nextConfig;
