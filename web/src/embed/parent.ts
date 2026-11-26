import { envelope, exactOrigin, INSTANCE_ID, parseMessage } from "./protocol";

// The parent's side of the embed protocol (./protocol): what the loader runs on a
// customer's page around the <iframe> that holds reflow's viewer. Dependency-free, like the loader.
//
// A message is taken only when all of these hold:
//  - event.origin is the viewer's origin, the origin of the frame's src (exact, never "*");
//  - event.source is that frame's window, so another frame of the viewer's origin is not heard;
//  - it is a message of this protocol, version and instance (parseMessage).
// What it sends goes to the frame's window with the viewer's origin as the target, so a frame that
// navigated elsewhere never receives it.

export type ViewerHandlers = {
  /** The viewer is listening. goToPage works from here on. */
  onReady?: () => void;
  onTitle?: (title: string) => void;
  onPage?: (page: number) => void;
  /** The reader asked to close the viewer. Close the dialog and return focus to the link. */
  onClose?: () => void;
};

export type ViewerConnection = {
  /** Ask the viewer to go to a page. False (and nothing sent) before the viewer is ready. */
  goToPage: (page: number) => boolean;
  /** Stop listening. Call it when the frame is removed. */
  disconnect: () => void;
};

/**
 * Listen to the viewer in `frame`, whose src is `src` (embedSrc's URL, with this `instance`).
 * `win` is the parent's window (the default, `window`, outside tests).
 */
export function connectViewer(
  { frame, src, instance, win = window, ...on }: { frame: HTMLIFrameElement; src: string; instance: string; win?: Window } & ViewerHandlers,
): ViewerConnection {
  let origin: string | null = null;
  try {
    origin = exactOrigin(new URL(src).origin);
  } catch {
    origin = null;
  }
  if (origin === null || !INSTANCE_ID.test(instance)) throw new Error("connectViewer: src must be an http(s) URL and instance an instance id");
  const viewerOrigin = origin;
  let ready = false;

  const listener = (event: MessageEvent) => {
    if (event.origin !== viewerOrigin || event.source === null || event.source !== frame.contentWindow) return;
    const message = parseMessage(event.data, "viewer", instance);
    if (!message) return;
    switch (message.type) {
      case "ready":
        ready = true;
        on.onReady?.();
        break;
      case "title":
        on.onTitle?.(message.title);
        break;
      case "page":
        on.onPage?.(message.page);
        break;
      case "close":
        on.onClose?.();
        break;
    }
  };
  win.addEventListener("message", listener);

  return {
    goToPage(page) {
      const target = frame.contentWindow;
      if (!ready || !target || !Number.isInteger(page) || page < 1) return false;
      target.postMessage(envelope(instance, { type: "page", page }), viewerOrigin);
      return true;
    },
    disconnect() {
      ready = false;
      win.removeEventListener("message", listener);
    },
  };
}
