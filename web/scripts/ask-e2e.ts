// "Ask this document" end to end in a real Chromium, against a fake Ollama.
//
//   bun run ask:e2e [--base URL] [--fake-port N] [--out DIR] [--no-server]
//
// What it starts (and stops, by PID, only these):
//   - scripts/fake-ollama.ts on --fake-port (default 11999), a deterministic /api/chat;
//   - `next start` on the port of --base (default http://127.0.0.1:3417, never 3000) with
//     OLLAMA_HOST pointing at the fake. Needs `bun run build` first and the documents from
//     `bun run docs:build resnet`. With --no-server it uses a server you already started with
//     that OLLAMA_HOST.
// It runs the answer scenarios (an answer, show on page, show in transcript, the shortcuts, a
// declined question, a made-up quote, the model off, a two-page answer and a table passage, axe in
// both modes with forced colors and reduced motion, the heading "Show on page"), then the sidebar
// accordion's (Contents and Ask this document: keyboard and mouse, shortcuts on a collapsed section,
// the state kept over a reload, axe with both open, the 320 px sheet), each with a screenshot in
// --out (default /tmp/reflow-ask-e2e/<time>) and DOM assertions, prints PASS or FAIL for each,
// and exits 1 if any failed. Chromium: $CHROME_PATH, else /usr/bin/chromium, else Playwright's own.

import { mkdirSync, readFileSync } from "node:fs";
import { gzipSync } from "node:zlib";
import { join, resolve } from "node:path";
import type { Browser, BrowserContext, Page } from "playwright";
import AxeBuilder from "@axe-core/playwright";
import { dataRoot } from "../src/lib/paths";
import { passagesPath, readSidecar } from "../src/lib/published";
import { boxPercent } from "../src/lib/source-region";
import { launchChromium, TRANSCRIPT_READY } from "./lib/browser";
import { waitFor } from "./lib/net";
import { kb, norm } from "./lib/text";
import type { AskResult } from "../src/lib/ask-contract";

const web = resolve(import.meta.dir, "..");
const data = resolve(dataRoot(process.env, web));

// ---- arguments ------------------------------------------------------------------------------

const argv = process.argv.slice(2);
const arg = (name: string, fallback: string): string => {
  const i = argv.indexOf(name);
  return i >= 0 ? (argv[i + 1] ?? fallback) : fallback;
};
const base = arg("--base", "http://127.0.0.1:3417").replace(/\/+$/, "");
const appPort = Number(new URL(base).port);
const fakePort = Number(arg("--fake-port", "11999"));
const out = resolve(arg("--out", `/tmp/reflow-ask-e2e/${new Date().toISOString().replace(/[:.]/g, "-")}`));
const ownServer = !argv.includes("--no-server");
if (appPort === 3000 || fakePort === 3000) {
  console.error("ask-e2e: port 3000 is the operator's dev server; pick another");
  process.exit(2);
}
mkdirSync(out, { recursive: true });

// ---- processes ------------------------------------------------------------------------------

const started: Bun.Subprocess[] = [];

function startFake(): Bun.Subprocess {
  const p = Bun.spawn([process.execPath, join(web, "scripts/fake-ollama.ts"), "--port", String(fakePort), "--delay", "500"], {
    stdout: Bun.file(join(out, "fake-ollama.log")),
    stderr: Bun.file(join(out, "fake-ollama.log")),
  });
  started.push(p);
  return p;
}

function stop(p: Bun.Subprocess): void {
  if (p.exitCode === null) p.kill();
}

// ---- tiny test harness ----------------------------------------------------------------------

type Outcome = { n: number | string; name: string; ok: boolean; notes: string[]; shots: string[] };
const outcomes: Outcome[] = [];
let current: Outcome;

async function scenario(n: number | string, name: string, body: () => Promise<void>): Promise<void> {
  current = { n, name, ok: true, notes: [], shots: [] };
  outcomes.push(current);
  try {
    await body();
  } catch (e) {
    current.ok = false;
    current.notes.push(`threw: ${e instanceof Error ? e.message : String(e)}`);
  }
  console.log(`${current.ok ? "PASS" : "FAIL"}  ${n}. ${name}`);
  for (const note of current.notes) console.log(`        ${note}`);
}

function check(cond: unknown, what: string): void {
  if (!cond) {
    current.ok = false;
    current.notes.push(`FAILED: ${what}`);
  }
}

function note(text: string): void {
  current.notes.push(text);
}

async function shot(page: Page, label: string): Promise<void> {
  // Lazy page images near the viewport load a moment after a scroll; a screenshot waits for them.
  await page.waitForFunction(
    () => [...document.querySelectorAll<HTMLImageElement>("[data-page] img")].every((img) => {
      const r = img.getBoundingClientRect();
      return r.bottom < 0 || r.top > innerHeight || img.complete;
    }),
    null, { timeout: 5_000 },
  ).catch(() => {});
  const file = join(out, `${String(current.n).padStart(2, "0")}-${label}.png`);
  await page.screenshot({ path: file });
  current.shots.push(file);
  note(`screenshot ${file}`);
}

// ---- helpers over the page ------------------------------------------------------------------

// The viewer's own polite status region: the one directly in the viewer, beside its "Document view"
// toolbar. A page may have other polite regions, each named for what it reports: with builds on, the
// build tracking's "Build status" (docs/builds.md). The viewer's announcements go to its own, so
// that is the one these scenarios count and read.
const STATUS = "div:has(> [role=toolbar][aria-label='Document view']) > [role=status][aria-live]";

/** Every polite status region on the page: the viewer's own or not, its name, and whether a modal
 *  hides it from assistive technology. */
const statusRegions = (page: Page) => page.evaluate((own) =>
  [...document.querySelectorAll("[role=status][aria-live]")].map((el) => ({
    own: el.matches(own), name: el.getAttribute("aria-label"), hidden: !!el.closest("[aria-hidden=true], [inert]"),
  })), STATUS);

/** Records every distinct text the viewer's status region takes, into window.__said. Checks first
 *  that the viewer has exactly one, that no modal hides it, and that any other region is named. */
async function recordStatus(page: Page): Promise<void> {
  const regions = await statusRegions(page);
  const own = regions.filter((r) => r.own);
  check(own.length === 1 && !own[0].hidden, `the viewer has one status region, not hidden (${JSON.stringify(regions)})`);
  check(regions.every((r) => r.own || !!r.name), `every other status region is named (${JSON.stringify(regions)})`);
  await page.evaluate((sel) => {
    const w = window as unknown as { __said: string[]; __saidOn?: boolean };
    w.__said = [];
    if (w.__saidOn) return;
    w.__saidOn = true;
    const read = () => (document.querySelector(sel)?.textContent ?? "").replace(/\u00a0/g, " ").trim();
    new MutationObserver(() => {
      const t = read();
      if (t && w.__said[w.__said.length - 1] !== t) w.__said.push(t);
    }).observe(document.body, { subtree: true, childList: true, characterData: true });
  }, STATUS);
}

const said = (page: Page): Promise<string[]> => page.evaluate(() => (window as unknown as { __said: string[] }).__said);
const lastSaid = async (page: Page): Promise<string> => (await said(page)).at(-1) ?? "";

const activeIs = (page: Page, sel: string): Promise<boolean> =>
  page.evaluate((s) => !!document.activeElement?.matches(s), sel);

// The sidebar's section headers. The page holds two copies of the sidebar (the column and the
// Contents sheet); only the one on screen is visible.
const askHeader = (page: Page) => page.locator("button[aria-controls$='-ask-panel']:visible");
const tocHeader = (page: Page) => page.locator("button[aria-controls$='-toc-panel']:visible");
const askForm = (page: Page) => page.locator("form[aria-label^='Ask a question']");
const expanded = async (header: ReturnType<typeof askHeader>): Promise<boolean> => (await header.getAttribute("aria-expanded")) === "true";

/** Opens the viewer. The view mode is remembered per browser profile and the sidebar's sections per
 *  tab: unless `keepState`, both start from their defaults (transcript; Contents open, Ask closed). */
async function openViewer(page: Page, slug = "resnet", keepState = false): Promise<void> {
  if (!keepState) {
    await page.goto(`${base}/about`, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => sessionStorage.clear());
  }
  await page.goto(`${base}/view/${slug}`, { waitUntil: "load" });
  const toggle = page.locator("button[aria-label='Accessible transcript']");
  await toggle.waitFor({ timeout: 60_000 });
  // The Ask header joins once the manifest is in (it is only in the sheet on a narrow screen).
  await page.locator("button[aria-controls$='-ask-panel']").first().waitFor({ state: "attached", timeout: 60_000 });
  await page.waitForTimeout(500);
  if ((await toggle.getAttribute("aria-pressed")) !== "true") await toggle.click();
  try {
    await page.waitForSelector(TRANSCRIPT_READY, { timeout: 20_000 });
  } catch (e) {
    const file = join(out, `stuck-${Date.now()}.png`);
    await page.screenshot({ path: file });
    const body = await page.evaluate(() => document.body.innerText.slice(0, 300));
    throw new Error(`the transcript did not show (${file}): ${body.replace(/\s+/g, " ")}; ${String(e)}`);
  }
  await recordStatus(page);
}

