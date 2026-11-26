// The owner of one spawned Ollama's process group: a small process of its own, so the group is
// stopped however the Next server ends, even when the server's JavaScript never runs again (a
// terminal closed, Ctrl+C while its event loop is busy and next dev SIGKILLs it, an OOM kill).
//
// Run as `node -e WATCHDOG <graceMs>` with OLLAMA_HOST set, and pipes for stdin, stdout and stderr.
// It is not detached, so it stays in the server's process group, which is the terminal's job: the
// tty's SIGINT (Ctrl+C), SIGQUIT (Ctrl+\) and SIGHUP reach it directly.
//
// - It starts `nice -n 10 ollama serve` detached: the server leads a new session and process group,
//   which its model runners inherit, and nothing from the terminal reaches that group.
// - stdout, one JSON object per line: {"pid":N} once the server is spawned (N is the `ollama serve`
//   process, as `nice` execs it, and the group's number); {"exit":{"code","signal"}} once it has
//   exited and what was left of its group has been SIGKILLed; {"error":"..."} if it could not be
//   spawned. The watchdog exits after either of the last two.
// - stderr: the server's output, as it comes.
// - It stops the group on "stop" from stdin, at the end of stdin (the server that spawned it has
//   exited, however it died), and on SIGINT, SIGTERM, SIGHUP or SIGQUIT: SIGTERM to the group, then
//   SIGKILL after graceMs if the server is still running. Any of these after the first does nothing.
// - The group's number is the server's PID, which the kernel cannot reuse while the watchdog has
//   not reaped it. The group is signalled only before that, and once in the callback that reaps it,
//   when only members still in the group (which keep its number taken) can receive the SIGKILL.
//
// A string, not a function's text, so no bundler or minifier (webpack, Turbopack) can change it.

export const WATCHDOG = String.raw`"use strict";
process.title = "reflow-ollama-watchdog";
const { spawn } = require("node:child_process");
const { writeSync } = require("node:fs");
const graceMs = Number(process.argv[1]);
const write = (fd, data) => {
  try {
    writeSync(fd, data);
  } catch {
    // the Next server has gone; the group is stopped all the same
  }
};
const tell = (message) => write(1, JSON.stringify(message) + "\n");
let server;
let reaped = false;
let stopping = false;
let status;
const signalGroup = (signal) => {
  if (reaped || !server || !server.pid) return;
  try {
    process.kill(-server.pid, signal);
  } catch {
    // the group is empty
  }
};
const stop = () => {
  if (stopping) return;
  stopping = true;
  signalGroup("SIGTERM");
  setTimeout(() => signalGroup("SIGKILL"), graceMs);
};
for (const signal of ["SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT"]) process.on(signal, stop);
process.stdin.on("data", (chunk) => {
  if (String(chunk).includes("stop")) stop();
});
process.stdin.on("end", stop);
process.stdin.on("error", stop);
server = spawn("nice", ["-n", "10", "ollama", "serve"], { detached: true, stdio: ["ignore", "pipe", "pipe"] });
server.on("error", (e) => {
  tell({ error: e.message });
  process.exit(1);
});
if (server.pid) tell({ pid: server.pid });
for (const stream of [server.stdout, server.stderr]) stream.on("data", (chunk) => write(2, chunk));
const finish = () => {
  tell({ exit: status });
  process.exit(0);
};
server.on("exit", (code, signal) => {
  signalGroup("SIGKILL");
  reaped = true;
  status = { code, signal };
  // Its last output first, unless a process outside the group still holds its pipes.
  setTimeout(finish, 1000);
});
server.on("close", finish);
`;
