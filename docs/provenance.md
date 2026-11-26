# Document provenance and freshness

A published document is a baseline. Evals, captures and benchmarks read it: the conversion in
`web/.reflow/<slug>/`, its public copy in `web/public/docs/<slug>/` and the PDF copy in
`web/public/pdfs/<slug>.pdf`. A baseline is right only when the document is what a rebuild in your
checkout would publish. Each build records what its conversion read, so a check can tell.
Timestamps and commit dates cannot tell. A document built after the newest commit can still come
from an old checkout, from uncommitted edits, or from another worktree's source.

## Check a document before you use it

```sh
cd web
bun run docs:check resnet fw9          # each one's status and why; exits 1 unless all are current
bun run docs:check --summary           # counts for every sample document
bun run docs:build --stale resnet fw9  # rebuild here only the ones that are not current
```

| Status | Meaning |
| --- | --- |
| `current` | The record matches this checkout's converter source, the PDF, the options and the dependency versions, and the public copies are this conversion. |
| `stale` | One of those differs. The reason names what differs: the changed source files, the option, the package. |
| `missing` | The document is not converted here, or it is converted but not published. |
| `unverifiable` | No record describes this conversion, it read inputs a record cannot cover, its inputs changed while it was built, or the source PDF is not here to compare. |

An unverifiable document is never current. Main's documents from before records were kept are all
unverifiable until they are rebuilt.

The check only reads. It takes no claim, and it says when a build of the document is queued or
running. It took 0.62 s for all 95 sample documents on this machine. `--stale` builds the documents that are
stale, missing or unverifiable, through the same claims and the same machine worker budget as any
`docs:build`. A document that somebody else is building is left to them.

## Each build records what it read

`docs:build` (`web/scripts/build-docs.ts`) writes `DATA_ROOT/.provenance/<slug>.json` for each
document it converts, from the command line or from the web app's `/builds` page. Before
`reflow convert-many` starts, it runs `python -m reflow.provenance snapshot`. Once the documents
are published and audited, `record` reads the inputs again and writes each record. The record
holds:

| Field | What it holds |
| --- | --- |
| `source` | The digest of every file in the reflow package that ran (code, templates and viewer assets), each file's hash, and the git commit and uncommitted files. The commit describes the build. It never decides the status. |
| `pdf` | The source PDF's sha256 and size. |
| `options` | Every `reflow.run.Options` field that can change the output. A password is kept only as whether one was given. |
| `dependencies` | `stagecache.tool_versions()` (Python, the parser, OCR and model packages, and the OCR programs), plus `markupsafe`. |
| `output` | The `"v"` of the conversion's `manifest.json`: the digest of what it published. |
| `uncovered` | The inputs the conversion read that no record can pin down. |
| `changed_during_build` | The inputs that differed between the snapshot and the record. |

`reflow.provenance` holds the comparison, and `web/scripts/freshness.ts` says where each document
lives.

## The source identity is the page-check cache's

The source digest is the hash of every file under `src/reflow`, by content. It is the same digest
that keys the page-check cache. An uncommitted edit counts as much as a commit. A change outside
`src/reflow` (the web app, tests, docs, scripts) leaves every document current. So a main checkout
that is behind origin/main on unrelated commits does not need a rebuild.

Any change under `src/reflow` makes every document stale. No record can prove that a converter
change leaves a document as it was without converting the document again. The stale reason lists
the changed files, so you can judge which documents your task needs. Then rebuild only those.

The check hashes the `src/reflow` of the checkout it runs in, because `docs:build` puts that
checkout's `src` first on `PYTHONPATH`. The check refuses to run if Python imports another
checkout's reflow.

## A record cannot cover everything

A record does not see these inputs change:

- Python packages outside the dependency list, such as numpy and charset-normalizer.
- Docling's model weights in the Hugging Face cache (with `REFLOW_ENGINE=hybrid` or `docling`).
  Only the Docling package versions are recorded.
- What the OCR programs run with: ghostscript, tesseract's language data, the system Python's
  Pillow that the word-grading plugin runs under, other system libraries, and fonts. Only the
  `ocrmypdf` and `tesseract` program versions are recorded.
- Environment variables that the OCR programs read, such as `TESSDATA_PREFIX` and
  `OMP_THREAD_LIMIT`.
- An edit that is made and undone while a build runs.
- `audit.json`, which the build writes after the conversion with `reflow audit` (which also reads
  `REFLOW_PDF_PASSWORD`). It is not part of the output digest.

