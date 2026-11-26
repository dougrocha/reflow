// Screenshots of a fixed set of documents, so a design change can be compared before and after.
//
//   bun run capture [--base URL] [--label NAME] [--allow-stale] [slug ...]
//   bun run capture compare BEFORE AFTER [--port N]
//
// capture: opens /view/<slug> of every document in DOCS on a running server (default
// http://localhost:$REFLOW_PORT, else :3417; --port picks another) and saves the transcript view at 1440x900 and at 390x844 (DPR 2), then
// clicks "Download transcript (HTML)", keeps the file and saves it rendered at the same two sizes.
// The folder is .captures/<label>/ in the web/ folder of the main checkout, shared by every
// worktree. The label defaults to the short commit, with "-dirty" when the checkout has changes.
//
// compare: puts two captures side by side (before | after) in .captures/compare/<before>-vs-<after>/.
// BEFORE and AFTER are labels of earlier captures or git refs. A ref with no capture yet is
// captured first, from a temporary worktree at that commit that runs its own dev server on --port
// (default 3417).
//
// Every capture reads the converted output in public/docs, which the commits share. A change to
// the Python renderer (its stylesheet included) shows only after `bun run docs:build --stale` of the
// set. So capture first checks this checkout's documents (what `bun run dev` here serves) and refuses
// (exit 2) while one is not current against the checkout (docs/provenance.md); --allow-stale
// captures anyway. capture.json records each document's status. A commit that compare captures from
// a temporary worktree shows the main checkout's documents, which no commit's check describes.
//
// Requires a Chromium binary: $CHROME_PATH, else /usr/bin/chromium, else Playwright's own.

import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import type { Page } from "playwright";
import { escapeHtml } from "../src/lib/format";
import { dataRoot, docsRoot, pdfsRoot } from "../src/lib/paths";
import { checkFreshness, formatVerdicts, type Verdict } from "./freshness";
import { launchChromium, TRANSCRIPT_READY } from "./lib/browser";
import { mainCheckout } from "./lib/checkout";
import { waitFor } from "./lib/net";

type Doc = {
  slug: string;
  covers: string;
};

const DOCS: Doc[] = [
  { slug: "az-accident-insurance-flyer", covers: "flyer: figures, footnote markers" },
  { slug: "da-complex-rfp-diagrams", covers: "headings, lists" },
  { slug: "da-assessment-appeal-application", covers: "form, fieldsets" },
  { slug: "da-financial-audit-comprehensive", covers: "tables, long Contents" },
  // Page 1 was read by OCR: it has the OCR note and words OCR is unsure of.
  { slug: "az-claim-form-instruction-booklet", covers: "scanned page with OCR marks" },
];

type Width = 1440 | 390;
const SIZES: { width: Width; height: number; scale: number }[] = [
  { width: 1440, height: 900, scale: 1 },
  { width: 390, height: 844, scale: 2 },
];
const VIEWS = ["view", "download"] as const;

const DEFAULT_PORT = 3417; // a server this script starts for `compare`
const TIMEOUT_MS = 120_000; // first dev-mode hits compile slowly
// Next's dev indicator would sit on top of the transcript in every screenshot.
const HIDE_DEV_OVERLAY = "nextjs-portal { display: none !important; }";

function fail(msg: string, code = 1): never {
  console.error(`capture: ${msg}`);
  process.exit(code);
}

function git(args: string[], cwd = process.cwd()): string {
  const r = spawnSync("git", args, { cwd, encoding: "utf8" });
  if (r.status !== 0) throw new Error(`git ${args.join(" ")}: ${r.stderr.trim()}`);
  return r.stdout.trim();
}

/** The main checkout: every worktree shares its captures. */
const MAIN = mainCheckout(resolve(import.meta.dir, "../.."));
const ROOT = process.env.REFLOW_CAPTURES ?? join(MAIN, "web", ".captures");
const WEB = resolve(import.meta.dir, "..");

function shotName(slug: string, view: (typeof VIEWS)[number], width: Width): string {
  return `${slug}--${view}-${width}.png`;
}

/** Wait until the page's fonts and the images in view have loaded, so a shot is not half drawn.
 *  An image that is not drawn (in a `hidden` figure) never loads lazily, so it is skipped. */
