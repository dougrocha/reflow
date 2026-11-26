# Verify parallel conversion

## What the check protects

A conversion with a process pool publishes the same output as `-j 1`, and fails exactly when
`-j 1` fails. convert.py states this promise: "with the same output as jobs=1". A pool runs for
`reflow convert -j N`, which is the default of the CLI, and for every document of
`reflow convert-many`.

Most users run with a pool, so a break here reaches most readers. A difference shows only on some
documents and some schedules, and the page-check suite runs one mode. A reader would get missing
links, missing tag evidence or missing review warnings, and nobody would notice.

## Run the check

1. Make sure that the main checkout's `.venv` has `hypothesis` from the `dev` group. In the main
   checkout, run `uv sync`. If you must not change the other packages in that venv, run this from
   a worktree instead:
   `UV_PROJECT_ENVIRONMENT=<main>/.venv uv sync --inexact --no-install-project`.
2. Run the gate:

   ```sh
   mise run verify-parallel             # about 25 s: the gate, then the open findings (none now)
   ```

3. Read the result. Exit 0 and a final `OK:` line mean that the gate passed and that each open
   finding still reproduces as recorded. The open findings are the `FINDINGS` list in run.sh,
   which is empty now.

Two variants run more:

```sh
mise run verify-parallel selftest    # also: each model property fails on a broken variant
TLA_N=3 mise run verify-parallel     # a deeper model bound (about 1 min)
```

The run sets up its own tools:

- `mise` installs the pinned Java the first time (`mise.toml`: `java = "temurin-25.0.4+7.0.LTS"`).
- `run.sh` downloads `tla2tools.jar` v1.7.4 from the TLA+ GitHub release into
  `~/.cache/reflow/tla/` and checks its sha256,
  `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`.
- Python is the main checkout's `.venv`. Set `REFLOW_PYTHON` to use another interpreter.
- Set `KEEP_OUTPUT=1` to keep the raw TLC and pytest output. The script prints where it is.

## Two tools check the two halves of the property

### TLA+ checks every schedule

The model is `model/ParallelConvert.tla`, checked with TLC. The order in which the parent thread,
the `_Early` and `_Parse` prefetch threads and the pool's tasks run is a question about the design,
across convert.convert. No Python tool can run every schedule of the real pipeline, so a model
checker explores them all.

The model covers a pooled run with a private pool and with the shared pool of convert-many. It
covers failures in extraction, in planning, in a later pass, in `walk_tags`, in `scan_links` and
in a page parse. The operator `Serial` is the `-j 1` result, built from the same PageReader rules.

### Hypothesis checks the real code

Whether the code does what the model assumes is a question about the code's behavior, so these
tests run the real functions. They are in `checks/test_real_code.py`.

- `test_pooled_extraction_and_planning_match_serial` runs the real `extract_document` and
  `layout.analyze` (`offsets_of`, `plan_pages`) on fw9 and resnet. It uses `AdversarialPool`,
  which pickles the arguments and result of every task as the process boundary does. Tasks finish
  one at a time, in an order that Hypothesis picks, and only while the caller is blocked. It tries
  jobs 2-4 and chunk sizes None, 1, 2 and 5. The pages, tables, trace and nodes must equal those
  of the serial run.
- `test_pooled_page_parse_matches_serial` runs the real `read_contents` with weights and a pool on
  fw9 and resnet. The pages it yields, in the order asked, must equal a serial parse.
- The `test_prefetch_*` tests call the real code in convert's order: `prefetch.early`, the
  reader's `seed`, `prefetch.start` twice (once for analyze's `on_planning`, once for
  `engine._heuristic`), `prefetch.finish`, then `read_links` and `shared_tags`. Each test
  runs once with a pool and once without. The pages, the tag tree and the link annotations are
  synthetic. A document has up to 5 pages, any seeded, failing, linked or tagged subsets of them,
  and a cap, walk or scan failure.
- A test named `test_finding_*` is an open finding. run.sh runs it apart from the gate.

### Properties and their checks