`current` means the same inputs, not the same bytes. OCR is not reproducible run to run, so a
rebuild of a document with OCR'd pages from the same inputs can still publish other words on them.

A conversion that asks a model for figure descriptions (`REFLOW_ALT_TEXT`) or for assist answers is
never current. The answers, and the caches of them that a rebuild carries over from the previous
output, are not inputs that a record can pin down.

## A record belongs to one conversion

A record keeps the conversion's manifest `"v"`. A record whose `"v"` differs from the conversion's
describes another build, so the document is unverifiable. This is what happens when a conversion
is replaced without a new record: by an older `docs:build`, by a plain `reflow convert`, or by a
worktree on a branch from before records were kept. A failed build keeps the previous output, and
the previous output keeps its record.

Records live in `DATA_ROOT/.provenance/`, outside the conversion folder. A converter refuses an
output folder that holds files it did not write, and seeded documents reach worktrees on older
branches. A record inside the folder would make those converters fail.

## Worktrees seed main's documents with their records

`scripts/worktree-setup` copies a document's record with the document, as one unit. Then it prints
how many of the documents are current, stale, missing or unverifiable against the worktree. A
seeded document is main's output. It is current only when its record matches the worktree. Build
records, claims and queues are never copied.

## Refresh locally, and maintain the shared baseline only from main

A **local targeted refresh** rebuilds only the documents your task reads, in your checkout:

```sh
cd web && bun run docs:build --stale <slug>...
```

Name the documents that your task reads, such as `GOLDEN_SLUGS` in `web/scripts/ask-golden.ts` for
`ask:eval` or `DOCS` in `web/scripts/capture.ts` for captures. The refresh writes only this checkout's
`web/.reflow` and `web/public`. It changes nothing in main or in other worktrees. Don't rebuild the
whole corpus because main moved.

The **shared baseline** is the main checkout's documents, which every new worktree seeds. Rebuild
it only when you maintain it, and then rebuild it in the main checkout, at a clean main
that is up to date with origin/main:

```sh
git -C <main> status --short          # must print nothing
git -C <main> pull --ff-only
cd <main>/web && bun install && bun run docs:build --stale
```

This rebuilds only the documents whose records do not match, and nothing if main moved only outside
`src/reflow`. Never rebuild the baseline from a branch or from a main with uncommitted edits. The
records would name those edits, and every worktree would see the documents as stale.

## Evaluations and captures refuse documents that are not current

`bun run ask:eval` checks the documents that its questions read. It exits 2 unless every one of
them is current, and prints why and the `docs:build --stale` command that fixes it.
`--allow-stale` measures anyway, lists the documents that are not current, and writes each
document's status into the `--json` file. When you report a baseline, say that `docs:check` found
its documents current.

`bun run capture` does the same for the documents it captures, in this checkout (what
`bun run dev` here serves), and writes each document's status into `capture.json`. Refreshing the
five documents of the capture set with `docs:build --stale` took 27 s. `capture compare` of a git
ref captures it from a temporary worktree that shows the main checkout's documents, so that side is
not checked.

## The stage cache speeds up docs:build when you ask for it

`reflow.stagecache` keeps the reading of each PDF: the working copy and document facts, OCR,
extraction, and the page reads for the tag tree and links. The page-check harness always uses it.
`docs:build --stage-cache` uses the harness's cache in the main checkout too
(`.pytest_cache/reflow-page-checks-stages`, or under `REFLOW_PAGE_CHECKS_CACHE`), and so does
`reflow convert --stage-cache DIR`. A change to analysis or rendering code then converts again
without reading the PDF again:

```sh
cd web && bun run docs:build --stage-cache --stale p17 fw9
```

Each stage's key holds the file it reads, the code it runs (the modules it imports, found by
walking the imports) and the package and OCR program versions:

