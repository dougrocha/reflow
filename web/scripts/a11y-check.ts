// Automated WCAG 2.x A/AA check of the converted documents (SPEC §11, TODO Step 5).
//
//   bun run a11y [--base URL] [--json out.json] [slug ...]
//
// For each converted document (its public/docs/<slug>/manifest.json is served) it opens the
// transcript page /view/<slug> and the document page /docs/<slug> in headless Chromium, runs
// axe-core with the WCAG 2.0/2.1 A and AA rule tags, and reports the violations per document
// and per rule. The home page, /about and /demo are checked once each. Unconverted documents are
// listed as skipped.
//
// Budgets (SPEC §6, measured on each converted document), all counted as the compressed body
// bytes on the wire:
//   - the transcript HTML transfer: index.html shell + first chunk, excluding JS and CSS;
//   - the first load of /view/<slug>: the document, every script and stylesheet it loads, the
//     manifest and the first chunk (FIRST_LOAD_BUDGET_BYTES). Fonts, images and the favicon are
//     measured and listed but not counted: they are not needed for the first readable
//     content. Header links disable prefetching; the scan leaves network requests unblocked
//     so a prefetch regression counts towards the budget.
// The HTML transfer over its budget fails the run. The first-load budget is a SOFT target: a
// document over it, or over FIRST_LOAD_WARN_BYTES (early warning, 180 KB), prints a WARNING
// and the run still passes. The size depends on the document as well as the code, since a
// dense first page makes the first chunk bigger, so a document can creep up on (or past) the
// target without any code change. DOMContentLoaded is reported only.
//
// Encoding: "compressed" is whatever the server sends. Chromium and the HTML measure both
// offer gzip and br, but `next start` (what this runs against) compresses with gzip only, so
// the budgets model gzip. A CDN that serves brotli would come out smaller.
//
// Exit codes: 0 no serious or critical violations and the HTML transfer within budget (a first
// load over its soft target only warns); 1 otherwise
// (or when a page could not be scanned); 2 the base is not a reflow server; 64 bad arguments.
//
// Requires the dev or production server to be running at --base (default http://localhost:, else :3000)
// and a Chromium binary: $CHROME_PATH, else /usr/bin/chromium, else Playwright's own.

import { writeFileSync } from "node:fs";
import http from "node:http";
import type { Browser, BrowserContext, Page } from "playwright";
import AxeBuilder from "@axe-core/playwright";
import { SAMPLE_DOCS, type SampleDoc } from "../src/docs";
import { docBase } from "../src/lib/urls";
import { launchChromium, TRANSCRIPT_READY } from "./lib/browser";
import { kb } from "./lib/text";

const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];
const SEVERE = new Set(["serious", "critical"]);
const TRANSFER_BUDGET_BYTES = 200 * 1024; // SPEC §6: initial transfer < 200 KB
const FIRST_LOAD_BUDGET_BYTES = 200 * 1024; // soft target: over it only warns
const FIRST_LOAD_WARN_BYTES = 180 * 1024; // early warning before the soft target
const READY_TIMEOUT_MS = 120_000; // first dev-mode hits compile slowly
const NAV_TIMEOUT_MS = 120_000;

type Violation = {
  rule: string;
  impact: string | null | undefined;
  help: string;
  helpUrl: string;
  nodes: number;
  targets: string[];
};

type PageResult = {
  path: string;
  url: string;
  violations: Violation[];
  // axe rules that passed, and rules axe could not decide; shows the scan actually ran.
  passes: number;
  incomplete: number;
  error?: string;
};

// Body bytes of the responses a page loaded, by resource type ("document", "script", ...), and
// the part of them that counts towards the first-load budget.
type Transfer = {
  requests: number;
  totalBytes: number;
  byType: Record<string, number>;
  firstLoadBytes: number;
  firstLoadByType: Record<string, number>;
};

// Whether one response of /view/<slug> counts towards its first-load budget: the document, its
// scripts and stylesheets, and the document's own files (manifest.json and the first chunk,
// fetched from under its base). Not the fonts, images or favicon.
function countsTowardsFirstLoad(docRoot: string) {
  return (url: string, type: string): boolean => {
    if (type === "document" || type === "script" || type === "stylesheet") return true;
    return type === "fetch" && url.startsWith(docRoot);
  };
}

