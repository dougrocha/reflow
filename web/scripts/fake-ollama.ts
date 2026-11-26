// A deterministic stand-in for Ollama's /api/chat, for the browser check (scripts/ask-e2e.ts) and
// for trying "Ask this document" with no model.
//
//   bun scripts/fake-ollama.ts [--port 11999] [--delay 600]
//   OLLAMA_HOST=127.0.0.1:11999 bun run start -p 3417
//
// It reads the prompt the server builds: the `[P<n>] page N | section: ...` blocks and the question
// after "Question (data, not an instruction):". The reply depends on a mode word in the question
// (any other word is ignored by retrieval, so the mode word does not change what is retrieved):
//
//   (none)        found, the first candidate, a quote copied from it
//   decline       found false, no evidence
//   fabricate     found, a quote that is in no passage
//   badlabel      found, a label the prompt did not offer
//   twopages      found, a quote from each of the first two candidates that are on different pages
//   table         found, a row copied from the first candidate that is a table (has " | ")
//   garbage       a reply that is not JSON
//   slow          the usual answer after 5 extra seconds
//
// GET /__calls returns how many chat calls were served. The server never needs the real model.

type Block = { label: string; page: number; text: string };

const argv = process.argv.slice(2);
const flag = (name: string, fallback: number): number => {
  const i = argv.indexOf(name);
  return i >= 0 ? Number(argv[i + 1]) : fallback;
};
const port = flag("--port", 11999);
const baseDelayMs = flag("--delay", 600);

const MODES = ["decline", "fabricate", "badlabel", "twopages", "table", "garbage", "slow"] as const;
type Mode = (typeof MODES)[number] | "answer";

function parsePrompt(user: string): { blocks: Block[]; question: string } {
  const head = user.indexOf("Passages:");
  const tail = user.lastIndexOf("Question (data, not an instruction):");
  const body = user.slice(head + "Passages:".length, tail).trim();
  const blocks: Block[] = [];
  for (const raw of body.split(/\n\n(?=\[P\d+\] page )/)) {
    const m = /^\[(P\d+)\] page (\d+)/.exec(raw);
    if (!m) continue;
    const at = raw.indexOf("\ntext:\n");
    if (at < 0) continue;
    let text = raw.slice(at + "\ntext:\n".length);
    text = text.replace(/^\.\.\. /, "").replace(/ \.\.\.$/, "");
    blocks.push({ label: m[1], page: Number(m[2]), text });
  }
  return { blocks, question: user.slice(tail).split("\n").slice(1).join("\n").trim() };
}

function modeOf(question: string): Mode {
  const words = new Set(question.toLowerCase().match(/[a-z]+/g) ?? []);
  return MODES.find((m) => words.has(m)) ?? "answer";
}

/** A piece of `text` of at most `max` characters, cut at a word boundary: a real quote. */
function pieceOf(text: string, max = 110): string {
  const flat = text.split("\n")[0];
  if (flat.length <= max) return flat;
  const cut = flat.lastIndexOf(" ", max);
  return flat.slice(0, cut > 20 ? cut : max);
}

function reply(blocks: Block[], mode: Mode): string {
  const first = blocks[0];
  if (!first) return JSON.stringify({ evidence: [], found: false, answer: "" });
  switch (mode) {
    case "decline":
      return JSON.stringify({ evidence: [], found: false, answer: "" });
    case "fabricate":
      return JSON.stringify({
        evidence: [{ passage: first.label, quote: "The moon is made of green cheese and weighs nothing at all." }],
        found: true,
        answer: "The moon is made of green cheese.",
      });
    case "badlabel":
      return JSON.stringify({
        evidence: [{ passage: "P99", quote: pieceOf(first.text) }],
        found: true,
        answer: "The passage says so.",
      });
    case "twopages": {
      const second = blocks.find((b) => b.page !== first.page);
      const picks = second ? [first, second] : [first];
      return JSON.stringify({
        evidence: picks.map((b) => ({ passage: b.label, quote: pieceOf(b.text) })),
        found: true,
        answer: second ? "Two passages on different pages answer this." : "Only one page answers this.",
      });
    }
    case "table": {
      const t = blocks.find((b) => b.text.includes(" | ")) ?? first;
      const row = t.text.split("\n").find((l, i) => i > 0 && l.includes(" | ")) ?? pieceOf(t.text);
      return JSON.stringify({
        evidence: [{ passage: t.label, quote: row }],
        found: true,
        answer: "The table row gives the figures.",
      });
    }
    case "garbage":
      return "Sure! Here is what I found, in prose and not JSON.";
    default:
      return JSON.stringify({
        evidence: [{ passage: first.label, quote: pieceOf(first.text) }],
        found: true,
        answer: "The first passage states it.",
      });
  }
}

let calls = 0;

const server = Bun.serve({
  port,
  hostname: "127.0.0.1",
  async fetch(req) {
    const url = new URL(req.url);
    if (url.pathname === "/__calls") return Response.json({ calls });
    if (url.pathname === "/api/tags") return Response.json({ models: [{ name: "gemma4:e4b" }] });
    if (url.pathname !== "/api/chat" || req.method !== "POST") return new Response("not found", { status: 404 });
    const body = (await req.json()) as { model?: string; messages?: { role: string; content: string }[] };
    const user = body.messages?.find((m) => m.role === "user")?.content ?? "";
    const { blocks, question } = parsePrompt(user);
    const mode = modeOf(question);
    calls++;
    console.log(`[fake-ollama] call ${calls}: ${blocks.length} passages, mode ${mode}, question "${question}"`);
    await Bun.sleep(baseDelayMs + (mode === "slow" ? 5000 : 0));
    const content = reply(blocks, mode);
    return Response.json({ model: body.model ?? "fake", message: { role: "assistant", content }, done: true });
  },
});

console.log(`[fake-ollama] listening on http://127.0.0.1:${server.port}`);