/** Alt+Shift+A: opens the Ask section if it is collapsed and focuses the box. */
async function openAsk(page: Page): Promise<void> {
  await page.keyboard.press("Alt+Shift+A");
  await page.waitForFunction(() => document.activeElement?.matches("form[aria-label^='Ask a question'] input"), null, { timeout: 30_000 });
}

/** Types a question in the open box and presses Enter; resolves with the route's parsed reply. */
async function askQuestion(page: Page, question: string): Promise<AskResult> {
  const input = page.locator("form[aria-label^='Ask a question'] input");
  await input.fill(question);
  const reply = page.waitForResponse((r) => r.url().includes("/api/ask/") && r.request().method() === "POST", { timeout: 60_000 });
  await page.keyboard.press("Enter");
  const res = await reply;
  const json = (await res.json()) as AskResult;
  // The panel or the message renders a moment after the response.
  await page.waitForFunction(
    () => !!document.querySelector("article[aria-labelledby]") || !!document.querySelector("form[aria-label^='Ask a question'] ~ p"),
    null, { timeout: 15_000 },
  );
  return json;
}

type Passage = { id: string; n: number; b: number[]; k: string; t: string };
const passageCache = new Map<string, Map<string, Passage>>();
function passageOf(slug: string, id: string): Passage | undefined {
  let m = passageCache.get(slug);
  if (!m) {
    m = new Map();
    const lines = readFileSync(passagesPath(data, slug), "utf8").split("\n").filter(Boolean);
    for (const line of lines.slice(1)) {
      const p = JSON.parse(line) as Passage;
      m.set(p.id, p);
    }
    passageCache.set(slug, m);
  }
  return m.get(id);
}

async function layoutNode(slug: string, page: number, node: string): Promise<{ bbox: number[]; w: number; h: number; role: string } | undefined> {
  const d = (await readSidecar(data, slug, page)) as { w: number; h: number; nodes: { id: string; bbox: number[]; role: string }[] } | null;
  const n = d?.nodes.find((x) => x.id === node);
  return d && n ? { bbox: n.bbox, w: d.w, h: d.h, role: n.role } : undefined;
}

type Overlay = { page: number; active: boolean; left: string; top: string; width: string; height: string };
const overlays = (page: Page): Promise<Overlay[]> =>
  page.evaluate(() =>
    [...document.querySelectorAll<HTMLElement>("[data-source-overlay]")].map((el) => ({
      page: Number(el.closest("[data-page]")?.getAttribute("data-page")),
      active: el.hasAttribute("data-active"),
      left: el.style.left, top: el.style.top, width: el.style.width, height: el.style.height,
    })),
  );

const near = (a: string, b: string): boolean => Math.abs(parseFloat(a) - parseFloat(b)) < 0.01;

// ---- console errors --------------------------------------------------------------------------

// Everything the browser logs as an error, or throws and nobody catches, on any page the run
// opens. The one thing expected: Chromium logs "Failed to load resource" for an HTTP error status,
// and a few scenarios make /api/ask/ answer with one on purpose (the model off: 503; an unusable
// reply: 502). The route's JSON for those is asserted by the scenarios themselves.
const unexpected: string[] = [];
let expectedErrors = 0;

function watch(ctx: BrowserContext): BrowserContext {
  ctx.on("page", (p) => {
    p.on("console", (m) => {
      if (m.type() !== "error") return;
      if (m.text().startsWith("Failed to load resource") && m.location().url.includes("/api/ask/")) { expectedErrors++; return; }
      unexpected.push(`${p.url()}: ${m.text()} (${m.location().url})`);
    });
    p.on("pageerror", (e) => unexpected.push(`${p.url()}: pageerror: ${e.message}`));
  });
  return ctx;
}
const tracked = watch;

// ---- the run --------------------------------------------------------------------------------

const QUESTION = "Why are deeper neural networks more difficult to train?";