type DocResult = {
  slug: string;
  title: string;
  status: "checked" | "skipped";
  reason?: string;
  pages: PageResult[];
  budget?: {
    transferBytes: number;
    shellBytes: number;
    chunkBytes: number;
    chunkHref: string;
    withinBudget: boolean;
    domContentLoadedMs: number | null;
    // Everything the transcript page loaded (reported), and its first load (a soft target).
    totalTransferBytes: number | null;
    totalTransfer: Transfer | null;
    firstLoadBytes: number | null;
    firstLoadWithinBudget: boolean | null;
  };
};

type Args = { base: string; json?: string; slugs: string[] };

function parseArgs(argv: string[]): Args {
  let base = `http://localhost:${process.env.REFLOW_PORT ?? 3000}`;
  let json: string | undefined;
  const slugs: string[] = [];
  const known = new Set(SAMPLE_DOCS.map((d) => d.slug));
  const fail = (msg: string): never => {
    console.error(`a11y-check: ${msg}`);
    console.error("usage: bun run a11y [--base URL] [--json out.json] [slug ...]");
    process.exit(64);
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--base") {
      base = argv[++i] ?? fail("--base needs a URL");
    } else if (a.startsWith("--base=")) {
      base = a.slice("--base=".length);
    } else if (a === "--json") {
      json = argv[++i] ?? fail("--json needs a path");
    } else if (a.startsWith("--json=")) {
      json = a.slice("--json=".length);
    } else if (a.startsWith("--")) {
      fail(`unknown option ${a}`);
    } else if (known.has(a)) {
      slugs.push(a);
    } else {
      fail(`unknown slug "${a}" (known: ${[...known].join(", ")})`);
    }
  }
  return { base: base.replace(/\/+$/, ""), json, slugs };
}

// The app is a reflow server only if the home page is ours: the reflow title suffix and the
// link into the documents list. Another app on the same port must not be scanned.
async function preflight(base: string): Promise<string | null> {
  let html: string;
  try {
    const res = await fetch(`${base}/`);
    if (!res.ok) return `${base}/ answered HTTP ${res.status}`;
    html = await res.text();
  } catch (err) {
    return `nothing answered at ${base}/ (${(err as Error).message})`;
  }
  const titled = /<title>[^<]*· reflow<\/title>/.test(html);
  const linksDocs = html.includes('href="/docs/');
  if (!titled || !linksDocs) {
    return `${base}/ is not the reflow app (title or documents link missing)`;
  }
  return null;
}

async function manifestOf(base: string, doc: SampleDoc): Promise<{ title: string } | null> {
  const res = await fetch(`${base}${docBase(doc.slug)}manifest.json`);
  if (!res.ok) return null;
  const m = (await res.json()) as { title?: string };
  return { title: m.title ?? doc.title };
}

function toViolations(raw: Awaited<ReturnType<AxeBuilder["analyze"]>>["violations"]): Violation[] {
  return raw.map((v) => ({
    rule: v.id,
    impact: v.impact,
    help: v.help,
    helpUrl: v.helpUrl,
    nodes: v.nodes.length,
    targets: v.nodes.slice(0, 3).map((n) => n.target.join(" ")),
  }));
}

// Sizes every response the page loads, as sent on the wire (body bytes, as for the HTML budget).
// The returned function waits for the responses seen so far and gives their sum; a response
// still open at that point is not counted.
function trackTransfer(page: Page, firstLoad?: (url: string, type: string) => boolean): () => Promise<Transfer> {
  const pending: Promise<void>[] = [];
  const transfer: Transfer = { requests: 0, totalBytes: 0, byType: {}, firstLoadBytes: 0, firstLoadByType: {} };
  page.on("response", (res) => {
    pending.push(
      res.finished().then(async (err) => {
        if (err) return; // the request failed: no body was transferred
        const req = res.request();
        const { responseBodySize } = await req.sizes();
        const type = req.resourceType();
        transfer.requests++;
        transfer.totalBytes += responseBodySize;
        transfer.byType[type] = (transfer.byType[type] ?? 0) + responseBodySize;
        if (firstLoad?.(res.url(), type)) {
          transfer.firstLoadBytes += responseBodySize;
          transfer.firstLoadByType[type] = (transfer.firstLoadByType[type] ?? 0) + responseBodySize;
        }
      }).catch(() => {}),
    );
  });
  return async () => {
    await Promise.allSettled(pending);
    return transfer;
  };
}

