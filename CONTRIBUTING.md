# Contributing

Write every commit message in the Conventional Commits format.

## Worktrees

- In a new worktree (`git worktree add`), run `scripts/worktree-setup` first. It takes a
  few seconds and is safe to run again. It installs `web/node_modules` (a real folder, since
  Turbopack rejects a symlink) and seeds the main checkout's published documents
  (`web/public/docs`, `web/public/pdfs` and `web/.reflow`) as reflink copies, each with its
  provenance record. Then it reports how many of them are current against this worktree (see
  Documents below). Build records, claims and queues are never copied. Where reflinks don't
  work, such as in `/tmp`, it seeds nothing; name the documents you need
  (`scripts/worktree-setup resnet fw9`) to copy them in full.
- Start the worktree's dev server with `scripts/dev start` (also `status` and `stop`). It picks
  a free port and records it as `REFLOW_PORT` in `.worktree/env`. Source that file to use the
  same value in a shell.

## Documents

A document's conversion is a baseline for evals, captures and benchmarks, so trust it only when
it is current. `docs/provenance.md` has the details.

- Before you measure, compare or capture against documents, run
  `cd web && bun run docs:check <slug>...`. It says whether each document is current, stale,
  missing or unverifiable against this checkout, and why. Each `docs:build` records what its
  conversion read: the converter source by content (uncommitted edits too), the PDF, the options
  and the dependency versions. A seeded document is main's output, current here only when its
  record matches. An unverifiable document is never current. That includes main's documents
  from before records were kept.
- Refresh locally with `bun run docs:build --stale <slug>...`. It rebuilds, in this checkout
  only, the named documents that are not current. Name only the documents your task reads
  (`GOLDEN_SLUGS` for `ask:eval`, `DOCS` for captures). Don't rebuild the whole corpus because
  main moved. A change outside `src/reflow` leaves every document current, and the stale reason
  names the converter files that changed. Add `--stage-cache` to reuse each PDF's reading (OCR
  and extraction) from the page-check harness's shared cache. It publishes the same output, and
  after a change to analysis or rendering an OCR'd document rebuilds in seconds.
- The main checkout's documents are the shared baseline that every new worktree seeds. Rebuild
  them only when you maintain the baseline, and then do it in the main checkout,
  clean and up to date with origin/main, with `bun run docs:build --stale` in its `web/`. Never
  rebuild them from a branch or from a main with uncommitted edits.
- `bun run ask:eval` and `bun run capture` refuse (exit 2) while a document they read is not
  current, and `--allow-stale` goes on anyway. When you report a baseline, say that its
  documents were current.

## Which checks a change needs

Run `scripts/affected` before you push. It reads `git diff` against main, including uncommitted
and untracked files, and prints the smallest set of checks that covers the change, with each
command and its time. `scripts/affected <commit>` does the same for one commit (for a merge, what
its PR changed), and `scripts/affected A..B` for a range.

There is no hosted CI, and there will be none: no GitHub Actions or any similar service. The
gate runs on this machine instead. Commit your change, then run `scripts/gate` on the PR's head
(a long job, so through `scripts/long-run`). It runs every check that `scripts/affected` prints
and keeps a record keyed by the commit in `~/.cache/reflow/gate/<commit>/`. Merge a PR only when
`scripts/gate status <head>` exits 0, which means the PR's head has a passing record. A file that
`scripts/affected` has no rule for fails the gate until the script has one.

- The script follows the table below, and the first row that matches a file wins. The script
  generates the table (`scripts/affected --write-table`), and `tests/test_affected.py` fails when
  the two differ. To change a rule, edit the script, not this table. The verify-parallel row
  reads its files from the coverage record in `verify/parallel-conversion/README.md`.
- The times come from runs on one machine on 2026-11-02, while other jobs ran (load 2 to 7).
  Read them as rough: a check takes longer on a busy machine.
