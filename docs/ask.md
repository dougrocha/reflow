# Ask this document

The viewer's sidebar has an **Ask a question** item. A reader types a question, and the server
finds the passages of the document that share its words. The server asks a model to read those
passages, then checks the model's answer against the document before it shows anything. The
reader gets the document's own words (the quote), the quote's page and section, and the place
highlighted on the page image or in the transcript. Under the quote is a short summary that the
model wrote, labelled with the model's name.

The browser never talks to a model. One server route does all the work: `POST /api/ask/<slug>`, in
`web/src/app/api/ask/[slug]/route.ts`, with the logic in `web/src/lib/ask/`. The model gets each
passage's text, page number, and section under a label such as `P1`, and it names a passage only
by that label. It never sees node ids or boxes.

## The reader sees a quote, its place, and a summary

The Ask item works as follows.

- The sidebar is an accordion with two items, **Table of contents** and **Ask a question**. The
  download and print buttons stay outside both. Either item can be open, or both. The table of
  contents starts open and Ask starts closed, and the tab keeps the choice in `sessionStorage`. On a
  narrow screen, the same accordion is in the Contents sheet. The Ask code loads when the reader
  first opens the item.
- **Alt+Shift+A** opens the Ask item if it is collapsed and focuses the question box. Enter sends
  the question.
- When an answer arrives, focus moves to the answer panel. The panel shows the quote, its page and
  section, "Summary by <model>", and a **Show on page** and a **Show in transcript** button for each
  quote. "Back to answer" returns from the highlight. **Alt+Shift+R** returns to the answer from
  anywhere.
- While a question is in flight, Submit stays focusable (`aria-disabled`). A new question replaces
  the one in flight, and Escape cancels it ("Question cancelled."). When no question is in flight,
  Escape clears the box, and a second Escape collapses the item and focuses its header. An answer
  that arrives while the item is collapsed opens the item.
- "Show in transcript" cannot mark the passage when the passage's page did not load or has no such
  block. The reader then hears "Could not find the passage in the transcript." If the page has an
  image, the viewer shows the passage on the page instead.
- When the document does not answer the question, the panel says "No answer found" and gives one
  sentence of why. When the model is off, the panel says that the assistant is not running and that
  Find still works.
- The item appears only on documents that have a `passages.jsonl`.

The reader can trust the quote and the place. The summary is the model's wording.

## The server checks every answer against the document

The server answers a question in four steps.

1. On the first question about a document, the server loads `<data root>/<slug>/passages.jsonl`
   into an in-memory BM25 index with k1 1.2 and b 0.3. It keeps at most two documents and drops
   the least recently used one first. If a document is converted again while the server runs, the
   server reloads it on the next question.
2. The server takes the best passages for the question, at most 8. It drops a passage that scores
   under 0.3 of the best score, unless fewer than 3 passages would remain. If no word of the
   question is in the document, the answer is "no answer", and the server does not call the model.
3. The server sends the question and those passages to the model and asks for JSON: quotes copied
   from named passages, then `found` and `answer`. The passages total at most 6,000 characters. For
   a passage over 900 characters, the model gets only the 900-character window with the most words
   of the question.
4. The server accepts a quote only if it finds the quote in the passage that the quote names, or in
   another offered passage. The quote that the reader sees is the document's slice, at most 240
   characters, never the model's string. The server refuses a summary that has a number in none of
   these places: the text of the cited passages and the question. The summary may also give where
   a cited passage is: "page N" for its page, "section N", "table N" or "figure N" for a number in
   its section trail or lead-in, or a trail or lead-in phrase whole. Any other number that is only
   in a trail or lead-in is refused, so a heading that holds the answer must be quoted itself. It
   reads digits of any script as ASCII digits. It also refuses a summary over 600
   characters, and it strips bidi and other control characters from the summary. Anything that
   does not check out becomes "no answer", not a shaky answer.

A heading is a passage like a paragraph. It has its own node id and box, the headings above it
as its section trail, and no lead-in. So the reader can see it quoted, and "Show on page" outlines
it. A heading that holds the answer, such as "FY 2024-25 revenue and financing $18.6 million", is
then quotable. Before, a heading was only in the section trail of the passages under it, and the
server refused every answer that took a number from the trail.

