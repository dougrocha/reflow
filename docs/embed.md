# The embeddable viewer

`/embed/<id>` is the viewer that another site opens in a frame when a visitor activates a link to
a PDF that reflow has converted. The resolver (`docs/resolver.md`) gives its URL; the
loader (below) opens it. The same URL, opened directly, is the document's
shareable hosted page.

```
customer page (https://example.org)                reflow
  GET /v1/resolve?site=acme&url=<pdf>  ─────────►  {status: "ready", viewer: ".../embed/fw9?site=acme"}
  <iframe src=".../embed/fw9?site=acme&parent=https://example.org&instance=<random>">
     ◄── ready, title, page, close ───  viewer   (postMessage to https://example.org only)
     ─── page ──────────────────────►   viewer   (postMessage to reflow's origin only)
```

## The route

- **The id is a registered document**: the slug of a catalogue document (`web/src/docs.ts`). Not
  a base URL, not a path, not a variant. Anything else is a 404. The viewer's base is always
  `/docs/<id>/`, so nothing in the URL chooses which HTML runs on reflow's origin.
- **Framing** (`web/src/lib/resolver/embed.ts`, sent by `web/src/proxy.ts`). The page sends
  `Content-Security-Policy: frame-ancestors <parent>` with the one origin in `parent` when all of
  these hold, and `frame-ancestors 'none'` otherwise:
  - `site` names a site of the resolver's index, and that site registered the document;
  - `parent` is an exact origin in the site's `origins`;
  - `instance` is an instance id (`[A-Za-z0-9_-]{16,64}`);
  - each of the three is in the query once.

  The browser checks frame-ancestors against the real parent, so a page that names an origin it
  is not on cannot frame the viewer. `site=` is an identifier, not authentication: it chooses the
  record, and the browser proves the parent. `/embed` gets no `X-Frame-Options`; every other path
  keeps `X-Frame-Options: SAMEORIGIN`. The response is `Cache-Control: no-store`, because its
  headers depend on the query and on the index, which can change at any time.
- **Not offered in the embed:** Ask (it needs a per-site budget beyond the per-client limit
  first), the builds panel (the page never calls `/api/builds`), and the workspace header.

## The protocol

`web/src/embed/protocol.ts`. Every message is one flat object:

```
{ protocol: "reflow-embed", version: 1, instance: "<id>", type: "<type>", ...fields }
```

| From | Type | Fields | Meaning |
| --- | --- | --- | --- |
| viewer | `ready` | none | the viewer listens; the parent may send from now on |
| viewer | `title` | `title`: string, at most 500 characters | the document's title, for the dialog's name |
| viewer | `page` | `page`: integer from 1 | the page at the reading line |
| viewer | `close` | none | the reader pressed Close or Escape |
| parent | `page` | `page`: integer from 1 | go to this page |

A message is taken only when its origin is the other side's exact origin, its source is the other
side's window (`window.parent` in the viewer, the frame's `contentWindow` in the parent), and it
has exactly the fields above, this protocol, version 1 and this frame's instance. Anything else is
ignored without a reply. Both sides post to the exact origin of the other, never `*`.

- `web/src/embed/viewer.ts` is the viewer's side. It reads `parent` and `instance` from its URL,
  and also requires `location.ancestorOrigins[0]` to be `parent` where the browser has it.