| Property (one sentence) | Model invariant | Real-code test | Status |
| --- | --- | --- | --- |
| Pages and plans are collected in page order, and every page image is written, whatever order tasks finish in | `SameOrderAndImages` | `test_pooled_extraction_and_planning_match_serial` | gate |
| A pooled run publishes exactly what `-j 1` publishes, or fails when it fails | `SameAsSerial` | `test_prefetch_gives_the_evidence_pass_what_jobs_1_gives` | gate |
| While `_Parse` is alive, the parent never touches the PageReader, which is not thread-safe | `ReaderOneCaller` | (model only) | gate |
| After `output.staged` removes the stage, no page-image task, `_Early` or `_Parse` is still running | `StageQuiet` | (model only) | gate |
| A published output has every page image | `PublishedWhole` | (model only) | gate |
| No schedule leaves the parent waiting forever | TLC deadlock check | (model only) | gate |
| The text witness's suspect pages are gathered in page order, and every witness task is done before `prepare` returns, so extraction never starts beside one | `WitnessJoined`, and `wit` in `SameOrderAndImages` and `SameAsSerial` | `tests/test_text_trust.py` | gate |
| A page that fails to parse fails a pooled run when it fails `-j 1`, and the links are the same (was F1) | `F1SameFailureAndLinks` | `test_prefetch_failed_link_read_fails_both_runs_and_keeps_the_links` | gate |
| A page that fails to parse gives a pooled run the same tag evidence and `order-unchecked` fact (was F2) | `F2SameTagEvidence` | `test_prefetch_failed_page_read_gives_the_same_tag_evidence` | gate |

The gate also checks `TypeOK`.

### The self-test breaks the model on purpose

`selftest` runs the model with each broken variant (`Break`) and expects an invariant to fail.

| Break | What it changes | Invariant that must fail |
| --- | --- | --- |
| `extAsCompleted` | Collects chunks as they complete | `SameOrderAndImages` |
| `noFinish` | convert skips `prefetch.finish` | `ReaderOneCaller` |
| `noCancelWait` | Cancels page images without waiting (shared pool only) | `StageQuiet` |
| `noAbandon` | Drops `prefetch.abandon` | `StageQuiet` |
| `noImageFinish` | Publish does not wait for the images | `PublishedWhole` |
| `restoreNoImages` | A run that restored a saved model publishes without starting the page images, carried or not | `PublishedWhole` |
| `witAsCompleted` | The text witness's pages are gathered as they finish, not in page order | `SameOrderAndImages` |
| `noWitJoin` | `prepare` returns without waiting for the witness tasks | `WitnessJoined` |
| `triedFirst` | `PageReader.get` marks pages tried before it parses them (the old rule) | `F1SameFailureAndLinks` and `F2SameTagEvidence` |

## No finding is open

The gate holds every property.

### F1 and F2 were fixed in #78

`PageReader.get` (extract/pagecontent.py) added the pages to `_tried` before it parsed them. When
`_Parse` (analyze/prefetch.py) failed, it swallowed the exception, and its pages stayed "tried" but
unread. The evidence pass then got `{}` for them. It did not parse them and fail, as `-j 1` does.

- In F1, a pooled run published where `-j 1` fails, and silently dropped the links of the page that
  failed to parse. On resnet.pdf with page 3 failing, it published 80 link spans instead of 243.
- In F2, a pooled run lost the `order-unchecked` warning, or built the tag evidence from a subset
  of pages. fw9.pdf with page 6 failing shows it.

The fix marks the pages tried only after the read returns. A parse that raises leaves them untried,
so the evidence pass parses them and raises as `-j 1` does. The cost is that a failing page is
parsed twice, once by `_Parse` and once by the evidence pass. `Break=triedFirst` in the model
restores the old rule, and the selftest shows that F1 and F2 fail on it.

### Record a new open finding

1. Add a real-code test named `test_finding_*` that fails, and a model invariant that TLC reports
   as violated.
2. Add `"ID INVARIANT TEST"` to the `FINDINGS` list in run.sh. `run.sh` then expects both to fail.
   Once the code is fixed, it reports `CHANGED: ID no longer fails`.
3. When the finding is fixed, add the invariant to the `tlc gate ...` list of the gate.
4. Remove the entry from `FINDINGS`.
5. Rename the test without "finding".
6. Update the reader rules of the model to the new code.
7. Rewrite this section.

## The coverage record ties the model to the code

The sync commit below predates this repository's public history, so `git diff` against it does not work here.

**Sync commit: `9b0f8a3f260ef8072b80cc95ffb6350256b6d372`**. At this commit, the model and the
checks were confirmed to describe the code below.

To see whether a change touches the covered code:

