// Putting a staged folder in place of a published one (public/docs/<slug>), so a reader of the
// folder never finds it missing.
//
// On Linux the two folders trade places in one renameat2(RENAME_EXCHANGE) call: a reader sees the
// old document or the new one, and a kill at any moment leaves one of them in place. Where that
// call is not available (another platform, or a file system without it), the old folder moves
// aside to `<to>.old` and the staged one takes its place: a reader can miss the document only
// between those two renames, and `recover` (run before the next publish) moves the old folder back
// when a kill came between them. The previous output is deleted only once the new one is in place.

import { existsSync } from "node:fs";
import { rename, rm } from "node:fs/promises";
import { dlopen, FFIType } from "bun:ffi";

const AT_FDCWD = -100;
const RENAME_EXCHANGE = 2;

type Exchange = (a: string, b: string) => boolean;

/** renameat2(RENAME_EXCHANGE) through libc, or null where it cannot be loaded. */
function loadExchange(): Exchange | null {
  if (process.platform !== "linux") return null;
  try {
    const lib = dlopen("libc.so.6", {
      renameat2: { args: [FFIType.i32, FFIType.ptr, FFIType.i32, FFIType.ptr, FFIType.u32], returns: FFIType.i32 },
    });
    const z = (s: string) => Buffer.from(`${s}\0`);
    return (a, b) => lib.symbols.renameat2(AT_FDCWD, z(a), AT_FDCWD, z(b), RENAME_EXCHANGE) === 0;
  } catch {
    return null;
  }
}

let exchange: Exchange | null | undefined;

/** Put `staging` in place of `to`, removing what was there. With `atomic: false` the exchange is
 *  not tried (a test of the fallback). */
export async function swapIn(staging: string, to: string, { atomic = true } = {}): Promise<void> {
  if (!existsSync(to)) {
    await rename(staging, to);
    return;
  }
  if (atomic) {
    exchange ??= loadExchange();
    if (exchange?.(staging, to)) {
      await rm(staging, { recursive: true, force: true }); // now the old document
      return;
    }
  }
  // Any failure of the exchange falls back here; a real error (a missing folder) shows below.
  const old = `${to}.old`;
  await rm(old, { recursive: true, force: true });
  await rename(to, old);
  try {
    await rename(staging, to);
  } catch (e) {
    await rename(old, to);
    throw e;
  }
  await rm(old, { recursive: true, force: true });
}

/** Undo what a publish killed part way left beside `to`: the old document moved aside with
 *  nothing in its place goes back, and any other `<to>.old` is removed. */
export async function recover(to: string): Promise<void> {
  const old = `${to}.old`;
  if (!existsSync(old)) return;
  if (!existsSync(to)) await rename(old, to);
  else await rm(old, { recursive: true, force: true });
}