- `web/src/embed/parent.ts` is the parent's side, for the loader: `connectViewer({frame, src,
  instance, onReady, onTitle, onPage, onClose})` returns `goToPage` and `disconnect`. With
  `embedSrc` and `newInstanceId` from the protocol module it is everything the loader needs, and
  it imports nothing else.

## The loader

`web/src/embed/loader.ts` is the one script a site adds to its pages. It has no dependencies, and
the bundle is about 6 KB (`web/src/embed/loader-build.test.ts` holds it under 8 KB).

```html
<script src="https://<reflow origin>/embed/loader.js" data-site="KEY" defer></script>
```

`data-site` is the site key of the resolver's index (`docs/resolver.md`). The script reads which
origin it came from (`document.currentScript.src`) and talks only to that origin: the resolver,
and the viewer in the frame. A script with no `data-site` or a bad one, or on a page whose origin
cannot be named (`file:`, a sandboxed frame), does nothing, and every link stays what it was.

- **What the page's CSP needs.** Three entries, all for reflow's origin: `script-src` for
  `/embed/loader.js`, `connect-src` for the resolver call (`GET /v1/resolve`), and `frame-src` for
  the viewer. (The first plan listed two. The resolver is a `fetch`, so a page with `connect-src` or
  `default-src` that does not name reflow blocks it, and then every link goes to its PDF.)
  **No `style-src` entry.** The loader never writes a `style` attribute, never adds a `<style>`
  element and never uses eval. It sizes the dialog and the frame through the CSSOM
  (`element.style.setProperty`) and tints the backdrop with a constructable stylesheet
  (`adoptedStyleSheets`), which a CSP does not govern. In a browser without constructable
  stylesheets the backdrop is the browser's own. The proof for the loader ran a page with
  `default-src 'none'; script-src <loader>; connect-src <reflow>; frame-src <reflow>`.
- **A link is a PDF link** when its URL path ends in `.pdf` (any case, the query and the fragment
  ignored), it has `type="application/pdf"`, or it carries `data-reflow` (any value but `off`; this
  is how a link such as `/download?id=3` is named, and the resolver decides). `data-reflow="off"`
  opts a link out, whatever else it has. `download` links, links that open anywhere but `_self`
  and links that are not http or https are left to the browser. Where a link opens is its own
  `target`, or else the page's `<base target>`, as the browser decides it. The rules are `pdfLink`
  in `web/src/embed/loader-rules.ts`.
- **Which link was clicked** is the innermost `<a>` with an href on the click's composed path
  (`clickedLink` in `loader.ts`). So an SVG `<a>`, a link inside an open shadow root and a link
  around a component's shadow content all count. A link inside a closed shadow root is out of
  reach, and it goes to its PDF as before.
- **One delegated click listener** on the document, so links added later work. It takes the main
  button with no modifier that the page has not already handled (`isPlainClick`). Ctrl, Cmd, Shift
  or Alt, a middle click and a right click are never intercepted. Enter on a focused link is a
  click event, so the keyboard works the same way.
- **On a click** the loader holds the navigation (`preventDefault`: the resolver answers later than
  the click) and asks `GET /v1/resolve?site=<key>&url=<the link's URL, without its fragment>` with
  no cookies and a 1.5 s limit. `ready` opens the viewer. Anything else, a timeout, an error, a
  status that is not 200 or an answer that fails the check below, ends in
  `location.assign(link.href)`: the page goes to the PDF the link names. A second click while the
  first waits is held too and does nothing more.
- **The resolver's answer is untrusted.** `readyDocument` takes `{status: "ready", viewer}` only
  when `viewer` is `/embed/<id>?site=<this site>` on the origin the script came from, with no
  other parameter, fragment or credential, and `<id>` is `[A-Za-z0-9_-]{1,128}`. The loader keeps
  the document id and builds the frame's URL itself (`viewerUrl`, then `embedSrc` adding `parent`
  and `instance`). Nothing else in the answer is read. An answer that names another origin, scheme,
  port or path is a click that goes to the PDF.
- **The overlay** is a modal `<dialog>` that holds the viewer's `<iframe>`:
  - it is named from the viewer's `title` message (until then "Accessible document viewer"), and so
    is the frame ("<title>: accessible version");
  - Escape closes it: natively when focus is on the dialog, and through the viewer's `close`
    message when focus is inside the frame. The viewer's Close button sends the same message. The
    loader moves focus into the frame once the viewer says `ready`, so its shortcuts work, and
    holds it on the dialog until then. A frame that has not loaded yet has no Escape handler, so
    in that moment Escape may do nothing: Back always works;
  - it adds one history entry (`history.pushState`, keeping any state the page had, under the
    key `reflowEmbed`), so Back closes it. Every other way to close pops that entry first, so the
    history is as it was. A page's own `popstate` handler sees these two events;
  - focus returns to the link, the page's scroll is restored, and the dialog and the frame are
    removed;
  - if the viewer has not said `ready` in 10 s (a frame the browser refused, a dead network), the
    overlay closes the way Back would, so its history entry goes first, and then the page goes to
    the PDF, once;
  - in a browser with no modal `<dialog>` the click goes straight to the PDF. Nothing is added to
    the history and the scroll is not locked first.
- **The link is never changed.** Its `href`, its accessible name and its attributes are not
  touched, so without the script, or when it fails, every link works as it did.

### Building and serving

`bun run loader:build` (`web/scripts/build-loader.ts`) bundles `loader.ts` with `parent.ts` and
`protocol.ts` (`Bun.build`, minified, one IIFE that defines no global) into
`web/public/embed/loader.js`. The file is generated and not committed, like `public/docs`; the
`predev` and `prebuild` scripts run the build, so `bun run dev` and `bun run build` always serve
the current source. After a change to the loader under a running dev server, run
`bun run loader:build`.