1. Run the diff:

   ```sh
   git diff --stat 9b0f8a3f260ef8072b80cc95ffb6350256b6d372 -- src/reflow/convert.py src/reflow/pipeline.py src/reflow/saved.py src/reflow/run.py src/reflow/output.py src/reflow/analyze/engine.py src/reflow/analyze/layout.py src/reflow/analyze/pageplan.py src/reflow/pool.py src/reflow/analyze/prefetch.py src/reflow/extract/parallel.py src/reflow/extract/weights.py src/reflow/extract/pagecontent.py src/reflow/extract/tags.py src/reflow/extract/links.py src/reflow/render/pages.py src/reflow/analyze/evidence.py src/reflow/prepare.py src/reflow/ingest/text_trust.py src/reflow/ingest/witness.py src/reflow/ingest/font_map.py
   ```

2. Read the diff for the functions in the table. A diff that touches none of them needs no model
   change. Examples are a docstring, or an option unrelated to the pool.

| Code (file: function) | Model action | Real-code check |
| --- | --- | --- |
| convert.py: `convert`. First `saved.Reuse.restore`; with a saved model, start_page_images, publish. Else the order is `pipeline.read_pages` (early, build), start_page_images, `pipeline.finish` (prefetch.finish, evidence.witness, later passes), publish. Then the `finally` with `images.cancel()` and `prefetch.abandon` | `Restore`, `Early` .. `Swap`, `Finally`, `CancelWait`, `Abandon` | The prefetch tests call the same sequence; `tests/test_saved.py` restores at jobs 2 |
| saved.py: `Reuse.restore` (copies a proved model into the stage in the parent: no pool task, no thread; replays a decisions store on it with `pipeline.redecide`; clones the previous output's `pages/` when `_carry_pages` proves them the render's), `Reuse.close` (after publish: build.json, a copy into the stage root) | `Restore` (`saved`, `carried`) | `tests/test_saved.py`, `tests/test_rerender.py` |
| run.py: `workers_for`, and a private pool's `shutdown(wait=True, cancel_futures=True)` | `Shutdown` (`sharedPool`) | - |
| output.py: `staged`, `_swap` (rmtree or swap after the body; the stage's flock is held until then, and the swap runs under the output's lock), `recover` (before the stage exists, in the parent: no pool task, no thread) | `Rmtree`, `Swap` | `tests/test_output.py` (a kill mid-swap, a killed run's stage, a live run's stage) |
| output.py: `start_page_images`, `publish` (no image task when `run.state.pages_carried`; else take it and `finish`, then the parent-only, pure writes: `write_page_layouts`, `render_html`, `stamp_version` over every file of the site, page images included, then `write_passages`) | `ImgStart`, `Publish` | `tests/test_passages.py` (passages.jsonl bytes at jobs 1 and 2), `tests/test_parallel.py` (every published file of fw9 at jobs 1 and 2, without page images) |
| render/pages.py: `PageImages.finish`, `PageImages.cancel`, `start_page_images`, `_render_batch` | `Publish`, `Finally`, `CancelWait`, `ImgRun`, `ImgDone` | - |
| analyze/engine.py: `_read_pages` (`page_weights` before `prefetch.early`, `weights` to `extract_document`, `run.state.page_weights` for the reader), `_extract` (the scan-field pass after the seed, with no pool and no reader), `_hand_over_parse` (seed), `_heuristic` (`on_planning`, then `prefetch.start`) | `Early`, `Seed`, `PlanStarted`, `StartAfter` | The prefetch tests |
| pool.py: `submit_runs` (submits in `order`, yields in the order of the calls, cancels the unstarted runs when a run fails or the reader stops); used by `extract_document`, `read_contents`' weighted path, `render_figures` and `ingest.pages` | `ExtSubmit`, `ExtCollect`, `ExtTask` | `test_pooled_extraction_*`, `tests/test_pool.py` |
| extract/parallel.py: `extract_document` (chunks cut by `weighted_chunks`, handed to `submit_runs` with the heaviest first, collected in page order) | `ExtSubmit`, `ExtCollect`, `ExtTask` | `test_pooled_extraction_*` (with and without weights) |
| extract/weights.py: `page_weights`, `weighted_chunks` (pikepdf in the parent, before the early thread starts) | None. The size of a chunk and the order it is submitted in fall under "any task, any order" | `test_pooled_extraction_*` |
| analyze/layout.py: `analyze` (pooled when `len(pages) >= MIN_PAGES`), `_prepare` (`offsets_of`) | `PlanSubmit`, `PlanSerial` (`pooledPlan`) | `test_pooled_extraction_*` |
| analyze/pageplan.py: `plan_pages` (submit, `submitted()`, collect in `zip(ranges, futures)`), `offsets_of`, `plan_chunk` | `PlanSubmit`, `PlanStarted`, `PlanCollect`, `PlanTask` | `test_pooled_extraction_*` |
| analyze/prefetch.py: `early`, `_Early.run`, `_collect`, `start`, `_Parse.run`, `finish`, `abandon` | `Early`, `EarlyRun`, `CollectEarly`, `StartParse`, `PGet`, `PGetRead`, `Finish`, `Abandon` | The prefetch tests (real) |
| extract/pagecontent.py: `PageReader.get` (`words` for every page, none or some of them), `seed`, `reader_for`. Also `read_contents`, which with `weights` and a pool cuts runs by weight and gives them to `submit_runs`, the heaviest first, to be yielded in the order asked | `MissingIn`, `PGetBegin`, `PGetEnd`, `Seed`, `Links`, `Tags` | The prefetch tests (real), `test_pooled_page_parse_matches_serial` |
| extract/links.py: `read_links` | `Links` | The prefetch tests (real) |
| extract/tags.py: `shared_tags`, `read_tags`, `walk_tags`, `Walked.pages`, `MAX_TAG_PAGES` | `Tags`, `EarlyRun` (`overCap`) | The prefetch tests (real) |
| analyze/evidence.py: `witness` (links, then `shared_tags`) | The order of `Links` and `Tags` | The prefetch tests call them in this order |
| prepare.py: `prepare` (ingest, then `text_trust.preflight`, then `font_map.repair` and `text_trust.recheck`, then `needs_ocr` and `ocr_pages`, all in the parent before any pass) | `Restore` (not saved) to `WitSubmit`, then `Early` after `WitCollect` | `tests/test_text_trust.py`, `tests/test_font_map.py` |
| ingest/text_trust.py: `preflight` (judges the pages in the parent under the pdfium lock, then hands the suspect ones to `witness.read_pages` and records facts from what it returns, in page order) | `WitSubmit`, `WitCollect` (`suspect`) | `tests/test_text_trust.py`; `tests/test_saved.py` and `tests/pages/test_stage_equivalence.py` (fw9-shifted) compare a fresh and a reused conversion |
| ingest/text_trust.py: `recheck` (judges the pages the font repair changed again in the parent under the pdfium lock, against the witness words `preflight` already gathered: no pool task) | None. A serial step after `WitCollect` and before `Early` | `tests/test_font_map.py` |
| ingest/font_map.py: `repair` (votes from the witness words `preflight` gathered, then writes the patched copy to a sibling, checks it and `os.replace`s it over `working`, all in the parent under the pdfium lock: no pool task, no thread), `mark_nodes` (in `pipeline.finish`, serial, after the passes) | None. A serial step after `WitCollect` and before `Early` | `tests/test_font_map.py`; `tests/test_saved.py` compares a fresh and a reused conversion of a repaired font |
| ingest/witness.py: `read_pages` (one future per page on the run's pool, `future.result()` in page order; an error, the pool's too, is the page's, never raised), `witness_page` (a pool task: renders the page, runs Tesseract, through the stage cache) | `WitSubmit`, `WitRun`, `WitDone`, `WitCollect` | `tests/test_text_trust.py` |

Move the sync commit forward only after you read the covered diff and confirm that the model and
the checks still describe the code at the new commit. A green run is not enough.

### How the record was proven

At the sync commit of the first version of this check, its steps were followed on two scratch
breaks in the real code. Both were reverted afterwards, and the gate passed again.

- `extract_document` collected `as_completed(futures)`. The real-code gate failed, and fw9's pages
  came back as `[4, 5, 1, 3, 6, 2]` with jobs=2. The model, with `ExtCollect` changed to match,
  failed `SameAsSerialWhenPagesParse`.
- `convert` ran without `prefetch.finish(run)`. The model, with `Finish` changed to match, failed
  `ReaderOneCaller`, because the parent reached `Links` while `_Parse` was alive. The real-code
  tests do not see this break, because they call `finish` themselves. This is why a change to
  convert's order must be mirrored in the model.

At the fix for #78, the selftest proved the `triedFirst` break. After `PageReader.get` took the
words per page (#47), the same break in the real code, with `_tried` updated before
`_load_or_read`, failed `test_prefetch_failed_link_read_fails_both_runs_and_keeps_the_links` and
`test_prefetch_failed_page_read_gives_the_same_tag_evidence`.

After the weighted chunks (#47), two more scratch breaks in the real code were tried and reverted:

- `extract_document` collected the futures in `order`, the order they were submitted in with the
  heaviest first, instead of page order. `test_pooled_extraction_and_planning_match_serial` failed
  on its page-order assertion.
- `read_contents` yielded its runs in `order` instead of the order asked.
  `test_pooled_page_parse_matches_serial` failed.

#184 moved the passes of `convert` into `pipeline.read_pages` and `pipeline.finish`, unchanged
and in the same order, with `start_page_images` between them as before (the "prefetch" step now
times only `prefetch.finish`). `convert` gained one serial branch before them: `saved.Reuse.restore`
copies a proved saved model into the stage in the parent, starts no thread and submits nothing,
and the run then starts the page images and publishes. The model gained `saved` and the `Restore`
action, with the assumption that a saved model is one a run that published made (its key holds
every model input), so a restored run's outcome is `Serial`'s. The `restoreNoImages` break shows
that `PublishedWhole` covers the new path. `publish` takes the `Model` and no longer writes
document.json, review.json or trace/, which `pipeline.finish` writes before it; the waits are
unchanged. The figure crops moved from render/pages.py to extract/crops.py, unchanged. The gate
and the selftest passed. The sync commit did not move.

A change for #105 made a conversion with a decisions file savable and reusable, and made
`saved.Reuse.restore` do two new things in the parent, after it proves the model and before the
run starts anything. First, `pipeline.redecide` replays the run's store on the restored Document
(`decisions.revert`, then `confidence.apply`, `decisions.apply` and `review.queue`, the code
`pipeline.finish` runs through the same `pipeline.decide`) and rewrites document.json and
review.json. It is serial Python with no pool task, thread or wait, so it falls under `Restore`
as before. Second, when the previous output's render key equals this run's and its `pages/`
holds exactly the file names this run would write, `_carry_pages` clones that folder into the
stage and sets `run.state.pages_carried`; `start_page_images` then starts nothing and
`publish` renders nothing. The model gained a `carried` variable (only with `saved`): `Restore`
sets `imgWritten` to every page and goes straight to `Publish`, with no `ImgStart`, so no image
task is running at `Finally`. `PublishedWhole` and `SameOrderAndImages` still say a published
output has every image, and `restoreNoImages` still fails them for a run that restores and
neither renders nor carries. A scratch break (`carried` leaves `imgWritten` empty) failed
`SameAsSerial`, which shows TLC explores `carried = TRUE`. The gate and the selftest passed with
the changed model. `tests/test_rerender.py` checks on the real code that a carried `pages/` is
byte-identical to a rendered one, and that a render setting or a damaged folder renders again.

#308 added the text-trust preflight to `prepare`, so prepare.py, ingest/text_trust.py and
ingest/witness.py joined the record. Between ingest and replacement OCR, the parent judges each
page and hands the suspect ones (`suspect`) to `witness.read_pages`: one task per page on the
run's pool (the shared pool too), each a Tesseract process in a worker slot, gathered with
`future.result()` in page order. A task's failure, or the pool's, is recorded on its page and
never raised, so the model treats it as a task that finished. `prepare` returns only after the
last result, so nothing else is in flight: the model gained `WitSubmit`, `WitRun`, `WitDone` and
`WitCollect` before `Early`, the invariant `WitnessJoined`, and `wit` in the outcome, so
`SameAsSerial` and `SameOrderAndImages` compare the order the witness's pages were gathered in.
A restored run reads the facts from the saved Document and runs no witness (`Restore` sets
`witOrder` to `Serial`'s). The breaks `witAsCompleted` and `noWitJoin` fail `SameOrderAndImages`
and `WitnessJoined`. The gate and the selftest passed.

#309 added the font repair to `prepare`, so ingest/font_map.py joined the record. After
`preflight` returns, every witness task has finished; `font_map.repair` reads the witness words
it kept, and when it decodes a font it writes the patched copy to a sibling, checks it and
`os.replace`s it over `working`, in the parent under the pdfium lock. `text_trust.recheck` then
judges the changed pages again against the same words. Neither submits a task or starts a
thread, and both end before `needs_ocr`, so they fall between `WitCollect` and `Early` and the
model needs no new action: the working copy that every later pass reads is still final before
any of them starts. `font_map.mark_nodes` runs in `pipeline.finish` after the passes, serial, on
the Document. A restored run reads the repair from the saved Document and runs none of it.

`reflow rerender` runs `convert(..., must_reuse=True)`: when `Reuse.restore` finds no saved
model, convert raises `saved.NotReusable` right after it, before `prepare` or any thread, task or
image. The run goes to `Finally` with nothing started, as a raise in `prepare` does, so no model
action is added: `StageQuiet` has nothing to wait for. `tests/test_rerender.py` checks that a
refused rerender leaves the output byte-identical and no stage folder.

## Read a failure

### `FAIL: the model breaks ...`

TLC prints the violated invariant, the document it chose (`bad`, `linkPages`, `seeded`, `failAt`,
`sharedPool`, and so on) and the steps by action name. Map each action to the code with the
coverage record, and replay the steps against the code you changed.

- If the code allows the sequence, it is a real bug.
- If the sequence is impossible in the code, it is a modelling error. One example is a step that
  the model makes atomic but that the code cannot interleave. Fix the model so that the action
  matches the code, and say so in the commit message.
- Never weaken an invariant to make it pass.

### `FAIL: the real code breaks ...`

Hypothesis prints the smallest failing input. This check runs the real functions, so a failure is
a real difference between the pooled and serial paths. The exception is when the stand-ins of the
test no longer match the code. The stand-ins are `AdversarialPool` for a ProcessPoolExecutor, and
the patched `read_contents`, `walk_tags` and `scan_links`. If a signature changed, update the
stand-in to the new signature, not the assertion.

### `CHANGED: Fn no longer fails`

Either the finding was fixed, or the check stopped exercising it. Check with the real-code test,
then follow "Record a new open finding" to move it into the gate.

### `ERROR: ... did not run to a verdict`

This is a tool problem, such as a TLA+ syntax error, a missing jar, a missing `hypothesis`, or an
import error. The script prints the tail of the output.

## Update the verification for an intended change

1. Find the changed functions in the coverage record.
2. Change the matching action of the model so that it does what the new code does. Keep the same
   order, the same blocking (a join or wait is an enabling condition), and the same step
   boundaries.
   - For a new wait-free step that another thread can interleave with, split the action in two.
   - For new state that crosses threads, such as a new `RunState` field or a new reader call, add a
     variable.
3. If the change adds a path that the real-code tests can call, call it from them in the same
   order that convert does.
4. Run `mise run verify-parallel selftest`. Each property must still fail on its broken variant.
   That shows that the change did not make an invariant vacuous.
5. Update the coverage record and the sync commit.

## Bounds and assumptions

### Model bounds

N is 2 by default: 2 pages, so 2 extraction chunks, 2 planning chunks and 2 image batches. TLC
checks every subset of seeded, failing, linked and tagged pages, with every failure point and both
kinds of pool. That is 1.4M distinct states in about 5 s. At `TLA_N=3` it is 33M states in about
1 min.

### Model abstractions

- The pool may run any submitted task at any time, which is a superset of real schedules. This
  covers the weighted cuts and the heaviest-first submission in `extract_document` and
  `read_contents`. They change which pages share a task and which task starts first, and the
  results are still collected in page order.
- The model does not hold what a page contains, only which page it is and its order.
- A page's parse fails the same way in every mode and on every attempt.
- Seeded pages parsed without error.

### Real-code bounds

- The completion-order test runs 12 examples over fw9 and resnet.
- The page-parse test runs 8 examples over fw9 and resnet.
- The prefetch tests run 400 examples each, over documents of up to 5 pages.
- Hypothesis runs with `derandomize=True`, so a run is reproducible. It samples and does not
  enumerate. The model is the exhaustive part.

## What the check does not cover

- The work inside a page task, such as whether `extract_chunk` or `plan_chunk` depend on pages
  outside their chunk. The completion-order test checks this on two real samples only.
  tests/test_parallel.py and tests/test_pageplan.py check a few more documents byte for byte.
- Ingest on the pool (`ingest.pages`), figure crops (`render_figures`), and the hybrid and Docling
  engines.
- The several documents of convert-many on threads that share one pool, with
  `_PoolHolder.replace` after a BrokenProcessPool, and the `PDFIUM_LOCK` discipline between those
  threads. The model treats the shared pool as the pool of one conversion that is not shut down.
- The stage cache (reflow.stagecache), which the page-check harness and `--stage-cache` use. Its
  locks, temp names and eviction have contention tests of their own in `tests/test_cachedir.py`,
  and `tests/test_stagecache.py` checks that cached and uncached conversions publish the same
  manifest "v".
- The `/ID` of `working.pdf`, which qpdf makes fresh on every run.
