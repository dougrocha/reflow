import { afterEach, beforeEach, describe, expect, jest, test } from "bun:test";
import { start } from "./loader";

// The loader itself (loader.ts) in happy-dom: which clicks it takes, and that every way the overlay
// ends leaves the page as it was, with exactly one navigation to the PDF when it fails. The rules it
// applies are tested alone in loader.test.ts; the real browser run is in the PR's proof.

const REFLOW = "https://reflow.test";
const happy = (window as unknown as { happyDOM: { setURL: (url: string) => void; settings: Record<string, unknown> } }).happyDOM;

let navigated: string[];
/** The overlay's history entry, if any, at the moment of each navigation. */
let entryAtNavigation: unknown[];
let fetched: string[];
let answer: unknown;
let stop: (() => void) | null;
const realFetch = globalThis.fetch;
const realBack = history.back.bind(history);

beforeEach(() => {
  happy.setURL("https://customer.test/page");
  happy.settings.disableIframePageLoading = true;
  // happy-dom pops the entry but, once the page has a URL, fires no popstate. A browser does; the
  // Chromium run in the PR proves the real Back.
  history.back = () => {
    realBack();
    setTimeout(() => dispatchEvent(new PopStateEvent("popstate", { state: history.state })), 10);
  };
  document.head.innerHTML = "";
  document.body.innerHTML = "";
  document.documentElement.style.overflow = "";
  navigated = [];
  entryAtNavigation = [];
  fetched = [];
  answer = { status: "ready", viewer: "/embed/fw9?site=acme" };
  globalThis.fetch = (async (url: string) => {
    fetched.push(String(url));
    return new Response(JSON.stringify(answer));
  }) as typeof fetch;
  const script = document.createElement("script");
  script.setAttribute("src", `${REFLOW}/embed/loader.js`);
  script.setAttribute("data-site", "acme");
  stop = start(script as HTMLScriptElement, navigate);
});

function navigate(url: string) {
  navigated.push(url);
  entryAtNavigation.push(entry());
}

afterEach(async () => {
  jest.useRealTimers();
  // Close what a test left open, so its history entry and listener do not reach the next test.
  for (const dialog of document.querySelectorAll("dialog[data-reflow-embed]")) {
    dialog.dispatchEvent(new Event("cancel", { cancelable: true }));
    await popped();
  }
  stop?.();
  globalThis.fetch = realFetch;
  history.back = realBack;
  jest.useRealTimers();
  document.querySelectorAll("dialog").forEach((d) => d.remove());
});

const flush = async () => {
  for (let i = 0; i < 20; i++) await Promise.resolve();
};
/** Wait for the popstate that history.back() fires later, as a browser's does. */
const popped = () => new Promise((r) => setTimeout(r, 30));
const click = (el: Element) => {
  const event = new MouseEvent("click", { bubbles: true, cancelable: true, composed: true, button: 0 });
  el.dispatchEvent(event);
  return event;
};
const overlay = () => document.querySelector("dialog[data-reflow-embed]");
const entry = () => (history.state as Record<string, unknown> | null)?.reflowEmbed;

