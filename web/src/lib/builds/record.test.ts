import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { readdir, readFile, writeFile, mkdir } from "node:fs/promises";
import { join } from "node:path";
import {
  isFinished, listJobs, newJob, parseJob, prune, readJob, writeAtomic, writeJob,
} from "./record";
import { SLUG_RE } from "../../pipelines";
import { removeDir, tempDir } from "./testkit";

let dir: string;
beforeEach(async () => (dir = await tempDir()));
afterEach(() => removeDir(dir));

describe("the job record", () => {
  test("a job round-trips through its file", async () => {
    const job = newJob("fw9", "ui", 1_000, { stage: "Rendering page images", done: 3, total: 9 });
    await writeJob(dir, job);
    expect(await readJob(dir, job.id)).toEqual(job);
  });

  test("a write leaves no temp file and replaces whole files only", async () => {
    const job = newJob("fw9", "ui", 1_000);
    for (let i = 0; i < 20; i++) await writeJob(dir, { ...job, done: i });
    expect(await readdir(join(dir, "jobs"))).toEqual([`${job.id}.json`]);
    expect((await readJob(dir, job.id))?.done).toBe(19);
  });

  test("a write that fails removes its temp file and leaves the old file", async () => {
    const path = join(dir, "x.json");
    await writeFile(path, "old");
    await expect(writeAtomic(join(dir, "missing", "x.json"), "new")).rejects.toThrow();
    expect(await readFile(path, "utf8")).toBe("old");
    expect(await readdir(dir)).toEqual(["x.json"]);
  });

  test("a reader racing a writer never sees a partial file", async () => {
    const job = newJob("fw9", "ui", 1_000);
    await writeJob(dir, job);
    let seen = 0;
    const writer = (async () => {
      for (let i = 0; i < 60; i++) await writeJob(dir, { ...job, error: "x".repeat(2_000), done: i });
    })();
    const reader = (async () => {
      for (let i = 0; i < 60; i++) {
        const read = await readJob(dir, job.id);
        expect(read).not.toBeNull();
        seen++;
      }
    })();
    await Promise.all([writer, reader]);
    expect(seen).toBe(60);
  });

  test("files that are not records are skipped", async () => {
    const good = newJob("fw9", "cli", 5);
    await writeJob(dir, good);
    await writeFile(join(dir, "jobs", "garbage.json"), "{not json");
    await writeFile(join(dir, "jobs", "0000000aa-deadbeef.json"), JSON.stringify({ id: "0000000aa-deadbeef", slug: "../x" }));
    expect((await listJobs(dir)).map((j) => j.id)).toEqual([good.id]);
  });

  test("parseJob rejects bad slugs, states and ids", () => {
    const ok = newJob("fw9", "ui", 1);
    expect(parseJob(ok)).toEqual(ok);
    for (const bad of [{ ...ok, slug: "../etc" }, { ...ok, slug: "a/b" }, { ...ok, state: "weird" }, { ...ok, id: "../x" }, { ...ok, who: "bot" }, null, 3]) {
      expect(parseJob(bad)).toBeNull();
    }
    expect(SLUG_RE.test("az-eft-enrollment")).toBe(true);
    expect(SLUG_RE.test("..")).toBe(false);
  });

  test("the queue order is enqueuedAt, then id", async () => {
    const a = newJob("a1", "ui", 100);
    const b = newJob("b1", "ui", 50);
    const c = { ...newJob("c1", "ui", 100) };
    for (const j of [a, b, c]) await writeJob(dir, j);
    const [first, ...rest] = (await listJobs(dir)).map((j) => j.id);
    expect(first).toBe(b.id);
    expect(rest).toEqual([a.id, c.id].sort());
  });

  test("prune keeps the newest finished jobs and every active one", async () => {
    const jobs = Array.from({ length: 8 }, (_, i) =>
      newJob(`d${i}`, "ui", 1_000 + i, { state: i < 6 ? "done" : "running", finishedAt: i < 6 ? 2_000 + i : null }));
    for (const j of jobs) await writeJob(dir, j);
    await prune(dir, 3, 10_000);
    const left = (await listJobs(dir)).map((j) => j.slug);
    expect(left).toEqual(["d3", "d4", "d5", "d6", "d7"]);
    expect((await listJobs(dir)).filter(isFinished).length).toBe(3);
  });

  test("prune removes old temp files and keeps young ones", async () => {
    await mkdir(join(dir, "jobs"), { recursive: true });
    await writeFile(join(dir, "jobs", "a.json.1.aa.tmp"), "x");
    await prune(dir, 3, Date.now() + 120_000);
    expect(await readdir(join(dir, "jobs"))).toEqual([]);
    await writeFile(join(dir, "jobs", "b.json.1.aa.tmp"), "x");
    await prune(dir, 3, Date.now());
    expect(await readdir(join(dir, "jobs"))).toEqual(["b.json.1.aa.tmp"]);
  });
});

test("a record without a kind is a build; a record with another kind is rejected", () => {
  const base = { id: "0abcdefgh-0123abcd", slug: "fw9", who: "ui", state: "queued", enqueuedAt: 1 };
  expect(parseJob(base)?.kind).toBe("build");
  expect(parseJob({ ...base, kind: "rerender" })?.kind).toBe("rerender");
  for (const kind of ["rebuild", "", null, 3]) expect(parseJob({ ...base, kind })).toBeNull();
});