- A page check's `-k` names spec pages, so the harness converts only their documents. The script
  prints a `-k` as the covering check only when the change names the pages: a spec, a sample
  PDF, or a check function in `tests/pages/checks.py`. Converter code runs on every page, and the
  specs don't say which documents it changes, so the whole quick suite covers it. For some
  converter files the script also prints a `-k` of the pages whose specs test that feature, as a
  faster first run while you iterate.
- The quick suite's time depends on what the stage cache keeps. It is warm after a change to
  analysis or rendering. It re-extracts after a change to code that extraction imports, and runs
  cold after a change to ingest, OCR, the model or the Python packages. The script reads which
  case applies from `reflow.stagecache`.

<!-- generated by scripts/affected --write-table -->
| Check | Command | Time | Note |
| --- | --- | --- | --- |
| unit tests | `.venv/bin/python -m pytest -o pythonpath=src -q` | 27 s | on xdist workers from the worker budget; 1 min 25 s serial, when the budget has no 2 slots free |
| page checks, quick | `.venv/bin/python -m pytest -o pythonpath=src -q -m pages --quick` | 58 s warm, 1 min 32 s re-extracting, 2 min 31 s cold |  |
| web typecheck and lint | `cd web && bun run typecheck && bun run lint` | 4 s |  |
| web unit tests | `cd web && bun test src` | 1 min 2 s |  |
| crossing tests | `cd web && bun test scripts/crossing` | 10 s |  |
| verify-parallel | `mise run verify-parallel` | 26 s |  |
| ruff and mypy | `.venv/bin/python scripts/lint` | 27 s | fails on a problem lint-baseline.json does not count; under 1 s once mypy's cache in .worktree/ is warm |
| label-assist eval | `.venv/bin/python scripts/eval_label_assist.py` | 3 s | compare the accuracy it prints with main's |
| structure-assist eval | `.venv/bin/python scripts/eval_structure_assist.py` | 4 s | compare the accuracy it prints with main's |
| scan-field eval | `.venv/bin/python scripts/eval_scan_fields.py` | 2 min | compare the accuracy it prints with main's |
| robustness suite | `.venv/bin/python -m pytest -o pythonpath=src -q -m pages --robustness tests/robustness` | 5 min 4 s | converts every damaged copy whole; writes scorecard.md and run.json |
| captures | `cd web && bun run capture` | 21 s | needs this worktree's dev server (`scripts/dev start`) and its five documents current (see Testing). `docs:build --stale --stage-cache` of the five took 9 s |
| unit tests, one file | `.venv/bin/python -m pytest -o pythonpath=src -q <file>` | test_affected.py 2 s, test_checks.py 2 s, test_label_eval.py 4 s, test_structure_eval.py 2 s, test_scan_eval.py under 1 s, test_build_process.py 1 s, test_long_run.py 2 s, test_nightly.py 9 s, test_complexity_eval.py 2 s, test_direct_eval.py under 1 s, test_gate.py under 1 s, test_heldout.py under 1 s | |
| page checks, one file | `.venv/bin/python -m pytest -o pythonpath=src -q -m pages --quick <file>` | test_calendar_page.py under 1 s, test_calibration.py 6 s, test_docling_order.py 9 s, test_mark_layer.py 12 s, test_page_scan_calibration.py 6 s, test_pages.py 31 s, test_stage_equivalence.py 59 s | warm |
| page checks, -k | `.venv/bin/python -m pytest -o pythonpath=src -q -m pages --quick -k "test_page[<spec>-"` | fw9-1 3 s, p17-138 (an excerpt of a long document) 19 s | warm |