async function settle(page: Page) {
  await page.evaluate(async () => {
    await document.fonts.ready;
    const inView = [...document.images].filter((img) =>
      img.getClientRects().length > 0 && img.getBoundingClientRect().top < innerHeight);
    await Promise.all(inView.map((img) => (img.complete ? null : new Promise((r) => { img.onload = img.onerror = r; }))));
  });
  await page.waitForTimeout(300);
}

async function checkServer(base: string) {
  const res = await fetch(`${base}/`).catch(() => null);
  if (!res) fail(`nothing answers at ${base}. Start one with \`scripts/dev start\`, or pass --base.`, 2);
  const title = /<title>([^<]*)/.exec(await res.text())?.[1] ?? "";
  if (!res.ok || !/reflow/i.test(title)) fail(`${base} is not a reflow server (status ${res.status}, title "${title}")`, 2);
}

/** Every view of every document in `docs`, from the server at `base`, into ROOT/<label>/.
 *  `freshness`, when the documents were checked, goes into capture.json. */
async function capture(base: string, label: string, docs: Doc[], freshness?: Verdict[]): Promise<string> {
  await checkServer(base);
  const out = join(ROOT, label);
  rmSync(out, { recursive: true, force: true });
  mkdirSync(out, { recursive: true });
  const browser = await launchChromium();
  const errors: string[] = [];
  try {
    for (const doc of docs) {
      for (const size of SIZES) {
        const context = await browser.newContext({
          viewport: { width: size.width, height: size.height }, deviceScaleFactor: size.scale, acceptDownloads: true,
        });
        const page = await context.newPage();
        page.setDefaultTimeout(TIMEOUT_MS);
        try {
          await page.goto(`${base}/view/${doc.slug}`, { timeout: TIMEOUT_MS });
          await page.addStyleTag({ content: HIDE_DEV_OVERLAY });
          await page.locator(TRANSCRIPT_READY).first().waitFor();
          await settle(page);
          await page.screenshot({ path: join(out, shotName(doc.slug, "view", size.width)) });
          console.log(`  ${doc.slug} view ${size.width}`);
          // The download is the same file at any width; take it once, from the wide layout,
          // where the button is in the Contents sidebar rather than behind a menu.
          if (size.width === 1440) {
            const html = join(out, `${doc.slug}-transcript.html`);
            const download = page.waitForEvent("download");
            await page.getByRole("button", { name: "Download transcript (HTML)" }).click();
            await (await download).saveAs(html);
          }
        } catch (e) {
          errors.push(`${doc.slug} view ${size.width}: ${(e as Error).message.split("\n")[0]}`);
        } finally {
          await context.close();
        }
      }
      const html = join(out, `${doc.slug}-transcript.html`);
      if (!existsSync(html)) continue;
      for (const size of SIZES) {
        const context = await browser.newContext({
          viewport: { width: size.width, height: size.height }, deviceScaleFactor: size.scale,
        });
        const page = await context.newPage();
        try {
          // Its page images still load from the server, which is still up.
          await page.goto(`file://${html}`, { timeout: TIMEOUT_MS });
          await settle(page);
          await page.screenshot({ path: join(out, shotName(doc.slug, "download", size.width)) });
          console.log(`  ${doc.slug} download ${size.width}`);
        } catch (e) {
          errors.push(`${doc.slug} download ${size.width}: ${(e as Error).message.split("\n")[0]}`);
        } finally {
          await context.close();
        }
      }
    }
  } finally {
    await browser.close();
  }
  const documents = freshness?.map((v) => ({ slug: v.slug, status: v.status }));
  const meta = { label, commit: git(["rev-parse", "HEAD"], WEB), base, capturedAt: new Date().toISOString(), docs: docs.map((d) => d.slug), documents, errors };
  writeFileSync(join(out, "capture.json"), `${JSON.stringify(meta, null, 1)}\n`);
  for (const e of errors) console.error(`  error: ${e}`);
  console.log(`${errors.length ? "captured with errors" : "captured"}: ${out}`);
  if (errors.length) process.exitCode = 1;
  return out;
}

/** The short commit of the checkout, marked when it has changes the commit does not hold. */
function checkoutLabel(): string {
  const sha = git(["rev-parse", "--short", "HEAD"], WEB);
  return git(["status", "--porcelain"], WEB) ? `${sha}-dirty` : sha;
}

