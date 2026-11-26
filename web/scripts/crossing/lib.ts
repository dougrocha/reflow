// Helpers of the crossing check (crossing-mark.ts). Every reader throws HarnessError when it cannot
// read a stamp or a marker: a value it cannot read is never taken as 0 or as current.
//
// Two of them depend on text that the product and Next own, and are the likeliest to break on an
// upgrade: the page stamp is read from the escaped JSON of the RSC payload RenderedFrom writes into the
// HTML (parsePage), and watch() tells a refresh by Next's `rsc` header, the "refetch" marker in
// `next-router-state-tree` and `x-nextjs-cache`.
import { closeSync, constants, existsSync, openSync, readFileSync, renameSync, rmSync, writeSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { auditPath } from "../../src/lib/published";
import { buildsDir, publicationsFile } from "../../src/lib/paths";
import type { Page } from "playwright";
import type { Scratch } from "./scratch";
import { HarnessError, type Result } from "./verdict";
import { sleep } from "../lib/net";

const t0 = Date.now();
export const at = () => Date.now() - t0;
/** The number in the fixture's audit title, "crossing fixture audit n": which publication a page shows. */
const AUDIT = /crossing fixture audit (\d+)/;

/** The document's publication generation, from the build record. */
export function generation(s: Scratch, slug = "fw9"): number {
  const g = (() => {
    try {
      return JSON.parse(readFileSync(publicationsFile(buildsDir(s.data)), "utf8"))[slug]?.generation;
    } catch {
      return undefined;
    }
  })();
  if (!Number.isInteger(g)) throw new HarnessError(`could not read the generation of ${slug} from the build record`);
  return g;
}

/** The audit number the files on disk hold now: what a page read now would show. */
export function truth(s: Scratch, slug = "fw9"): number {
  try {
    const n = JSON.parse(readFileSync(auditPath(s.data, slug), "utf8")).findings[0].title.match(AUDIT)?.[1];
    if (n !== undefined) return Number(n);
  } catch { /* below */ }
  throw new HarnessError(`could not read the fixture audit number of ${slug} from the data root`);
}

/** The stamp and audit number of a served document page (its HTML). */
export function parsePage(html: string, slug = "fw9"): { stamp: number; audit: number } {
  const stamp = html.match(new RegExp(`generations\\\\":\\{[^}]*\\\\"${slug}\\\\":(\\d+)`))?.[1];
  if (stamp === undefined) throw new HarnessError(`could not read the generations stamp of ${slug} from the page's HTML`);
  const audit = html.match(AUDIT)?.[1];
  if (audit === undefined) throw new HarnessError(`could not read the fixture audit number from the page's HTML`);
  return { stamp: Number(stamp), audit: Number(audit) };
}

/** The body of GET `url`, read within `ms`: a request that hangs (on a held read, say) is no experiment. */
export async function get(url: string, ms = 10_000): Promise<string> {
  try {
    return await (await fetch(url, { signal: AbortSignal.timeout(ms) })).text();
  } catch (e) {
    throw new HarnessError(`GET ${url} did not answer within ${ms / 1000} s: ${String(e)}`);
  }
}

export async function load(base: string, slug = "fw9") {
  return parsePage(await get(`${base}/docs/${slug}`), slug);
}

/** The audit number the tab shows (#audit), or null while it has none to read (a reload, say). */
export function domContent(page: Page): Promise<number | null> {
  return page.evaluate(() => {
    const n = (document.querySelector("#audit") as HTMLElement | null)?.innerText.match(/crossing fixture audit (\d+)/)?.[1];
    return n === undefined ? null : Number(n);
  });
}

/** One poll, as a tab makes it, without a report: what each document has published. */
export const poll = async (base: string) => JSON.parse(await get(`${base}/api/builds`)).published as Record<string, number>;

/** Loads the page until its stamp is the published generation (the poll marks the stale data each
 *  time). Whether it got there within `ms`. */
export async function settle(base: string, slug = "fw9", ms = 20_000): Promise<boolean> {
  const start = at();
  while (at() - start < ms) {
    const published = (await poll(base))[slug] ?? 0;
    if ((await load(base, slug)).stamp === published) return true;
    await sleep(100);
  }
  return false;
}

type Refresh = { t: number; status: number; cache: string; stamps: Record<string, number> | null };

/** Everything the tab asks of the server that matters here: the polls (what they answered, and their
 *  query), the router.refresh() calls (RSC requests whose router state tree carries Next's "refetch"
 *  marker: status, cache header, and the generations the refreshed page was rendered from, null when
 *  they could not be read), and full page loads after the first (a reload: Next falls back to one when
 *  a refresh fails). */
export function watch(page: Page) {
  const w = {
    loads: 0, polls: [] as { t: number; query: string; published: Record<string, number> }[], refreshes: [] as Refresh[],
    get reloads() { return Math.max(0, this.loads - 1); },
  };
  page.on("load", () => void w.loads++);
  page.on("response", async (r) => {
    const url = new URL(r.url());
    const h = r.request().headers();
    try {
      if (url.pathname === "/api/builds" && r.request().method() === "GET") {
        w.polls.push({ t: at(), query: decodeURIComponent(url.search), published: (await r.json()).published });
      } else if (h.rsc === "1" && !h["next-router-prefetch"] && decodeURIComponent(h["next-router-state-tree"] ?? "").includes('"refetch"')) {
        const body = r.status() === 200 ? await r.text() : "";
        const m = body.match(/"generations":(\{[^}]*\})/)?.[1];
        w.refreshes.push({ t: at(), status: r.status(), cache: r.headers()["x-nextjs-cache"] ?? "-", stamps: m ? JSON.parse(m) : null });
      }
    } catch { /* aborted */ }
  });
  return w;
}

/** Waits until the tab shows audit `want`; the time it took, or null after `ms`. */
export async function until(page: Page, want: number, ms: number): Promise<number | null> {
  const start = at();
  while (at() - start < ms) {
    // A reload in between destroys the page's context: read again.
    if ((await domContent(page).catch(() => null)) === want) return at() - start;
    await sleep(100);
  }
  return null;
}

/** Arms the gate: the next read of the manifest is held until release(). */
export function stall(fifo: string): void {
  rmSync(fifo, { force: true });
  rmSync(`${fifo}.ready`, { force: true });
  execFileSync("mkfifo", [fifo]);
}

/** Lets the held reader go: the FIFO is moved away first (later readers do not wait), then opened
 *  read-write (never blocks), written and closed, which ends every read of it. */
export function release(fifo: string): void {
  rmSync(`${fifo}.ready`, { force: true });
  if (!existsSync(fifo)) return;
  const moved = `${fifo}.out`;
  renameSync(fifo, moved);
  const fd = openSync(moved, constants.O_RDWR | constants.O_NONBLOCK);
  writeSync(fd, "x");
  closeSync(fd);
  rmSync(moved, { force: true });
}

export function check(results: Result[], id: string, kind: Result["kind"], ok: boolean, what: string, print: (line: string) => void): boolean {
  print(`${ok ? "PASS" : "FAIL"}  ${id} ${what}`);
  results.push({ id, kind, ok, what });
  return ok;
}
