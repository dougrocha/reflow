import { exactOrigin } from "@/embed/protocol";

// The origin this server is reached at, as a visitor's browser (and a customer's loader) names it.
// The resolver builds the viewer URL from it, and the loader takes an answer only when that
// URL's origin is the origin its own script came from (src/embed/loader-rules.ts). So it has to
// be the origin the request was made to, not whatever name the server believes it has: Next gives
// `req.url` the host it listens as (`localhost`), so with the app reached at `127.0.0.1:<port>`
// the answer named `localhost:<port>` and every click fell back to the PDF.
//
// One owner, in this order:
//  1. REFLOW_PUBLIC_ORIGIN, when it is an exact origin: the deployment says what its public
//     origin is. Set it behind a proxy or CDN that terminates TLS or rewrites Host.
//  2. The `Host` header of the request, with the scheme of `req.url`, when Host is a plain host
//     name or IP address with an optional port.
//  3. `req.url`'s own origin.
//
// Trust: Host is whatever the client sent. That is safe here because the origin only shapes the
// answer to the client that sent it (a loader refuses an answer for any origin but its script's),
// and a shared cache must not mix hosts (resolve.ts varies the answer by Host). It is not safe
// to use for anything a third party reads, such as a link in an e-mail: that is what
// REFLOW_PUBLIC_ORIGIN is for.

const HOST = /^(?:[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?|\[[0-9a-f:.]+\])(?::\d{1,5})?$/i;

let reported = false;

export function publicOrigin(req: Request, env: Record<string, string | undefined> = process.env): string {
  const configured = env.REFLOW_PUBLIC_ORIGIN;
  if (configured !== undefined && configured !== "") {
    const exact = exactOrigin(configured);
    if (exact !== null) return exact;
    if (!reported) {
      reported = true;
      console.error(`REFLOW_PUBLIC_ORIGIN=${JSON.stringify(configured)} is not an exact http(s) origin; using the request's`);
    }
  }
  const url = new URL(req.url);
  const host = req.headers.get("host");
  if (host !== null && HOST.test(host)) {
    try {
      const origin = new URL(`${url.protocol}//${host}`).origin;
      if (exactOrigin(origin) !== null) return origin;
    } catch {
      // Not a host: the request's own origin.
    }
  }
  return url.origin;
}