| Area | Files | Checks |
| --- | --- | --- |
| This script and its table | `scripts/affected`, `CONTRIBUTING.md` | `tests/test_affected.py`, `tests/test_gate.py` |
| verify-parallel checks | `verify/parallel-conversion/**`, `mise.toml`, `mise.lock` | verify-parallel |
| Prose | `**/*.md`, `docs/**`, `.gitignore` | none |
| Label assist (opt-in) | `src/reflow/analyze/label_assist.py` | unit tests, label-assist eval, structure-assist eval, scan-field eval |
| Structure assist (opt-in) | `src/reflow/analyze/structure_assist.py` | unit tests, structure-assist eval |
| Scan fields (opt-in) | `src/reflow/analyze/scan_fields.py`, `src/reflow/analyze/scan_rules.py` | unit tests, scan-field eval |
| Docling and hybrid engines (opt-in) | `src/reflow/analyze/docling_layout.py`, `src/reflow/analyze/hybrid.py` | unit tests, `tests/pages/test_docling_order.py` |
| Models, transcription, vision, page vision and polish (opt-in) | `src/reflow/analyze/assist.py`, `src/reflow/analyze/vision.py`, `src/reflow/analyze/vision_gates.py`, `src/reflow/analyze/ask.py`, `src/reflow/analyze/local_model.py`, `src/reflow/analyze/direct.py`, `src/reflow/analyze/direct_html.py`, `src/reflow/analyze/form_repair.py`, `src/reflow/analyze/page_vision.py`, `src/reflow/analyze/proposals.py`, `src/reflow/analyze/polish.py` | unit tests |
| Progress-line protocol (read by the web build) | `src/reflow/progress_lines.py` | unit tests, web typecheck and lint, web unit tests |
| CLI, audit, worker budget, caches, provenance | `src/reflow/cli.py`, `src/reflow/checks/**`, `src/reflow/budget.py`, `src/reflow/cachedir.py`, `src/reflow/provenance.py` | unit tests |
| Stage cache | `src/reflow/stagecache.py`, `tests/stage_equivalence.py` | unit tests, `tests/pages/test_stage_equivalence.py` |
| Viewer CSS and JS | `src/reflow/viewer/**` | captures |
| Converter | `src/reflow/**` | unit tests, page checks, quick |
| A page spec | `tests/pages/specs/*.json` | page checks, -k: that page |
| A page-check function | `tests/pages/checks.py` | `tests/pages/test_checks.py`, page checks, -k: the specs whose checks use the changed functions |
| Page-check harness | `tests/pages/corpus.py`, `tests/pages/specs.py`, `tests/pages/test_pages.py`, `tests/conftest.py`, `tests/pages/fixtures/**` | unit tests, page checks, quick |
| Robustness harness | `tests/robustness/**` | robustness suite, `tests/test_robustness_oracle.py`, `tests/test_robustness_manifest.py` |
| Real badly made PDFs and their manifest | `samples/robustness/**` | robustness suite, `tests/test_robustness_manifest.py` |
| A test file | `tests/**/test_*.py` | that file |
| Label-assist eval and its data | `scripts/eval_label_assist.py`, `tests/label_assist/**` | label-assist eval, structure-assist eval, `tests/test_label_eval.py`, `tests/test_structure_eval.py` |
| Structure-assist eval | `scripts/eval_structure_assist.py` | structure-assist eval, `tests/test_structure_eval.py` |
| Scan-field eval and its data | `scripts/eval_scan_fields.py`, `tests/scan_fields/**`, `tests/prosepage.py` | scan-field eval, `tests/test_scan_eval.py` |
| Complexity-routing eval and its labels | `scripts/eval_complexity.py`, `tests/complexity/**` | `tests/test_complexity_eval.py` |
| Direct-transcription eval | `scripts/eval_direct.py` | `tests/test_direct_eval.py` |
| Other test helpers | `tests/**` | the test files that import it, else unit tests |
| Lint and its baseline | `scripts/lint`, `lint-baseline.json` | ruff and mypy |
| Held-out corpus and its fetch | `samples/heldout/**`, `scripts/fetch_heldout.py` | `tests/test_heldout.py` |
| Long-run jobs | `scripts/long-run` | `tests/test_long_run.py`, `tests/test_nightly.py`, `tests/test_gate.py` |
| Local gate | `scripts/gate` | `tests/test_gate.py` |
| Nightly runner | `scripts/nightly` | `tests/test_nightly.py` |
| Systemd units | `ops/**` | none |
| Web build helper | `web/scripts/build-process.py` | `tests/test_build_process.py` |
| Published-format schema and its contract | `schema/**` | unit tests, web typecheck and lint, web unit tests |
| Crossing scripts | `web/scripts/crossing/**` | web typecheck and lint, crossing tests |
| Web app and scripts | `web/**` | web typecheck and lint, web unit tests |
| Python dependencies | `pyproject.toml`, `uv.lock`, `.python-version` | unit tests, page checks, quick |
| Checkout lookup, for the scripts and tests | `scripts/checkout.py` | `tests/test_checkout.py` |
| Benchmarks and other scripts | `scripts/**` | none |
| A sample PDF | `samples/**` | page checks, -k: its document's specs |
| Python in `src/` and `tests/`, and `pyproject.toml`, `uv.lock` (in addition to its row above) | `src/**/*.py`, `tests/**/*.py`, `pyproject.toml`, `uv.lock` | ruff and mypy |
| The coverage record of verify-parallel-conversion, in `src/reflow/` (in addition to its row above) | `convert.py`, `pipeline.py`, `saved.py`, `run.py`, `output.py`, `analyze/engine.py`, `analyze/layout.py`, `analyze/pageplan.py`, `pool.py`, `analyze/prefetch.py`, `extract/parallel.py`, `extract/weights.py`, `extract/pagecontent.py`, `extract/tags.py`, `extract/links.py`, `render/pages.py`, `analyze/evidence.py`, `prepare.py`, `ingest/text_trust.py`, `ingest/witness.py`, `ingest/font_map.py` | verify-parallel |
<!-- end of generated table -->