/**
 * Capture a commit from a temporary worktree: its own dependencies, the converted output of the
 * main checkout, and its own dev server, which is stopped and removed afterwards.
 */
async function captureCommit(sha: string, port: number): Promise<string> {
  const base = `http://localhost:${port}`;
  if (await fetch(`${base}/`).then(() => true, () => false)) fail(`port ${port} is taken; pass --port to capture ${sha}`, 2);
  const tree = join(tmpdir(), `reflow-capture-${sha}`);
  if (existsSync(tree)) { rmSync(tree, { recursive: true, force: true }); git(["worktree", "prune"], MAIN); }
  git(["worktree", "add", "--detach", tree, sha], MAIN);
  let server: ChildProcess | undefined;
  try {
    const web = join(tree, "web");
    mkdirSync(join(web, "public"), { recursive: true });
    for (const dir of ["docs", "pdfs"]) symlinkSync(join(MAIN, "web", "public", dir), join(web, "public", dir));
    console.log(`${sha}: installing dependencies in ${tree}`);
    const install = spawnSync("bun", ["install", "--frozen-lockfile"], { cwd: web, stdio: "inherit" });
    if (install.status !== 0) throw new Error(`bun install failed in ${web}`);
    console.log(`${sha}: starting a dev server on :${port}`);
    server = spawn("bun", ["run", "dev", "-p", String(port)], { cwd: web, detached: true, stdio: "ignore" });
    await waitFor(`${base}/`, { timeoutMs: TIMEOUT_MS, intervalMs: 1000, ok: (r) => r.ok, failed: () => (server?.exitCode != null ? `the dev server exited with ${server.exitCode}` : undefined) });
    return await capture(base, sha, DOCS);
  } finally {
    if (server?.pid) await stopGroup(server.pid);
    // The server may still have been writing .next when it stopped; delete what is left, then
    // let git forget the worktree.
    rmSync(tree, { recursive: true, force: true });
    git(["worktree", "prune"], MAIN);
  }
}

/** Stop the dev server and its children, which share its process group, and wait until they exit. */
async function stopGroup(pgid: number) {
  const alive = () => { try { process.kill(-pgid, 0); return true; } catch { return false; } };
  try { process.kill(-pgid, "SIGTERM"); } catch { return; }
  for (let i = 0; i < 100 && alive(); i++) await Bun.sleep(100);
  if (alive()) process.kill(-pgid, "SIGKILL");
}

/** The capture folder for a label or a git ref, capturing the commit when it has none yet. */
async function resolveCapture(name: string, port: number): Promise<{ label: string; dir: string }> {
  if (existsSync(join(ROOT, name, "capture.json"))) return { label: name, dir: join(ROOT, name) };
  let sha: string;
  try {
    sha = git(["rev-parse", "--short", "--verify", `${name}^{commit}`], WEB);
  } catch {
    fail(`"${name}" is neither a capture in ${ROOT} nor a git ref`, 64);
  }
  if (existsSync(join(ROOT, sha, "capture.json"))) return { label: sha, dir: join(ROOT, sha) };
  return { label: sha, dir: await captureCommit(sha, port) };
}

function dataUrl(path: string): string {
  return `data:image/png;base64,${readFileSync(path).toString("base64")}`;
}

