import { envelope, exactOrigin, INSTANCE_ID, parseMessage, type ViewerMessage } from "./protocol";

// The viewer's side of the embed protocol (./protocol), in the frame at /embed/<id>.
//
// The frame's URL names the parent's origin (`parent`) and the instance (`instance`). The server
// frames the page only when the site lists that origin, and then sends
// `Content-Security-Policy: frame-ancestors <that origin>`, so the browser itself refuses any other
// parent. That is what makes `parent` safe to post to: a page that names an origin it is not on
// cannot frame the viewer at all. Where the browser says who the parent is (Chromium's
// location.ancestorOrigins), it must say the same.
//
// A message is taken only when event.origin is that origin, event.source is window.parent, and
// it is a message of this protocol, version and instance. What the viewer sends goes to
// window.parent with that origin as the target, never "*".

export type EmbedContext = { parent: string; instance: string };

/** The parent and instance this frame was opened for, or null when it is not framed for one: not in
 *  a frame, no or malformed `parent`/`instance`, or a browser that names a different parent. */
export function embedContext(win: Pick<Window, "location" | "parent">): EmbedContext | null {
  if (win.parent === (win as unknown)) return null;
  const params = new URLSearchParams(win.location.search);
  const parents = params.getAll("parent");
  const instances = params.getAll("instance");
  if (parents.length !== 1 || instances.length !== 1) return null;
  const parent = exactOrigin(parents[0]);
  const instance = instances[0];
  if (parent === null || !INSTANCE_ID.test(instance)) return null;
  const ancestors = win.location.ancestorOrigins;
  if (ancestors && ancestors.length > 0 && ancestors[0] !== parent) return null;
  return { parent, instance };
}

export type ParentConnection = {
  send: (message: ViewerMessage) => void;
  disconnect: () => void;
};

/** Listen to the parent named by `context`, and send to it. `onPage` gets each page it asks for. */
export function connectParent(
  win: Pick<Window, "parent" | "addEventListener" | "removeEventListener">,
  { parent, instance }: EmbedContext,
  onPage: (page: number) => void,
): ParentConnection {
  const listener = (event: MessageEvent) => {
    if (event.origin !== parent || event.source === null || event.source !== win.parent) return;
    const message = parseMessage(event.data, "parent", instance);
    if (message) onPage(message.page);
  };
  win.addEventListener("message", listener);
  return {
    send(message) {
      win.parent.postMessage(envelope(instance, message), parent);
    },
    disconnect() {
      win.removeEventListener("message", listener);
    },
  };
}
