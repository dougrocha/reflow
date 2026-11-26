import { describe, expect, test } from "bun:test";
import { Gate } from "./gate";

const deferred = <T = void>() => {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
};
const tick = () => new Promise((r) => setTimeout(r, 0));

describe("Gate", () => {
  test("runs one call at a time and queues the next in order", async () => {
    const gate = new Gate({ concurrent: 1, waiting: 2, perMinute: 100 });
    const order: string[] = [];
    const a = deferred();
    const first = gate.run(async () => (order.push("a"), await a.promise, "A"));
    const second = gate.run(async () => (order.push("b"), "B"));
    await tick();
    expect(order).toEqual(["a"]);
    a.resolve();
    expect(await first).toEqual({ ran: "A" });
    expect(await second).toEqual({ ran: "B" });
    expect(order).toEqual(["a", "b"]);
  });

  test("refuses as busy when the queue is full", async () => {
    const gate = new Gate({ concurrent: 1, waiting: 1, perMinute: 100 });
    const hold = deferred();
    const first = gate.run(() => hold.promise);
    const second = gate.run(async () => 1);
    const third = await gate.run(async () => 2);
    expect(third).toEqual({ busy: expect.any(Number) });
    hold.resolve();
    await first;
    expect(await second).toEqual({ ran: 1 });
  });

  test("refuses when the minute's budget is spent and recovers after it", async () => {
    let t = 0;
    const gate = new Gate({ concurrent: 1, waiting: 1, perMinute: 2, now: () => t });
    await gate.run(async () => 1);
    t = 10_000;
    await gate.run(async () => 2);
    const refused = await gate.run(async () => 3);
    expect(refused).toEqual({ busy: 50 });
    t = 60_000;
    expect(await gate.run(async () => 4)).toEqual({ ran: 4 });
  });

  test("a charge spends one call, run takes it over without a second, and refund gives it back", async () => {
    const gate = new Gate({ concurrent: 1, waiting: 1, perMinute: 2, now: () => 0 });
    const a = gate.charge();
    expect(a).toEqual({ charged: 0 });
    expect(await gate.run(async () => 1, undefined, 0)).toEqual({ ran: 1 });
    const b = gate.charge();
    expect("charged" in b && b.charged).toBe(0);
    expect(gate.charge()).toEqual({ busy: 60 });
    gate.refund(0);
    expect(gate.charge()).toEqual({ charged: 0 });
  });

  test("run refunds a charge when the caller had already gone", async () => {
    const gate = new Gate({ concurrent: 1, waiting: 1, perMinute: 1, now: () => 0 });
    const stamp = gate.charge();
    expect(stamp).toEqual({ charged: 0 });
    const gone = new AbortController();
    gone.abort();
    await expect(gate.run(async () => 1, gone.signal, 0)).rejects.toThrow();
    expect(gate.charge()).toEqual({ charged: 0 });
  });

  test("run refunds a charge it could not run: queue full", async () => {
    const gate = new Gate({ concurrent: 1, waiting: 0, perMinute: 2, now: () => 0 });
    const hold = deferred();
    const first = gate.run(() => hold.promise);
    await tick();
    const spent = gate.charge();
    expect(spent).toEqual({ charged: 0 });
    expect(await gate.run(async () => 2, undefined, 0)).toEqual({ busy: 10 });
    expect(gate.charge()).toEqual({ charged: 0 }); // the refused call's charge came back
    hold.resolve();
    await first;
  });

  test("a failing call releases the slot", async () => {
    const gate = new Gate({ concurrent: 1, waiting: 1, perMinute: 100 });
    await expect(gate.run(async () => { throw new Error("boom"); })).rejects.toThrow("boom");
    expect(await gate.run(async () => "ok")).toEqual({ ran: "ok" });
  });

  test("a waiting call that aborts leaves the queue and rejects with the reason", async () => {
    const gate = new Gate({ concurrent: 1, waiting: 1, perMinute: 100 });
    const hold = deferred();
    const first = gate.run(() => hold.promise);
    const controller = new AbortController();
    let ran = false;
    const waiting = gate.run(async () => ((ran = true), 1), controller.signal);
    const reason = new Error("gone");
    controller.abort(reason);
    await expect(waiting).rejects.toBe(reason);
    // its queue place is free again
    const next = gate.run(async () => 2);
    hold.resolve();
    await first;
    expect(await next).toEqual({ ran: 2 });
    expect(ran).toBe(false);
  });

  test("an already aborted signal rejects without running", async () => {
    const gate = new Gate({ concurrent: 1, waiting: 1, perMinute: 100 });
    const signal = AbortSignal.abort(new Error("early"));
    await expect(gate.run(async () => 1, signal)).rejects.toThrow("early");
    expect(await gate.run(async () => 2)).toEqual({ ran: 2 });
  });

  test("a call that waits past maxWaitMs is refused as busy and gives its place up", async () => {
    const gate = new Gate({ concurrent: 1, waiting: 1, perMinute: 100, maxWaitMs: 20 });
    const hold = deferred();
    const first = gate.run(() => hold.promise);
    const waited = await gate.run(async () => 1);
    expect(waited).toEqual({ busy: 1 });
    hold.resolve();
    await first;
    expect(await gate.run(async () => 3)).toEqual({ ran: 3 });
  });
});
