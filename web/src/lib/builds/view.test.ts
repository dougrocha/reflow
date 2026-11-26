import { describe, expect, test } from "bun:test";
import { newJob } from "./record";
import {
  announcements, behind, behindParam, formatAgo, Freshness, lowest, parseBehind, applyDecisionsView, pollPath, progressOf, queuePosition, statusView, STUCK_POLLS, type BuildsData, type JobRecord,
} from "./view";

const NOW = 10_000_000;
const job = (slug: string, fields: Partial<JobRecord> = {}, at = 1) => newJob(slug, "ui", at, fields);
const data = (over: Partial<BuildsData> = {}): BuildsData => ({ concurrency: 1, active: [], recent: [], built: {}, published: {}, ...over });

describe("a rerender job says it is applying decisions", () => {
  const rerender = (fields: Partial<JobRecord> = {}) => job("fw9", { kind: "rerender", ...fields });
  test("running, failed, cancelled and interrupted", () => {
    expect(statusView(data({ active: [rerender({ state: "running", stage: "Rendering" })] }), "fw9", NOW))
      .toMatchObject({ tone: "running", text: "Applying decisions: Rendering" });
    expect(statusView(data({ active: [rerender({ state: "running" })] }), "fw9", NOW).text).toBe("Applying decisions");
    const ended = (state: JobRecord["state"], error: string | null = null) => data({ recent: [rerender({ state, finishedAt: NOW, error })] });
    expect(statusView(ended("failed", "bad"), "fw9", NOW).text).toBe("Applying decisions failed: bad");
    expect(statusView(ended("cancelled"), "fw9", NOW).text).toBe("Applying decisions cancelled");
    expect(statusView(ended("interrupted"), "fw9", NOW).text).toBe("Applying decisions interrupted");
  });

  test("announcements name it", () => {
    const seen = new Map<string, string>();
    const queued = rerender();
    announcements(seen, data({ active: [queued] }), true);
    expect(announcements(seen, data({ active: [{ ...queued, state: "running" }] }), false)).toEqual(["fw9: applying decisions."]);
    expect(announcements(seen, data({ recent: [{ ...queued, state: "done", finishedAt: Date.now() }] }), false)).toEqual(["fw9: decisions applied."]);
  });

  test("a build still reads as Building", () => {
    expect(statusView(data({ active: [job("fw9", { state: "running" })] }), "fw9", NOW).text).toBe("Building");
  });
});

describe("the Apply decisions button", () => {
  test("idle: ready; any active job of the slug, or a request in flight, makes it busy and says why", () => {
    expect(applyDecisionsView(data(), "fw9", false)).toEqual({ busy: false, label: "Apply decisions" });
    expect(applyDecisionsView(data({ recent: [job("fw9", { state: "failed" })] }), "fw9", false).busy).toBe(false);
    expect(applyDecisionsView(data(), "fw9", true)).toEqual({ busy: true, label: "Starting…" });
    expect(applyDecisionsView(data({ active: [job("fw9", { kind: "rerender" })] }), "fw9", false)).toEqual({ busy: true, label: "Queued" });
    expect(applyDecisionsView(data({ active: [job("fw9", { kind: "rerender", state: "running" })] }), "fw9", false)).toEqual({ busy: true, label: "Applying…" });
    expect(applyDecisionsView(data({ active: [job("fw9", { state: "running" })] }), "fw9", false)).toEqual({ busy: true, label: "Building…" });
    expect(applyDecisionsView(data({ active: [job("resnet", { state: "running" })] }), "fw9", false).busy).toBe(false);
  });
});

