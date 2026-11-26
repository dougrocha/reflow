// Loaded into the crossing check's server (NODE_OPTIONS=--require=<this file>, crossing-mark.ts). It
// holds ONE read of $CROSSING_MANIFEST, after the bytes were read, while the FIFO $CROSSING_GATE
// exists. Nothing about the bytes or the cache changes; it widens the interval between a render's read
// of a publication and the saving of the page that read went into, which is where a later publication
// and its mark can overtake it. The reader signals it has reached the gate by writing <gate>.ready; it
// goes on when the FIFO is released (lib.ts release()). Re-armed whenever the gate has been removed.
// With either variable unset it does nothing.
/* eslint-disable @typescript-eslint/no-require-imports -- a CommonJS preload */
const promises = require("node:fs/promises");
const fs = require("node:fs");
const gate = process.env.CROSSING_GATE;
const manifest = process.env.CROSSING_MANIFEST;
if (gate && manifest) {
  const realRead = promises.readFile;
  let held = false;
  promises.readFile = async function (path, ...args) {
    const contents = await realRead.call(this, path, ...args);
    if (!fs.existsSync(gate)) held = false;
    else if (!held && String(path) === manifest) {
      held = true;
      fs.writeFileSync(gate + ".ready", JSON.stringify({ time: Date.now() }));
      await realRead.call(this, gate);
    }
    return contents;
  };
}
