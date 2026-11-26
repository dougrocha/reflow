import { describe, expect, test } from "bun:test";
import { chmodSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { staleKeys } from "./stale";

describe("staleKeys", () => {
  test("only the keys docs:build --check calls stale are stale", async () => {
    const dir = mkdtempSync(join(tmpdir(), "stale-"));
    const fake = join(dir, "bun");
    writeFileSync(fake, `#!/bin/sh\ncat <<'EOF'\n${JSON.stringify([
      { slug: "fw9", status: "current", reasons: [] },
      { slug: "fw9~haiku", status: "stale", reasons: ["the options changed"] },
      { slug: "fw9~docling", status: "unverifiable", reasons: ["no record"] },
      { slug: "other", status: "stale", reasons: [] },
    ])}\nEOF\n`);
    chmodSync(fake, 0o755);
    expect([...await staleKeys(["fw9", "fw9~haiku", "fw9~docling"], dir, fake)]).toEqual(["fw9~haiku"]);
    expect([...await staleKeys(["fw9"], dir, join(dir, "missing"))]).toEqual([]);
  });

  test("concurrent checks of the same keys share one process; a later one starts another", async () => {
    const dir = mkdtempSync(join(tmpdir(), "stale-"));
    const fake = join(dir, "bun");
    const runs = join(dir, "runs");
    writeFileSync(fake, `#!/bin/sh\necho x >> ${runs}\nsleep 0.3\necho '[{"slug":"fw9~haiku","status":"stale"}]'\n`);
    chmodSync(fake, 0o755);
    const answers = await Promise.all([
      staleKeys(["fw9", "fw9~haiku"], dir, fake), staleKeys(["fw9~haiku", "fw9"], dir, fake), staleKeys(["fw9", "fw9~haiku"], dir, fake),
    ]);
    expect(answers.map((a) => [...a])).toEqual([["fw9~haiku"], ["fw9~haiku"], ["fw9~haiku"]]);
    expect(readFileSync(runs, "utf8").trim().split("\n")).toHaveLength(1);
    await staleKeys(["fw9", "fw9~haiku"], dir, fake);
    expect(readFileSync(runs, "utf8").trim().split("\n")).toHaveLength(2);
  });

  test("a check that does not answer in time is killed and calls nothing stale", async () => {
    const dir = mkdtempSync(join(tmpdir(), "stale-"));
    const fake = join(dir, "bun");
    writeFileSync(fake, "#!/bin/sh\nexec sleep 30\n");
    chmodSync(fake, 0o755);
    const started = Date.now();
    expect([...await staleKeys(["fw9"], dir, fake, 200)]).toEqual([]);
    expect(Date.now() - started).toBeLessThan(5000);
  });
});