describe("status of a document", () => {
  test("never built, and built some time ago", () => {
    expect(statusView(data(), "fw9", NOW)).toMatchObject({ tone: "unbuilt", text: "Not built" });
    expect(statusView(data({ built: { fw9: NOW - 5 * 60_000 } }), "fw9", NOW)).toMatchObject({ tone: "built", text: "Built 5 min ago" });
  });

  test("queued shows its place; running shows stage and progress", () => {
    const a = job("fw9", { state: "running", stage: "Rendering page images", done: 12, total: 52 }, 1);
    const b = job("resnet", {}, 2);
    const c = job("p17", {}, 3);
    const d = data({ active: [a, b, c] });
    expect(statusView(d, "resnet", NOW).text).toBe("Queued, next");
    expect(statusView(d, "p17", NOW).text).toBe("Queued, number 2");
    expect(queuePosition(d, c)).toBe(2);
    expect(queuePosition(d, a)).toBeNull();
    const running = statusView(d, "fw9", NOW);
    expect(running).toMatchObject({ tone: "running", text: "Building: Rendering page images" });
    expect(running.progress).toEqual({ value: 12, max: 52, text: "12 of 52" });
  });

  test("a running job with no total is indeterminate", () => {
    expect(progressOf(job("fw9", { state: "running", stage: "Writing HTML" }))).toEqual({ value: null, max: null, text: "In progress" });
    expect(progressOf(job("fw9", { state: "running" }))?.text).toBe("Starting");
    expect(progressOf(job("fw9"))).toBeNull();
  });

  test("a failed build shows its error until the document is built again", () => {
    const failed = job("fw9", { state: "failed", error: "ValueError: bad", finishedAt: NOW - 1000 });
    expect(statusView(data({ recent: [failed] }), "fw9", NOW)).toMatchObject({ tone: "failed", text: "Build failed: ValueError: bad" });
    expect(statusView(data({ recent: [failed], built: { fw9: NOW } }), "fw9", NOW).tone).toBe("built");
    expect(statusView(data({ recent: [failed], built: { fw9: NOW - 60_000 } }), "fw9", NOW).tone).toBe("failed");
  });

  test("cancelled and interrupted are named", () => {
    for (const [state, text] of [["cancelled", "Build cancelled"], ["interrupted", "Build interrupted"]] as const) {
      expect(statusView(data({ recent: [job("fw9", { state, finishedAt: NOW })] }), "fw9", NOW).text).toBe(text);
    }
  });

  test("ages", () => {
    expect(formatAgo(NOW - 10_000, NOW)).toBe("just now");
    expect(formatAgo(NOW - 3 * 3_600_000, NOW)).toBe("3 h ago");
    expect(formatAgo(NOW - 24 * 3_600_000, NOW)).toBe("1 day ago");
    expect(formatAgo(NOW - 5 * 86_400_000, NOW)).toBe("5 days ago");
  });
});

describe("announcements", () => {
  test("nothing on the first look, a sentence per state change, none for progress", () => {
    const seen = new Map<string, string>();
    const a = job("fw9", { state: "running", stage: "Reading pages", done: 1, total: 6 });
    expect(announcements(seen, data({ active: [a] }), true)).toEqual([]);
    expect(announcements(seen, data({ active: [{ ...a, done: 2 }] }), false)).toEqual([]);
    expect(announcements(seen, data({ active: [{ ...a, done: 5, stage: "Writing HTML" }] }), false)).toEqual([]);
    const done = { ...a, state: "done" as const, finishedAt: Date.now() };
    expect(announcements(seen, data({ recent: [done] }), false)).toEqual(["fw9: built."]);
    expect(announcements(seen, data({ recent: [done] }), false)).toEqual([]);
  });

  test("a new queued job, a failure with its reason, a cancel", () => {
    const seen = new Map<string, string>();
    announcements(seen, data(), true);
    const q = job("resnet");
    expect(announcements(seen, data({ active: [q] }), false)).toEqual(["resnet: queued."]);
    const failed = { ...q, state: "failed" as const, error: "boom", finishedAt: Date.now() };
    expect(announcements(seen, data({ recent: [failed] }), false)).toEqual(["resnet: build failed. boom"]);
    const c = job("p17");
    announcements(seen, data({ active: [c] }), false);
    expect(announcements(seen, data({ recent: [{ ...c, state: "cancelled" as const, finishedAt: Date.now() }] }), false)).toEqual(["p17: build cancelled."]);
  });

  test("old history on a later look is not announced", () => {
    const seen = new Map<string, string>();
    announcements(seen, data(), true);
    const old = job("fw9", { state: "done", finishedAt: Date.now() - 3_600_000 });
    expect(announcements(seen, data({ recent: [old] }), false)).toEqual([]);
  });
});

describe("what a page shows against what is published", () => {
  test("behind: only the documents the page rendered, at another generation; 0 and none are the same", () => {
    expect(behind({ fw9: 2 }, { fw9: 2, resnet: 5 })).toEqual([]);
    expect(behind({}, { fw9: 3 })).toEqual([]); // a page with no document (/about) is never behind
    expect(behind({ fw9: 2 }, { fw9: 3 })).toEqual(["fw9"]);
    expect(behind({ resnet: 0 }, {})).toEqual([]); // rendered before any publication; none since
    expect(behind({ resnet: 0 }, { resnet: 1 })).toEqual(["resnet"]); // the build-time "Not converted" shell
    expect(behind({ fw9: 4 }, {})).toEqual(["fw9"]); // the record was reset: refresh rather than trust the old
    expect(behind({ fw9: 2, p17: 1 }, { fw9: 2, p17: 2 })).toEqual(["p17"]);
  });

  test("lowest: a page that read a document twice shows the older of the two reads", () => {
    expect(lowest([{ fw9: 3 }, { fw9: 2, p17: 1 }, { p17: 4 }])).toEqual({ fw9: 2, p17: 1 });
    expect(lowest([])).toEqual({});
  });
});