describe("which links it takes", () => {
  test("a link with no target of its own follows the page's <base target>", async () => {
    document.head.innerHTML = `<base target="_blank">`;
    document.body.innerHTML = `<a href="/a.pdf">A</a><a href="/b.pdf" target="_self">B</a>`;
    const [a, b] = document.querySelectorAll("a");
    expect(click(a).defaultPrevented).toBe(false);
    expect(click(b).defaultPrevented).toBe(true);
    await flush();
    expect(fetched.map((u) => new URL(u).searchParams.get("url"))).toEqual(["https://customer.test/b.pdf"]);
  });

  test("an SVG link", async () => {
    document.body.innerHTML = `<svg><a href="/s.pdf"><text>S</text></a></svg>`;
    expect(click(document.querySelector("svg text")!).defaultPrevented).toBe(true);
    await flush();
    expect(new URL(fetched[0]).searchParams.get("url")).toBe("https://customer.test/s.pdf");
  });

  test("a link around a component's shadow content, and a link inside a shadow root", async () => {
    document.body.innerHTML = `<a href="/w.pdf"><span id="host"></span></a><div id="host2"></div>`;
    const inner = document.getElementById("host")!.attachShadow({ mode: "open" });
    inner.innerHTML = `<b>W</b>`;
    expect(click(inner.querySelector("b")!).defaultPrevented).toBe(true);
    await flush();
    stop?.();
    stop = start(Object.assign(document.createElement("script"), { src: `${REFLOW}/embed/loader.js` }), navigate);
    expect(stop).toBeNull(); // no data-site
    const script = document.createElement("script");
    script.setAttribute("src", `${REFLOW}/embed/loader.js`);
    script.setAttribute("data-site", "acme");
    stop = start(script, navigate);
    const root = document.getElementById("host2")!.attachShadow({ mode: "open" });
    root.innerHTML = `<a href="/in.pdf"><i>I</i></a>`;
    expect(click(root.querySelector("i")!).defaultPrevented).toBe(true);
    await flush();
    expect(fetched.map((u) => new URL(u).searchParams.get("url"))).toEqual(["https://customer.test/w.pdf", "https://customer.test/in.pdf"]);
  });
});

describe("how the overlay ends", () => {
  test("no modal dialog: one navigation to the PDF, and the page untouched", async () => {
    jest.useFakeTimers();
    const showModal = HTMLDialogElement.prototype.showModal;
    (HTMLDialogElement.prototype as { showModal?: unknown }).showModal = undefined;
    try {
      document.body.innerHTML = `<a href="/a.pdf">A</a>`;
      const before = history.length;
      click(document.querySelector("a")!);
      await flush();
      jest.advanceTimersByTime(20_000);
      expect(navigated).toEqual(["https://customer.test/a.pdf"]);
      expect(history.length).toBe(before);
      expect(document.documentElement.style.overflow).toBe("");
      expect(overlay()).toBeNull();
    } finally {
      HTMLDialogElement.prototype.showModal = showModal;
    }
  });

  test("no ready in time: the overlay and its history entry go, then one navigation to the PDF", async () => {
    jest.useFakeTimers();
    document.body.innerHTML = `<a href="/a.pdf">A</a>`;
    click(document.querySelector("a")!);
    await flush();
    expect(overlay()).not.toBeNull();
    const ours = entry();
    expect(ours).toMatch(/^[0-9a-f]{32}$/);
    expect(document.documentElement.style.overflow).toBe("hidden");
    jest.advanceTimersByTime(9_999);
    expect(navigated).toEqual([]);
    jest.advanceTimersByTime(20);
    expect(entry()).not.toBe(ours);
    expect(entryAtNavigation).toHaveLength(1);
    expect(entryAtNavigation[0]).not.toBe(ours); // the entry went first
    expect(overlay()).toBeNull();
    expect(document.documentElement.style.overflow).toBe("");
    expect(navigated).toEqual(["https://customer.test/a.pdf"]);
  });

  test("Escape: the overlay and its history entry go, focus returns to the link, no navigation", async () => {
    document.body.innerHTML = `<a href="/a.pdf">A</a>`;
    const link = document.querySelector("a")!;
    click(link);
    await flush();
    const ours = entry();
    expect(ours).toMatch(/^[0-9a-f]{32}$/);
    overlay()!.dispatchEvent(new Event("cancel", { cancelable: true }));
    expect(navigated).toEqual([]);
    await popped();
    expect(entry()).not.toBe(ours);
    expect(overlay()).toBeNull();
    expect(document.activeElement).toBe(link);
    expect(navigated).toEqual([]);
  });

  test("an answer that is not ready: one navigation, no overlay", async () => {
    answer = { status: "unknown" };
    document.body.innerHTML = `<a href="/a.pdf">A</a>`;
    click(document.querySelector("a")!);
    click(document.querySelector("a")!);
    await flush();
    expect(navigated).toEqual(["https://customer.test/a.pdf"]);
    expect(fetched.length).toBe(1);
    expect(overlay()).toBeNull();
  });
});
