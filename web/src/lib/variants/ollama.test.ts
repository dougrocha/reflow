import { describe, expect, test } from "bun:test";
import { remembered } from "./ollama";

describe("remembered", () => {
  test("callers that arrive while it loads share one load, and the answer is kept for the ttl", async () => {
    let loads = 0;
    let clock = 0;
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    const models = remembered(async () => { loads++; await gate; return ["a:b"]; }, 1000, () => clock);
    const asks = [models(), models(), models()];
    release();
    expect(await Promise.all(asks)).toEqual([["a:b"], ["a:b"], ["a:b"]]);
    clock = 999;
    await models();
    expect(loads).toBe(1);
    clock = 1000;
    await models();
    expect(loads).toBe(2);
  });
});
