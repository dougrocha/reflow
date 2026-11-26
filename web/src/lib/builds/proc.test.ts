import { afterEach, describe, expect, test } from "bun:test";
import { spawn, type ChildProcess } from "node:child_process";
import { readdir, readFile } from "node:fs/promises";
import { realProcs } from "./proc";

// Real processes, only ones this test starts (`sleep`), each stopped by its own pid.
const started: ChildProcess[] = [];
function sleeper(args: string[] = ["30"]): ChildProcess {
  const child = spawn(args[0] === "sh" ? "sh" : "sleep", args[0] === "sh" ? args.slice(1) : args, { stdio: "ignore" });
  started.push(child);
  return child;
}
afterEach(() => {
  for (const c of started.splice(0)) if (c.pid && c.exitCode === null) c.kill("SIGKILL");
});
const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

describe("process helpers", () => {
  test("a live process has a start time, a gone one has none", async () => {
    expect(await realProcs.start(process.pid)).toBeGreaterThan(0);
    expect(await realProcs.start(2 ** 22 + 12345)).toBeNull();
    const child = sleeper(["0.01"]);
    await new Promise((r) => child.once("close", r));
    expect(await realProcs.start(child.pid!)).toBeNull();
  });

  test("this test process is not a build", async () => {
    const start = (await realProcs.start(process.pid))!;
    expect(await realProcs.isBuild({ pid: process.pid, start }, "x".repeat(6) + "-00000000", "cli")).toBe(false);
  });

  test("killTree stops the process and its children and nothing else", async () => {
    const tree = sleeper(["sh", "-c", "sleep 30 & sleep 30 & wait"]);
    const bystander = sleeper();
    await wait(300);
    const start = (await realProcs.start(tree.pid!))!;
    const kids = await childrenOf(tree.pid!);
    expect(kids).toHaveLength(2); // the two sleeps
    await realProcs.killTree({ pid: tree.pid!, start }, 2000);
    await wait(100);
    for (const pid of [tree.pid!, ...kids]) expect(await realProcs.start(pid)).toBeNull();
    expect(await realProcs.start(bystander.pid!)).not.toBeNull();
  });

  test("killTree does nothing when the start time is not the process's", async () => {
    const victim = sleeper();
    await wait(50);
    const start = (await realProcs.start(victim.pid!))!;
    await realProcs.killTree({ pid: victim.pid!, start: start + 1 }, 200);
    expect(await realProcs.start(victim.pid!)).toBe(start);
  });
});

async function childrenOf(parent: number) {
  const kids: number[] = [];
  for (const n of (await readdir("/proc")).filter((n) => /^\d+$/.test(n))) {
    const text = await readFile(`/proc/${n}/stat`, "utf8").catch(() => "");
    if (text.slice(text.lastIndexOf(")") + 2).split(" ")[1] === String(parent)) kids.push(Number(n));
  }
  return kids;
}