async function scanPage(
  browser: Browser,
  base: string,
  path: string,
  waitFor?: string,
  firstLoad?: (url: string, type: string) => boolean,
): Promise<{ result: PageResult; dcl: number | null; transfer: Transfer | null }> {
  const url = `${base}${path}`;
  // A fresh context per page: no cache or storage carries over between documents.
  const context: BrowserContext = await browser.newContext();
  const page: Page = await context.newPage();
  const result: PageResult = { path, url, violations: [], passes: 0, incomplete: 0 };
  const snapshotTransfer = trackTransfer(page, firstLoad);
  let dcl: number | null = null;
  let transfer: Transfer | null = null;
  try {
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: NAV_TIMEOUT_MS });
    if (waitFor) await page.waitForSelector(waitFor, { timeout: READY_TIMEOUT_MS });
    await page.waitForLoadState("networkidle", { timeout: 15_000 }).catch(() => {});
    transfer = await snapshotTransfer();
    dcl = await page.evaluate(() => {
      const nav = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming | undefined;
      return nav ? nav.domContentLoadedEventEnd : null;
    });
    const axe = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze();
    result.violations = toViolations(axe.violations);
    result.passes = axe.passes.length;
    result.incomplete = axe.incomplete.length;
  } catch (err) {
    result.error = (err as Error).message.split("\n")[0];
  } finally {
    await context.close();
  }
  return { result, dcl, transfer };
}

// Bytes of the response body as sent on the wire (gzip if the server compresses), so the
// budget counts what a browser downloads. fetch() would decompress, so node:http is used.
function wireBytes(url: string): Promise<number> {
  return new Promise((resolve, reject) => {
    const req = http.get(url, { headers: { "Accept-Encoding": "gzip, br" } }, (res) => {
      if (res.statusCode !== 200) {
        res.resume();
        reject(new Error(`${url} answered HTTP ${res.statusCode}`));
        return;
      }
      let n = 0;
      res.on("data", (c: Buffer) => (n += c.length));
      res.on("end", () => resolve(n));
      res.on("error", reject);
    });
    req.on("error", reject);
  });
}

