// Runs once when the Next server starts. It starts the Ollama that "Ask this document" talks to
// (in development, or with REFLOW_ASK_OLLAMA=auto), so it is up before the first question (see
// docs/ask.md). When builds are on, it also starts the build runner, so jobs that were queued before
// a restart run without anyone opening a page (see docs/builds.md). Neither start is awaited, and
// neither depends on the other's gate.

export async function register(): Promise<void> {
  // The check must wrap the imports: Next compiles this file for the edge runtime too, and only
  // removes the imports there when they sit inside this exact condition.
  if (process.env.NEXT_RUNTIME === "nodejs") {
    const { startAskOllama } = await import("@/lib/ask");
    startAskOllama();
    const { startBuildsRunner } = await import("@/lib/builds/system");
    startBuildsRunner();
  }
}
