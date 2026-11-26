// A per-client question budget beside the gate's process-wide one. Used by ask.ts alone.

const MINUTE_MS = 60_000;
/** How many clients' records the table keeps. Past it, the one idle longest is dropped. */
const MAX_CLIENTS = 10_000;
/** The longest client key kept; a longer header value is cut, so a record's size is bounded too. */
const MAX_KEY_CHARS = 64;
/** The key of every request whose client is unknown. They share one budget. */
export const SHARED_CLIENT = "-";
const DEFAULT_PER_MINUTE = 5;

export type ClientLimitConfig = {
  perMinute: number;
  /** The request header to read the client's address from, lower case; undefined: none is trusted. */
  header: string | undefined;
};

/** Pure over `env`. REFLOW_ASK_PER_CLIENT: questions a minute for one client, a positive whole
 *  number (default 5; anything else is the default). REFLOW_ASK_CLIENT_HEADER: the name of a header
 *  that a proxy you control sets or overwrites with the client's address (default unset). */
export function readClientLimitConfig(env: Record<string, string | undefined> = process.env): ClientLimitConfig {
  const raw = (env.REFLOW_ASK_PER_CLIENT ?? "").trim();
  const n = /^\d+$/.test(raw) ? Number(raw) : 0;
  const header = (env.REFLOW_ASK_CLIENT_HEADER ?? "").trim().toLowerCase();
  return { perMinute: n > 0 ? n : DEFAULT_PER_MINUTE, header: header || undefined };
}

/** Who sent the request. Next.js 16 gives a route handler no peer address (`request.ip` is gone
 *  and the socket is not reachable), and it fills `x-forwarded-for` from the socket only when the
 *  client sent none, so every header is the client's to write. Without a configured `header`, every
 *  request is the one SHARED_CLIENT. With one, the last comma-separated entry of its value: a proxy
 *  appends to `x-forwarded-for`, so the last entry is the one the nearest proxy saw, and the client
 *  cannot change it. A missing or empty header is also SHARED_CLIENT, never a free pass. */
export function clientKey(headers: Headers, header: string | undefined): string {
  if (!header) return SHARED_CLIENT;
  const last = (headers.get(header) ?? "").split(",").pop()?.trim().toLowerCase() ?? "";
  return last === "" ? SHARED_CLIENT : last.slice(0, MAX_KEY_CHARS);
}

export class ClientLimiter {
  /** Admission times within the last minute, oldest first, by client. Ordered by each client's
   *  latest admission (a client is moved to the end when it is admitted), so the first entry is
   *  the one idle longest. */
  private readonly clients = new Map<string, number[]>();

  constructor(
    readonly options: { perMinute: number; maxClients?: number; now?: () => number },
  ) {}

  private now(): number {
    return (this.options.now ?? Date.now)();
  }

  /** Clients with a record, for tests and the log. */
  get size(): number {
    return this.clients.size;
  }

  /** Spends one of the client's calls: { charged } is the stamp to `refund`, { busy: seconds } when
   *  the client has none left. A refusal changes nothing and keeps no record. */
  charge(client: string): { charged: number } | { busy: number } {
    const now = this.now();
    this.sweep(now);
    const times = this.clients.get(client) ?? [];
    while (times.length > 0 && now - times[0] >= MINUTE_MS) times.shift();
    if (times.length >= this.options.perMinute) {
      return { busy: Math.max(1, Math.ceil((times[0] + MINUTE_MS - now) / 1000)) };
    }
    times.push(now);
    this.clients.delete(client);
    this.clients.set(client, times);
    const max = this.options.maxClients ?? MAX_CLIENTS;
    while (this.clients.size > max) {
      const oldest = this.clients.keys().next();
      if (oldest.done) break;
      this.clients.delete(oldest.value);
    }
    return { charged: now };
  }

  /** Gives back a call `charge` spent. */
  refund(client: string, stamp: number): void {
    const times = this.clients.get(client);
    if (!times) return;
    const at = times.indexOf(stamp);
    if (at >= 0) times.splice(at, 1);
    if (times.length === 0) this.clients.delete(client);
  }

  /** Drops clients whose last admission is a minute old, from the idle end. */
  private sweep(now: number): void {
    for (const [client, times] of this.clients) {
      if (times.length > 0 && now - times[times.length - 1] < MINUTE_MS) break;
      this.clients.delete(client);
    }
  }
}