async function main(): Promise<void> {
  let fake = startFake();
  if (ownServer) {
    const next = Bun.spawn([join(web, "node_modules/.bin/next"), "start", "-p", String(appPort), "-H", "127.0.0.1"], {
      cwd: web,
      env: { ...process.env, OLLAMA_HOST: `127.0.0.1:${fakePort}`, REFLOW_ASK_PROVIDER: "ollama", REFLOW_ASK_MODEL: "gemma4:e4b" },
      stdout: Bun.file(join(out, "next.log")),
      stderr: Bun.file(join(out, "next.log")),
    });
    started.push(next);
  }
  await waitFor(`http://127.0.0.1:${fakePort}/api/tags`);
  await waitFor(`${base}/view/resnet`);

  const browser: Browser = await launchChromium();
  const context: BrowserContext = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();

  let answer: Extract<AskResult, { kind: "answer" }> | undefined;

  // 1 -----------------------------------------------------------------------------------------
  await scenario(1, "Alt+Shift+A opens the Ask section and focuses the box; a question gives an answer with a verified quote", async () => {
    await openViewer(page);
    check((await page.locator(STATUS).count()) === 1, "the viewer has exactly one polite status region of its own");
    check((await askHeader(page).count()) === 1, "the Ask section's header is present (askReady)");
    check(!(await expanded(askHeader(page))), "the Ask section starts collapsed");
    await openAsk(page);
    check(await expanded(askHeader(page)), "Alt+Shift+A opened the Ask section");
    check(await activeIs(page, "form[aria-label^='Ask a question'] input"), "the box has focus after Alt+Shift+A");
    const result = await askQuestion(page, QUESTION);
    check(result.kind === "answer", `the route answered (got ${result.kind})`);
    if (result.kind !== "answer") return;
    answer = result;
    const heard = await said(page);
    note(`status said: ${JSON.stringify(heard.slice(-3))}`);
    check(heard.some((t) => t.startsWith("Searching the document")), "status said Searching first");
    check(/^Answer found on page \d+/.test(heard.at(-1) ?? ""), "status ends with 'Answer found on page N'");
    const panel = page.locator("article[aria-labelledby]");
    const quote = (await panel.locator("blockquote").first().innerText()).trim();
    const ev = result.evidence[0];
    check(norm(quote) === norm(ev.quote), "the panel's quote is the route's quote");
    const passage = passageOf("resnet", ev.node);
    check(!!passage && norm(passage.t).includes(norm(quote)), "the quote is a substring of the passage text in passages.jsonl");
    check(await activeIs(page, "article[aria-labelledby]"), "focus moved to the answer article");
    check((await panel.locator("p", { hasText: "Summary by gemma4:e4b" }).count()) === 1, "the model's answer is labelled 'Summary by gemma4:e4b'");
    await shot(page, "answer");
  });

  // 2 -----------------------------------------------------------------------------------------
  await scenario(2, "Show on page: original view, the overlay matches the layout box, focus on Back to answer", async () => {
    if (!answer) throw new Error("scenario 1 gave no answer");
    const ev = answer.evidence[0];
    await page.keyboard.press("Tab");
    check(await activeIs(page, "button[data-ask-show-page='0']"), "Tab from the answer reaches Show on page");
    await page.keyboard.press("Enter");
    await page.waitForSelector("[data-source-overlay]", { timeout: 15_000 });
    await page.waitForFunction(() => document.activeElement?.matches("[data-ask-back]"), null, { timeout: 5_000 }).catch(() => {});
    check((await page.locator("button[aria-label='Original pages']").getAttribute("aria-pressed")) === "true", "the view is Original pages");
    const list = await overlays(page);
    check(list.length === 1 && list[0].page === ev.page && list[0].active, `one active overlay on page ${ev.page} (got ${JSON.stringify(list)})`);
    const node = await layoutNode("resnet", ev.page, ev.node);
    check(!!node, `the layout sidecar of page ${ev.page} has node ${ev.node}`);
    if (node && list[0]) {
      const want = boxPercent(node.bbox, node.w, node.h);
      check(near(list[0].left, want.left) && near(list[0].top, want.top) && near(list[0].width, want.width) && near(list[0].height, want.height),
        `overlay ${JSON.stringify(list[0])} equals boxPercent of the sidecar box ${JSON.stringify(want)}`);
    }
    check(await activeIs(page, "[data-ask-back]"), "focus is on Back to answer");
    check((await lastSaid(page)).startsWith("Showing"), `status says Showing... (${await lastSaid(page)})`);
    await shot(page, "show-on-page");
    await page.keyboard.press("Enter");
    await page.waitForTimeout(400);
    check(await activeIs(page, "button[data-ask-show-page='0']"), "Enter on Back to answer returns focus to the panel's Show on page");
  });

  // 3 -----------------------------------------------------------------------------------------
  await scenario(3, "Show in transcript: mark equals the quote, the block has focus", async () => {
    if (!answer) throw new Error("scenario 1 gave no answer");
    const ev = answer.evidence[0];
    await page.locator("button[data-ask-show-transcript='0']").focus();
    await page.keyboard.press("Enter");
    await page.waitForSelector("mark[data-ask], [data-ask-block]", { timeout: 15_000 });
    await page.waitForTimeout(300);
    check((await page.locator("button[aria-label='Accessible transcript']").getAttribute("aria-pressed")) === "true", "the view is the transcript");
    const marks = await page.locator("mark[data-ask]").allInnerTexts();
    check(marks.length >= 1 && norm(marks.join(" ")) === norm(ev.quote), `mark text equals the quote (${JSON.stringify(marks).slice(0, 120)})`);
    const blockText = await page.evaluate((n) => document.querySelector(`[data-node="${CSS.escape(n)}"]`)?.textContent ?? "", ev.node);
    check(norm(blockText).includes(norm(ev.quote)), "the quote is a substring of the transcript text of [data-node]");
    const focusedNode = await page.evaluate(() => document.activeElement?.getAttribute("data-node") ?? null);
    check(focusedNode === ev.node, `the block has focus (${focusedNode})`);
    check((await lastSaid(page)).startsWith(`Highlighted on page ${ev.page}`), `status says Highlighted on page ${ev.page} (${await lastSaid(page)})`);
    await shot(page, "show-in-transcript");
  });

  // 4 -----------------------------------------------------------------------------------------
  await scenario(4, "Alt+Shift+R returns to the answer; Escape, Escape collapses the section and focuses its header", async () => {
    check(await activeIs(page, "[data-node]"), "focus starts inside the transcript");
    await page.keyboard.press("Alt+Shift+R");
    await page.waitForTimeout(300);
    check(await activeIs(page, "article[aria-labelledby]"), "Alt+Shift+R focuses the Answer region");
    await shot(page, "back-to-answer");
    await openAsk(page);
    await page.keyboard.press("Escape");
    check((await page.locator("form[aria-label^='Ask a question'] input").inputValue()) === "", "first Escape clears the box");
    check(await expanded(askHeader(page)), "the section is still open after the first Escape");
    await page.keyboard.press("Escape");
    await page.waitForTimeout(200);
    check(!(await askForm(page).isVisible()), "second Escape collapses the section");
    check(await activeIs(page, "button[aria-controls$='-ask-panel']"), "focus is on the Ask section's header");
    check(!(await expanded(askHeader(page))), "the header says collapsed");
    check(await expanded(tocHeader(page)), "the table of contents is not affected");
    await shot(page, "closed");
  });

  // 5 -----------------------------------------------------------------------------------------
  await scenario(5, "A declining model gives 'No answer found', no highlight, no error style", async () => {
    await openViewer(page);
    await openAsk(page);
    const result = await askQuestion(page, "What does the paper say about deeper networks? decline");
    check(result.kind === "none" && result.why === "not-stated", `route says none/not-stated (got ${JSON.stringify(result)})`);
    const msg = await page.locator("form[aria-label^='Ask a question'] ~ p").innerText();
    check(msg.startsWith("No answer found"), `message: ${msg}`);
    check((await page.locator("mark[data-ask]").count()) === 0, "no mark");
    check((await page.locator("[data-source-overlay]").count()) === 0, "no overlay");
    check((await page.locator("form[aria-label^='Ask a question'] ~ [role=alert], form[aria-label^='Ask a question'] ~ * [role=alert]").count()) === 0, "the message is not an alert");
    check((await page.locator("article[aria-labelledby]").count()) === 0, "no answer panel");
    check(await activeIs(page, "form[aria-label^='Ask a question'] input"), "focus stays in the box");
    const described = await page.locator("form[aria-label^='Ask a question'] input").getAttribute("aria-describedby");
    check(!!described && (await page.locator(`#${CSSescape(described)}`).innerText()) === msg, "the message is the input's description");
    await shot(page, "no-answer");
  });

  // 6 -----------------------------------------------------------------------------------------
  await scenario(6, "A fabricated quote, an unoffered label and a non-JSON reply never reach the reader as answers", async () => {
    await openViewer(page);
    await openAsk(page);
    const fabricated = await askQuestion(page, "What does the paper say about residual learning? fabricate");
    check(fabricated.kind === "none" && fabricated.why === "unverified", `fabricated: ${JSON.stringify(fabricated)}`);
    const msg = await page.locator("form[aria-label^='Ask a question'] ~ p").innerText();
    check(msg.startsWith("No answer found that could be checked"), `message: ${msg}`);
    check(!(await page.content()).includes("green cheese"), "the invented quote appears nowhere on the page");
    await shot(page, "fabricated");
    const badLabel = await askQuestion(page, "What does the paper say about shortcut connections? badlabel");
    check(badLabel.kind === "none" && badLabel.why === "unverified", `badlabel: ${JSON.stringify(badLabel)}`);
    const garbage = await askQuestion(page, "What does the paper say about identity mappings? garbage");
    check(garbage.kind === "error" && garbage.why === "unusable-reply", `garbage: ${JSON.stringify(garbage)}`);
    const gmsg = await page.locator("form[aria-label^='Ask a question'] ~ p").innerText();
    check(gmsg.includes("could not be used"), `garbage message: ${gmsg}`);
    check((await page.locator("article[aria-labelledby]").count()) === 0, "no panel after any of the three");
  });

  // 8 (before 7, which stops the fake) --------------------------------------------------------
  await scenario(8, "A two-evidence answer on two pages: both outlines, Next moves the active one and focus", async () => {
    await openViewer(page);
    await openAsk(page);
    const result = await askQuestion(page, "How do residual networks compare with plain networks on ImageNet? twopages");
    check(result.kind === "answer" && result.evidence.length === 2, `two evidence items (got ${JSON.stringify(result).slice(0, 200)})`);
    if (result.kind !== "answer" || result.evidence.length < 2) return;
    const [a, b] = result.evidence;
    check(a.page !== b.page, `on two pages (${a.page}, ${b.page})`);
    await page.locator("button[data-ask-show-page='0']").click();
    await page.waitForSelector("[data-source-overlay]", { timeout: 15_000 });
    await page.waitForTimeout(500);
    let list = await overlays(page);
    check(list.length === 2, `both overlays exist (got ${list.length})`);
    check(list.find((o) => o.active)?.page === a.page, `the first is active on page ${a.page}`);
    check((await page.locator("[data-ask-step=prev]").isDisabled()) && !(await page.locator("[data-ask-step=next]").isDisabled()), "Previous disabled, Next enabled");
    await shot(page, "first-of-two");
    await page.locator("[data-ask-step=next]").click();
    await page.waitForTimeout(500);
    list = await overlays(page);
    check(list.find((o) => o.active)?.page === b.page, `Next makes the page ${b.page} outline active`);
    check(await activeIs(page, "[data-ask-step]"), "focus stays on a step button");
    check((await lastSaid(page)).startsWith("Showing"), `status: ${await lastSaid(page)}`);
    await shot(page, "second-of-two");
  });

  // A table passage (part of scenario 8's coverage of the design: row groups have the TABLE's box).
  await scenario("8b", "A table passage: the quote is a row, the outline is the whole table", async () => {
    await openViewer(page);
    await openAsk(page);
    const result = await askQuestion(page, "What is the error of 34 layers plain compared with ResNet? table");
    check(result.kind === "answer", `answer (got ${result.kind})`);
    if (result.kind !== "answer") return;
    const ev = result.evidence[0];
    const node = await layoutNode("resnet", ev.page, ev.node);
    note(`evidence node ${ev.node} role ${node?.role ?? "?"}, quote ${JSON.stringify(ev.quote)}`);
    check(node?.role === "table", `the evidence node is a table in the layout (role ${node?.role})`);
    check(/\d/.test(ev.quote) && ev.quote.includes("|"), "the quote is a table row with cells");
    if (node) {
      const [x0, top, x1, bottom] = ev.region.bbox;
      const same = [x0, top, x1, bottom].every((v, i) => Math.abs(v - node.bbox[i]) < 0.02);
      check(same, `the region is the table's box ${JSON.stringify(node.bbox)} (got ${JSON.stringify(ev.region.bbox)})`);
    }
    await page.locator("button[data-ask-show-transcript='0']").click();
    await page.waitForSelector("mark[data-ask], [data-ask-block]", { timeout: 15_000 });
    const hasMark = (await page.locator("mark[data-ask]").count()) > 0;
    const hasBlock = (await page.locator("[data-ask-block]").count()) > 0;
    note(`transcript highlight: ${hasMark ? "mark" : hasBlock ? "block outline" : "none"}`);
    check(hasMark || hasBlock, "the table is highlighted in the transcript");
    await page.waitForTimeout(300);
    await shot(page, "table-transcript");
    await page.locator("button[data-ask-show-page='0']").click();
    await page.waitForSelector("[data-source-overlay]", { timeout: 15_000 });
    await page.waitForTimeout(400);
    await shot(page, "table-original");
  });

  // 9 -----------------------------------------------------------------------------------------
  await scenario(9, "axe with the panel open in both modes; forced colors; reduced motion", async () => {
    await openViewer(page);
    await openAsk(page);
    const result = await askQuestion(page, QUESTION);
    check(result.kind === "answer", "an answer to scan");
    const severe = async (label: string) => {
      const axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      const bad = axe.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
      note(`axe ${label}: ${axe.violations.length} violations, ${bad.length} serious or critical, ${axe.passes.length} rules passed`);
      for (const v of axe.violations) note(`  ${v.impact} ${v.id}: ${v.help} (${v.nodes.length} nodes: ${v.nodes.map((n) => n.target.join(" ")).slice(0, 3).join("; ")})`);
      check(bad.length === 0, `no serious or critical violations in ${label}`);
    };
    await severe("transcript with the answer panel");
    await shot(page, "axe-transcript");
    await page.locator("button[data-ask-show-page='0']").click();
    await page.waitForSelector("[data-source-overlay]", { timeout: 15_000 });
    await page.waitForTimeout(500);
    await severe("original pages with the outline");
    await shot(page, "axe-original");
    await page.locator("button[data-ask-show-transcript='0']").click();
    await page.waitForSelector("mark[data-ask], [data-ask-block]", { timeout: 15_000 });
    await severe("transcript with the highlighted quote");

    // Forced colors: the outline must stay visible and take a system color.
    const forced = await browser.newContext({ viewport: { width: 1280, height: 900 }, forcedColors: "active" });
    const fp = await forced.newPage();
    await openViewer(fp);
    await openAsk(fp);
    await askQuestion(fp, "Why do deeper neural networks have a degradation problem?");
    await fp.locator("button[data-ask-show-page='0']").click();
    await fp.waitForSelector("[data-source-overlay]", { timeout: 15_000 });
    await fp.waitForTimeout(500);
    const border = await fp.evaluate(() => {
      const el = document.querySelector<HTMLElement>("[data-source-overlay][data-active]");
      return el ? getComputedStyle(el).borderTopColor + " " + getComputedStyle(el).borderTopWidth : "";
    });
    note(`forced-colors outline border: ${border}`);
    check(!!border && !border.startsWith("rgb(17, 24, 39)") && !border.startsWith("rgba(0, 0, 0, 0)") && !border.endsWith(" 0px"), "the outline keeps a border in forced colors, in a system color");
    const file = join(out, "09-forced-colors.png");
    await fp.screenshot({ path: file });
    current.shots.push(file);
    note(`screenshot ${file}`);
    await fp.locator("button[data-ask-show-transcript='0']").click();
    await fp.waitForSelector("mark[data-ask], [data-ask-block]", { timeout: 15_000 });
    await fp.waitForTimeout(300);
    const markStyle = await fp.evaluate(() => {
      const el = document.querySelector<HTMLElement>("mark[data-ask], [data-ask-block]");
      if (!el) return "";
      const s = getComputedStyle(el);
      return `${s.backgroundColor} | ${s.color} | underline ${s.textDecorationLine} ${s.textDecorationThickness}`;
    });
    note(`forced-colors highlight: ${markStyle}`);
    const file2 = join(out, "09-forced-colors-transcript.png");
    await fp.screenshot({ path: file2 });
    current.shots.push(file2);
    await forced.close();

    // Reduced motion: the whole flow with no console errors.
    const reduced = await browser.newContext({ viewport: { width: 1280, height: 900 }, reducedMotion: "reduce" });
    const rp = await reduced.newPage();
    const rErrors: string[] = [];
    rp.on("console", (m) => { if (m.type() === "error") rErrors.push(m.text()); });
    rp.on("pageerror", (e) => rErrors.push(e.message));
    await openViewer(rp);
    await openAsk(rp);
    await askQuestion(rp, "How are the shortcut connections described in the residual learning framework?");
    await rp.locator("button[data-ask-show-page='0']").click();
    await rp.waitForSelector("[data-source-overlay]", { timeout: 15_000 });
    await rp.locator("[data-ask-back]").click();
    await rp.locator("button[data-ask-show-transcript='0']").click();
    await rp.waitForSelector("mark[data-ask], [data-ask-block]", { timeout: 15_000 });
    await rp.waitForTimeout(300);
    const file3 = join(out, "09-reduced-motion.png");
    await rp.screenshot({ path: file3 });
    current.shots.push(file3);
    note(`reduced-motion console errors: ${rErrors.length}${rErrors.length ? " " + JSON.stringify(rErrors) : ""}`);
    check(rErrors.length === 0, "no console errors with prefers-reduced-motion");
    await reduced.close();

    // A narrow screen: the Ask section is in the Contents sheet; nothing scrolls sideways.
    const narrow = await browser.newContext({ viewport: { width: 320, height: 700 } });
    const np = await narrow.newPage();
    await openViewer(np);
    await openAsk(np);
    await askQuestion(np, QUESTION);
    const overflow = await np.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    note(`320 px wide: horizontal overflow ${overflow}px`);
    check(overflow <= 0, "no horizontal scrolling at 320 px");
    const sheetOverflow = await np.evaluate(() => {
      const el = document.querySelector<HTMLElement>("[data-slot=sheet-content]");
      return el ? el.scrollWidth - el.clientWidth : 0;
    });
    note(`320 px wide: the sheet's horizontal overflow ${sheetOverflow}px`);
    check(sheetOverflow <= 0, "the sheet does not scroll sideways at 320 px");
    const file4 = join(out, "09-320px.png");
    await np.screenshot({ path: file4 });
    current.shots.push(file4);
    await narrow.close();
  });

  // 10 ----------------------------------------------------------------------------------------
  await scenario(10, "Regression: heading Show on page and Back to transcript; first-load cost", async () => {
    await openViewer(page);
    const button = page.locator("[data-show-source]").first();
    await button.scrollIntoViewIfNeeded();
    const text = await button.evaluate((el) => el.previousElementSibling?.textContent?.trim() ?? "");
    await button.click();
    await page.waitForSelector("[data-source-overlay]", { timeout: 15_000 });
    await page.waitForTimeout(500);
    check((await page.locator("button[aria-label='Original pages']").getAttribute("aria-pressed")) === "true", "Show on page switches to Original pages");
    check((await overlays(page)).length === 1, "one outline");
    check(await activeIs(page, "[data-source-back]"), "focus is on Back to transcript ([data-source-back])");
    check((await page.locator("[data-ask-back]").count()) === 0, "no ask bar for a heading");
    await shot(page, "heading-show-on-page");
    await page.locator("[data-source-back]").click();
    await page.waitForTimeout(600);
    check((await page.locator("button[aria-label='Accessible transcript']").getAttribute("aria-pressed")) === "true", "Back to transcript returns to the transcript");
    check((await overlays(page)).length === 0, "the outline is gone");
    const focused = await page.evaluate(() => document.activeElement?.tagName + ":" + (document.activeElement?.textContent ?? "").trim().slice(0, 60));
    note(`focus after Back: ${focused} (heading was "${text}")`);
    check(/^H\d:/.test(focused), "focus is on the heading");
    await shot(page, "heading-back");

    // First load: scripts and styles transferred (gzip) for /view/resnet, and the lazy Ask chunk.
    const fresh = await context.newPage();
    const scripts = new Set<string>();
    fresh.on("response", (r) => { if (r.request().resourceType() === "script") scripts.add(r.url()); });
    await fresh.goto(`${base}/about`, { waitUntil: "domcontentloaded" });
    await fresh.evaluate(() => sessionStorage.clear());
    await fresh.goto(`${base}/view/resnet`, { waitUntil: "load" });
    await fresh.waitForSelector(TRANSCRIPT_READY);
    await fresh.locator("button[aria-controls$='-ask-panel']:visible").waitFor({ timeout: 30_000 });
    await fresh.waitForTimeout(1500);
    const holdsAsk = async (urls: Iterable<string>): Promise<number> => {
      let found = 0;
      for (const url of urls) {
        const body = Buffer.from(await (await fetch(url, { headers: { "accept-encoding": "identity" } })).arrayBuffer());
        if (body.includes("Ask a question about this document")) found += gzipSync(body).length;
      }
      return found;
    };
    check((await holdsAsk(scripts)) === 0, "the Ask code is not part of the first load while the section is closed");
    const closedTotal = await (async () => {
      let t = 0;
      for (const url of scripts) t += gzipSync(Buffer.from(await (await fetch(url, { headers: { "accept-encoding": "identity" } })).arrayBuffer())).length;
      return t;
    })();
    await fresh.keyboard.press("Alt+Shift+A");
    await fresh.waitForSelector("form[aria-label^='Ask a question']");
    let total = 0;
    for (const url of scripts) {
      total += gzipSync(Buffer.from(await (await fetch(url, { headers: { "accept-encoding": "identity" } })).arrayBuffer())).length;
    }
    const askChunk = await holdsAsk(scripts);
    check(askChunk > 0, "opening the section loaded the Ask code");
    note(`scripts of /view/resnet with Ask closed: ${kb(closedTotal)} gzip; after opening Ask: ${kb(total)}; the Ask chunk is ${kb(askChunk)} gzip`);
    await fresh.close();
  });

  // 11 ----------------------------------------------------------------------------------------
  await scenario(11, "Submit keeps focus while asking; Escape cancels the question in flight", async () => {
    await openViewer(page);
    await openAsk(page);
    const input = page.locator("form[aria-label^='Ask a question'] input");
    const submit = page.locator("form[aria-label^='Ask a question'] button[type=submit]");
    await input.fill("What problem do deeper networks run into when they are trained?");
    await submit.focus();
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => (window as unknown as { __said: string[] }).__said.some((t) => t.startsWith("Searching")), null, { timeout: 5_000 });
    check(await activeIs(page, "form[aria-label^='Ask a question'] button[type=submit]"), "focus stays on Submit while the question is in flight");
    // Playwright's isDisabled() counts aria-disabled too, so read the property.
    check((await submit.getAttribute("aria-disabled")) === "true" && !(await submit.evaluate((b) => (b as HTMLButtonElement).disabled)), "Submit is aria-disabled, not disabled");
    await page.keyboard.press("Enter");
    check(await activeIs(page, "form[aria-label^='Ask a question'] button[type=submit]"), "pressing it again keeps focus and sends nothing new");
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => (window as unknown as { __said: string[] }).__said.at(-1) === "Question cancelled.", null, { timeout: 5_000 });
    check((await input.inputValue()).startsWith("What problem"), "the question stays in the box");
    check((await submit.getAttribute("aria-disabled")) === null, "Submit is active again");
    await page.waitForTimeout(900);
    check((await page.locator("article[aria-labelledby]").count()) === 0, "the cancelled question's answer never lands");
    check((await lastSaid(page)) === "Question cancelled.", `nothing was said after the cancel (${await lastSaid(page)})`);
    await page.keyboard.press("Escape");
    check((await input.inputValue()) === "", "the next Escape clears the box");
  });

  // 12 ----------------------------------------------------------------------------------------
  await scenario(12, "A find match over the quote: neither kind of mark removes the other", async () => {
    await openViewer(page);
    await openAsk(page);
    const result = await askQuestion(page, QUESTION);
    if (result.kind !== "answer") throw new Error(`no answer (${result.kind})`);
    const ev = result.evidence[0];
    const word = ev.quote.match(/[A-Za-z]{5,}/g)?.sort((a, b) => b.length - a.length)[0];
    if (!word) throw new Error("the quote has no word to find");
    const askText = async () => norm((await page.locator("mark[data-ask]").allInnerTexts()).join(" "));
    const findInside = (sel: string) => page.evaluate(
      ([outer, node]) => !!document.querySelector(`[data-node="${CSS.escape(node)}"] ${outer}`),
      [sel, ev.node] as const,
    );
    // Find first: its marks are under the quote's block when the match is in it.
    await page.keyboard.press("Alt+Shift+F");
    await page.keyboard.type(word);
    await page.keyboard.press("Enter");
    await page.waitForSelector("mark[data-find]", { timeout: 15_000 });
    const findInQuote = async () => page.evaluate((node) => !!document.querySelector("mark[data-find]")?.closest(`[data-node="${CSS.escape(node)}"]`), ev.node);
    for (let i = 0; i < 20 && !(await findInQuote()); i++) {
      await page.keyboard.press("Enter");
      await page.waitForTimeout(150);
    }
    check(await findInQuote(), `a find match for "${word}" is in the quote's block`);
    await page.locator("button[data-ask-show-transcript='0']").click();
    await page.waitForSelector("mark[data-ask]", { timeout: 15_000 });
    const quoteNorm = norm(ev.quote);
    check((await askText()) === quoteNorm, "the answer's marks cover the quote with a find mark under them");
    check(await findInside("mark[data-find]"), "the find mark is still in the block");
    // Step find on: its old mark is cleared, the answer's marks must stay.
    await page.keyboard.press("Alt+Shift+F");
    await page.keyboard.press("Enter");
    await page.waitForTimeout(400);
    check((await askText()) === quoteNorm, `stepping find leaves the answer's marks (${await askText()})`);
    // Step until the find mark sits inside the answer's mark, then clear the answer.
    for (let i = 0; i < 20 && !(await page.locator("mark[data-ask] mark[data-find]").count()); i++) {
      await page.keyboard.press("Enter");
      await page.waitForTimeout(150);
    }
    if (await page.locator("mark[data-ask] mark[data-find]").count()) {
      await page.locator("button", { hasText: "Clear answer" }).click();
      await page.waitForTimeout(300);
      check((await page.locator("mark[data-ask]").count()) === 0, "clearing the answer removes its marks");
      check((await page.locator("mark[data-find]").count()) > 0, "and leaves the find match highlighted");
    } else {
      note("no find match fell inside the answer's mark; the second half was skipped");
    }
    await shot(page, "marks-overlap");
  });

  // 13 ----------------------------------------------------------------------------------------
  await scenario(13, "Show in transcript when the passage's block is not on the page: says so and shows it on the page", async () => {
    await openViewer(page);
    await openAsk(page);
    const result = await askQuestion(page, QUESTION);
    if (result.kind !== "answer") throw new Error(`no answer (${result.kind})`);
    const ev = result.evidence[0];
    // As if the converted page and the loaded HTML disagreed.
    await page.evaluate((n) => document.querySelector(`[data-node="${CSS.escape(n)}"]`)?.removeAttribute("data-node"), ev.node);
    await page.locator("button[data-ask-show-transcript='0']").click();
    await page.waitForSelector("[data-source-overlay]", { timeout: 15_000 });
    await page.waitForTimeout(400);
    const heard = await lastSaid(page);
    check(heard.startsWith("Could not find the passage in the transcript."), `status says it could not find the passage (${heard})`);
    check(heard.includes(`on page ${ev.page}`), "and where it is shown instead");
    check((await overlays(page)).length >= 1, "the passage is outlined on the page image");
    await shot(page, "not-in-transcript");
  });

  // 14 ----------------------------------------------------------------------------------------
  await scenario(14, "The sidebar is an accordion: Table of contents, then Ask a question; the downloads stay outside", async () => {
    await openViewer(page);
    const headers = page.locator("aside[aria-label] h2 > button");
    check((await headers.count()) === 2, `two headings with a button in each (got ${await headers.count()})`);
    const names = (await headers.allInnerTexts()).map((t) => t.trim());
    check(names[0] === "Table of contents" && names[1] === "Ask a question", `in that order (got ${JSON.stringify(names)})`);
    for (const header of [tocHeader(page), askHeader(page)]) {
      const controls = await header.getAttribute("aria-controls");
      check(!!controls && (await page.evaluate((id) => !!document.getElementById(id), controls)), `aria-controls names an element (${controls})`);
      check((await header.getAttribute("aria-expanded")) !== null, "aria-expanded is set");
    }
    check(await expanded(tocHeader(page)), "the table of contents is open by default");
    check(!(await expanded(askHeader(page))), "Ask a question is closed by default");
    check(await page.locator("aside nav[aria-label='Document outline'] a").first().isVisible(), "the outline shows");
    const inView = await page.evaluate(() => {
      const b = document.querySelector("aside button[aria-controls$='-ask-panel']")?.getBoundingClientRect();
      return !!b && b.top >= 0 && b.bottom <= innerHeight;
    });
    check(inView, "the Ask header is in view without scrolling, whatever the length of the outline");
    check((await page.locator("aside [id$='-ask-panel']").getAttribute("hidden")) !== null, "the Ask panel is hidden");
    check((await askForm(page).count()) === 0, "nothing of Ask is loaded or drawn yet");
    check((await page.locator("[role=toolbar] button[aria-keyshortcuts='Alt+Shift+A']").count()) === 0, "the toolbar has no Ask button");
    const ys = [await tocHeader(page).boundingBox(), await askHeader(page).boundingBox()];
    check(!!ys[0] && !!ys[1] && ys[0].y < ys[1].y, "the table of contents is above Ask");
    const download = page.locator("aside button", { hasText: "Download transcript (HTML)" });
    check(await download.isVisible(), "the download button shows in the sidebar");
    check(await download.evaluate((el) => !el.closest("[id$='-toc-panel'], [id$='-ask-panel']")), "and is outside both accordion items");
    await tocHeader(page).click();
    check(await download.isVisible(), "the download button stays when the table of contents is collapsed");
    await tocHeader(page).click();
    await shot(page, "default");
  });

  // 15 ----------------------------------------------------------------------------------------
  await scenario(15, "Expand and collapse by keyboard and mouse; both items can be open", async () => {
    await openViewer(page);
    const ask = askHeader(page);
    const toc = tocHeader(page);
    await ask.focus();
    await page.keyboard.press("Enter");
    await askForm(page).waitFor({ state: "visible", timeout: 30_000 });
    check(await expanded(ask), "Enter on the header opens Ask");
    check(await activeIs(page, "button[aria-controls$='-ask-panel']"), "focus stays on the header");
    await page.keyboard.press("Space");
    check(!(await expanded(ask)) && !(await askForm(page).isVisible()), "Space closes it");
    await ask.click();
    check((await expanded(ask)) && (await askForm(page).isVisible()), "a click opens it again");
    check(await expanded(toc), "the table of contents is still open: both are open");
    await toc.click();
    check(!(await expanded(toc)) && !(await page.locator("aside nav a").first().isVisible()), "a click on the table of contents closes it");
    check((await expanded(ask)) && (await askForm(page).isVisible()), "Ask is unaffected");
    await toc.focus();
    await page.keyboard.press("Enter");
    check((await expanded(toc)) && (await page.locator("aside nav a").first().isVisible()), "Enter opens the table of contents");
    check(await expanded(ask), "both are open at once");
    await page.keyboard.press("Tab");
    check(await activeIs(page, "button[aria-controls$='-toc-panel'] ~ *, aside nav a"), "Tab goes from the header into its panel");
    await shot(page, "both-open");
  });

  // 16 ----------------------------------------------------------------------------------------
  await scenario(16, "Alt+Shift+A and Alt+Shift+R open a collapsed Ask item; an answer opens it too", async () => {
    await openViewer(page);
    await tocHeader(page).click();
    check(!(await expanded(askHeader(page))), "Ask starts collapsed");
    await openAsk(page);
    check(await expanded(askHeader(page)), "Alt+Shift+A opened Ask");
    check(!(await expanded(tocHeader(page))), "and left the table of contents as it was");
    check(await activeIs(page, "form[aria-label^='Ask a question'] input"), "and focused the box");
    const result = await askQuestion(page, QUESTION);
    check(result.kind === "answer", "an answer to return to");
    await askHeader(page).click();
    check(!(await askForm(page).isVisible()), "the header collapses Ask");
    await page.locator("[data-page] [data-page-label]").first().focus();
    await page.keyboard.press("Alt+Shift+R");
    await page.waitForFunction(() => document.activeElement?.matches("article[aria-labelledby]"), null, { timeout: 5_000 }).catch(() => {});
    check(await expanded(askHeader(page)), "Alt+Shift+R opened Ask");
    check(await activeIs(page, "article[aria-labelledby]"), "and focused the answer");
    await askHeader(page).click();
    await openAsk(page);
    check((await expanded(askHeader(page))) && (await activeIs(page, "form[aria-label^='Ask a question'] input")), "Alt+Shift+A on the collapsed item opens it and focuses the box");
    // A question answered while the item is collapsed opens it, and the answer takes focus.
    await page.locator("form[aria-label^='Ask a question'] input").fill("What problem do deeper networks run into when they are trained? once more");
    await page.keyboard.press("Enter");
    await askHeader(page).click();
    check(!(await askForm(page).isVisible()), "collapsed while the question is in flight");
    await page.waitForFunction(() => document.querySelector("button[aria-controls$='-ask-panel']")?.getAttribute("aria-expanded") === "true", null, { timeout: 15_000 }).catch(() => {});
    check(await expanded(askHeader(page)), "the answer opened the item");
    await page.waitForTimeout(300);
    check(await activeIs(page, "article[aria-labelledby]"), "and took focus");
    await shot(page, "answer-reopened");
  });

  // 17 ----------------------------------------------------------------------------------------
  await scenario(17, "Which items are open is remembered over a reload, for the tab", async () => {
    await openViewer(page);
    await askHeader(page).click();
    await tocHeader(page).click();
    await page.reload({ waitUntil: "load" });
    await page.locator("button[aria-controls$='-ask-panel']:visible").waitFor({ timeout: 60_000 });
    check(await expanded(askHeader(page)), "Ask is still open after the reload");
    check(!(await expanded(tocHeader(page))), "the table of contents is still closed");
    await askForm(page).waitFor({ state: "visible", timeout: 30_000 });
    check(!(await activeIs(page, "form[aria-label^='Ask a question'] input")), "the reload does not move focus into the box");
    note(`stored: ${await page.evaluate(() => sessionStorage.getItem("reflow:sidebar-sections"))}`);
    await shot(page, "after-reload");
    const other = await context.newPage();
    await other.goto(`${base}/view/resnet`, { waitUntil: "load" });
    await other.locator("button[aria-controls$='-ask-panel']:visible").waitFor({ timeout: 60_000 });
    check(!(await expanded(askHeader(other))) && (await expanded(tocHeader(other))), "a new tab starts from the defaults");
    await other.close();
    await page.reload({ waitUntil: "load" });
    await page.locator("button[aria-controls$='-ask-panel']:visible").waitFor({ timeout: 60_000 });
    await askHeader(page).click();
    await tocHeader(page).click();
  });

  // 18 ----------------------------------------------------------------------------------------
  await scenario(18, "axe with both items open (and with Ask closed), in both view modes", async () => {
    await openViewer(page);
    await openAsk(page);
    check((await expanded(askHeader(page))) && (await expanded(tocHeader(page))), "both items are open");
    const severe = async (label: string) => {
      const axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      const bad = axe.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
      note(`axe ${label}: ${axe.violations.length} violations, ${bad.length} serious or critical, ${axe.passes.length} rules passed`);
      for (const v of axe.violations) note(`  ${v.impact} ${v.id}: ${v.help} (${v.nodes.length} nodes: ${v.nodes.map((n) => n.target.join(" ")).slice(0, 3).join("; ")})`);
      check(bad.length === 0, `no serious or critical violations in ${label}`);
    };
    await severe("transcript, both items open");
    await page.locator("button[aria-label='Original pages']").click();
    await page.waitForSelector("[data-page] img", { timeout: 15_000 });
    await severe("original pages, both items open");
    await shot(page, "axe-original-both-open");
    await askHeader(page).click();
    await severe("original pages, Ask closed");
    await page.locator("button[aria-label='Accessible transcript']").click();
    await page.waitForSelector(TRANSCRIPT_READY, { timeout: 15_000 });
    await severe("transcript, Ask closed");
  });

  // 19 ----------------------------------------------------------------------------------------
  await scenario(19, "Layout: both items open, the Ask item and the answer stay in view", async () => {
    await openViewer(page);
    await openAsk(page);
    const result = await askQuestion(page, QUESTION);
    check(result.kind === "answer", "an answer to lay out");
    const m = await page.evaluate(() => {
      const aside = document.querySelector("aside")!;
      const rect = (el: Element | null) => { const r = el?.getBoundingClientRect(); return r ? { top: r.top, bottom: r.bottom } : null; };
      const toc = document.querySelector<HTMLElement>("aside [id$='-toc-panel']")!;
      return {
        vh: innerHeight,
        header: rect(document.querySelector("aside button[aria-controls$='-ask-panel']")),
        article: rect(aside.querySelector("article")),
        download: rect([...aside.querySelectorAll("button")].find((b) => b.textContent?.includes("Download transcript")) ?? null),
        tocMax: getComputedStyle(toc).maxHeight,
        tocClient: toc.clientHeight, tocScroll: toc.scrollHeight,
      };
    });
    note(`layout: ${JSON.stringify(m)}`);
    check(!!m.header && m.header.bottom <= m.vh, "the Ask header is in the window");
    check(!!m.article && m.article.top >= 0 && m.article.bottom <= m.vh, "the answer is in the window");
    check(!!m.download && m.download.bottom <= m.vh, "the download button is in the window");
    check(Math.abs(parseFloat(m.tocMax) - 0.45 * m.vh) < 2, `the outline is capped at 45vh while Ask is open (${m.tocMax})`);
    await shot(page, "layout");
  });

  // 20 ----------------------------------------------------------------------------------------
  await scenario(20, "320 px: the sheet shows both items; Ask works in it and its buttons leave it", async () => {
    const narrow = await browser.newContext({ viewport: { width: 320, height: 700 } });
    const np = await narrow.newPage();
    try {
      await openViewer(np);
      await recordStatus(np);
      check((await np.locator("aside[aria-label]").isVisible()) === false, "the column is not on screen");
      const trigger = np.locator("button[aria-label='Table of contents and ask a question']");
      await trigger.click();
      const dialog = np.locator("[role=dialog]");
      await dialog.waitFor({ state: "visible", timeout: 10_000 });
      const sheetHeaders = dialog.locator("h2 > button");
      const names = (await sheetHeaders.allInnerTexts()).map((t) => t.trim());
      check(JSON.stringify(names) === JSON.stringify(["Table of contents", "Ask a question"]), `both items are in the sheet (${JSON.stringify(names)})`);
      check(await dialog.getByRole("button", { name: /Download transcript/ }).isVisible(), "the download button is under them");
      await shot(np, "sheet-default");
      await np.locator("[role=dialog] button[aria-controls$='-ask-panel']").click();
      await askForm(np).waitFor({ state: "visible", timeout: 30_000 });
      await shot(np, "sheet-both-open");
      await np.locator("form[aria-label^='Ask a question'] input").fill(QUESTION);
      const reply = np.waitForResponse((r) => r.url().includes("/api/ask/"), { timeout: 60_000 });
      await np.keyboard.press("Enter");
      await reply;
      await np.waitForSelector("article[aria-labelledby]", { timeout: 15_000 });
      check(await activeIs(np, "article[aria-labelledby]"), "the answer takes focus in the sheet");
      const overflow = await np.evaluate(() => ({
        page: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        sheet: (() => { const el = document.querySelector<HTMLElement>("[data-slot=sheet-content]"); return el ? el.scrollWidth - el.clientWidth : 0; })(),
      }));
      check(overflow.page <= 0 && overflow.sheet <= 0, `no sideways scrolling (${JSON.stringify(overflow)})`);
      await shot(np, "sheet-answer");
      // Show on page: the sheet gives way to the page, and focus lands on Back to answer.
      await np.locator("button[data-ask-show-page='0']").click();
      await np.waitForSelector("[data-source-overlay]", { timeout: 15_000 });
      await np.waitForFunction(() => document.activeElement?.matches("[data-ask-back]"), null, { timeout: 5_000 }).catch(() => {});
      // The sheet fades out for 200 ms before it leaves the page.
      await dialog.waitFor({ state: "hidden", timeout: 3_000 }).catch(() => {});
      check((await dialog.count()) === 0 || !(await dialog.isVisible()), "Show on page closed the sheet");
      check(await activeIs(np, "[data-ask-back]"), "focus is on Back to answer");
      await shot(np, "sheet-show-on-page");
      // Back to answer: the sheet opens with Ask, and focus is on that passage's Show on page.
      await np.locator("[data-ask-back]").click();
      await dialog.waitFor({ state: "visible", timeout: 10_000 });
      await np.waitForFunction(() => document.activeElement?.matches("button[data-ask-show-page='0']"), null, { timeout: 5_000 }).catch(() => {});
      check(await activeIs(np, "button[data-ask-show-page='0']"), "Back to answer reopens the sheet and focuses Show on page");
      // Show in transcript: the same, ending in the transcript.
      await np.locator("button[data-ask-show-transcript='0']").click();
      await np.waitForSelector("mark[data-ask], [data-ask-block]", { timeout: 15_000 });
      await dialog.waitFor({ state: "hidden", timeout: 3_000 }).catch(() => {});
      check(!(await dialog.isVisible().catch(() => false)), "Show in transcript closed the sheet");
      check(await activeIs(np, "[data-node]"), "focus is on the passage");
      // Alt+Shift+A with the sheet closed opens it with Ask and the box.
      await np.keyboard.press("Alt+Shift+A");
      await np.waitForFunction(() => document.activeElement?.matches("form[aria-label^='Ask a question'] input"), null, { timeout: 10_000 }).catch(() => {});
      check(await activeIs(np, "form[aria-label^='Ask a question'] input"), "Alt+Shift+A opens the sheet and focuses the box");
      // Escape clears, Escape collapses and focuses the header (inside the sheet).
      await np.keyboard.press("Escape");
      await np.keyboard.press("Escape");
      await np.waitForTimeout(200);
      check(await activeIs(np, "button[aria-controls$='-ask-panel']"), "Escape, Escape focuses the Ask header in the sheet");
      check(!(await expanded(askHeader(np))), "and collapses it");
      await np.keyboard.press("Escape");
      await np.waitForTimeout(500);
      check(!(await dialog.isVisible().catch(() => false)), "Escape on the header closes the sheet");
      check(await activeIs(np, "button[aria-label='Table of contents and ask a question']"), "focus returns to the toolbar button");
    } finally {
      await narrow.close();
    }
  });

  // 21 ----------------------------------------------------------------------------------------
  await scenario(21, "A viewer with no askUrl (the dialog on /about) has no Ask item", async () => {
    await page.goto(`${base}/about#reflow=${encodeURIComponent("/pdfs/resnet.pdf")}`, { waitUntil: "load" });
    await page.waitForSelector(`[role=dialog] ${TRANSCRIPT_READY}`, { timeout: 60_000 });
    check((await page.locator("[role=dialog] aside[aria-label] h2 > button").count()) === 1, "one accordion item");
    check((await page.locator("[role=dialog] aside[aria-label] h2 > button").innerText()).trim() === "Table of contents", "the table of contents");
    check((await page.locator("[role=dialog] button[aria-controls$='-ask-panel']").count()) === 0, "no Ask item");
    check((await page.locator("[role=dialog] aside[aria-label]").getAttribute("aria-label")) === "Table of contents", "the column is named for what it holds");
    await page.keyboard.press("Alt+Shift+A");
    await page.waitForTimeout(300);
    check((await askForm(page).count()) === 0, "Alt+Shift+A does nothing");
    await shot(page, "no-ask");
  });

  // 22 ----------------------------------------------------------------------------------------
  await scenario(22, "Resizing from narrow to wide with the sheet open: it closes, focus moves to the same control in the column", async () => {
    const ctx = tracked(await browser.newContext({ viewport: { width: 800, height: 700 } }));
    const rp = await ctx.newPage();
    // Where the Ask form is: in the sheet, in the column, and whether a modal hides it.
    const where = () => rp.evaluate(() => ({
      dialog: !!document.querySelector("[role=dialog]"),
      forms: [...document.querySelectorAll("form[aria-label^='Ask a question']")].map((f) => ({
        sheet: !!f.closest("[role=dialog]"), column: !!f.closest("aside[aria-label]"), hidden: !!f.closest("[aria-hidden=true], [inert]"),
      })),
    }));
    const widen = async () => { await rp.setViewportSize({ width: 1280, height: 700 }); await rp.locator("[role=dialog]").waitFor({ state: "hidden", timeout: 3_000 }).catch(() => {}); await rp.waitForTimeout(500); };
    const narrowAgain = async () => { await rp.setViewportSize({ width: 800, height: 700 }); await rp.waitForTimeout(300); };
    try {
      await openViewer(rp);
      await openAsk(rp);
      check((await where()).forms.every((f) => f.sheet), "at 800 px the form is in the sheet");
      const result = await askQuestion(rp, QUESTION);
      check(result.kind === "answer", "an answer");
      check(await activeIs(rp, "[role=dialog] article[aria-labelledby]"), "the answer has focus in the sheet");
      const text = await rp.locator("article[aria-labelledby]").innerText();
      // the answer has focus
      await widen();
      const w = await where();
      check(!w.dialog, "the sheet closed when the window became wide");
      check(w.forms.length === 1 && w.forms[0].column && !w.forms[0].hidden, `the one form is in the column and not hidden (${JSON.stringify(w.forms)})`);
      await rp.waitForFunction(() => document.activeElement?.matches("aside[aria-label] article[aria-labelledby]"), null, { timeout: 3_000 }).catch(() => {});
      check(await activeIs(rp, "aside[aria-label] article[aria-labelledby]"), "focus is on the answer in the column");
      check((await rp.locator("article[aria-labelledby]").innerText()) === text, "the answer is the same");
      const regions = await statusRegions(rp);
      check(regions.filter((r) => r.own).length === 1 && regions.every((r) => (r.own ? !r.hidden : !!r.name)),
        `one status region in the viewer, not hidden now the sheet has closed; any other is named (${JSON.stringify(regions)})`);
      await shot(rp, "widen-answer");
      await rp.keyboard.press("Alt+Shift+A");
      check(await activeIs(rp, "aside[aria-label] form[aria-label^='Ask a question'] input"), "Alt+Shift+A then focuses the box in the column");
      check((await rp.evaluate(() => !!document.activeElement?.closest("[aria-hidden=true], [inert]"))) === false, "and it is not under a modal");

      // A draft in the box keeps its focus and its text.
      await narrowAgain();
      await openAsk(rp);
      await rp.locator("form[aria-label^='Ask a question'] input").fill("a draft that was not sent");
      await widen();
      await rp.waitForFunction(() => document.activeElement?.matches("aside[aria-label] form[aria-label^='Ask a question'] input"), null, { timeout: 3_000 }).catch(() => {});
      check(await activeIs(rp, "aside[aria-label] form[aria-label^='Ask a question'] input"), "the box has focus in the column");
      check((await rp.locator("form[aria-label^='Ask a question'] input").inputValue()) === "a draft that was not sent", "with its draft");
      await rp.keyboard.type("!");
      check((await rp.locator("form[aria-label^='Ask a question'] input").inputValue()) === "a draft that was not sent!", "and typing goes on from the end, not over it");

      // A passage button, and a contents entry.
      await narrowAgain();
      await rp.keyboard.press("Alt+Shift+R");
      await rp.locator("[role=dialog] button[data-ask-show-transcript='0']").focus();
      await widen();
      await rp.waitForFunction(() => document.activeElement?.matches("aside[aria-label] button[data-ask-show-transcript='0']"), null, { timeout: 3_000 }).catch(() => {});
      check(await activeIs(rp, "aside[aria-label] button[data-ask-show-transcript='0']"), "Show in transcript keeps focus in the column");
      await narrowAgain();
      await rp.keyboard.press("Alt+Shift+O");
      const entry = await rp.evaluate(() => { const a = document.activeElement; return a?.closest("[role=dialog]") && a.matches("nav a") ? a.getAttribute("href") : null; });
      check(!!entry, `Alt+Shift+O in the narrow layout opens the sheet on an outline entry (${entry})`);
      await widen();
      await rp.waitForFunction((h) => document.activeElement?.matches(`aside[aria-label] nav a[href="${h}"]`), entry, { timeout: 3_000 }).catch(() => {});
      check(await activeIs(rp, `aside[aria-label] nav a[href="${entry}"]`), "the same outline entry has focus in the column");
      await shot(rp, "widen-outline");
    } finally {
      await ctx.close();
    }
  });

  // 23 ----------------------------------------------------------------------------------------
  await scenario(23, "Resizing from wide to narrow with focus in the column: focus goes to the sheet's button, the draft is kept", async () => {
    const ctx = tracked(await browser.newContext({ viewport: { width: 1280, height: 800 } }));
    const rp = await ctx.newPage();
    const trigger = "button[aria-label='Table of contents and ask a question']";
    try {
      await openViewer(rp);
      await recordStatus(rp);
      await openAsk(rp);
      await rp.locator("form[aria-label^='Ask a question'] input").fill("a draft in the wide layout");
      await rp.setViewportSize({ width: 800, height: 700 });
      await rp.waitForTimeout(400);
      check(await activeIs(rp, trigger), `focus is on the sidebar's button, not <body> (${await rp.evaluate(() => document.activeElement?.tagName)})`);
      check((await rp.locator("[role=dialog]").count()) === 0, "no sheet was opened for the reader");
      check((await lastSaid(rp)).startsWith("The sidebar is now in a panel"), `the status says where it went (${await lastSaid(rp)})`);
      await shot(rp, "narrow-focus");
      await rp.keyboard.press("Enter");
      await rp.locator("[role=dialog]").waitFor({ state: "visible", timeout: 10_000 });
      check((await rp.locator("form[aria-label^='Ask a question'] input").inputValue()) === "a draft in the wide layout", "the draft is in the sheet's box");
      await rp.keyboard.press("Alt+Shift+A");
      await rp.waitForFunction(() => document.activeElement?.matches("[role=dialog] form[aria-label^='Ask a question'] input"), null, { timeout: 3_000 }).catch(() => {});
      check(await activeIs(rp, "[role=dialog] form[aria-label^='Ask a question'] input"), "Alt+Shift+A focuses it");
      await rp.keyboard.press("Escape");
      await rp.keyboard.press("Escape");
      await rp.keyboard.press("Escape");
      await rp.locator("[role=dialog]").waitFor({ state: "hidden", timeout: 3_000 }).catch(() => {});
      // An outline entry in the column.
      await rp.setViewportSize({ width: 1280, height: 800 });
      await rp.waitForTimeout(300);
      await rp.locator("aside[aria-label] nav a").first().focus();
      await rp.setViewportSize({ width: 800, height: 700 });
      await rp.waitForTimeout(400);
      check(await activeIs(rp, trigger), "an outline entry that had focus: focus goes to the button too");
      // Focus on the page is left alone.
      await rp.setViewportSize({ width: 1280, height: 800 });
      await rp.waitForTimeout(300);
      await rp.locator("[data-page] [data-page-label]").first().evaluate((el) => { el.setAttribute("tabindex", "-1"); (el as HTMLElement).focus(); });
      await rp.setViewportSize({ width: 800, height: 700 });
      await rp.waitForTimeout(400);
      check(!(await activeIs(rp, trigger)) && (await rp.evaluate(() => !!document.activeElement?.closest("[data-page]"))), "focus on the page stays on the page");
    } finally {
      await ctx.close();
    }
  });

  // 24 ----------------------------------------------------------------------------------------
  await scenario(24, "Alt+Shift+O inside the sheet opens a collapsed table of contents and focuses its outline", async () => {
    const ctx = tracked(await browser.newContext({ viewport: { width: 800, height: 700 } }));
    const rp = await ctx.newPage();
    const inSheetEntry = () => rp.evaluate(() => { const a = document.activeElement; return !!a && !!a.closest("[role=dialog]") && a.matches("nav a"); });
    try {
      await openViewer(rp);
      await rp.locator("button[aria-label='Table of contents and ask a question']").click();
      await rp.locator("[role=dialog]").waitFor({ state: "visible", timeout: 10_000 });
      const toc = rp.locator("[role=dialog] button[aria-controls$='-toc-panel']");
      await toc.click();
      check((await toc.getAttribute("aria-expanded")) === "false", "the table of contents is collapsed in the sheet");
      await toc.focus();
      await rp.keyboard.press("Alt+Shift+O");
      await rp.waitForTimeout(200);
      check((await toc.getAttribute("aria-expanded")) === "true", "Alt+Shift+O from its header expanded it");
      check(await inSheetEntry(), "and focused an outline entry in the sheet");
      check(await rp.locator("[role=dialog]").isVisible(), "the sheet stays open");
      await shot(rp, "O-from-header");
      // From the question box, with the table of contents collapsed again.
      await toc.click();
      await rp.keyboard.press("Alt+Shift+A");
      await rp.waitForFunction(() => document.activeElement?.matches("[role=dialog] form[aria-label^='Ask a question'] input"), null, { timeout: 30_000 });
      check((await toc.getAttribute("aria-expanded")) === "false", "collapsed again, with the focus in the question box");
      await rp.keyboard.press("Alt+Shift+O");
      await rp.waitForTimeout(200);
      check((await toc.getAttribute("aria-expanded")) === "true" && (await inSheetEntry()), "Alt+Shift+O from the question box expands it and focuses an entry");
      check((await askHeader(rp).getAttribute("aria-expanded")) === "true", "Ask is as it was");
      // The sheet is closed, the item collapsed: the shortcut opens the sheet on the outline.
      await rp.keyboard.press("Escape");
      await rp.locator("[role=dialog]").waitFor({ state: "hidden", timeout: 3_000 }).catch(() => {});
      await rp.locator("button[aria-label='Table of contents and ask a question']").click();
      await rp.locator("[role=dialog]").waitFor({ state: "visible", timeout: 10_000 });
      await toc.click();
      await rp.keyboard.press("Escape");
      await rp.locator("[role=dialog]").waitFor({ state: "hidden", timeout: 3_000 }).catch(() => {});
      await rp.keyboard.press("Alt+Shift+O");
      await rp.locator("[role=dialog]").waitFor({ state: "visible", timeout: 10_000 });
      await rp.waitForTimeout(400);
      check(await inSheetEntry(), "with the sheet closed, the shortcut opens it on an outline entry");
      check((await toc.getAttribute("aria-expanded")) === "true", "with the table of contents expanded");
    } finally {
      await ctx.close();
    }
  });

  await scenario(25, "A focused question stays visible when the narrow viewport shrinks; the shortcut reveals it again", async () => {
    const ctx = tracked(await browser.newContext({ viewport: { width: 800, height: 700 } }));
    const rp = await ctx.newPage();
    const inputIsVisible = () => rp.evaluate(() => {
      const input = document.querySelector<HTMLInputElement>("[role=dialog] form[aria-label^='Ask a question'] input");
      const sheet = input?.closest("[role=dialog]");
      if (!input || !sheet || document.activeElement !== input) return false;
      const box = input.getBoundingClientRect();
      const bounds = sheet.getBoundingClientRect();
      return box.top >= Math.max(0, bounds.top) && box.bottom <= Math.min(innerHeight, bounds.bottom);
    });
    try {
      await openViewer(rp);
      await rp.keyboard.press("Alt+Shift+A");
      await rp.waitForFunction(() => document.activeElement?.matches("[role=dialog] form[aria-label^='Ask a question'] input"));
      await rp.setViewportSize({ width: 320, height: 240 });
      await rp.waitForTimeout(400);
      check(await inputIsVisible(), "the focused input is inside the sheet after a height change within the narrow layout");
      await shot(rp, "question-after-shrink");
      const sheet = rp.locator("[data-slot=sheet-content]");
      await sheet.hover({ position: { x: 20, y: 100 } });
      await rp.mouse.wheel(0, -1000);
      await rp.waitForTimeout(300);
      check(!(await inputIsVisible()), "scrolling the sheet away leaves its input focused but out of view");
      await rp.keyboard.press("Alt+Shift+A");
      await rp.waitForTimeout(200);
      check(await inputIsVisible(), "the shortcut reveals the input even when it already has focus");
      await shot(rp, "question-after-repeated-shortcut");
    } finally {
      await ctx.close();
    }
  });

  // 7 (last: it stops the fake) ---------------------------------------------------------------
  await scenario(7, "Fake model stopped: 'not running right now'; Find and Show on page still work", async () => {
    await openViewer(page);
    stop(fake);
    await fake.exited;
    await openAsk(page);
    const result = await askQuestion(page, "What is the degradation problem in deep networks?");
    check(result.kind === "unavailable" && result.why === "model-offline", `route says unavailable/model-offline (got ${JSON.stringify(result)})`);
    const msg = await page.locator("form[aria-label^='Ask a question'] ~ p").innerText();
    check(msg.includes("not running right now"), `message: ${msg}`);
    await shot(page, "model-offline");
    await page.keyboard.press("Alt+Shift+F");
    await page.keyboard.type("residual");
    await page.keyboard.press("Enter");
    await page.waitForSelector("mark[data-find]", { timeout: 15_000 });
    check((await page.locator("mark[data-find]").count()) > 0, "Find still highlights matches");
    await page.keyboard.press("Escape");
    const button = page.locator("[data-show-source]").first();
    await button.scrollIntoViewIfNeeded();
    await button.click();
    await page.waitForSelector("[data-source-overlay]", { timeout: 15_000 });
    check((await overlays(page)).length === 1, "heading Show on page still outlines");
    await shot(page, "viewer-still-works");
    // Bring the model back: the next question works without reloading the server.
    fake = startFake();
    await waitFor(`http://127.0.0.1:${fakePort}/api/tags`);
    await openViewer(page);
    await openAsk(page);
    const again = await askQuestion(page, "What does the paper say about the degradation problem when networks get deeper?");
    check(again.kind === "answer", `after the model returns, the answer is back (got ${again.kind})`);
  });

  // Every page of every context was watched: a console error or an uncaught exception that is not
  // one of the expected ones fails the run.
  await scenario("console", "No unexpected console error or uncaught exception on any page", async () => {
    for (const e of unexpected.slice(0, 10)) note(`  ${e.slice(0, 240)}`);
    check(unexpected.length === 0, `${unexpected.length} unexpected console error(s) or exception(s)`);
    note(`${expectedErrors} expected error(s): the browser's own log of an HTTP error status from /api/ask/ (the model off, an unusable reply)`);
  });
  await browser.close();
}

function CSSescape(id: string): string {
  return id.replace(/([^\w-])/g, "\\$1");
}

try {
  await main();
} catch (e) {
  console.error(e);
  outcomes.push({ n: 0, name: "run", ok: false, notes: [String(e)], shots: [] });
} finally {
  for (const p of started) stop(p);
}
const failed = outcomes.filter((o) => !o.ok);
console.log(`\n${outcomes.length - failed.length} of ${outcomes.length} scenarios passed. Screenshots and logs: ${out}`);
process.exit(failed.length ? 1 : 0);