The app serves it at `/embed/loader.js` (`LOADER_PATH`). `src/proxy.ts` lets that path through
(every other `/embed/<x>` is a document id), and `next.config.ts` gives it `Cache-Control: public,
max-age=3600`, `Access-Control-Allow-Origin: *` (for a page that includes it with `crossorigin`)
and the usual `nosniff`. The path is not versioned and the file changes with releases, so a page
should not pin it with `integrity=`.

## Trying it

To see the loader work on a page that is not reflow's, `scripts/dev start` runs one server that
is both: the app at `http://localhost:<port>`, and a small customer page at
`http://127.0.0.1:<port>/demo-site`. `localhost` and `127.0.0.1` are different origins, so the
page loads the loader, calls the resolver and frames the viewer across origins, as a real site
does, with no second process. `scripts/dev status` prints both addresses. The app's `/demo` tab
explains the snippet and links to the customer page. (It links out: the app's CSP keeps it from
framing another origin, and the viewer may be framed only by the origins its site lists.)

- **The customer page** (`web/demo-site/index.html`, served by `src/demo-site/server.ts` through
  the route `src/app/demo-site/[[...path]]/route.ts`) has the snippet with `data-site="demo"`,
  links to `/demo-site/files/fw9.pdf` and `/demo-site/files/accident-insurance-flyer.pdf`, and one
  link the site did not register (`/demo-site/files/resnet.pdf`), whose click goes to the PDF. It
  serves those files from `public/pdfs`, nothing else, and its CSP is the one above:
  `default-src 'none'` with reflow's origin for `script-src`, `connect-src` and `frame-src`
  (`next.config.ts` leaves `/demo-site` out of the app's own headers). A request that reaches the
  route on any host but the demo origin is redirected there. The demo needs those documents
  converted (`bun run docs:build fw9 az-accident-insurance-flyer`) and `resnet.pdf` in `public/pdfs`.
- **The `demo` site** is built in (`src/lib/resolver/demo.ts`, see `docs/resolver.md`): the
  resolver, the CORS answer and `frame-ancestors` are the real ones, for the origin that
  `scripts/dev` gives the server as `REFLOW_DEMO_ORIGIN`. Without that variable there is no
  `demo` site and the route is a 404. A `demo` site in `resolver.json` replaces it.
- **Run it by hand** with
  `REFLOW_DEMO_ORIGIN=http://127.0.0.1:3200 REFLOW_PUBLIC_ORIGIN=http://localhost:3200 bun run dev -p 3200`.
  The two origins must be spelled exactly as the browser reaches them. On a deployment, give the
  demo origin a second hostname that reaches the same server.
- **The public origin.** The resolver names the viewer on the origin the request was made to
  (`docs/resolver.md`, "The public origin"), so the loader accepts the answer whichever name you
  reached the app by. Behind a proxy that rewrites `Host` or terminates TLS, set
  `REFLOW_PUBLIC_ORIGIN`.

## The raw published files

A document's own pages (`/docs/<slug>/index.html`, `/docs/<slug>/chunks/*.html` and any SVG) are
the converter's HTML of an untrusted PDF. They are served with the app's CSP plus

```
sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox allow-downloads allow-modals
```

so they run in an opaque origin, with no access to reflow's storage and no same-origin access to
its APIs. The tokens keep the standalone viewer working: its script (chunks load in place), links
that open a new tab, saving the PDF, printing. There is no `allow-same-origin` (it would undo the
sandbox) and no `allow-forms` (the transcripts have no `<form>`). The chunk pages and `toc.json`
carry `Access-Control-Allow-Origin: *`, because the standalone viewer fetches them from its opaque
origin. The rule is in `web/next.config.ts`, and for `/api/files/docs/...` too; the app's pages
(`/docs/<slug>`, `/docs/<slug>/compare`) and the PDF are not sandboxed.

## Deployment

On one origin the code cannot keep the viewer away from the APIs that change state. A deployment
that frames the viewer on other sites must:

- Serve `/embed/*`, `/docs/*`, `/pdfs/*`, `/v1/resolve`, `/api/source/*` and `/_next/*` from a
  viewer origin that routes nothing else: no `/api/builds`, `/api/decisions`, `/api/ask`, `/builds`
  or `/review`, and no future dashboard. Those stay on another origin, or are off
  (`REFLOW_BUILDS` unset in production).
- Set no cookies on the viewer origin. Ideally serve the raw published files from a third,
  cookie-less origin, which makes the sandbox a second wall rather than the only one.
- Keep the `no-store` of `/embed` responses in any CDN in front of it, or key the cache by the
  full URL and accept that an edit of the index shows only after the cached copy expires.
- Not turn Ask on in the embed until a per-site budget exists.
- Serve `/embed/loader.js` from the viewer origin too: the loader talks to the origin it came from.
