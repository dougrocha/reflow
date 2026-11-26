# The resolver

`GET /v1/resolve?site=<key>&url=<pdf url>` says whether a link to a PDF has a conversion that is
ready. The embed loader (`docs/embed.md`) calls it when a visitor activates a link. It reads only what
is already converted; it never starts a conversion.

```
{"status": "ready", "viewer": "https://<reflow origin>/embed/<document id>?site=<key>"}
{"status": "unknown"}
```

`unknown` is the one answer for everything that is not ready: an unknown site, an origin the site
does not list, a URL the site has no document for, a document with no readable `manifest.json`.
The answer never says which, so the endpoint cannot list sites or documents and a site cannot
see another site's documents. A missing or malformed `site` or `url` is a 400.

- **`site` is an identifier, not authentication.** It is in the page source of every page that
  uses it. Nothing behind it is secret; a site sees only the documents the operator registered
  for it.
- **CORS.** `Access-Control-Allow-Origin` is the request's `Origin` when the site lists it, and
  absent otherwise, so a browser cannot read an answer across origins for a site that did not
  list the page. A request with no `Origin` (curl, the same origin) is answered. Never `*`.
- **The viewer URL** is the embeddable viewer for that site (`docs/embed.md`). The loader adds
  `parent` and `instance` before it opens it in a frame.
- **The public origin.** The viewer URL is absolute, on the origin the request was made to, and the
  loader takes an answer only when that is the origin its own script came from. Next gives
  `req.url` the host the server listens as (`localhost`), not the one the client used, so
  `publicOrigin` (`src/lib/resolver/origin.ts`) is the one place that decides, in this order:
  1. `REFLOW_PUBLIC_ORIGIN`, when it is an exact origin (scheme, host, port, no path). Set it in
     any deployment behind a proxy or CDN that terminates TLS or rewrites `Host`.
  2. The request's `Host` header with the scheme of `req.url`, when `Host` is a plain host name or
     IP address with an optional port (anything else is ignored).
  3. `req.url`'s own origin.

  `Host` is the client's word. That is safe here because the origin shapes only the answer to the
  client that sent it (a loader refuses an answer for any origin but its script's), and the answer
  varies by `Host` for shared caches. Forwarding headers (`X-Forwarded-*`, `Forwarded`) are never read.
- **Caching.** `public, max-age=60`, varying by `Origin` and `Host`. The viewer URL is not versioned
  yet, so the answer is kept short.
- **No rate limit here.** The endpoint is read-only and cheap, and a URL is capped at 2048
  characters. A per-client limit belongs with the work that decides how a client is told apart.

## The index

The operator writes `<DATA_ROOT>/resolver.json` (`web/.reflow/resolver.json` by default). It is read
again when its size or modification time changes. Without it every link is `unknown`.

```json
{ "sites": { "acme": {
    "origins": ["https://example.org"],
    "documents": { "https://example.org/files/w9.pdf": "fw9" } } } }
```

- A site key is `[A-Za-z0-9_-]{1,64}`. `origins` are exact origins (scheme, host and port, no
  path, no wildcard): the site's pages, which may read the answer and frame the viewer. They are
  not the domains PDFs may be fetched from, which is a different list (later).
- A document is a registered one: the slug of a catalogue document (`web/src/docs.ts`), not a
  variant and not a path. Entries that are not right are dropped and logged, never repaired.
- Source URLs are matched by their normal form: lower-case scheme and host, no default port, no
  fragment, no dot segments. The path and the query are kept as written, so `/download?id=3` and
  `?id=4` are different documents.
- Ready means `public/docs/<id>/manifest.json` exists and is one the viewer's schema accepts, with
  pages. A document published by an older converter is `unknown` until it is built again.

`src/lib/resolver/index.ts` has the one read interface the route needs (`ResolverStore.site`);
the file is its only backend. `resolverStore()` in the same file is the one constructor, used by the
resolver route and by the embed proxy (`src/proxy.ts`), so they cannot read different indexes.

### The demo site

`resolverStore()` adds one built-in site behind the file: `demo` (`src/lib/resolver/demo.ts`), for
trying the loader end to end (`docs/embed.md`, "Trying it"). Its only origin is
`REFLOW_DEMO_ORIGIN`, read from the environment and never from a request, and it has no site unless
that is an exact origin. Its documents are `DEMO_DOCUMENTS`: `<origin>/files/fw9.pdf` and
`<origin>/files/accident-insurance-flyer.pdf`. A site `demo` in the operator's `resolver.json` wins, whole.
`scripts/dev` sets the variable for the dev server it starts. A production deployment leaves it
unset, so `demo` is an unknown site there.
