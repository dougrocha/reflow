/**
 * Whether builds can be started from this server: a development server, or `REFLOW_BUILDS=1`.
 * Read on every request, never at build time (`next build` runs with NODE_ENV=production and the
 * flag is set when the server starts). When it is off the routes answer 404 and nothing renders.
 */
export function buildsEnabled(env: Record<string, string | undefined> = process.env): boolean {
  return env.NODE_ENV === "development" || env.REFLOW_BUILDS === "1";
}
