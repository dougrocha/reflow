import { expect, test } from "bun:test";
import { createServer } from "node:net";
import { get } from "./lib";
import { HarnessError } from "./verdict";

test("a request that never answers is a harness failure, not a hang", async () => {
  const server = createServer(() => {}); // accepts, never answers
  await new Promise<void>((ok) => server.listen(0, "127.0.0.1", ok));
  const { port } = server.address() as { port: number };
  await expect(get(`http://127.0.0.1:${port}/docs/fw9`, 200)).rejects.toBeInstanceOf(HarnessError);
  server.close();
});