| Stage | Reads | Keyed by |
| --- | --- | --- |
| ingest | the source PDF | its bytes and name, the ingest code |
| OCR | the working copy ingest made of that PDF | the source PDF, the plan (each page's reason and mode), the languages, the OCR and ingest code |
| extraction | the working copy after OCR | its sha256, the extraction code |
| page reads | the working copy after OCR | its sha256, the pages and whether their words are read, the page-read code |

### A render-only change renders the saved model

With the stage cache on, a conversion also keeps its finished model, so a change that only the
renderer reads (a stylesheet, a template, `render/html.py`, the page widths) renders it again
instead of building it. `reflow.saved` defines two identities:

- The model key. It is the stage key of `stagecache.STAGES["model"]` (`reflow.pipeline`, which
  runs every pass, and `reflow.prepare`): that closure's code and resources, the tool
  versions, the source's name and bytes, every option that is not a render setting, and the OCR
  runner with its data. Model code imports no render or output code, and
  `tests/test_stagecache_keys.py` fails if it does. The figure crops are model code
  (`extract/crops.py`), because the figure pass reads their pixels.
- The render key. It is the model key, the closure of `reflow.convert` (output, `render/`, its
  templates and `viewer/`, which the import walk reaches through `resources.files`), the render
  settings (the `Options` fields marked `RENDER`), and the renderer's packages.

A conversion writes both keys to `build.json`, beside `document.json`, with the digest of the
model's files (`names.MODEL_NAMES`: `document.json`, `review.json`, `model.json` with the figures
map, `trace/`, `figures/`, the working copy and its OCR word grades). The next conversion of the
same PDF looks for its own model key in the previous output, then in
`<stage root>/model/<pdf>/<key>`. It copies those files into its stage, and uses them only if they
hash to the recorded digest and `document.json` parses back to the same text. Otherwise it builds
the model, and the result is the same. No model is kept or reused when it reads what no key can
pin down: a model's answers (alt text, the assists, page vision), Docling's weights, a password,
OCR data the runner can't identify, or OCR that failed. The CLI prints `Model: ...`, and
`Summary.model` says which way the model came. `reuse_model=False` builds it anyway. The
page-check harness passes that for its timing runs.

Extraction and the page reads were keyed by the OCR outcome (the runner and the pages it OCR'd)
until this was fixed, which let them pair a reading with a working copy it was not made from. The
page-read code leaves out the OCR rules and the qpdf repair, so an edit to them ran OCR again but
kept the old page reads. Eviction goes entry by entry and OCR output differs from run to run, so
an OCR entry evicted on its own ran OCR again but kept the old extraction. Now both are keyed by
the bytes they parse. A working copy restored from the ingest or OCR entry has the same bytes on
every hit, so the entries stay warm. One made again is new bytes, because qpdf writes a fresh
`/ID` and OCR differs run to run, and it misses. `tests/test_stagecache.py` reproduces the evicted
OCR entry: main's keys published the evicted run's text.

Cached and uncached conversions publish the same manifest `"v"`. The tests check this on a small
document with an OCR'd page, and these documents showed it too. Each was converted alone with
`reflow convert` on this machine, with no cache, then an empty cache, then the cache it filled:

| Document | No cache | Empty cache | Filled cache |
| --- | --- | --- | --- |
| `fw9` (6 pages) | 1.1 s | 1.4 s | 1.3 s |
| `p17` (142 pages, no OCR) | 15.1 s | 16.2 s | 11.6 s |
| `az-claim-form-instruction-booklet` (one OCR page) | 17.5 s | 17.9 s | 1.9 s |

`docs:build` of the three at once took 41.6 s with no cache (twice), 42.4 s with an empty cache and
29.0 s with a filled one. Most of the rest was the source-PDF audit, which runs after every
conversion has finished. It parsed the pages of `p17`'s tag tree in one process, 14.2 s. It now
parses them on a pool within the machine's worker budget, joined only for a tree long enough to
use one, in 7.0 s with the same report, and the warm `docs:build` of the three took 21.2 s and
25.6 s on a machine busy with other work.

A record says when its build used the stage cache, with the hash of the working copy that keyed
it, and `docs:check` says so too. Such a build is current like any other: the keys hold the code
and versions each stage runs, so it read the inputs its record names. Three limits remain:

- Nothing evicts the cache but the page-check harness's `prune`, at the end of a page-check
  session: 4 GB and 14 days, the most recently used kept. `docs:build --stage-cache` of the whole
  corpus adds to it without pruning.
- A stage's key hashes the code on disk when the key is computed, and a build can import more of
  it later. After a build the key is computed again, and a result is not stored when it changed,
  so an edit made during a build is not cached under the old code's key. This narrows the window
  and does not close it: an edit reverted before the check, or code the process had already
  imported, is not caught. The saved model (`reflow.saved`) is not rechecked.
- The OCR entry stands for the working copy it read by the source PDF and the ingest code, not by
  that copy's bytes. A working copy made again differs only in its `/ID`, which qpdf takes from
  the clock (checked on `fw9` and `p17`), so OCR's output is what OCR of the new copy would give,
  up to OCR's own variation.
