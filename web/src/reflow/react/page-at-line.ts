/** Where in the scroller's visible box the page being read is picked: 30% down from its top. */
const READING_LINE = 0.3;

/**
 * The page at the reading line. A gap between cards belongs to the card above it. At the very
 * top of the scroller, and when every card starts below the line, it is the first page.
 */
export function pageAtLine(cards: readonly { page: number; top: number }[], line: number, atTop: boolean): number {
  if (cards.length === 0) return 1;
  let page = cards[0].page;
  if (atTop) return page;
  for (const card of cards) {
    if (card.top > line) break;
    page = card.page;
  }
  return page;
}

interface ReadingPageDeps {
  /** The scroller. */
  root: HTMLElement;
  /** The [data-page] cards, read at each update. */
  cards: () => readonly HTMLElement[];
  /** True while a page the view was moved to holds the counter. */
  isPinned: () => boolean;
  setPage: (page: number) => void;
  /** Injected in tests. */
  ResizeObserver?: typeof ResizeObserver;
  raf?: (cb: FrameRequestCallback) => number;
  cancelRaf?: (id: number) => void;
}

/**
 * Keeps the page at the reading line up to date: it is read from the cards' positions at the
 * start, on scroll, and whenever the scroller or a card changes size (a skeleton that becomes a
 * page, the toolbar growing, a window resize). Returns the cleanup.
 */
export function trackReadingPage(deps: ReadingPageDeps): () => void {
  const { root, cards, isPinned, setPage } = deps;
  const Observer = deps.ResizeObserver ?? ResizeObserver;
  const raf = deps.raf ?? ((cb: FrameRequestCallback) => window.requestAnimationFrame(cb));
  const cancelRaf = deps.cancelRaf ?? ((id: number) => window.cancelAnimationFrame(id));

  let frame: number | null = null;
  let stopped = false;

  const update = () => {
    frame = null;
    if (stopped || isPinned()) return;
    const box = root.getBoundingClientRect();
    const tops = cards().map((c) => ({ page: Number(c.dataset.page), top: c.getBoundingClientRect().top }));
    setPage(pageAtLine(tops, box.top + READING_LINE * box.height, root.scrollTop <= 0));
  };
  const schedule = () => {
    if (frame === null) frame = raf(update);
  };

  const observer = new Observer(schedule);
  observer.observe(root);
  cards().forEach((c) => observer.observe(c));
  root.addEventListener("scroll", schedule, { passive: true });
  schedule();

  return () => {
    stopped = true;
    root.removeEventListener("scroll", schedule);
    observer.disconnect();
    if (frame !== null) cancelRaf(frame);
    frame = null;
  };
}
