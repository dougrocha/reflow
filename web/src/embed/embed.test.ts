import { describe, expect, test } from "bun:test";
import { connectViewer } from "./parent";
import { embedSrc, envelope, exactOrigin, INSTANCE_ID, newInstanceId, parseMessage, PROTOCOL, VERSION } from "./protocol";
import { connectParent, embedContext } from "./viewer";

// Both sides of the embed protocol with stand-in windows: a window is an EventTarget that records
// what is posted to it, and a message is a MessageEvent-shaped event with the origin and source
// the browser would give it.

const VIEWER = "https://reflow.test";
const PARENT = "https://example.org";
const ID = "0123456789abcdef0123456789abcdef";
const OTHER_ID = "fedcba9876543210fedcba9876543210";

class FakeWindow extends EventTarget {
  posted: { data: unknown; target: string }[] = [];
  parent: FakeWindow = this;
  location = { search: "", ancestorOrigins: undefined as unknown as DOMStringList };
  postMessage(data: unknown, target: string) {
    this.posted.push({ data, target });
  }
}

/** Deliver `data` to `win` as the browser would, from `source` at `origin`. */
function deliver(win: FakeWindow, data: unknown, origin: string, source: unknown) {
  const event = new Event("message");
  Object.defineProperties(event, { data: { value: data }, origin: { value: origin }, source: { value: source } });
  win.dispatchEvent(event);
}

const msg = (body: Record<string, unknown>, instance = ID) => ({ protocol: PROTOCOL, version: VERSION, instance, ...body });

describe("parseMessage", () => {
  test("takes each message of its direction", () => {
    expect(parseMessage(msg({ type: "ready" }), "viewer", ID)).toEqual(msg({ type: "ready" }) as never);
    expect(parseMessage(msg({ type: "title", title: "Form W-9" }), "viewer", ID)?.type).toBe("title");
    expect(parseMessage(msg({ type: "page", page: 3 }), "viewer", ID)?.type).toBe("page");
    expect(parseMessage(msg({ type: "close" }), "viewer", ID)?.type).toBe("close");
    expect(parseMessage(msg({ type: "page", page: 3 }), "parent", ID)?.type).toBe("page");
  });

  test.each([
    ["another instance", msg({ type: "close" }, OTHER_ID)],
    ["another version", { ...msg({ type: "close" }), version: 2 }],
    ["a version as a string", { ...msg({ type: "close" }), version: "1" }],
    ["another protocol", { ...msg({ type: "close" }), protocol: "other" }],
    ["no envelope", { type: "close" }],
    ["an unknown type", msg({ type: "navigate", url: "https://evil.test" })],
    ["an extra field", msg({ type: "close", html: "<img onerror=alert(1)>" })],
    ["a page that is not a page", msg({ type: "page", page: 0 })],
    ["a fractional page", msg({ type: "page", page: 1.5 })],
    ["a page as a string", msg({ type: "page", page: "3" })],
    ["a title that is not a string", msg({ type: "title", title: 3 })],
    ["a title too long", msg({ type: "title", title: "x".repeat(501) })],
    ["a missing field", msg({ type: "title" })],
    ["a string", JSON.stringify(msg({ type: "close" }))],
    ["an array", [msg({ type: "close" })]],
    ["null", null],
  ])("refuses %s", (_, data) => expect(parseMessage(data, "viewer", ID)).toBeNull());

  test("a parent may only ask for a page", () => {
    expect(parseMessage(msg({ type: "close" }), "parent", ID)).toBeNull();
    expect(parseMessage(msg({ type: "ready" }), "parent", ID)).toBeNull();
  });

  test("an instance that is not an instance id matches nothing, even itself", () => {
    expect(parseMessage(msg({ type: "close" }, ""), "viewer", "")).toBeNull();
  });
});

