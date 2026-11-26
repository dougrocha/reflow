// Admission control for model calls: concurrency, queue depth, per-minute budget. Used by ask.ts alone.

type GateOptions = {
  concurrent: number;
  waiting: number;
  perMinute: number;
  /** Longest a call may wait in the queue before it is refused as busy. Unset: no limit. It bounds
   *  a request's total time to maxWaitMs plus one model call. */
  maxWaitMs?: number;
  /** Clock in milliseconds, injectable for tests (default Date.now). */
  now?: () => number;
};

const MINUTE_MS = 60_000;
/** The retry hint when the queue is full: roughly one model call's worth of patience. */
const QUEUE_FULL_RETRY_SECONDS = 10;

type Waiter = {
  start: () => void;
  cleanup: () => void;
};

export class Gate {
  private running = 0;
  private readonly queue: Waiter[] = [];
  /** Admission times within the last minute, oldest first. */
  private readonly admitted: number[] = [];

  constructor(readonly options: GateOptions) {}

  private now(): number {
    return (this.options.now ?? Date.now)();
  }

  /** Spends one call of the minute's budget ahead of `run`: { charged } is the stamp to hand to
   *  `run` (which then spends no second call) or to `refund`; { busy: seconds } when none is left. */
  charge(): { charged: number } | { busy: number } {
    const now = this.now();
    while (this.admitted.length > 0 && now - this.admitted[0] >= MINUTE_MS) this.admitted.shift();
    if (this.admitted.length >= this.options.perMinute) {
      const oldest = this.admitted[0] ?? now;
      return { busy: Math.max(1, Math.ceil((oldest + MINUTE_MS - now) / 1000)) };
    }
    this.admitted.push(now);
    return { charged: now };
  }

  /** Gives back a call `charge` spent and `run` has not taken over. */
  refund(stamp: number): void {
    const at = this.admitted.indexOf(stamp);
    if (at >= 0) this.admitted.splice(at, 1);
  }

  /** Runs `fn` when a slot is free. Resolves { busy: seconds-until-a-retry-may-succeed } without
   *  running when the queue is full, the minute's budget is spent, or the wait outlasts maxWaitMs.
   *  A waiting call whose `signal` aborts leaves the queue and rejects with the abort reason. The
   *  slot is released only after `fn` settles (finally). A `charged` stamp from `charge` is the
   *  call's budget; run refunds it on every exit that does not run `fn`. */
  async run<T>(fn: () => Promise<T>, signal?: AbortSignal, charged?: number): Promise<{ ran: T } | { busy: number }> {
    if (signal?.aborted) {
      if (charged !== undefined) this.refund(charged);
      signal.throwIfAborted();
    }
    let stamp = charged;
    if (stamp === undefined) {
      const spent = this.charge();
      if ("busy" in spent) return spent;
      stamp = spent.charged;
    }
    if (this.running >= this.options.concurrent && this.queue.length >= this.options.waiting) {
      this.refund(stamp);
      return { busy: QUEUE_FULL_RETRY_SECONDS };
    }

    if (this.running >= this.options.concurrent) {
      const outcome = await this.wait(stamp, signal);
      if (outcome === "timeout") {
        return { busy: Math.max(1, Math.ceil((this.options.maxWaitMs ?? 0) / 1000)) };
      }
    } else {
      this.running++;
    }
    try {
      return { ran: await fn() };
    } finally {
      this.release();
    }
  }

  /** Waits for a slot. On "slot" the caller holds it; on "timeout" it holds nothing and its
   *  budget entry is refunded; on abort it rejects, also refunded. */
  private wait(stamp: number, signal?: AbortSignal): Promise<"slot" | "timeout"> {
    return new Promise((resolve, reject) => {
      let timer: ReturnType<typeof setTimeout> | undefined;
      const leave = () => {
        waiter.cleanup();
        const at = this.queue.indexOf(waiter);
        if (at >= 0) this.queue.splice(at, 1);
        this.refund(stamp);
      };
      const onAbort = () => {
        leave();
        reject(signal?.reason);
      };
      const waiter: Waiter = {
        start: () => {
          waiter.cleanup();
          this.running++;
          resolve("slot");
        },
        cleanup: () => {
          if (timer !== undefined) clearTimeout(timer);
          signal?.removeEventListener("abort", onAbort);
        },
      };
      if (this.options.maxWaitMs !== undefined) {
        timer = setTimeout(() => {
          leave();
          resolve("timeout");
        }, this.options.maxWaitMs);
      }
      signal?.addEventListener("abort", onAbort, { once: true });
      this.queue.push(waiter);
    });
  }

  private release(): void {
    this.running--;
    this.queue.shift()?.start();
  }
}
