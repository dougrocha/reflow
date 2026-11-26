# Document builds in the web app

An operator can see every document build and start one from the documents list. An operator can also
watch a build's progress, queue several builds, and cancel or retry them. A build started from the
command line (`bun run docs:build resnet`) shows up in the same list as a build started from the
page.

Builds are an operator tool. They start processes on the machine that runs the server, so they are
**off unless the server is a development server or `REFLOW_BUILDS=1`**. See
[Builds are off unless you turn them on](#builds-are-off-unless-you-turn-them-on).

## Turn builds on

Builds need Linux with pidfd support, and `python3` 3.9 or newer with only the standard library.

```sh
cd web
bun run dev                               # builds are on in development
REFLOW_BUILDS=1 bun run start -H 127.0.0.1 -p 3447   # or a production build, explicitly (never port 3000)
REFLOW_BUILDS_CONCURRENCY=2 ...           # two page builds at once (default 3, from 1 to 8)
bun run docs:build fw9                    # shows up in the page as a "cli" job
```

`build-docs.ts` finds its helper, the web folder, and the default data root from its own path. You
can therefore run `bun /abs/path/web/scripts/build-docs.ts fw9` from any folder. In a worktree, the
converter uses the main checkout's venv with the current checkout on `PYTHONPATH`. It reads samples
that are missing from the worktree from the main checkout. Each document it converts also gets a
provenance record in `DATA_ROOT/.provenance/`, which `bun run docs:check` compares with the
checkout. See [docs/provenance.md](provenance.md).

## The build record lives in `.builds/`

Everything lives under `<data root>/.builds/`, which is `web/.reflow/.builds/` or
`$REFLOW_DATA_ROOT/.builds/`. `.reflow/` is gitignored and never served, and `.builds` is not a
document slug.

```
.builds/
  jobs/<id>.json        one file per job
  jobs/<id>.cancel      exists when a cancel was asked for
  claims/<slug>.json    the job that owns the document: {"job": "<id>"}
  publications.json     per document: its publication generation, and a write in progress
  coordination.lock     a kernel flock shared by every server and CLI writer
```

A job is a `JobRecord`, defined in `web/src/lib/builds/record.ts`.

| Field | Meaning |
| --- | --- |
| `id` | `<ms since epoch, base 36>-<8 random hex>`. It sorts by time and is safe as a file name. |
| `slug` | A document from `SAMPLE_DOCS`. |
| `who` | `ui` when the page queued the job, `cli` when `bun run docs:build` did. |
| `state` | `queued`, `running`, `done`, `failed`, `cancelled`, or `interrupted`. |
| `stage`, `done`, `total` | The stage that reflow is in ("Rendering page images"), and its progress in pages or chunks. `total` is null when unknown. |
| `enqueuedAt`, `startedAt`, `finishedAt` | Epoch ms. Each is null until its event happens. |
| `error` | One line, for `failed` and `interrupted`. |
| `pid`, `pidStart` | The build script (`build-docs.ts`) and its start time from `/proc/<pid>/stat`. The start time keeps a reused pid from passing as the script. |
| `runnerPid`, `runnerStart` | The server that dispatched a page job, until the script's pid is recorded. |
| `childPid`, `childStart` | The current converter or audit worker, recorded before the worker may run. |

No file stores the queue order. The queue is the queued jobs sorted by `enqueuedAt`, then by `id`.
`writeAtomic` writes every file to `<name>.<pid>.<random hex>.tmp` and renames it over the target,
so a reader sees either the old file or the new one. A reader skips a file that fails to parse.
History keeps the newest 50 finished jobs.

The server and the CLI both write through `BuildStore` (`web/src/lib/builds/store.ts`). Every
decision that reads and changes shared state holds `coordination.lock`. These decisions are
enqueue, claims, dispatch, progress, cancel, and recovery. `BuildStore` takes the lock through
`web/scripts/build-process.py`, which keeps a Linux `flock` until its pipe closes. When a writer
dies, its pipe closes, which releases the lock. There is no lease to expire. Do not delete or
replace `coordination.lock` while writers are active.

The build directory is trusted local storage on one host. It must not be writable by untrusted
users, and it must not be shared across machines.

## One build of a document at a time

`claims/<slug>.json` names the job that owns a document. In one transaction, enqueue checks the
current owner and writes the new claim before the job record. A second enqueue of the same slug
returns the existing job with `created: false`.

The claim stays while the job is queued, and while its script or its recorded worker lives. A
converter that survives its killed script therefore still keeps the document. The runner checks
the claim before dispatch. Dispatch reads the jobs, the claims and the cancel markers under the lock,
asks `schedule()` (`web/src/lib/builds/schedule.ts`, a function of plain data) which queued page jobs
to start, cancel or interrupt, and applies that plan. Before the script adopts a job, it checks the claim, the state, the
pid, and the start time. A duplicate script exits before it can start a converter.

## A job is a build or a rerender

`JobRecord.kind` is `"build"` or `"rerender"`. A record without a kind is a build; any other value
makes the record unreadable (`parseJob`). A **rerender** applies the document's recorded decisions
(`.decisions/<slug>.jsonl`, [review-decisions.md](review-decisions.md)) to the model its last
conversion saved, without converting again. The page labels it "Applying decisions".

- **Command.** `bun scripts/build-docs.ts --rerender <slug>` (one document). It follows the build
  protocol: the job and claim, the publications, the provenance record. It sets `audit.json` aside
  in memory and removes it as a publication (`reflow rerender` refuses a folder with foreign
  files), runs `reflow rerender <data>/<slug> --decisions <data>/.decisions/<slug>.jsonl
  --source <sample PDF> [--engine E] [--alt-text P] --progress-json` (with `--stage-cache`
  when the script has it), publishes the public names, and
  writes the saved audit back as a publication. The PDF is the one the audit was made from, so it
  is not run again. The rerender's own stage seconds are not published: `build-info.json` keeps the
  `timings` of the conversion it replaces (none, when that build recorded none), since a rerender
  converts nothing.
- **Fallback to a full build.** Every build passes the decisions store, so every build records a
  `build.json` that its own output can be rerendered from, with or without the stage cache. When
  there is no saved model (a document built before this, or by hand without a store) or
  `reflow rerender` exits 4 (the converter, tools, OCR data, options or sample PDF changed), the job's stage says "No saved model; running a full build" or
  "The converter changed; running a full build", and the normal build runs for the slug, which
  applies the decisions too.
- **Queue.** `POST /api/builds {slug, kind: "rerender"}` (any other kind is 400). Retry keeps the
  kind. One job per slug as before: a rerender asked for while a build is queued or running returns
  that build, which applies the decisions as well; a build asked for while a page's rerender is
  queued turns that job into a build (same id and place); while a rerender runs, it is returned.
  The runner passes `--rerender` for a rerender job. A script adopts only a job of its own kind.
- **Only on request.** A rerender starts from the *Apply decisions* button (shown beside *Rebuild*
  in the document's Decisions section when decisions are waiting) or the command. Recording a
  decision never starts a job.

## The runner starts page builds inside the Next server

`web/src/lib/builds/runner.ts` runs the queue of `ui` jobs inside the Next server. One instance
lives on `globalThis`, and `web/src/instrumentation.ts` starts it at boot. Queued jobs therefore
run after a restart even if nobody opens the page.

- **Concurrency.** `REFLOW_BUILDS_CONCURRENCY` sets the number of page builds at once, from 1 to 8.
  The default is 3. Each build is one `reflow convert-many` process, and they share the machine's
  worker budget (`src/reflow/budget.py`): a pool's tasks hold its slots only while they run, so a
  build that starts while another runs still gets its share of the workers. That holds for every
  pool of worker processes, not only `convert-many`'s: `reflow convert --jobs`, `rerender`, the
  source audit and the page-check and robustness harnesses meter theirs the same way, so none of
  them can leave a later build with one worker. (pytest-xdist's workers, started up front, are
  the one fleet that holds its slots for the session.) Every build passes
  `--stage-cache`, so a document's OCR and extraction are read from the cache in the main checkout
  when its inputs have not changed; the published output is the same. The runner fills free slots from the front of the queue when a job is added or finishes,
  on each page poll, and on a one-second timer. Servers that share a data root must use the same
  setting. A command-line build does not count against it. The GPU is not part of this limit:
  each conversion takes the machine's GPU lease (`src/reflow/budget.py`) around its stages that
  load a model, so builds wait for each other only there, and their other stages run side by side.
- **No lock without work.** Each pass first reads the record without the lock. It takes the lock
  only if something would change: a job whose process is gone, a claim or a publication left by a
  dead process, or a queued page job with a free slot. An idle server starts no helper process.
- **Spawn.** The runner runs `nice -n 19 bun scripts/build-docs.ts <slug>` in `web/`, with
  `REFLOW_BUILD_JOB=<id>`. `REFLOW_BUILDS_BUN` overrides the `bun` executable. The slug, checked
  against `SAMPLE_DOCS`, is the only variable argument. The runner records the child's pid before
  it releases the transaction. A child whose server died before that point cannot adopt the job.
- **One script for every build.** `build-docs.ts` does every build and writes its progress, for
  the page and the CLI alike. With `REFLOW_BUILD_JOB`, it adopts that job. Without it, it creates
  one `cli` job per slug and takes their claims. Progress comes from
  `reflow convert-many --progress-json` as JSON lines on stdout, such as
  `{"event":"progress","out":"<dir>","stage":"Rendering page images","done":12,"total":52}`. The
  script writes progress to the job at most about four times a second. A document that converted
  also gets one `{"event":"timings","out":"<dir>","seconds":{"extract":1.2,...},"gpuWait":{...}}`
  line before its `ok` line: the seconds each stage took (`Summary.seconds`) and the part of each
  spent waiting for the GPU lease. The script does not print it. It passes the timings to
  `reflow.provenance`, and they are published as `timings` in the document's `build-info.json`
  ("How this transcript was made"), and the panel lists them: the total, then each stage of 0.1 s or
  more, largest first, with the GPU wait noted and the rest summed as "other". A build from before
  this change has none, and the panel shows no time row for it. `reflow convert-many` also prints the stages that took 0.1 s or more to
  stderr, one line per document.
- **Applying decisions without a build.** `reflow rerender <out> --decisions FILE
  --progress-json` replays a decisions store on the conversion in `<out>` without converting the
  PDF again, and prints the same `start`, `ok`, `FAILED` and progress lines as `convert-many`; it
  exits 4 with the reason when a full build is needed (`docs/review-decisions.md`, "Applying
  decisions without a full conversion"). The builds queue runs it as a `rerender` job.
- **Workers.** Each converter and audit worker starts through a Python wrapper. The wrapper waits
  until the script has recorded the worker's pid and start time, then calls `exec`, which keeps
  both. If the script died first, the wrapper gets EOF and exits without running anything.
- **Restart recovery.** A running job whose script and recorded worker are gone becomes
  `interrupted`, or `cancelled` if its cancel marker exists. A queued page job stays queued. A live
  recorded converter keeps its job active after its script dies, and Cancel can stop it.
- **Cancel.** Cancel writes the cancel marker under `coordination.lock`, which dispatch also holds,
  so a queued job cannot slip into running. One CLI invocation shares one converter across its documents.
  Cancelling one of them therefore stops every active document of that invocation. The UI says so
  beside the Cancel buttons.
- **Stopping a process tree.** The helper opens pidfds and checks each saved pid against its
  `/proc` start time. It freezes the tree while it finds the tree's processes, sends TERM and CONT,
  and waits five seconds. It then sends KILL to what is left and waits for exit. Signals go through
  the pinned pidfds, so a reused pid is never hit. The helper does not defend against hostile
  process creation or against descendants that daemonize, so workers must stop on TERM. The helper
  runs in its own session and ignores INT, TERM, and HUP. It resumes anything it froze before it
  exits. If something SIGKILLs the helper, the caller runs it again.
- **Stopping the script.** A SIGINT or SIGTERM to `build-docs.ts`, even during startup, marks every
  unfinished record "Stopped by <signal>", frees the claims, and exits with 130 or 143. Any other
  failure exits with 1.
- **Retry.** A retry is an ordinary enqueue of the same slug. It is allowed for `failed`,
  `interrupted`, and `cancelled` jobs.

## The API under `/api/builds`

Every route is under `/api/builds` and answers JSON with `Cache-Control: no-store`. An error is
`{error: "<sentence>"}`.

| Route | Answer |
| --- | --- |
| `GET /api/builds/enabled` | `{enabled: boolean}`, with 200 whether builds are on or off. It reads only the gate. |
| `GET /api/builds` | `{concurrency, active, recent, built, published}`. `active` is the running jobs, then the queued jobs in order. `recent` is the 20 newest finished jobs. `built` is the mtime of `public/docs/<slug>/manifest.json`. `published` is each document's publication generation. It takes an optional `?behind=<slug>:<generation>,...`. See [A stuck page reports itself](#a-stuck-page-reports-itself). |
| `POST /api/builds` `{slug, kind?}` | 202 `{job, created: true}`. 200 `{job, created: false}` when the slug is already queued or running. `kind` may only be `"rerender"` (see [A job is a build or a rerender](#a-job-is-a-build-or-a-rerender)); another value is 400. |
| `POST /api/builds/<id>/cancel` | 200 `{job}`. 404 for an unknown job. 409 when the job has already finished. |
| `POST /api/builds/<id>/retry` | 202 or 200 as for `POST /api/builds`. 404 for an unknown job. 409 when the job is not failed, interrupted, or cancelled. |

The page polls `GET /api/builds` every second while a job is queued or running. It also polls
every second for its first ten polls while it is behind a publication. Otherwise it polls every
5 s, and it does not poll while the tab is hidden.

### Why the page polls instead of streaming

The server sees a CLI build only by reading the build directory, so a stream would read the same
files. A poll needs no connection cleanup and survives a server restart. Each poll reads at most
about 70 small files.

## Builds are off unless you turn them on

`buildsEnabled(env)` in `web/src/lib/builds/gate.ts` turns builds on when
`NODE_ENV === "development"` or `REFLOW_BUILDS === "1"`. The gate reads these on every request,
never at build time, so one production build can serve with builds on or off.

- **Off.** Every `/api/builds` route except `enabled` answers an empty 404, and `/builds` is a
  404. The runner does not start, and no build control renders.
- **On.** The Rebuild buttons, the status cells, and the Builds page exist.
- **Every request.** `Host` must be `localhost`, `127.0.0.1` or `[::1]`. `Sec-Fetch-Site`, when
  present, must be `same-origin`, and `none` is also allowed for `GET` and `HEAD`. `Origin`, when
  present, must match the request's host, and a write (`POST`) must carry it: a browser always
  sends one. These are the `/api/decisions` checks. Enqueue needs `application/json` and a body of
  at most 1,000 bytes. `slug` must be a `SAMPLE_DOCS` slug, a variant already built, or a variant
  the document's variants list offers: a fixed pipeline, or an Ollama one for an installed vision
  model (as of the last 15 seconds). `GET /api/variants/<slug>?fresh=1` runs its provenance check
  only with builds on, and concurrent checks share one process. The routes accept requests
  without `Sec-Fetch-Site`, such as requests from curl or a script. These checks are not
  authentication. **Do not set `REFLOW_BUILDS=1` on a server the
  public can reach**, because anyone who can reach the port can spend the machine's CPU.

### Where the server listens

`bun run dev` is `next dev -H 127.0.0.1`, and the production line above passes the same `-H`.
`next dev` and `next start` listen on every interface unless given `-H`. The same gate also turns
on the `/api/decisions` routes (`docs/review-decisions.md`, "Recording decisions from the web"),
which change the words a published conversion shows, so the listening address matters more now.

- **What the request checks stop:** a page on another site, a DNS-rebinding page (its requests
  carry the attacker's host name, and the builds and decisions routes accept only `localhost`, `127.0.0.1`
  and `[::1]` as `Host`), and a page on another origin of the same machine.
- **What they do not stop:** a program that can reach the port and sends `Host: localhost` with a
  matching `Origin`. A route cannot tell where a request came from: Next takes `x-forwarded-for`
  from the client when it sends one, and `Host` and `Origin` are whatever the client writes. On the
  machine, that program is any local user or process. If the server listens on a network
  interface, it is any host on that network. These docs never describe writes as limited to local
  processes.
- **So:** a server started on another interface with the gate on (for example
  `bunx next dev -H 0.0.0.0` to test on a phone) accepts builds and decisions from that whole
  network. A reviewer on another machine, or behind a reverse proxy, needs an authenticated
  design, which is out of scope here.

`web/src/proxy.ts` gates `/builds` before anything renders, by rewriting the request to a path
that does not exist. A page cannot do this itself. Under Cache Components, the page streams inside
a `Suspense` boundary, so the status is already 200 by the time the page could call `notFound()`.
The same proxy gives a real 404 for `/docs/<slug>`, `/view/<slug>`, and `/inspect/<slug>` when the
slug is not in `SAMPLE_DOCS` or is malformed percent-encoding. The check is a set lookup. The
published files (`/docs/<slug>/<file>`) do not pass through the proxy.

The pages read nothing per request for builds. They stay static, with the same `Cache-Control` as
with builds off. The root layout wraps the content in a client `BuildsProvider`, which asks
`GET /api/builds/enabled` once after hydration. When builds are off, the provider does nothing
more. When builds are on, the controls appear and polling starts. With builds off, each page still
carries the provider's client code, about 3.6 KB gzip, and makes one probe request per load.

## Pages update when a build publishes

What a page shows changes when a build **publishes**, not when it finishes.

### Each write to a document's files is a publication

Three writes are publications: the public swap of `public/docs/<slug>`, the source audit
(`audit.json`), and the removal of the old audit before a conversion. `build-docs.ts` runs each
one through `BuildStore.publish`. `BuildStore.publish` marks the document as publishing in
`publications.json` and does the write. It then adds one to the document's generation, whatever
the job becomes after that. A write that throws still counts. If the script dies halfway,
recovery adds to the generation once the job's processes are gone.

### Reads carry the generation they saw

`web/src/lib/docs-data.ts` has two reads, `readDocument(slug)` and `readPage(slug, page)`. Each is
one `"use cache"` entry tagged `doc:<slug>`. Each reads the document's generation **before** its
files and returns the generation with the files, so a read's generation is never newer than its
content. Every page that shows a document renders `RenderedFrom`, which puts
`{slug: generation}` into the page. When a page read a document twice, it puts the lower
generation.

### The poll marks changed documents stale

`GET /api/builds` returns `published`, each document's current generation. It also marks as stale
the cached reads of every document whose generation changed since this server last asked, with
`revalidateTag("doc:<slug>", "max")`. The server keeps in memory what it has marked. Each server
on a shared data root therefore marks every publication itself, and a restarted server marks every
published document on its first poll. The cache itself survives a restart.

### The page refreshes until it is current

`Freshness` (`web/src/lib/builds/view.ts`) compares the generations that the page rendered with
`published`. While one differs, it calls `router.refresh()` at most once per poll answer, once
after a client navigation, and never while a refresh is in flight. After a refresh, focus stays
where it was, or goes to `main` if the focused element was removed. A current page never
refreshes.

### A stuck page reports itself

Next compares a mark with the time that a cache entry was *saved*, not with the time that its data
was read. Take a render that read publication A and is still running when B is published and
marked. That render saves A's page after the mark, and Next takes the entry as fresh, so refreshes
keep getting A.

To fix this, a page that finds itself behind on three poll answers in a row (`STUCK_POLLS`) sends
the generations it rendered, such as `?behind=fw9:154`. The server treats this as untrusted input.
It accepts only known slugs and whole numbers, and at most 100 pairs. The server marks a document
again when a page reports a generation below the published one, at most once every 3 s per
document (`RENEW_AFTER_MS`). A page that is only waiting normally becomes current before its third
answer and never reports.

### Why the poll marks entries stale, not expired

With `{ expire: 0 }`, the next request is a blocking miss. Next answers an RSC refresh of a
prerendered page that meets a miss with an empty 404, and the router then reloads the whole page.
The reader loses focus and scroll position. With `"max"`, Next serves the stale entry while it
reads the new one, and the page asks again at its next poll. `web/src/lib/builds/system.test.ts`
fails if the poll marks with anything but `doc:<slug>` and `"max"`.

### Pages catch up within seconds of a publication

These times come from six runs on 2026-10-26, on a production build with a fixture converter:

- An open tab that was already polling every second was current 2.0 to 2.1 s after a publication.
- A tab that first had to notice a build at the idle pace was current after 5.2 to 6.6 s.
- A tab stuck behind an overtaking render was current after 5.1 to 6.5 s.

A page behind its document typically makes three refreshes, each one cached RSC request of about
30 KB for a document page. The stale entry and the browser's HTTP cache
answer the first two refreshes. Plain loads after the poll had the new generation from the second load, 110
to 170 ms later. No load ever paired a generation with older content.

## Files added after the server started come from a route

`next start` indexes `public/` once at boot, so a file added later would be a 404. To serve those
files, `web/next.config.ts` has **fallback rewrites**. When no static file matches,
`/docs/:slug/:path+` goes to `/api/files/docs/...` and `/pdfs/:file` goes to `/api/files/pdfs/...`.
The route (`web/src/app/api/files/[kind]/[...path]/route.ts`, logic in
`web/src/lib/public-files.ts`) reads the file from `public/` on each request, with these rules:

- It serves only `public/docs/<slug>/...` of a `SAMPLE_DOCS` slug and `public/pdfs/<slug>.pdf`. The
  conversion in the data root (`audit.json`, `layout/`, `trace/`) is not reachable.
- Every path segment must be a plain name. The extension must be one of html, json, css, js, webp,
  png, jpg, gif, svg, pdf, or txt. The real path, with symlinks followed, must be a file inside
  that document's folder. Anything else is a 404.
- The headers match the static ones. A versioned URL (`?v=`) under `pages`, `figures`, `chunks`, or
  `assets` gets `immutable`. The rest of those folders and the entry JSON get
  `max-age=0, must-revalidate`, and everything else gets `max-age=0`. The route also sends `ETag`
  and `Last-Modified`, answers 304, and handles HEAD.

## The interface shows status on every document

- **Documents list.** `DocumentsTable` gives each row a status and a Rebuild button. The status is
  built at a time, queued with its place, building with its stage and a progress bar, or failed
  with the error. The list also links to the Builds page with the number of active jobs. On narrow
  screens, the status sits under the title.
- **Not converted notice.** On a document page, the notice shows a **Build** button and the build
  status in place of the command.
- **`/builds`.** The page shows the queue, with running jobs, then queued jobs, each with Cancel. It
  also shows recent jobs with Retry. Both lists update live.
- **Accessibility.** Every button's name includes the document. Progress is a native `<progress>`
  with `aria-label`, `aria-valuetext`, and visible text ("12 of 52 pages"). A polite live region
  named "Build status" announces state changes ("resnet: queued", "resnet: built"), never ticks.
  A busy button stays focusable (`aria-disabled`). After Cancel or Retry, focus moves to the
  affected row. The viewer has its own unnamed polite region, so `/view/<slug>` has two with builds
  on. `scripts/ask-e2e.ts` fails if a page has any other polite region without a name.

## Known limits

- **Recovery needs an open polling page.** A stale document heals only while a page that shows it
  is open and polling. A hidden tab updates when it is shown again. A document that no page has
  open stays as it is until a page opens it. Recovery does not reach a program that loads the HTML
  without polling.
- **The 3 s re-mark limit is per process.** Each server keeps its own marks and its own
  `RENEW_AFTER_MS` clock. Two servers on one data root each mark every publication.
- **Recheck the overtaking-render case after a Next upgrade.** The re-mark depends on how Next
  compares a mark with an entry's save time. Run `cd web && bun run check:crossing` (about 35 s). It
  builds a scratch copy of `web/` in the temp folder, serves it on the first free port from 3481
  (never 3000), and removes it. It passes when it prints `crossing-mark: all passed` and exits 0.
  Exit 1 means that a page stayed behind a publication: the re-mark no longer works on this Next.
  Exit 2 means that the check could not test the case. That includes the case where Next stopped
  serving the overtaking render as fresh. After a pass on a new Next version, run it with
  `--record` and commit `web/scripts/crossing/verified.json`. `bun test src` fails until you do.
  The scenario is described in the comment at the top of `web/scripts/crossing/crossing-mark.ts`.
  `--control` disables the re-mark in the scratch copy and passes only when the check then fails
  for the right reason (about a minute). Run it when the check itself changes.
- **Nothing marks reads stale with builds off.** The cached reads then follow the default cache
  life. A request more than 15 minutes after the read regenerates them in the background.
- **The client cache shows old content first.** A page shown from the client cache (Back, Forward,
  or a prefetch) shows what was cached until its refresh arrives.
- **Builds do not purge a shared HTTP cache.** Behind a shared HTTP cache, that cache keeps serving the
  HTML and RSC responses it holds after a publication. Builds are meant for a server with no cache
  in front of it.
- **A mark that Next never applies leaves pages behind.** If Next's cache handler fails, the pages
  stay behind until the next publication. They keep reporting, which marks again at most every 3 s.
- **A stop between two swaps leaves reads unmarked.** The converter swaps its data-root folder,
  which holds the sidecars and traces that `readPage` reads, just before the public swap's
  publication. A stop between the two leaves those reads unmarked until the document's next
  publication.

## Test builds

Run the automated tests from `web/`:

```sh
cd web
bun test src/lib/builds          # record, claims, queue, runner, recovery, cancel, gate, routes,
                                 # races (races.test.ts) and real processes (process.integration.test.ts)
bun run typecheck && bun run lint
bun run check:crossing           # production cache race, real Next and Chromium, about 35 s; not part of bun test
cd .. && .venv/bin/python -m pytest -o pythonpath=src -q tests/test_build_process.py   # the Python helper
```

To check builds by hand:

1. Start a production build with `REFLOW_BUILDS=1 bun run start -H 127.0.0.1 -p 3447`.
2. Rebuild a document from the list.
3. Queue a second document.
4. Cancel a job and retry it from `/builds`.
5. While the server runs, run `bun run docs:build <slug>` in a terminal. The build appears as a
   `cli` job. For a slug that is already queued or building, the command prints
   `<slug>: skipped, already <state> as job <id> (<who>)`.
6. Kill and restart the server while a build runs. The job keeps running and keeps its claim.
7. Restart the server with `REFLOW_BUILDS` unset. `/builds` and `/api/builds` must answer 404,
   and the list must show no build controls.
