import { demoOrigin } from "@/lib/resolver/demo";
import { demoResponse } from "@/demo-site/server";

// The demo customer page and its PDFs (src/demo-site/server.ts), served by this server so the loader
// can be tried with one process. The page belongs on the demo origin (REFLOW_DEMO_ORIGIN, another
// origin than the app's), so a request that reached it on any other host is sent there. Its headers
// are its own: next.config.ts leaves /demo-site out of the app's.

function answer(req: Request): Response {
  const demo = demoOrigin();
  const reflow = process.env.REFLOW_PUBLIC_ORIGIN;
  if (demo === null || !reflow) {
    return new Response("The demo customer page is off: start the server with scripts/dev start.", {
      status: 404, headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" },
    });
  }
  const url = new URL(req.url);
  // req.url names the server's own host; the Host header names the one the browser asked for.
  const host = req.headers.get("host");
  if (host === null || `${url.protocol}//${host}` !== demo) {
    return Response.redirect(`${demo}${url.pathname}`, 307);
  }
  return demoResponse(new Request(new URL(url.pathname, demo), { method: req.method }), { reflowOrigin: reflow });
}

export const GET = answer;
export const HEAD = answer;