/** One before | after image per view that either side has. */
async function compare(before: { label: string; dir: string }, after: { label: string; dir: string }) {
  const out = join(ROOT, "compare", `${before.label}-vs-${after.label}`);
  rmSync(out, { recursive: true, force: true });
  mkdirSync(out, { recursive: true });
  const browser = await launchChromium();
  const made: string[] = [];
  try {
    const page = await browser.newPage({ viewport: { width: 800, height: 600 } });
    for (const doc of DOCS) {
      for (const view of VIEWS) {
        for (const { width } of SIZES) {
          const name = shotName(doc.slug, view, width);
          const sides = [before, after].map((s) => ({ ...s, path: join(s.dir, name) }));
          if (!sides.some((s) => existsSync(s.path))) continue;
          const column = (s: (typeof sides)[number], role: string) => `<figure>
            <figcaption>${role}: ${escapeHtml(s.label)}</figcaption>
            ${existsSync(s.path) ? `<img src="${dataUrl(s.path)}" alt="">` : `<p class="none">no capture</p>`}
          </figure>`;
          await page.setContent(`<!doctype html><html><head><style>
            body { margin: 0; background: #d4d4d8; font: 600 22px/1.3 system-ui, sans-serif; }
            main { display: inline-flex; gap: 16px; padding: 16px; align-items: flex-start; }
            figure { margin: 0; background: #fff; }
            figcaption { padding: 8px 12px; background: #18181b; color: #fff; }
            img { display: block; }
            .none { width: ${width}px; padding: 48px 12px; margin: 0; color: #52525b; }
          </style></head><body><main>
            <h1 hidden>${escapeHtml(`${doc.slug} ${view} ${width}`)}</h1>
            ${column(sides[0], "before")}${column(sides[1], "after")}
          </main></body></html>`);
          await page.evaluate(() => Promise.all([...document.images].map((img) => img.decode())));
          const path = join(out, name);
          await page.locator("main").screenshot({ path });
          made.push(path);
        }
      }
    }
  } finally {
    await browser.close();
  }
  if (!made.length) fail(`neither ${before.dir} nor ${after.dir} holds a capture of the set`);
  console.log(`${made.length} before | after images: ${out}`);
}

/** This checkout's documents in `docs`: each one's status. Fails while one is not current, unless
 *  `allowStale`, which only warns. */
function checkDocuments(docs: Doc[], allowStale: boolean): Verdict[] {
  let verdicts: Verdict[];
  try {
    verdicts = checkFreshness(docs.map((d) => d.slug),
      { data: dataRoot(process.env, WEB), site: docsRoot(WEB), pdfs: pdfsRoot(WEB) });
  } catch (e) {
    const why = `the freshness check could not run: ${e instanceof Error ? e.message : String(e)}`;
    verdicts = docs.map((d) => ({ slug: d.slug, status: "unverifiable", reasons: [why] }));
  }
  const behind = verdicts.filter((v) => v.status !== "current");
  if (behind.length === 0) return verdicts;
  const rebuild = `bun run docs:build --stale ${behind.map((v) => v.slug).join(" ")}`;
  console.error(`${formatVerdicts(behind)}\n`);
  if (!allowStale) fail(`${behind.length} of the documents are not current here. Rebuild them with \`${rebuild}\`, or pass --allow-stale.`, 2);
  console.error(`capture: warning: capturing ${behind.length} documents that are not current here (${rebuild} would rebuild them)`);
  return verdicts;
}

function parse(argv: string[]) {
  const flags: Record<string, string> = {};
  const rest: string[] = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--allow-stale") {
      flags["allow-stale"] = "1";
    } else if (a === "--base" || a === "--label" || a === "--port") {
      if (!argv[i + 1]) fail(`${a} needs a value`, 64);
      flags[a.slice(2)] = argv[++i];
    } else if (a.startsWith("--")) {
      fail(`unknown option ${a}`, 64);
    } else {
      rest.push(a);
    }
  }
  return { flags, rest };
}

async function main() {
  const { flags, rest } = parse(process.argv.slice(2));
  const port = Number(flags.port ?? DEFAULT_PORT);
  if (!Number.isInteger(port) || port <= 0) fail(`bad --port ${flags.port}`, 64);
  if (rest[0] === "compare") {
    if (rest.length !== 3) fail("usage: bun run capture compare BEFORE AFTER [--port N]", 64);
    const before = await resolveCapture(rest[1], port);
    const after = await resolveCapture(rest[2], port);
    await compare(before, after);
  } else {
    const known = new Map(DOCS.map((d) => [d.slug, d]));
    const unknown = rest.filter((s) => !known.has(s));
    if (unknown.length) fail(`not in the capture set: ${unknown.join(", ")} (the set is ${[...known.keys()].join(", ")})`, 64);
    const docs = rest.length ? rest.map((s) => known.get(s)!) : DOCS;
    // The worktree's own dev server (scripts/dev, REFLOW_PORT) unless told otherwise.
    const base = (flags.base ?? `http://localhost:${flags.port ?? process.env.REFLOW_PORT ?? DEFAULT_PORT}`).replace(/\/$/, "");
    const freshness = checkDocuments(docs, flags["allow-stale"] === "1");
    await capture(base, flags.label ?? checkoutLabel(), docs, freshness);
  }
}

await main();