describe("when a page refreshes (Freshness)", () => {
  test("a page that is current never refreshes: on mount, on polls, on arrival", () => {
    const f = new Freshness();
    expect(f.shows({ fw9: 2 })).toBe(false);
    expect(f.polled({ fw9: 2, resnet: 7 })).toBe(false);
    expect(f.polled({ fw9: 2, resnet: 8 })).toBe(false); // another document published: not shown here
    expect(f.arrived()).toBe(false);
    expect(f.behind).toBe(false);
  });

  test("nothing is known before the first poll answer", () => {
    const f = new Freshness();
    expect(f.shows({ fw9: 1 })).toBe(false);
    expect(f.arrived()).toBe(false);
    expect(f.behind).toBe(false);
    expect(f.polled({ fw9: 2 })).toBe(true);
  });

  test("a refresh that comes back still behind waits for the next poll, which retries, until it is current", () => {
    const f = new Freshness();
    f.shows({ fw9: 1 });
    expect(f.polled({ fw9: 2 })).toBe(true); // refresh 1
    expect(f.polled({ fw9: 2 })).toBe(false); // in flight: never a second at once
    expect(f.shows({ fw9: 1 })).toBe(false); // its render: still the old data (not marked stale yet, or served stale)
    expect(f.landed()).toBe(true); // a poll answered while it was in flight: one more try
    expect(f.shows({ fw9: 1 })).toBe(false);
    expect(f.landed()).toBe(false); // no poll since: wait, no loop
    expect(f.behind).toBe(true);
    expect(f.polled({ fw9: 2 })).toBe(true); // retried on the next poll, however many times it takes
    expect(f.shows({ fw9: 2 })).toBe(false);
    expect(f.landed()).toBe(false);
    expect(f.polled({ fw9: 2 })).toBe(false); // current now
    expect(f.behind).toBe(false);
  });

  test("arriving at another path gives one chance: Back to a page read before a publication refreshes once", () => {
    const f = new Freshness();
    f.shows({ fw9: 3 });
    expect(f.polled({ fw9: 3 })).toBe(false);
    expect(f.shows({})).toBe(false); // left for /about
    expect(f.polled({ fw9: 4 })).toBe(false); // published while away: /about shows no document
    expect(f.arrived()).toBe(false);
    expect(f.shows({ fw9: 3 })).toBe(true); // Back: the router's cached copy, read at 3
    f.shows({ fw9: 4 });
    expect(f.landed()).toBe(false);
  });

  test("polls quickly while behind, then at the idle pace if the new data never comes; never stops retrying", () => {
    const f = new Freshness();
    f.shows({ fw9: 1 });
    expect(f.hurry).toBe(false);
    const quick: boolean[] = [];
    for (let i = 0; i < 14; i++) {
      expect(f.polled({ fw9: 2 })).toBe(true);
      quick.push(f.hurry);
      f.shows({ fw9: 1 });
      f.landed();
    }
    expect(quick).toEqual([...Array(10).fill(true), ...Array(4).fill(false)]);
    f.polled({ fw9: 2 });
    f.shows({ fw9: 2 });
    f.landed();
    expect(f.polled({ fw9: 2 })).toBe(false);
    expect(f.hurry).toBe(false);
    f.polled({ fw9: 3 }); // a later publication starts quick again
    expect(f.hurry).toBe(true);
  });

  test("a navigation whose data streams in after the arrival still gets its chance", () => {
    const f = new Freshness();
    f.polled({ resnet: 2 });
    expect(f.arrived()).toBe(false); // the shell is in; its document data is not
    expect(f.shows({ resnet: 1 })).toBe(true);
  });

  test("a publication of a document the page does not show refreshes nothing; one it shows does, once", () => {
    const f = new Freshness();
    f.shows({ fw9: 5, p17: 2 });
    f.polled({ fw9: 5, p17: 2 });
    expect(f.polled({ fw9: 5, p17: 2, resnet: 1 })).toBe(false);
    expect(f.polled({ fw9: 6, p17: 2, resnet: 1 })).toBe(true);
    expect(f.shows({ fw9: 6, p17: 2 })).toBe(false);
    expect(f.landed()).toBe(false);
  });
});

