import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { chmodSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { GET } from "@/app/api/variants/[slug]/route";
import { Runner } from "@/lib/builds/runner";
import { BuildStore } from "@/lib/builds/store";
import { makeBuilds, setBuildsForTests } from "@/lib/builds/system";
import { FakeProcs, fakeSpawn, removeDir, tempDir } from "@/lib/builds/testkit";

// GET /api/variants/<slug>?fresh=1 runs the provenance check: a process. A fake `bun` counts its runs.

let dir: string;
let runs: string;
const SAVED = { ...process.env };

beforeEach(async () => {
  dir = await tempDir();
  runs = join(dir, "runs");
  const fake = join(dir, "bun");
  writeFileSync(fake, `#!/bin/sh\necho x >> ${runs}\nsleep 0.3\necho '[{"slug":"fw9~haiku","status":"stale"}]'\n`);
  chmodSync(fake, 0o755);
  process.env.REFLOW_BUILDS_BUN = fake;
  for (const key of ["fw9", "fw9~haiku"]) {
    mkdirSync(join(dir, "docs", key), { recursive: true });
    writeFileSync(join(dir, "docs", key, "manifest.json"), "{}");
  }
  const procs = new FakeProcs();
  const store = new BuildStore({ dir, procs });
  setBuildsForTests(makeBuilds({
    store, runner: new Runner(store, { concurrency: 1, spawn: fakeSpawn(procs).spawn }),
    docsRoot: join(dir, "docs"), slugs: () => ["fw9"], revalidate: async () => {},
  }));
});
afterEach(async () => {
  setBuildsForTests(undefined);
  process.env = { ...SAVED };
  await removeDir(dir);
});

const fresh = () => GET(new Request("http://localhost:3447/api/variants/fw9?fresh=1", { headers: { host: "localhost:3447" } }), { params: Promise.resolve({ slug: "fw9" }) } as never);
const states = async (r: Response) => Object.fromEntries(((await r.json()).variants as { key: string; state: string }[]).map((v) => [v.key, v.state]));
const spawned = () => (existsSync(runs) ? readFileSync(runs, "utf8").trim().split("\n").length : 0);

describe("?fresh=1", () => {
  test("builds off: answers as without it and runs nothing", async () => {
    delete process.env.REFLOW_BUILDS;
    const r = await fresh();
    expect(r.status).toBe(200);
    expect((await r.json()).builds).toBe(false);
    expect(spawned()).toBe(0);
  });

  test("builds on: two concurrent requests run one check, and the stale variant says so", async () => {
    process.env.REFLOW_BUILDS = "1";
    const [a, b] = await Promise.all([fresh(), fresh()]);
    expect(await states(a)).toMatchObject({ fw9: "built", "fw9~haiku": "stale" });
    expect(await states(b)).toMatchObject({ "fw9~haiku": "stale" });
    expect(spawned()).toBe(1);
  });
});