describe("the protocol's helpers", () => {
  test("exactOrigin takes only an origin as URL writes it", () => {
    expect(exactOrigin("https://example.org")).toBe("https://example.org");
    expect(exactOrigin("http://localhost:3201")).toBe("http://localhost:3201");
    for (const v of ["https://example.org/", "https://Example.org", "*", "null", "file:///x", "https://example.org:443", 3, null,
      "https://*", "https://*.example.org"]) {
      expect(exactOrigin(v)).toBeNull();
    }
  });

  test("newInstanceId makes distinct ids the protocol accepts", () => {
    const a = newInstanceId();
    expect(INSTANCE_ID.test(a)).toBe(true);
    expect(newInstanceId()).not.toBe(a);
  });

  test("embedSrc adds the parent, the instance and a page to the resolver's viewer URL", () => {
    const src = new URL(embedSrc(`${VIEWER}/embed/fw9?site=acme`, { parent: PARENT, instance: ID, page: 2 }));
    expect(src.origin + src.pathname).toBe(`${VIEWER}/embed/fw9`);
    expect(Object.fromEntries(src.searchParams)).toEqual({ site: "acme", parent: PARENT, instance: ID, page: "2" });
  });
});

describe("connectViewer: the parent's side", () => {
  const setup = () => {
    const parent = new FakeWindow();
    const frameWindow = new FakeWindow();
    const frame = { contentWindow: frameWindow } as unknown as HTMLIFrameElement;
    const heard: unknown[] = [];
    const connection = connectViewer({
      frame, src: `${VIEWER}/embed/fw9?site=acme&parent=${PARENT}&instance=${ID}`, instance: ID, win: parent as unknown as Window,
      onReady: () => heard.push("ready"), onTitle: (t) => heard.push(["title", t]),
      onPage: (p) => heard.push(["page", p]), onClose: () => heard.push("close"),
    });
    return { parent, frameWindow, heard, connection };
  };

  test("hears the viewer's messages from the frame at the viewer's origin", () => {
    const { parent, frameWindow, heard } = setup();
    deliver(parent, msg({ type: "ready" }), VIEWER, frameWindow);
    deliver(parent, msg({ type: "title", title: "Form W-9" }), VIEWER, frameWindow);
    deliver(parent, msg({ type: "page", page: 4 }), VIEWER, frameWindow);
    deliver(parent, msg({ type: "close" }), VIEWER, frameWindow);
    expect(heard).toEqual(["ready", ["title", "Form W-9"], ["page", 4], "close"]);
  });

  test("ignores a message from another origin, another window, another instance or version", () => {
    const { parent, frameWindow, heard } = setup();
    deliver(parent, msg({ type: "close" }), "https://evil.test", frameWindow);
    deliver(parent, msg({ type: "close" }), VIEWER, new FakeWindow()); // another frame of the viewer's origin
    deliver(parent, msg({ type: "close" }), VIEWER, null);
    deliver(parent, msg({ type: "close" }, OTHER_ID), VIEWER, frameWindow);
    deliver(parent, { ...msg({ type: "close" }), version: 2 }, VIEWER, frameWindow);
    expect(heard).toEqual([]);
  });

  test("asks for a page only once the viewer is ready, at the viewer's exact origin", () => {
    const { parent, frameWindow, connection } = setup();
    expect(connection.goToPage(3)).toBe(false);
    expect(frameWindow.posted).toEqual([]);
    deliver(parent, msg({ type: "ready" }), VIEWER, frameWindow);
    expect(connection.goToPage(3)).toBe(true);
    expect(connection.goToPage(0)).toBe(false);
    expect(frameWindow.posted).toEqual([{ data: msg({ type: "page", page: 3 }), target: VIEWER }]);
  });

  test("hears nothing after disconnect", () => {
    const { parent, frameWindow, heard, connection } = setup();
    connection.disconnect();
    deliver(parent, msg({ type: "close" }), VIEWER, frameWindow);
    expect(heard).toEqual([]);
    expect(connection.goToPage(2)).toBe(false);
  });

  test("refuses a src that is not an http(s) URL or an instance that is not an id", () => {
    const frame = { contentWindow: new FakeWindow() } as unknown as HTMLIFrameElement;
    const win = new FakeWindow() as unknown as Window;
    expect(() => connectViewer({ frame, src: "/embed/fw9", instance: ID, win })).toThrow();
    expect(() => connectViewer({ frame, src: "javascript:alert(1)", instance: ID, win })).toThrow();
    expect(() => connectViewer({ frame, src: `${VIEWER}/embed/fw9`, instance: "x", win })).toThrow();
  });
});