describe("telling the server a page is stuck (Freshness.report)", () => {
  /** One poll answer, and the refresh it asks for coming back with the same old data. */
  const staleRound = (f: Freshness, published: Record<string, number>, rendered: Record<string, number>) => {
    f.polled(published);
    f.shows(rendered);
    f.landed();
  };

  test("nothing until the page has been behind for STUCK_POLLS poll answers, then what it rendered of the documents it is behind on", () => {
    const f = new Freshness();
    f.shows({ fw9: 1, p17: 4 });
    expect(f.report()).toEqual({}); // before the first poll
    f.polled({ fw9: 1, p17: 4 });
    expect(f.report()).toEqual({}); // current
    for (let i = 1; i < STUCK_POLLS; i++) {
      staleRound(f, { fw9: 2, p17: 4 }, { fw9: 1, p17: 4 });
      expect(f.report()).toEqual({}); // the stale answers the server and the browser give on the way
    }
    staleRound(f, { fw9: 2, p17: 4 }, { fw9: 1, p17: 4 });
    expect(f.behind).toBe(true);
    expect(f.report()).toEqual({ fw9: 1 }); // only the document it is behind on, at the generation it rendered
  });

  test("the usual way to catch up never reports: two stale answers, then the new data", () => {
    const f = new Freshness();
    f.shows({ fw9: 1 });
    f.polled({ fw9: 1 });
    staleRound(f, { fw9: 2 }, { fw9: 1 });
    staleRound(f, { fw9: 2 }, { fw9: 1 });
    f.polled({ fw9: 2 }); // the third answer; its refresh brings generation 2
    f.shows({ fw9: 2 });
    f.landed();
    expect(f.behind).toBe(false);
    expect(f.report()).toEqual({});
    f.polled({ fw9: 2 });
    expect(f.report()).toEqual({});
  });

  test("a page that is current again stops reporting, and counts from zero for the next publication", () => {
    const f = new Freshness();
    f.shows({ fw9: 1 });
    for (let i = 0; i <= STUCK_POLLS; i++) staleRound(f, { fw9: 2 }, { fw9: 1 });
    expect(f.report()).toEqual({ fw9: 1 });
    f.shows({ fw9: 2 });
    expect(f.report()).toEqual({});
    f.polled({ fw9: 2 });
    f.polled({ fw9: 3 });
    expect(f.report()).toEqual({}); // one answer behind: not stuck
  });

  test("generation 0, the build-time page of a document not converted then, is reported as 0", () => {
    const f = new Freshness();
    f.shows({ resnet: 0 });
    for (let i = 0; i <= STUCK_POLLS; i++) staleRound(f, { resnet: 2 }, { resnet: 0 });
    expect(f.report()).toEqual({ resnet: 0 });
  });
});

describe("the poll's behind query", () => {
  const known = (slug: string) => ["fw9", "resnet", "cdc-firstline-slides-es"].includes(slug);

  test("a report survives the trip, slug names with hyphens included", () => {
    const report = { fw9: 154, "cdc-firstline-slides-es": 0 };
    expect(parseBehind(new URLSearchParams(pollPath(report).split("?")[1]).get("behind"), known)).toEqual(report);
    expect(pollPath({})).toBe("/api/builds");
  });

  test("junk is dropped: unknown slugs, signs, fractions, exponents, empty parts, and more than max pairs", () => {
    expect(parseBehind(null, known)).toEqual({});
    expect(parseBehind("", known)).toEqual({});
    expect(parseBehind("nope:1,fw9:-3,fw9:1.5,fw9:1e3,fw9:,:4,fw9,resnet:abc,fw9:99999999999999999999", known)).toEqual({});
    expect(parseBehind("fw9:7,nope:1,resnet:0,,", known)).toEqual({ fw9: 7, resnet: 0 });
    expect(parseBehind("fw9:9,fw9:3", known)).toEqual({ fw9: 3 }); // the lowest: what the page is sure to show
    expect(parseBehind("fw9:1,resnet:2", known, 1)).toEqual({ fw9: 1 });
    expect(behindParam({ fw9: 1, resnet: 2 })).toBe("fw9:1,resnet:2");
  });
});
