import { afterAll, beforeAll, describe, expect, mock, test } from "bun:test";
import { execFileSync } from "node:child_process";
import { mkdir, open, rename, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { removeDir, tempDir } from "./builds/testkit";

// The stamped reads (docs-data.ts) outside Next: cacheTag is a no-op here, so every call reads the
// files. What is checked is the coupling the cache relies on: a read's generation is never newer
// than the files read with it, whenever a publication lands. A publication writes the files first,
// then raises the generation (BuildStore.publish); the reader must read the generation first.
//
// The generation read is held open on a FIFO in place of publications.json. The test waits until
// the reader has opened it, publishes (new files, then the new generation), and only then lets the
// read finish, answering with the generation as it is at that moment: the newest a read in progress
// could see. A reader that read the files before the generation, or alongside it, would pair the
// old files with the new generation, and these tests would fail.

const cache = await import("next/cache");
mock.module("next/cache", () => ({ ...cache, cacheTag: () => {} }));

const SAVED_CWD = process.cwd();
const SAVED_ROOT = process.env.REFLOW_DATA_ROOT;
let dir: string;
let data: typeof import("./docs-data");

const docs = () => join(dir, "public", "docs", "fw9");
const own = () => join(dir, "data", "fw9");
const record = () => join(dir, "data", ".builds", "publications.json");
const generationJson = (generation: number) => JSON.stringify({ fw9: { generation, publishedAt: 1, publishing: null } });

/** A manifest.json as reflow writes it (schema/manifest.schema.json), with `n` pages. */
const manifestJson = (n: number, fields: Record<string, unknown> = {}) => JSON.stringify({
  v: `v${n}`, title: `Title ${n}`, lang: "en", dir: "ltr", pageCount: n, pageSize: [612, 792], labelOffset: 0,
  imageWidths: [800], pages: {}, chunks: [{ href: "chunks/0001.html", first: 1, last: n }],
  toc: [], textBase: 1.1, text: Array.from({ length: n }, () => [0, 0, 0]), tocComplete: true, ...fields,
});

/** The files of publication `n`: each one says which publication wrote it. */
async function writeFiles(n: number): Promise<void> {
  await mkdir(join(own(), "layout"), { recursive: true });
  await mkdir(join(own(), "trace"), { recursive: true });
  await mkdir(docs(), { recursive: true });
  await writeFile(join(docs(), "manifest.json"), manifestJson(n));
  await writeFile(join(docs(), "review.json"), JSON.stringify(Array.from({ length: n }, (_, i) => ({ id: `r${i}`, page: 1, severity: "warning" }))));
  await writeFile(join(own(), "audit.json"), JSON.stringify({ filename: "fw9.pdf", passed: false, findings: [{ id: `a${n}` }] }));
  await writeFile(join(own(), "layout", "0001.json"), JSON.stringify({ w: n, h: 1, nodes: [] }));
  await writeFile(join(own(), "trace", "0001.json"), JSON.stringify({ publication: n }));
}

/** Publication `n`, as BuildStore.publish makes it: the files, then the generation. */
async function publish(n: number, generationNow: (n: number) => Promise<void>): Promise<void> {
  await writeFiles(n);
  await generationNow(n);
}

const setGeneration = async (n: number) => {
  await writeFile(`${record()}.tmp`, generationJson(n));
  await rename(`${record()}.tmp`, record());
};

/**
 * Runs `read` with publications.json replaced by a FIFO. Once the reader has opened it, publication
 * `next` is made, then the read is answered with the generation as it is now (`next`).
 */
async function readAcrossPublication<T>(read: () => Promise<T>, next: number): Promise<T> {
  await rm(record(), { force: true });
  execFileSync("mkfifo", [record()]);
  const result = read();
  const writer = await Promise.race([
    open(record(), "w"), // resolves once the reader has the FIFO open
    new Promise<never>((_, reject) => setTimeout(() => reject(new Error("the read never opened publications.json")), 5_000)),
  ]);
  let generation = next - 1;
  await publish(next, async (n) => void (generation = n));
  await writer.write(generationJson(generation));
  await writer.close();
  const out = await result;
  await rm(record(), { force: true });
  await setGeneration(next);
  return out;
}

beforeAll(async () => {
  dir = await tempDir();
  process.env.REFLOW_DATA_ROOT = join(dir, "data");
  await mkdir(join(dir, "data", ".builds"), { recursive: true });
  await writeFiles(1);
  await setGeneration(1);
  // DOCS_ROOT is public/docs under the working folder, read once when the module loads: a copy of
  // the module of its own, loaded from the temporary folder.
  process.chdir(dir);
  data = await import(`${new URL("./docs-data.ts", import.meta.url).pathname}?stamped`);
  process.chdir(SAVED_CWD);
});

afterAll(async () => {
  process.chdir(SAVED_CWD);
  if (SAVED_ROOT === undefined) delete process.env.REFLOW_DATA_ROOT;
  else process.env.REFLOW_DATA_ROOT = SAVED_ROOT;
  await removeDir(dir);
});

describe("a stamped read", () => {
  test("carries the generation of the publication it read, and 0 before any", async () => {
    const doc = await data.readDocument("fw9");
    expect(doc).toMatchObject({ slug: "fw9", generation: 1, pages: 1, title: "Title 1", images: { version: "v1" } });
    expect(doc.review).toHaveLength(1);
    expect(doc.audit?.findings[0].id).toBe("a1");
    expect(await data.readPage("fw9", 1)).toMatchObject({ slug: "fw9", generation: 1, sidecar: { width: 1 }, trace: { publication: 1 } });
    expect((await data.readDocument("resnet")).generation).toBe(0);
    expect((await data.readDocument("resnet")).pages).toBeNull();
    expect((await data.readDocument("resnet")).title).toBeNull();
    await expect(data.readDocument("../fw9")).rejects.toThrow("unknown document");
  });

  test("the title is the manifest's: it disagrees with the registry, is null when it is empty, and a manifest that is not one reads as outdated", async () => {
    const doc = await data.readDocument("fw9");
    expect(doc.title).toBe(`Title ${doc.pages}`);
    expect(data.shownTitle(doc, { title: "Registry label" })).toBe(`Title ${doc.pages}`);
    expect(data.shownTitle({ title: null }, { title: "Registry label" })).toBe("Registry label");
    await writeFile(join(docs(), "manifest.json"), manifestJson(9, { title: "" }));
    expect((await data.readDocument("fw9")).title).toBeNull();
    await writeFile(join(docs(), "manifest.json"), JSON.stringify({ pageCount: 9, v: "v9" }));
    expect(doc.outdated).toBeNull();
    const outdated = await data.readDocument("fw9");
    expect(outdated.outdated).toContain('manifest.json does not match its schema: (root): missing "title"');
    expect({ pages: outdated.pages, title: outdated.title, review: outdated.review }).toEqual({ pages: null, title: null, review: null });
    await writeFiles(1);
  });

  test("a review item without targets reads as none, and one with two keeps both", async () => {
    const targets = [{ code: "ocr-reading-lost", box: [1, 2, 3, 4] }, { code: "ocr-band-missing", box: [5, 6, 7, 8] }];
    await writeFile(join(docs(), "review.json"), JSON.stringify([
      { id: "a", page: 1, severity: "warning" },
      { id: "b", page: 1, severity: "warning", targets },
    ]));
    const doc = await data.readDocument("fw9");
    expect(doc.review?.map((item) => item.targets)).toEqual([[], targets]);
    await writeFiles(1);
  });

  test("readDocument: a publication landing during the read never pairs old files with the new generation", async () => {
    for (let n = 2; n <= 4; n++) {
      const doc = await readAcrossPublication(() => data.readDocument("fw9"), n);
      // Every file is from publication n (the read of the files started after the generation's).
      expect({ pages: doc.pages, title: doc.title, version: doc.images.version, review: doc.review?.length, audit: doc.audit?.findings[0].id })
        .toEqual({ pages: n, title: `Title ${n}`, version: `v${n}`, review: n, audit: `a${n}` });
      expect(doc.generation).toBeLessThanOrEqual(n);
    }
  });

  test("readPage: the same for a page's sidecar and trace", async () => {
    for (let n = 5; n <= 6; n++) {
      const page = await readAcrossPublication(() => data.readPage("fw9", 1), n);
      expect({ width: page.sidecar?.width, trace: page.trace as unknown }).toEqual({ width: n, trace: { publication: n } });
      expect(page.generation).toBeLessThanOrEqual(n);
    }
  });
});