describe("embedContext and connectParent: the viewer's side", () => {
  const framedWindow = (search: string, ancestors?: string[]) => {
    const parent = new FakeWindow();
    const win = new FakeWindow();
    win.parent = parent;
    win.location = { search, ancestorOrigins: ancestors as unknown as DOMStringList };
    return { parent, win };
  };
  const query = (p: Record<string, string>) => `?${new URLSearchParams(p)}`;

  test("the parent and instance come from the frame's URL", () => {
    const { win } = framedWindow(query({ site: "acme", parent: PARENT, instance: ID }));
    expect(embedContext(win as unknown as Window)).toEqual({ parent: PARENT, instance: ID });
  });

  test("none when not framed, when a parameter is missing, malformed or twice, or when the browser names another parent", () => {
    const top = new FakeWindow();
    top.location = { search: query({ parent: PARENT, instance: ID }), ancestorOrigins: undefined as unknown as DOMStringList };
    expect(embedContext(top as unknown as Window)).toBeNull();
    for (const search of [
      query({ instance: ID }), query({ parent: PARENT }), query({ parent: "*", instance: ID }),
      query({ parent: `${PARENT}/x`, instance: ID }), query({ parent: PARENT, instance: "short" }),
      `?parent=${encodeURIComponent(PARENT)}&parent=https%3A%2F%2Fevil.test&instance=${ID}`,
    ]) {
      expect(embedContext(framedWindow(search).win as unknown as Window)).toBeNull();
    }
    const lied = framedWindow(query({ parent: PARENT, instance: ID }), ["https://evil.test"]).win;
    expect(embedContext(lied as unknown as Window)).toBeNull();
    const told = framedWindow(query({ parent: PARENT, instance: ID }), [PARENT]).win;
    expect(embedContext(told as unknown as Window)).toEqual({ parent: PARENT, instance: ID });
  });

  test("sends to window.parent at the parent's exact origin, never *", () => {
    const { parent, win } = framedWindow("");
    const c = connectParent(win as unknown as Window, { parent: PARENT, instance: ID }, () => {});
    c.send({ type: "ready" });
    c.send({ type: "title", title: "Form W-9" });
    expect(parent.posted).toEqual([
      { data: envelope(ID, { type: "ready" }), target: PARENT },
      { data: envelope(ID, { type: "title", title: "Form W-9" }), target: PARENT },
    ]);
  });

  test("goes to a page the parent asks for, and ignores every forged ask", () => {
    const { parent, win } = framedWindow("");
    const pages: number[] = [];
    const c = connectParent(win as unknown as Window, { parent: PARENT, instance: ID }, (p) => pages.push(p));
    deliver(win, msg({ type: "page", page: 5 }), "https://evil.test", parent); // another origin
    deliver(win, msg({ type: "page", page: 5 }), PARENT, new FakeWindow()); // another window of the parent's origin
    deliver(win, msg({ type: "page", page: 5 }, OTHER_ID), PARENT, parent); // another instance
    deliver(win, { ...msg({ type: "page", page: 5 }), version: 2 }, PARENT, parent); // another version
    deliver(win, msg({ type: "close" }), PARENT, parent); // not a parent's message
    expect(pages).toEqual([]);
    deliver(win, msg({ type: "page", page: 3 }), PARENT, parent);
    expect(pages).toEqual([3]);
    c.disconnect();
    deliver(win, msg({ type: "page", page: 4 }), PARENT, parent);
    expect(pages).toEqual([3]);
  });
});
