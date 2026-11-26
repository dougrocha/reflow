import { describe, expect, test } from "bun:test";
import { pageAtLine, trackReadingPage } from "./page-at-line";

const at = (...tops: number[]) => tops.map((top, i) => ({ page: i + 1, top }));

describe("pageAtLine", () => {
  test("is the last card that starts at or above the line", () => {
    expect(pageAtLine(at(100, 280, 700), 300, false)).toBe(2);
    expect(pageAtLine(at(100, 280, 700), 280, false)).toBe(2);
  });

  test("a line in a gap between cards belongs to the upper card (the Carmel case)", () => {
    expect(pageAtLine(at(100, 400), 380, false)).toBe(1);
  });

  test("at the top of the scroller it is the first page, even with card 2 above the line", () => {
    expect(pageAtLine(at(100, 180), 300, true)).toBe(1);
  });

  test("a line above every card is the first page", () => {
    expect(pageAtLine(at(100, 400), 50, false)).toBe(1);
  });

  test("no cards is page 1", () => {
    expect(pageAtLine([], 300, false)).toBe(1);
    expect(pageAtLine([], 300, true)).toBe(1);
  });

  test("cards numbered other than 1..N return their own page", () => {
    const cards = [{ page: 4, top: 100 }, { page: 5, top: 300 }, { page: 9, top: 500 }];
    expect(pageAtLine(cards, 350, false)).toBe(5);
    expect(pageAtLine(cards, 50, false)).toBe(4);
    expect(pageAtLine(cards, 900, false)).toBe(9);
  });
});

// Fakes: just enough of the DOM for trackReadingPage.
class FakeCard {
  constructor(public page: number, public top: number) {}
  get dataset() { return { page: String(this.page) }; }
  getBoundingClientRect() { return { top: this.top }; }
}

class FakeRoot {
  top = 0;
  height = 1000;
  scrollTop = 0;
  listeners = new Map<string, () => void>();
  getBoundingClientRect() { return { top: this.top, height: this.height }; }
  addEventListener(type: string, fn: () => void) { this.listeners.set(type, fn); }
  removeEventListener(type: string, fn: () => void) { if (this.listeners.get(type) === fn) this.listeners.delete(type); }
  scroll() { this.listeners.get("scroll")?.(); }
}

class FakeResizeObserver {
  static last: FakeResizeObserver;
  targets: unknown[] = [];
  disconnected = false;
  constructor(public callback: () => void) { FakeResizeObserver.last = this; }
  observe(target: unknown) { this.targets.push(target); }
  disconnect() { this.disconnected = true; }
  fire() { this.callback(); }
}

function setup(cards: FakeCard[], opts: { pinned?: () => boolean } = {}) {
  const root = new FakeRoot();
  const pages: number[] = [];
  const queue = new Map<number, FrameRequestCallback>();
  let next = 1;
  const cancelled: number[] = [];
  const stop = trackReadingPage({
    root: root as unknown as HTMLElement,
    cards: () => cards as unknown as HTMLElement[],
    isPinned: opts.pinned ?? (() => false),
    setPage: (p) => pages.push(p),
    ResizeObserver: FakeResizeObserver as unknown as typeof ResizeObserver,
    raf: (cb) => { queue.set(next, cb); return next++; },
    cancelRaf: (id) => { cancelled.push(id); queue.delete(id); },
  });
  const flush = () => {
    const pending = [...queue.values()];
    queue.clear();
    pending.forEach((cb) => cb(0));
  };
  return { root, pages, queue, cancelled, stop, flush, observer: FakeResizeObserver.last };
}

describe("trackReadingPage", () => {
  test("the first value needs no observer event", () => {
    const { pages, flush } = setup([new FakeCard(1, 0), new FakeCard(2, 700)]);
    expect(pages).toEqual([]);
    flush();
    expect(pages).toEqual([1]);
  });

  test("a skeleton that becomes a page moves card 2 below the line", () => {
    const one = new FakeCard(1, -40), two = new FakeCard(2, 280); // 120 px skeleton; the line is at 300
    const { pages, flush, observer, root } = setup([one, two]);
    root.scrollTop = 40;
    flush();
    expect(pages.at(-1)).toBe(2);
    two.top = 346; // card 1 is 186 px once its page loads
    observer.fire();
    flush();
    expect(pages.at(-1)).toBe(1);
  });

  test("a change of the root's height alone moves the line from card 1 into card 2", () => {
    const { pages, flush, observer, root } = setup([new FakeCard(1, 100), new FakeCard(2, 400)]);
    root.scrollTop = 50;
    flush();
    expect(pages.at(-1)).toBe(1); // line at 300
    root.height = 1500; // line at 450
    observer.fire();
    flush();
    expect(pages.at(-1)).toBe(2);
    expect(observer.targets).toContain(root);
  });

  test("observes every card as well as the root", () => {
    const cards = [new FakeCard(1, 0), new FakeCard(2, 300)];
    const { observer } = setup(cards);
    expect(observer.targets).toHaveLength(3);
    cards.forEach((c) => expect(observer.targets).toContain(c));
  });

  test("a line between two cards reads the upper card, not the last value", () => {
    const { pages, flush, root } = setup([new FakeCard(1, 149), new FakeCard(2, 367)]);
    root.top = 93; root.height = 664; // the line is at 292; card 1 ends at 343... and the gap is 343-367
    root.scrollTop = 10;
    flush();
    expect(pages).toEqual([1]);
  });

  test("while pinned, nothing is set; after release, the next scroll sets it", () => {
    let pinned = true;
    const { pages, flush, root, observer } = setup([new FakeCard(1, -400), new FakeCard(2, 100)], { pinned: () => pinned });
    root.scrollTop = 500;
    flush();
    root.scroll(); flush();
    observer.fire(); flush();
    expect(pages).toEqual([]);
    pinned = false;
    root.scroll(); flush();
    expect(pages).toEqual([2]);
  });

  test("three scrolls before a frame give one update", () => {
    const { pages, queue, flush, root } = setup([new FakeCard(1, 0)]);
    flush();
    pages.length = 0;
    root.scroll(); root.scroll(); root.scroll();
    expect(queue.size).toBe(1);
    flush();
    expect(pages).toHaveLength(1);
  });

  test("cleanup removes the listener, disconnects, cancels the frame and ignores a late flush", () => {
    const { pages, queue, cancelled, flush, root, observer, stop } = setup([new FakeCard(1, 0)]);
    const late = [...queue.values()];
    stop();
    expect(root.listeners.size).toBe(0);
    expect(observer.disconnected).toBe(true);
    expect(cancelled).toHaveLength(1);
    late.forEach((cb) => cb(0));
    flush();
    expect(pages).toEqual([]);
  });
});