## The held-out corpus

`samples/heldout/` lists public PDFs that only a scorer reads (`samples/heldout/README.md`). Never
open those files, read their converted output, or write specs, rules or fixes for them; read only
the score a scorer prints. A person chooses the documents. Fetch them with
`.venv/bin/python scripts/fetch_heldout.py`, which checks each file's hash.

## Testing

- The fast suite holds the unit tests and takes about half a minute:
  `.venv/bin/python -m pytest -o pythonpath=src -q`. Run without paths, `-m` or `-n`, it runs
  on pytest-xdist workers, as many as the machine's worker budget gives it (its first line says
  how many), and holds those slots until it ends. With no 2 slots free it runs serially, in
  about a minute and a half. A run of named files or tests stays in one process; add `-n <N>`
  to spread it. A session-scoped or module-scoped fixture runs once in each worker that needs
  it.
- The page-check suite runs per-page checks on the real sample PDFs in `samples/`. Its specs
  are in `tests/pages/specs/`. While you work on a branch, run it in quick mode, which converts
  only the tested pages of long documents:
  `.venv/bin/python -m pytest -o pythonpath=src -q -m pages --quick`. Add `-k <doc>` to narrow
  the run. Quick mode still runs the review-queue cap check (`-k queue`) on the documents of
  20 pages or fewer.
- The full page-check suite (`-m pages`) takes about ten minutes. It runs once, on main, after
  a merge. A branch does not need it.
