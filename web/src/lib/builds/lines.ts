// The lines a converter process prints on stdout, as src/reflow/progress_lines.py defines them.
// lines.test.ts checks the two against each other.

/** The seconds each stage of a finished conversion took, and the part of each spent waiting for the
 *  GPU lease (reflow.progress_lines.timings_line). */
export type StageTimings = { seconds: Record<string, number>; gpuWait: Record<string, number> };

export type Line =
  | { kind: "status"; status: "start" | "ok" | "FAILED"; dir: string; details: string }
  | { kind: "progress"; out: string; stage: string; done: number | null; total: number | null }
  | { kind: "timings"; out: string; timings: StageTimings }
  | { kind: "other"; text: string };

/** The prefix of the details of a FAILED line from `reflow rerender` when it refuses to run. */
export const CANNOT_RERENDER = "CannotRerender";

export function parseLine(text: string): Line {
  if (text.startsWith("{")) {
    try {
      const e = JSON.parse(text) as {
        event?: string; out?: string; stage?: string; done?: number; total?: number | null;
        seconds?: Record<string, number>; gpuWait?: Record<string, number>;
      };
      if (e.event === "progress" && typeof e.out === "string" && typeof e.stage === "string") {
        return { kind: "progress", out: e.out, stage: e.stage, done: e.done ?? null, total: e.total ?? null };
      }
      if (e.event === "timings" && typeof e.out === "string" && typeof e.seconds === "object" && e.seconds !== null) {
        return { kind: "timings", out: e.out, timings: { seconds: e.seconds, gpuWait: e.gpuWait ?? {} } };
      }
    } catch {
      // not JSON: an ordinary line
    }
    return { kind: "other", text };
  }
  const [status, dir, ...rest] = text.split("\t");
  if (status === "start" || status === "ok" || status === "FAILED") {
    return { kind: "status", status, dir: dir ?? "", details: rest.join("\t") };
  }
  return { kind: "other", text };
}
