import { afterEach, beforeEach, expect, test } from "bun:test";
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { concurrencyFrom, getBuilds, setBuildsForTests, setRevalidateTagForTests } from "./system";
import { removeDir, tempDir } from "./testkit";

// The production callback, not a stand-in for it: getBuilds() builds the objects the server uses,
// from the environment, and its invalidatePublished() ends in the function that calls Next's
// revalidateTag. Only revalidateTag itself is replaced (setRevalidateTagForTests), by a recorder of
// its arguments, so neither Next's cache nor a mock of its module is involved.

let dir: string;
let calls: Parameters<NonNullable<Parameters<typeof setRevalidateTagForTests>[0]>>[];
const SAVED = { ...process.env };

beforeEach(async () => {
  dir = await tempDir();
  calls = [];
  process.env.REFLOW_DATA_ROOT = dir;
  setRevalidateTagForTests((...args) => void calls.push(args));
  setBuildsForTests(undefined);
});
afterEach(async () => {
  setRevalidateTagForTests(undefined);
  setBuildsForTests(undefined);
  process.env = { ...SAVED };
  await removeDir(dir);
});

const publish = async (slug: string, generation: number) => {
  await mkdir(join(dir, ".builds"), { recursive: true });
  await writeFile(join(dir, ".builds", "publications.json"), JSON.stringify({ [slug]: { generation, publishedAt: 1, publishing: null } }));
};

// With { expire: 0 } the first refresh of an open page after a publication is a blocking cache miss,
// which Next answers 404 with an empty body, and the router then loads the whole page again: focus,
// scroll and what the reader had open are lost (docs/builds.md, Why the poll marks entries stale, not
// expired). Nothing but the argument here keeps it from coming back.
test("the production callback marks a document's reads stale with the max profile, never { expire: 0 }", async () => {
  await publish("fw9", 1);
  expect(await getBuilds().invalidatePublished()).toEqual({ fw9: 1 });
  expect(calls).toEqual([["doc:fw9", "max"]]);
});

test("page builds run three at once unless REFLOW_BUILDS_CONCURRENCY says 1 to 8", () => {
  expect(concurrencyFrom({})).toBe(3);
  expect(concurrencyFrom({ REFLOW_BUILDS_CONCURRENCY: "1" })).toBe(1);
  expect(concurrencyFrom({ REFLOW_BUILDS_CONCURRENCY: "8" })).toBe(8);
  for (const bad of ["0", "9", "2.5", "many", ""]) expect(concurrencyFrom({ REFLOW_BUILDS_CONCURRENCY: bad })).toBe(3);
});