- `scripts/nightly` runs the slow jobs on one commit of origin/main and keeps one report per
  commit: the full page checks, the robustness suite (its scorecard is `scorecard.md` in the
  job's folder; see `docs/robustness-scorecard.md`), the full `docs:build`, the three evals and
  the conversion benchmark. It runs only when someone starts it, by hand or from the optional systemd timer in
  `ops/systemd/`, which is not installed. Before you run one of these jobs on main, check
  `scripts/nightly status`. If the report in `~/.cache/reflow/nightly/latest/` is for the current
  origin/main and has the job, read its `summary` and the job's `log` instead of running it.
  Otherwise run the job as the rest of this file says.
- Start any job that takes more than a few minutes (a test suite, `docs:build`, a benchmark)
  as a background job through `scripts/long-run <name> -- <cmd>`, and keep working while it
  runs. It writes the full output to `.worktree/runs/<name>-<time>/log`. When the job ends, it
  writes `status` (passed, failed or killed, with the exit code, wall time and commit) and
  `summary`, which lists only the failures. For pytest, `summary` has one line per failing
  test. For other commands, it has the lines that match an error pattern and the end of the
  log. Read `summary` first, and open `log` only when the summary doesn't explain a failure.
  `.worktree/runs/<name>-latest` is the newest run of that name, and
  `scripts/long-run status` lists the newest run of each name.
- While you iterate on a change, pass `--maxfail=1` (or `-x`) to pytest, so a broken change
  fails in minutes. Drop it for the last run before you file the PR.
- In a worktree, use the venv of the main checkout: `<main checkout>/.venv/bin/python`.
  The conversion cache lives in the main checkout, and every worktree shares it. Don't delete
  `.pytest_cache/reflow-page-checks*`: a page-check session ends by evicting from them by size
  and age (`prune` in `tests/pages/corpus.py`), and never removes an entry another run is
  reading. The tests also read the gitignored samples
  (`samples/docaccess`, `samples/azblue/site`) from the main checkout, so you have nothing to
  copy into a fresh worktree.
- Run the web checks with `cd web && bun run typecheck && bun run lint`.
- ruff and mypy run through `.venv/bin/python scripts/lint` (the main checkout's venv has the
  `lint` group). The code was never clean, so the script holds it to `lint-baseline.json`, the
  count of problems per file and rule: a change may not add a problem, and need not fix the old
  ones. When a change fixes some, run `scripts/lint --update` and commit the smaller baseline.
- The page checks test text and structure, not looks. For a change to how the transcript
  looks, capture screenshots of a fixed set of five documents (a flyer, an RFP, an appeal form,
  an audit and a scanned page; the set is `DOCS` in `web/scripts/capture.ts`). Run
  `cd web && bun run capture` with this worktree's dev server running (`scripts/dev start`; source
  `.worktree/env` so `REFLOW_PORT` names its port, or pass `--base`; the default is 3417). It saves
  the transcript view at 1440x900 and 390x844, and the downloaded HTML at both sizes, in
  `web/.captures/<commit>/` of the main checkout, which every worktree shares. Then
  `bun run capture compare main <commit>` puts before and after side by side in
  `web/.captures/compare/`. A ref with no capture yet is captured from a temporary worktree
  with its own dev server (`--port`, default 3417, so stop yours first or pass another port).
  Attach the composites to the PR. The captures read the converted output in `web/public/docs`, so a
  change to the Python renderer shows only after `bun run docs:build --stale` of those
  documents. `capture` refuses documents that are not current.
- Every conversion on the machine (the CLI, so `build-docs` too, and the page-check harness)
  shares one budget of worker processes (`src/reflow/budget.py`): CPU count minus 2 slots,
  fair shares between the runs converting at once. `REFLOW_WORKER_BUDGET` sets the size, and
  0 turns it off.
- The stages that load a model on the GPU (docling, the local alt-text model, and every request
  to a local Ollama server) also share a GPU lease in the same file: two holders at once, and
  the local alt-text model (17 GB) alone. A stage waits for it, and `convert` prints the wait
  next to the stage's time ("docling 14s (9s waiting for the GPU)"). `REFLOW_GPU_HOLDERS` sets
  the holders, and 0 turns it off. The lease covers only reflow's own requests. Ollama is a
  separate server: it keeps a model loaded for a few minutes after a request, and it serves
  other clients (a chat, `ollama run`, a model you pull) without the lease. Before you measure
  GPU time or memory, check `nvidia-smi` and `ollama ps` for what else is loaded.
- Several jobs can run tests on one machine at the same time. Never kill processes by a broad
  pattern, such as `pkill -f pytest` or `pkill node`. Stop only the PIDs that you started.

## Models

- The opt-in AI assist (`docs/assist.md`) runs on a local model through Ollama by default, or on the Anthropic API
  with the user's own key.