The index leaves out text that a model transcribed during conversion with
`reflow convert --assist local`, because that text is AI-drafted and not reviewed. A quote is
therefore always the document's own words. `src/reflow/render/passages.py` makes this choice.

## Each server process has its own limits

The server enforces these limits:

- One model call at a time, and at most 2 calls waiting. A call waits at most 130 s. After that,
  the server tells the reader that it is busy.
- 20 questions a minute, and 5 a minute for each client ([below](#each-client-has-its-own-budget)).
- A model timeout of 120 s.
- A cache of the last 200 answers. Identical questions in flight share one model call.
- A question of at most 300 characters, and a request body of at most 8,000 bytes. The route
  refuses a larger body while it streams in.

Every well-formed request that the cache cannot answer counts against the minute's 20 questions.
It counts before the server searches the document, so a question with no matching word counts too.
A request answered from the cache costs nothing. A request that shares another caller's identical
question gives its charge back.

## Set up a model provider

The provider is server configuration only. Readers never pick a model or bring a key.

To set up Ask, convert the documents, then start the server with a local model or with your
Anthropic API key:

```sh
cd web
# 1. Convert the documents, so each one has .reflow/<slug>/passages.jsonl
bun run docs:build resnet fw9

# 2. Local model (the default). Nothing leaves the machine.
ollama serve              # or the system service
ollama pull gemma4:e4b
bun run build && bun run start -p 3417       # never port 3000

# Or the Anthropic API with your own key, in the server environment
REFLOW_ASK_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-... bun run start -p 3417
```

The server calls the Anthropic API with a plain HTTPS request and your key. Reflow installs no SDK
for it.

## Server environment variables

The server reads these variables in `web/src/lib/ask/config.ts`, `client-limit.ts`, `ollama-supervisor.ts` and `retriever.ts`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `REFLOW_ASK_PROVIDER` | `ollama` | `ollama` or `anthropic`. |
| `REFLOW_ASK_MODEL` | `gemma4:e4b` for `ollama`, `claude-haiku-5-5` for `anthropic` | The model id for the chosen provider. |
| `REFLOW_ASK_OLLAMA` | unset | `auto` starts Ollama at server boot outside development. `off` never starts it. See [Ollama can start with the server](#ollama-can-start-with-the-server). |
| `REFLOW_ASK_OLLAMA_IDLE_MINUTES` | unset | Stops the Ollama that the app started after this many minutes with no question. When unset, that Ollama runs until the server exits. |
| `OLLAMA_HOST` | `localhost:11434` | Normalised the same way as the Python assist normalises it. |
| `ANTHROPIC_API_KEY` | none | Required for `anthropic`. Without it, readers see "questions are not set up". |
| `REFLOW_ASK_PER_CLIENT` | `5` | Questions a minute for one client, a positive whole number. Any other value gives 5. See [Each client has its own budget](#each-client-has-its-own-budget). |
| `REFLOW_ASK_CLIENT_HEADER` | unset | The name of a request header that your own proxy sets to the client's address, such as `x-real-ip` or `cf-connecting-ip`. Unset, no header is trusted and there is no per-client budget: only the 20-a-minute budget of the whole server applies. |
| `REFLOW_DATA_ROOT` | `web/.reflow` | The folder that holds the converted documents. |
| `REFLOW_ASK_RETRIEVER` | `bm25` | Experimental. `embed` or `hybrid` ranks passages with a local embedding model. See [Experimental: retrieval by embeddings](#experimental-retrieval-by-embeddings). |
| `REFLOW_ASK_EMBED_MODEL` | `qwen3-embedding:0.6b` | The Ollama embedding model for `embed` and `hybrid`. |
| `REFLOW_ASK_EMBED_CACHE` | `~/.cache/reflow/embeddings` | Where `embed` and `hybrid` cache the vectors. |

## Each client has its own budget

When `REFLOW_ASK_CLIENT_HEADER` is set, a client that has spent its 5 questions a minute is told that the server is busy, with the seconds
until its oldest question leaves the minute. It is the same answer (status 429, `Retry-After`) that
the 20-a-minute limit gives, and the viewer shows the same sentence. A client over its limit takes
nothing from the 20, so other readers still get through. A cached answer, and a question that
shares another caller's model call, count against neither. The server keeps the records of at most
10,000 clients and drops a client's record after a minute with no question.

**Who a client is.** Next.js 16 gives a route handler no peer address: `request.ip` is gone, and
the socket is out of reach. Next also fills `x-forwarded-for` from the socket only when the request
has no such header, so a client can write any `x-forwarded-for`, `x-real-ip` or similar header it
likes. The server therefore trusts no header by default, and then **there is no per-client budget**:
only the server's 20-a-minute budget applies, and one client can still spend it all.

To limit each client on its own, put a proxy that you control in front of the server, make it set
or overwrite a header with the client's address (a header the client cannot reach), and name that
header in `REFLOW_ASK_CLIENT_HEADER`. The server keys on the last comma-separated entry of the
header, which is the one a proxy appends to `x-forwarded-for`. Requests without that header, or
with an empty one, share one client's budget between them. Set the variable only when every request comes
through that proxy: a client that reaches the server directly can choose its own key.

## The route does not authenticate callers

The route has no authentication. Its checks and limits leave these gaps:

- The 20-a-minute budget, the queue, and the one-call limit apply per server process, not per
  client. One client can use all of them, and other clients then see "busy". The per-client budget
  stops that only when `REFLOW_ASK_CLIENT_HEADER` names a header that a proxy you control sets.
- The route accepts a request without a `Sec-Fetch-Site` header, such as a request from curl or a
  script. The same-origin check stops only browsers.
- With `anthropic`, **anyone who can reach the route spends your API key**, up to the budget. If
  the route is public, put your own authentication or rate limit in front of it.

## Errors give the reader a sentence and the log the detail

When something is wrong, the reader gets a short sentence, and the server log gets the detail on
an `[ask] ...` line. The log line names the fix: `ollama serve`, `ollama pull <model>`, the missing
variable, or "convert the document again". A document without `passages.jsonl` has no Ask item,
and its route answers "not prepared for questions". A page loaded before the document was
converted again gets "reload the page to ask".

## Ollama can start with the server

Under `next dev`, or with `REFLOW_ASK_OLLAMA=auto`, the `ollama` provider starts Ollama when the
Next server boots, so Ollama is up before the first question. `web/src/instrumentation.ts` makes
the call, and `web/src/lib/ask/ollama-supervisor.ts` does the work. Boot does not wait for Ollama.
In every other case, the operator runs Ollama. `REFLOW_ASK_OLLAMA=off` turns the supervisor off
everywhere. The supervisor never applies to `anthropic`.

- **Reuse first.** If `OLLAMA_HOST` is set, the supervisor uses that server and starts nothing.
  Otherwise, if `localhost:11434` answers `GET /api/version`, the supervisor uses it. The boot log
  says `[ask] using Ollama at <host>`.
- **Start.** Only when nothing answers, the supervisor runs `nice -n 10 ollama serve` with
  `OLLAMA_HOST=127.0.0.1:<free port>`. It uses the new server only after two checks pass. First,
  the child itself must hold a listening socket on that port, which the supervisor reads from
  `/proc/<pid>/fd` and `/proc/net/tcp`. Second, the child must answer `GET /api/version`. These
  checks need Linux. On another system, run `ollama serve` yourself or set `OLLAMA_HOST`. If
  another process takes the child's port, the supervisor replaces the child on a new port, up to 3
  spawns within 20 s. The log says `[ask] started Ollama pid N on 127.0.0.1:PORT`. A question that
  arrives during the start waits for the same start. After 8 s, the reader sees "Still working.
  The model may be starting up."
- **Failure.** Ollama can fail to start because `ollama` is not on `PATH`, or because the server
  exits or does not answer within 20 s. The log then gives the reason and Ollama's last lines of
  output, and a question gets "not running right now" (HTTP 503, `model-offline`). If the child
  dies later, the next question restarts it.
- **No model pull.** The supervisor does not pull the model for you. A missing model gives "not
  running right now", and the log says ``run `ollama pull <model>` ``.
- **Lifetime.** The child runs as long as the server, or until `REFLOW_ASK_OLLAMA_IDLE_MINUTES`
  pass with no question. The next question then starts it again. The supervisor keeps its state on
  `globalThis`, so a module reload after an edit reuses the running child.
- **Ownership and shutdown.** Each spawned server leads its own session and process group, and
  its model runners inherit that group. A small watchdog (`web/src/lib/ask/ollama-watchdog.ts`,
  `reflow-ollama-w` in `ps`) starts the server, and only the watchdog signals that group. The
  supervisor never stops a server that was already running. The watchdog stays in the dev server's
  process group. It stops its Ollama group when the dev server asks (an idle stop or a failed
  start), and when the dev server ends in any way, because the watchdog's stdin is a pipe from the
  server. It also stops the group on SIGINT, SIGQUIT, or SIGHUP to the terminal's job. To stop the
  group, it sends SIGTERM, then SIGKILL after 3 s. If something SIGKILLs the watchdog itself, stop
  the group with `kill -- -N`, where N is the pid in `started Ollama pid N`.

## Experimental: retrieval by embeddings

BM25 is the default and the only retriever that the server uses unless you opt in. With
`REFLOW_ASK_RETRIEVER=embed` or `hybrid`, `web/src/lib/ask/retriever.ts` ranks the passages with
an embedding model in the local Ollama (`POST /api/embed` at `OLLAMA_HOST`):

- `embed` ranks every passage by cosine similarity to the question.
- `hybrid` merges the BM25 ranking and the embedding ranking by reciprocal rank fusion.

Neither mode drops passages under a score floor, so a question with no word of the document still
goes to the model. The first question about a document embeds all its passages, which takes
minutes on a large document, and caches the vectors in `REFLOW_ASK_EMBED_CACHE` (default
`~/.cache/reflow/embeddings`) as `<model>/<slug>-<version>.f32` and `.json`. The cache is keyed by
the conversion's version, so worktrees with the same conversion share it, and a new conversion
gets new vectors. It is not in the conversion's folder, because a conversion refuses to replace a
folder that holds files it didn't write. Nothing evicts it: delete a model's folder to free the
space (about 0.5 GB for 0.6b over the golden documents and maricopa-directory). Only the
text of `passages.jsonl` is embedded, so text drafted by `--assist local` stays out. If the
embedding call fails, the server logs it and answers that question with BM25. The measurements
are in `docs/research/2026-11-ask-embeddings.md`.

## Each question sends up to 6,000 characters to the provider

Each question sends the question and at most 6,000 characters of the document's text to the
provider. That text is the best passages for the question, not the whole document.

| Provider | Where the text goes |
| --- | --- |
| `ollama` (default) | The Ollama server at `OLLAMA_HOST`, which is `localhost:11434` by default. Nothing leaves the machine unless `OLLAMA_HOST` points at another machine. |
| `anthropic` | `api.anthropic.com`, under the API terms of the owner of `ANTHROPIC_API_KEY`. Do not use it for documents with personal data. |

The browser never sees a key or a model address. The server logs the model, the time, and the
first 200 characters of an unusable reply. It writes that reply as an escaped JSON string, so a
crafted document cannot forge a log line. The server does not log the question.

## Run the checks

Run the checks from `web/`, on ports other than 3000. Each check stops only the processes that it
started.

```sh
bun run docs:build --stale resnet fw9 p17 frcp-2025   # the documents ask:e2e reads
bun run docs:build --stale scotus-mahmoud-v-taylor irs-w4-2026 uscis-i9 cdc-firstline-slides-es \
  cdc-antibiotics-trifold census-p60-273 nasa-se-handbook epa-ghg-inventory-2024   # the rest of ask:eval
bun run build                         # ask:e2e runs `next start`

bun test src/lib/ask                  # unit tests
bun run ask:e2e                       # browser check against a fake Ollama
bun run ask:eval                      # retrieval recall over the golden questions, fails under the floors
                                      # (refuses documents that are not current; see docs/provenance.md)
bun run ask:eval -- --bench <slug>    # time, memory and size of loading one document's corpus
bun run ask:eval -- --model           # the golden questions through the real model (manual)
bun run ask:eval -- --retriever hybrid --embed-model qwen3-embedding:0.6b   # the experimental retriever
bun run ask:eval -- --bench <slug> --retriever hybrid --sample 2000          # its cost, from a sample
bun run ask:ollama                    # the Ollama supervisor and watchdog with real processes (about 35 s)
```

Before you run `ask:ollama`, run `bun run docs:build resnet`. Run `ask:ollama` at normal niceness,
because it checks that the child's nice value is exactly 10. It picks an unused port, and `--port`
overrides it. Never use port 3000. It can run next to an existing Ollama on 11434.

## What each check covers

- `ask:e2e` (`scripts/ask-e2e.ts`) starts `scripts/fake-ollama.ts`, a deterministic `/api/chat`,
  and `next start`. It then drives Chromium through about 25 scenarios. They cover an answer, show
  on page, show in transcript, the shortcuts, a declined question, a made-up quote, the model off,
  a two-page answer, and a table passage. They also run axe in both view modes with forced colors
  and reduced motion, and check the heading "Show on page" and cancelling a question in flight.
  The last scenarios check the sidebar accordion: keyboard and mouse, the state kept over a reload,
  the 320 px sheet, and resizing. Screenshots go to `/tmp/reflow-ask-e2e/<time>`, and `--out`
  changes the folder. Playwright's forced-colors emulation switches the media query but does not
  recolor the page. The check proves that the forced-colors rules exist, not how they look in
  Windows High Contrast.
- `ask:ollama` (`scripts/ask-ollama-real.ts`) runs the supervisor and the watchdog against a fake
  `ollama` on `PATH`, with real signals and sockets. It checks SIGTERM then SIGKILL for a child
  that ignores SIGTERM, nice 10 and a session of its own, and runners that cannot outlive their
  server. It checks port collisions with and without `/proc` and `/proc/net/tcp6`, and a boot
  failure through `next dev` that returns 503 `model-offline`. On a real terminal (a pty running
  bash), it closes the terminal and presses Ctrl+C while the server's event loop is blocked.
- `ask:eval -- --model` calls the same `ask()` that the route calls, so the provider, the model,
  and the prompt are the server's. With `anthropic`, it refuses to run unless you also pass
  `--send-to-anthropic`. It is not part of any automated suite.

## Measured numbers

The retrieval measurements date from 2026-11-01; the model
measurements date from 2026-10-23 unless a paragraph says otherwise. Model replies vary a little
between runs, so one run is a sample.

### Retrieval finds the passage in the top 8 for 95% of questions

The golden set (`web/scripts/ask-golden.ts`, documents in `GOLDEN_SLUGS`) has 112 questions on 12
documents: 107 with an answer and 5 without. The questions are in four sets, so a change is never
judged only on the questions it was tuned on:

- **tune** has 28 questions, the original questions minus a held-out third.
- **hold** has 12 questions, every third original question, set aside before any tuning.
- **new** has 47 questions, written later from the documents' text.
- **confirm** has 20 questions, written last.

| | Questions | recall@1 | recall@8 |
| --- | --- | --- | --- |
| tune | 28 | 18 (64.3%) | 27 (96.4%) |
| hold | 12 | 9 (75.0%) | 12 (100%) |
| new | 47 | 27 (57.4%) | 44 (93.6%) |
| confirm | 20 | 15 (75.0%) | 20 (100%) |
| **all** | **107** | **69 (64.5%)** | **103 (96.3%)** |

These are from 2026-11-01. On 2026-10-28 the totals were 68 and 102. The converter changed in
between, and "phone number" moved into the top 8 (rank 5).

Making headings passages left these totals as they were: the same build with its headings left
out also scores 68 and 102. Individual ranks moved both ways, and none left the top 8.

`bun run ask:eval` fails under 58% at 1 or 90% at 8.

### Why retrieval is plain BM25 with b at 0.3

Retrieval is plain BM25 with no embeddings, so it fails when the question and the document use
different words.

`b` is 0.3, not the textbook 0.75. With 0.75, a one-line fragment that repeats the question's
words outscored the sentence that answers it, and recall@1 was 61 of 107. Such a fragment is a
list item, a callout, or a table cell under a heading.

These changes were tried and dropped, because none gained on the `new` and `confirm` sets:

- other k1 and b values
- other weights for the section trail and the lead-in
- stopwords for question filler
- stemming
- reading "%" as "percent"
- reranking the top 30 by bigrams, best window, or coverage
- an answer-type boost
- expanding the question with the top hits' headings
- changes to where `passages.py` cuts passages (making headings passages adds a passage kind and
  moves no cut point)

The 4 answerable questions not in the top 8 share no meaningful word with their passage:

- "formula" against an equation
- "how long can a deposition last" against "limited to one day of 7 hours"
- "become taxable" against "is more than $25,000"
- "what is verification", defined in one table cell of a document where 388 passages use the word

Fixing those needs a retriever that knows synonyms. Qwen3 embeddings were measured for this on
2026-11-01 and did not beat BM25 (`docs/research/2026-11-ask-embeddings.md`); they remain an
opt-in experiment, [Experimental: retrieval by embeddings](#experimental-retrieval-by-embeddings).

### The largest document loads in about 300 ms

`maricopa-directory` is the largest sample, with 94,258 passages and 22.3 MB of `passages.jsonl`.
32,631 of those passages are headings, because the directory sets each provider's name as a
heading; without headings it has 61,627 passages and 16.0 MB. The index builds in about 700 ms and
holds 18 to 61 MB of JS heap. The process's memory grew by 180 to 270 MB. A question takes about
2 ms. Measured on a busy machine with `bun run ask:eval -- --bench maricopa-directory`.

### The default model validated 38 of 40 answers

45 questions went through the real server path on a local Ollama, and 40 of them had an answer.
Retrieval put the expected passage in the top 8 for 35 of the 40. The table's columns mean the
following:

- **Validated** means that the server found the quote in the document.
- **Right** means that a cited passage holds the expected text.
- **Mis-cited** means a validated answer that cites a different passage than the golden one. Such
  an answer is often still fair.
- **Unverified** means that the model claimed an answer but the quote or a number did not check
  out, so the reader saw "no answer".

| Model | Found | Validated | Right | Mis-cited | Unverified | Declined | Refused of 5 unanswerable | Median per question |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `gemma4:e4b` (default) | 40 | 38 | 32 | 6 | 2 | 0 | 5 | 0.8 s |
| `gemma4:12b` | 38 | 38 | 34 | 4 | 0 | 2 | 5 | 1.5 s |
| `ornith-1.5:9b` | 37 | 34 | 30 | 4 | 3 | 2 | 5 | 2.0 s |

No model invented an answer to an unanswerable question. The first question of a run is slow, up
to 40 s while the model loads. After that, a question takes a second or two.

On all 107 answerable questions with the current retrieval, `gemma4:e4b` found 105 and validated
102. It was right on 95 (89%), mis-cited 7, was unverified on 3, and declined 2. It refused all 5
unanswerable questions. This is one run from 2026-10-28, after headings became passages. The run
before it, from 2026-10-23, found 104, validated 99, was right on 93, mis-cited 6, was unverified
on 5 and declined 3.

### Why `gemma4:e4b` is the default

`gemma4:e4b` validates as many answers as `gemma4:12b` at half the median time. It also uses about
4 GB less video memory, 5.7 GB against 9.9 GB (see `docs/assist.md`). If wrong places matter more
than speed, try `gemma4:12b`. `qwen3-vl:8b` does not work for Ask. It reasons at length before it
replies, so 7 of its first 18 questions failed, and its median was 35 s.

### Ask adds 6.1 KB to the first load

Ask adds 6.1 KB gzip to the first load of `/view/resnet`, measured with `bun run a11y`. The lazy
Ask chunk is 3.3 KB of that, and it loads after hydration.