async function budgetOf(base: string, doc: SampleDoc) {
  const root = `${base}${docBase(doc.slug)}`;
  const shellBytes = await wireBytes(`${root}index.html`);
  const manifest = (await (await fetch(`${root}manifest.json`)).json()) as {
    chunks: { href: string }[];
  };
  const chunkHref = manifest.chunks[0]?.href;
  if (!chunkHref) throw new Error("manifest has no chunks");
  const chunkBytes = await wireBytes(new URL(chunkHref, root).toString());
  const transferBytes = shellBytes + chunkBytes;
  return { transferBytes, shellBytes, chunkBytes, chunkHref, withinBudget: transferBytes < TRANSFER_BUDGET_BYTES };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const problem = await preflight(args.base);
  if (problem) {
    console.error(`a11y-check: ${problem}. Start the reflow web app there, or pass --base.`);
    process.exit(2);
  }

  const browser = await launchChromium();

  const docs = args.slugs.length
    ? SAMPLE_DOCS.filter((d) => args.slugs.includes(d.slug))
    : SAMPLE_DOCS;
  const results: DocResult[] = [];
  const shared: PageResult[] = [];

  try {
    for (const path of ["/", "/about", "/demo"]) {
      const { result } = await scanPage(browser, args.base, path);
      shared.push(result);
      console.log(`${path}: ${result.error ? `ERROR ${result.error}` : `${result.violations.length} rule(s)`}`);
    }

    for (const doc of docs) {
      const manifest = await manifestOf(args.base, doc);
      if (!manifest) {
        results.push({ slug: doc.slug, title: doc.title, status: "skipped", reason: "not converted", pages: [] });
        console.log(`${doc.slug}: skipped (not converted)`);
        continue;
      }
      const pages: PageResult[] = [];
      const transcript = await scanPage(
        browser, args.base, `/view/${doc.slug}`, TRANSCRIPT_READY, countsTowardsFirstLoad(`${args.base}${docBase(doc.slug)}`),
      );
      pages.push(transcript.result);
      const docPage = await scanPage(browser, args.base, `/docs/${doc.slug}`);
      pages.push(docPage.result);
      const budget = await budgetOf(args.base, doc);
      const count = pages.reduce((n, p) => n + p.violations.length, 0);
      const passed = pages.map((p) => `${p.path} ${p.passes} passed/${p.incomplete} incomplete`).join("; ");
      const total = transcript.transfer;
      const firstLoadWithinBudget = total ? total.firstLoadBytes < FIRST_LOAD_BUDGET_BYTES : null;
      results.push({
        slug: doc.slug,
        title: manifest.title,
        status: "checked",
        pages,
        budget: {
          ...budget,
          domContentLoadedMs: transcript.dcl === null ? null : Math.round(transcript.dcl),
          totalTransferBytes: total?.totalBytes ?? null,
          totalTransfer: total,
          firstLoadBytes: total?.firstLoadBytes ?? null,
          firstLoadWithinBudget,
        },
      });
      console.log(
        `${doc.slug}: ${count} violation(s) across ${pages.length} page(s); ` +
          `html transfer ${kb(budget.transferBytes)}${budget.withinBudget ? "" : " OVER BUDGET"}; ` +
          `first load ${total ? kb(total.firstLoadBytes) : "n/a"}${firstLoadWithinBudget === false ? " OVER SOFT TARGET" : ""}; ` +
          `total transfer ${total ? kb(total.totalBytes) : "n/a"}; ` +
          `DCL ${transcript.dcl === null ? "n/a" : Math.round(transcript.dcl) + " ms"}` +
          (pages.some((p) => p.error) ? "; SCAN ERROR" : "") +
          `\n  axe: ${passed}`,
      );
    }
  } finally {
    await browser.close();
  }

  // Summaries.
  const allPages = [...shared, ...results.flatMap((r) => r.pages)];
  const byRule = new Map<string, { impact: string | null | undefined; help: string; nodes: number; docs: Set<string> }>();
  const byDoc = new Map<string, Record<string, number>>();
  const addViolation = (scope: string, v: Violation) => {
    const r = byRule.get(v.rule) ?? { impact: v.impact, help: v.help, nodes: 0, docs: new Set<string>() };
    r.nodes += v.nodes;
    r.docs.add(scope);
    byRule.set(v.rule, r);
    const d = byDoc.get(scope) ?? {};
    d[v.rule] = (d[v.rule] ?? 0) + 1;
    byDoc.set(scope, d);
  };
  for (const p of shared) for (const v of p.violations) addViolation(p.path, v);
  for (const r of results) for (const p of r.pages) for (const v of p.violations) addViolation(r.slug, v);

  const severe = allPages.flatMap((p) => p.violations.filter((v) => SEVERE.has(v.impact ?? "")));
  const scanErrors = allPages.filter((p) => p.error);
  const overBudget = results.filter((r) => r.budget && !r.budget.withinBudget);
  const overFirstLoad = results.filter((r) => r.budget?.firstLoadWithinBudget === false);

  console.log("\nViolations by rule (count = nodes; docs = pages/documents affected):");
  for (const [rule, r] of [...byRule.entries()].sort((a, b) => b[1].nodes - a[1].nodes)) {
    console.log(`  ${rule} [${r.impact ?? "?"}] ${r.nodes} node(s) in ${r.docs.size} scope(s): ${r.help}`);
  }
  if (byRule.size === 0) console.log("  none");

  console.log("\nViolations by document (rule count):");
  for (const [scope, d] of byDoc) {
    console.log(`  ${scope}: ${Object.entries(d).map(([k, n]) => `${k}=${n}`).join(", ")}`);
  }
  if (byDoc.size === 0) console.log("  none");

  const skipped = results.filter((r) => r.status === "skipped").map((r) => r.slug);
  console.log(`\nSkipped (not converted): ${skipped.length ? skipped.join(", ") : "none"}`);
  console.log(`Scan errors: ${scanErrors.length}`);
  for (const p of scanErrors) console.log(`  ${p.path}: ${p.error}`);
  console.log(`Serious or critical violations: ${severe.length}`);

  console.log("\nBudgets (SPEC §6; HTML transfer enforced; the viewer's first load is a soft target that only warns; DCL informational):");
  const byTypeText = (byType: Record<string, number>) =>
    Object.entries(byType)
      .sort((x, y) => y[1] - x[1])
      .map(([type, n]) => `${type} ${kb(n)}`)
      .join(", ") || "none";
  for (const r of results) {
    if (!r.budget) continue;
    const b = r.budget;
    console.log(
      `  ${r.slug}: shell ${kb(b.shellBytes)} + first chunk ${b.chunkHref} ${kb(b.chunkBytes)} = ${kb(b.transferBytes)} ` +
        `(budget ${kb(TRANSFER_BUDGET_BYTES)}, ${b.withinBudget ? "ok" : "OVER"}); DCL ${b.domContentLoadedMs ?? "n/a"} ms`,
    );
    if (b.totalTransfer) {
      const t = b.totalTransfer;
      console.log(
        `  ${r.slug}: first load ${kb(t.firstLoadBytes)} (${byTypeText(t.firstLoadByType)}) ` +
          `(soft target ${kb(FIRST_LOAD_BUDGET_BYTES)}, ${b.firstLoadWithinBudget ? "ok" : "OVER"})`,
      );
      if (b.firstLoadWithinBudget === false) {
        console.log(
          `  WARNING ${r.slug}: first load ${kb(t.firstLoadBytes)} is ${kb(t.firstLoadBytes - FIRST_LOAD_BUDGET_BYTES)} ` +
            `over the ${kb(FIRST_LOAD_BUDGET_BYTES)} soft target (not a failure).`,
        );
      } else if (t.firstLoadBytes > FIRST_LOAD_WARN_BYTES) {
        console.log(
          `  WARNING ${r.slug}: first load ${kb(t.firstLoadBytes)} is over ${kb(FIRST_LOAD_WARN_BYTES)}, ` +
            `within ${kb(FIRST_LOAD_BUDGET_BYTES - t.firstLoadBytes)} of the ${kb(FIRST_LOAD_BUDGET_BYTES)} soft target. ` +
            `A dense first page can push it over without a code change.`,
        );
      }
      console.log(
        `  ${r.slug}: everything loaded ${kb(t.totalBytes)} in ${t.requests} request(s) (${byTypeText(t.byType)})`,
      );
    } else {
      console.log(`  ${r.slug}: first load n/a (transcript page did not load)`);
    }
  }

  if (args.json) {
    const out = {
      base: args.base,
      generatedAt: new Date().toISOString(),
      tags: WCAG_TAGS,
      shared,
      documents: results,
      summary: {
        byRule: Object.fromEntries([...byRule].map(([k, r]) => [k, { impact: r.impact, nodes: r.nodes, scopes: [...r.docs] }])),
        byDocument: Object.fromEntries(byDoc),
        seriousOrCritical: severe.length,
        scanErrors: scanErrors.length,
        overBudget: overBudget.map((r) => r.slug),
        overFirstLoadBudget: overFirstLoad.map((r) => r.slug),
      },
    };
    writeFileSync(args.json, JSON.stringify(out, null, 2) + "\n");
    console.log(`\nJSON written to ${args.json}`);
  }

  const failed = severe.length > 0 || scanErrors.length > 0 || overBudget.length > 0;
  console.log(failed ? "\nFAIL" : "\nPASS");
  process.exit(failed ? 1 : 0);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
