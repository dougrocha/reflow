/** Resolves after `ms` milliseconds. */
export const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Polls `url` until it answers with a status `ok` accepts (any below 500 by default) and
 *  resolves. Rejects with `failed()`'s message as soon as that returns one (the server's process
 *  has exited), or when `timeoutMs` passes. */
export async function waitFor(
  url: string,
  { timeoutMs = 30_000, intervalMs = 200, ok = (r: Response) => r.status < 500, failed = () => undefined as string | undefined } = {},
): Promise<void> {
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    const why = failed();
    if (why) throw new Error(why);
    if (await fetch(url).then(ok, () => false)) return;
    await Bun.sleep(intervalMs);
  }
  throw new Error(`${url} did not answer in ${timeoutMs / 1000} s`);
}
