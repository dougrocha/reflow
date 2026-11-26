import { randomBytes } from "node:crypto";
import { constants } from "node:fs";
import { open } from "node:fs/promises";
import { transaction } from "../builds/lock";
import { parseSnapshot } from "./parse";
import { stamp } from "./validate";
import { dataRoot, decisionsDir, storePath } from "../paths";
import { MAX_LINE, NO_HEADER, toWire, VERSION, type Snapshot, type StoreRecord } from "./types";

// The store file: the write protocol and the read protocol of docs/review-decisions.md. The same
// protocol as tests/decisions_kit.py, in TypeScript. Both lock `<data root>/.decisions/coordination.lock`
// (a kernel flock through the build-process helper), not the build lock.

/** A reason to answer a request with, from a `decide` callback that refuses. */
export type Refused = { refused: string; status: 404 | 409 };

export const isRefused = (x: StoreRecord | StoreRecord[] | Refused): x is Refused => !Array.isArray(x) && "refused" in x;

const { O_APPEND, O_CREAT, O_EXCL, O_NOFOLLOW, O_RDONLY, O_WRONLY } = constants;

const line = (value: Record<string, unknown>): Buffer => {
  const bytes = Buffer.from(`${JSON.stringify(value)}\n`, "utf8");
  if (bytes.length - 1 > MAX_LINE) throw new Error(`A store line is at most ${MAX_LINE} bytes, not ${bytes.length - 1}.`);
  return bytes;
};

/** The file's bytes, read once up to its size at that moment. A missing file is empty. A symlink,
 *  or anything that is not a regular file, is an error. */
async function readBytes(path: string): Promise<Buffer> {
  let fh;
  try {
    fh = await open(path, O_RDONLY | O_NOFOLLOW);
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === "ENOENT") return Buffer.alloc(0);
    throw e;
  }
  try {
    const st = await fh.stat();
    if (!st.isFile()) throw new Error(`${path} is not a file`);
    const buf = Buffer.alloc(st.size);
    let at = 0;
    while (at < st.size) {
      const { bytesRead } = await fh.read(buf, at, st.size - at, at);
      if (bytesRead === 0) break;
      at += bytesRead;
    }
    return buf.subarray(0, at);
  } finally {
    await fh.close();
  }
}

/** The read protocol: the file once, cut after its last `\n`, the torn tail and the unreadable
 *  lines counted, the header required. Throws for a symlink or a path that is not a file. */
export async function readStore(slug: string, root: string = dataRoot()): Promise<Snapshot> {
  return parseSnapshot(await readBytes(storePath(root, slug)));
}

async function writeAll(path: string, flags: number, data: Buffer): Promise<void> {
  const fh = await open(path, flags | O_NOFOLLOW, 0o644);
  try {
    const { bytesWritten } = await fh.write(data, 0, data.length);
    if (bytesWritten !== data.length) throw new Error("A short write to the decision store.");
    await fh.sync();
  } finally {
    await fh.close();
  }
}

/** Make sure the file has its header: create it with `wx` and write the header, or, when an
 *  earlier writer died between creating the file and writing, add the header to the empty file. */
async function ensureHeader(path: string, slug: string): Promise<void> {
  const header = line({ v: VERSION, kind: "store", id: randomBytes(8).toString("hex"), slug, created: stamp() });
  try {
    await writeAll(path, O_WRONLY | O_CREAT | O_EXCL, header);
    return;
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== "EEXIST") throw e;
  }
  if ((await readBytes(path)).length === 0) await writeAll(path, O_WRONLY | O_APPEND, header);
}

/**
 * Append one record (or the several of one request, in one write), durably, and answer only after `fsync`. Under the store folder's own lock:
 * create the header if the file is missing; if the last byte is not `\n`, a writer died mid-line,
 * so write one `\n` first; write the record line with one `write` on an `O_APPEND | O_NOFOLLOW`
 * descriptor; `fsync`; close.
 *
 * `next` is the record, or a function of the store as it is under the lock that returns the record
 * or a refusal, so a check ("is this decision already undone?") and the write cannot be separated
 * by another writer. A refusal writes nothing, not even a header.
 */
export async function append(
  slug: string,
  next: StoreRecord | StoreRecord[] | ((current: Snapshot) => StoreRecord | StoreRecord[] | Refused),
  root: string = dataRoot(),
): Promise<StoreRecord[] | Refused> {
  const path = storePath(root, slug);
  return transaction(decisionsDir(root), async () => {
    const before = parseSnapshot(await readBytes(path));
    if (before.error === NO_HEADER) {
      throw new Error("The decision store has no valid header; nothing written.");
    }
    const chosen = typeof next === "function" ? next(before) : next;
    if (isRefused(chosen)) return chosen;
    const records = Array.isArray(chosen) ? chosen : [chosen];
    const data = Buffer.concat(records.map((r) => line(toWire(r)))); // too long: before anything is written
    await ensureHeader(path, slug);
    const bytes = await readBytes(path);
    const repair = bytes.length > 0 && bytes[bytes.length - 1] !== 0x0a ? Buffer.from("\n") : Buffer.alloc(0);
    await writeAll(path, O_WRONLY | O_APPEND, Buffer.concat([repair, data]));
    return records;
  });
}
