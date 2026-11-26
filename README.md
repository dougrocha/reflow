# reflow

reflow turns PDFs into accessible HTML that loads fast. It builds the output to meet WCAG 2.1 AA
and Section 508. The HTML has real headings, lists, and tables with headers. reflow checks the
reading order against the PDF's own tags, and each figure carries alt text or a flag for review.
PDF/UA export is planned but not built yet. [SPEC.md](SPEC.md) is the full specification, and
SPEC §3 describes each pass of a conversion.

The converter is a Python CLI (`reflow`). A Next.js app in `web/`, which runs on bun, shows the
converted documents next to the original PDF. Each conversion writes a `review.json` file that
lists the items a person still has to check, such as figures without alt text.

## Requirements

reflow needs these tools:

- Python 3.12, as set in `.python-version`
- [uv](https://docs.astral.sh/uv/), for the Python dependencies
- [bun](https://bun.sh/) 1.4 or later, for the web app

These tools are optional:

- OCRmyPDF and Tesseract, to OCR image-only pages
- A GPU, which speeds up the `docling` engine and the local models. Both also run on the CPU.
- [Ollama](https://ollama.com/), for the opt-in AI assist

`reflow doctor` reports which of the optional tools this machine has.

## Install

```sh
uv sync                    # core converter and dev tools
uv sync --extra docling    # optional ML layout engine (docling) and local alt text, large download
uv sync --extra anthropic  # optional Anthropic API provider for the AI assist (docs/assist.md)
```

## Convert a PDF

```sh
uv run python scripts/fetch_samples.py    # once: downloads samples/resnet.pdf and samples/arxiv.pdf from arxiv.org
uv run reflow convert samples/resnet.pdf -o out
```

The command prints a summary. It shows the title and where the title came from, the counts of
chunks, headings, figures, and tables, the review items, and the seconds that each stage took.

reflow builds the output beside the output directory, and swaps it in only when the conversion
succeeds. If a conversion fails, the previous output stays as it was.

## Output files

The output directory holds the site and the data of the conversion:

- `index.html`, `chunks/`, `toc.json`, `manifest.json`, and `assets/` make up the accessible
  HTML view.
- `pages/` holds the page images for "view original", and `figures/` holds the figure crops.
  Both are WebP.
- `review.json` lists the items that a person still has to check.
- `original.pdf` is the input, byte for byte.
- `working.pdf`, `document.json`, `layout/`, `trace/`, `passages.jsonl`, and the alt-text and
  assist caches are working files, not for readers. "Ask a question" searches
  `passages.jsonl`.

## Convert options

| Option | Default | Meaning |
| --- | --- | --- |
| `-o, --output DIR` | `out` | Output directory |
| `--password [TEXT]` | `REFLOW_PDF_PASSWORD` or empty | Omit TEXT for a hidden prompt; an explicit value is visible in the process list |
| `--no-page-images` | images on | Skip the page images for "view original" |
| `--page-widths` | `800,1200` | Page image widths in px, in ascending order. The first width is the default image, and the others are `srcset` candidates. One width turns `srcset` off |
| `--srcset-max-pages` | `300` | A document with more pages gets only the first width. `0` means no limit |
| `--engine` | `heuristic` | Layout engine: `heuristic`, `docling`, or `hybrid`. `hybrid` runs the heuristics, with docling on hard pages |
| `--device` | `auto` | Device for docling and the local models: `auto`, `cuda`, `mps`, or `cpu`. `auto` picks cuda, then mps, else cpu. Naming a GPU that is not available is an error |
| `--alt-text` | `none` | `none` lists the figures without alt text in `review.json`. `local` also drafts suggestions with an on-device model. `local` needs the docling extra, and a GPU makes it much faster |
| `--assist` | `none` | `local` reads the writing OCR could not read, and hard scans, with a local vision model through Ollama (`qwen3-vl:8b`). A reading that passes its checks against the crop goes under it as a draft, listed in `review.json`. A signature, sent only to a model on this machine, gets its signer's name only when the model's reading matches the name its line prints in a name field ("Print Name:"). See [docs/assist.md](docs/assist.md#reading-unread-crops) |
| `--ocr` | `auto` | `auto` runs OCRmyPDF when it is installed, and flags the pages when it is not. It reads image-only pages, pages of unreadable text, and the textless parts of a page image in an untagged document. It redoes the text layer and keeps the page's images and visible text, except on pages of outlined or unreadable visible text, which it rasterizes. A page with a readable invisible layer keeps it. `none` never runs OCRmyPDF. `ocrmypdf` requires it |
| `--allow-restricted` | off | Convert a PDF whose owner forbids text extraction, and flag it. Without this option, reflow refuses such a PDF |
| `--scan-fields` | off | Find the form fields of a scanned form that has none. Rules read the page image and keep the places that they are sure of. No model runs |
| `--stage-cache DIR` | off | Keep the reading of each PDF (working copy, OCR, extraction) in DIR, and reuse it when the same PDF is converted again with the same reading code and package versions. The output is the same either way. See [docs/provenance.md](docs/provenance.md) |
| `-j, --jobs N` | CPU count minus 2, at least 1 | Worker processes. `1` runs everything in one process. The output is the same either way |

[Use the opt-in AI assist](#use-the-opt-in-ai-assist) describes the assist options:
`--assist-labels`, `--assist-structure`, `--assist-scan-fields`, `--assist-provider`,
`--assist-model`, and the `--assist-*-max-calls` caps.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | The conversion succeeded |
| 1 | reflow could not open the PDF |
| 2 | An option is bad, or the model provider, the OCR tool, or the password is missing |
| 3 | The PDF forbids text extraction, and `--allow-restricted` was not given |

## Use the opt-in AI assist

You can put three questions to a model. Each question is off until you turn it on:

- `--assist-labels` asks which printed caption labels a form field that the rules could not
  name.
- `--assist-structure` asks whether a doubtful heading on a form is a heading, a label, or a
  value.
- `--assist-scan-fields` asks which doubtful places on a scanned form are fields. It implies
  `--scan-fields`. It also asks the `--assist-labels` question about the fields that it found.

By default, the model runs on your machine through Ollama (`gemma4:e4b`):

```sh
ollama serve
ollama pull gemma4:e4b
uv run reflow convert samples/fw9.pdf -o out --assist-labels
```

To use the Anthropic API instead, install the `anthropic` package, set `ANTHROPIC_API_KEY`,
and pass `--assist-provider anthropic`. To pick another model, pass `--assist-model`.

Each assist asks at most 25 uncached questions per document. reflow caches the answers in the
output directory, so a rerun asks nothing new. [docs/assist.md](docs/assist.md) lists what
reflow sends and how it checks each answer.

## Other commands

The sample PDFs are in `samples/`. [samples/README.md](samples/README.md) describes them.

### Inspect a PDF

To inspect a PDF without converting it, run `reflow inspect`:

```sh
uv run reflow inspect samples/resnet.pdf          # metadata and page classes
uv run reflow inspect samples/resnet.pdf --json   # the document model as JSON
```

### Audit the source PDF

To check the source PDF against the SRC-* checks in SPEC §7, run `reflow audit`. The command
exits 1 when a check fails. A long tag tree is read on a pool of workers (`-j`, within the
machine's worker budget); the report is the same either way. `--format` prints the report as
a table (the default), `json`, `markdown` or a standalone `html` page.

`--verapdf` also validates the file against PDF/UA-1 with [veraPDF](https://verapdf.org)
(`paru -S verapdf`, which needs Java) and adds its failed rules to the report. A
non-compliant file then exits 1 too. Without `verapdf` on PATH, or for a file opened with a
password (veraPDF takes a password only on its command line), the veraPDF section says
`skip` and does not change the exit code.

```sh
uv run reflow audit samples/fw9.pdf
uv run reflow audit samples/fw9.pdf --json
uv run reflow audit samples/fw9.pdf --verapdf --format html > fw9-audit.html
```

### Convert several PDFs

To convert several PDFs on one shared process pool, run `reflow convert-many`. Its arguments
are pairs of a PDF and an output directory:

```sh
uv run reflow convert-many samples/resnet.pdf out/resnet samples/fw9.pdf out/fw9
```

The command prints one `ok` or `FAILED` line for each document. A failed document does not
stop the others, and the command exits 1 if any document failed.

`--max-docs` sets how many documents convert at the same time. The default is 2.
`convert-many` takes the same `-j`, `--no-page-images`, `--page-widths`, `--srcset-max-pages`,
`--engine`, `--device`, `--alt-text`, `--assist`, `--ocr`, and `--stage-cache` options as `convert`. With
`--progress-json`, it also prints its progress as JSON lines, for another program to read.

## Web app

The Next.js app in `web/` lists the converted documents. It opens each document in a viewer,
with the transcript beside the page images. The app has these pages:

- `/` lists the documents, and `/docs/<slug>` gives an overview of one document.
- `/view/<slug>` is the viewer. Its sidebar is an accordion of the table of contents and "Ask a
  question".
- `/inspect/<slug>` shows the page image with the boxes, the roles, and the traces behind each
  decision.
- `/review` lists the review items of every document.
- `/builds` lists the conversion jobs. On this page, you can rebuild a document, see the queue
  and the progress, cancel a job, and retry it. The page is on under `bun run dev`. In a
  production build, it is on only with `REFLOW_BUILDS=1`. See
  [docs/builds.md](docs/builds.md).

### Run the web app

`bun run docs:build` converts the sample PDFs into `web/.reflow/`. It copies the public files
to `web/public/docs/` and each PDF to `web/public/pdfs/`, where the web app reads them.

```sh
cd web
bun install
bun run docs:build resnet fw9   # convert some samples; no slugs converts every sample whose PDF is present
bun run dev                     # development server
```

`docs:build` passes `REFLOW_ENGINE` and `REFLOW_ALT_TEXT` through to `reflow convert-many`. It
records what each conversion read. `bun run docs:check resnet` says whether a document is
current against your checkout, and `bun run docs:build --stale resnet fw9` rebuilds only the ones
that are not. See [docs/provenance.md](docs/provenance.md).

### Web app scripts

| Script | Purpose |
| --- | --- |
| `bun run build` | Production build |
| `bun run start` | Serve the production build. Run `build` first |
| `bun run typecheck` | Generate the Next.js types and run `tsc --noEmit` |
| `bun run lint` | Oxlint |
| `bun run test` | Unit tests (`bun test src`) |
| `bun run a11y resnet fw9` | Accessibility check of the viewer pages. Needs a running server |
| `bun run ask:e2e` | Browser check of "Ask a question" against a fake model. Run `build` first |
| `bun run ask:eval` | Golden questions for "Ask a question". Measures the retrieval recall, or with `-- --model`, the real model. Refuses documents that are not current (`-- --allow-stale` measures anyway) |
| `bun run docs:check [slug ...]` | Whether each document is current, stale, missing or unverifiable against this checkout, and why ([docs/provenance.md](docs/provenance.md)) |
| `bun run ask:fake` | A fake Ollama, to try "Ask a question" with no model |
| `bun run ask:ollama` | Checks with real processes that the dev server starts and stops its own Ollama |
| `bun run check:crossing` | Production check of the overtaking-render case. Rerun it after a Next upgrade (`docs/builds.md`, Known limits) |

### Ask a question

"Ask a question" answers a question from the document. It shows where the answer is: the quote,
its page, and the place highlighted on the page image or in the transcript. The server finds the
passages and asks a model about them. By default it asks a local Ollama model (`gemma4:e4b`).
It can ask the Anthropic API with your own key instead. Before it shows anything, the server
checks that the quote is really in the document.

If no Ollama is running, `bun run dev` starts its own. Pull the model first with
`ollama pull gemma4:e4b`. [docs/ask.md](docs/ask.md) covers the setup, the privacy, the
environment variables, and the measured numbers.

## Tests

### Run the unit tests

The default run holds the fast unit tests and leaves out the page-check suite:

```sh
uv run pytest -q
```

Tests of the optional docling engine and the local models are skipped unless you install
them with `uv sync --extra docling`.

### Run the page checks

The page checks convert the sample PDFs and check chosen pages against the expected results.
Quick mode converts only the tested pages of long documents, which is enough while you work on
a branch. The full suite takes about ten minutes.

```sh
uv run pytest -m pages -q --quick          # quick mode; add -k <doc> to narrow
uv run pytest -m pages -q                  # every page of every document
uv run python tests/pages/report.py        # pass, xfail, and fail per page
```

The conversions are cached in `.pytest_cache/reflow-page-checks/`, keyed by the code.
[tests/pages/README.md](tests/pages/README.md) describes what the suite checks and how to add
pages.

### Regenerate the corpus scorecard

The corpus scorecard, [docs/corpus-scorecard.md](docs/corpus-scorecard.md), has one row per
document. A row shows the page-check outcomes, the word recall, the review items, and the
conversion speed. After a full page-check run, regenerate the scorecard:

```sh
uv run python tests/pages/report.py --write
```

### Check parallel conversion

To check that `--jobs N` converts as `--jobs 1` does, run the command below. It checks a TLA+
model of the orchestration of a conversion, and runs Hypothesis tests on the real code. It needs
[mise](https://mise.jdx.dev/) for Java.

```sh
mise run verify-parallel
```

### Check the web app for accessibility

To check the web app for accessibility, start the server in one terminal. Then run the check in
another terminal:

```sh
cd web
bun run start &          # after bun run build, or use bun run dev
bun run a11y resnet fw9
```

The check needs Chromium for Playwright (`bunx playwright install chromium`).

## Lint and type-check the code

```sh
uv run ruff check src tests
uv run mypy src/reflow --ignore-missing-imports
```

The lint tools come from the `lint` dependency group (`uv sync --group lint`). The Ruff and mypy
settings are in `pyproject.toml`. Mypy is advisory until the existing code passes it.

## Documentation

- [SPEC.md](SPEC.md): the product and accessibility specification, and the conversion passes
- [TODO.md](TODO.md): the open work
- [CONTRIBUTING.md](CONTRIBUTING.md): the rules for contributors
- [docs/ask.md](docs/ask.md): "Ask a question" in the web app
- [docs/assist.md](docs/assist.md): the opt-in AI assist and what it sends
- [docs/builds.md](docs/builds.md): the conversion jobs in the web app
- [docs/corpus-scorecard.md](docs/corpus-scorecard.md): the corpus scorecard
- [docs/project-guide.html](docs/project-guide.html): a visual guide to the project
- [docs/research/](docs/research/): research notes
- [tests/pages/README.md](tests/pages/README.md): the page-check suite
- [samples/README.md](samples/README.md): the sample PDFs and what each one covers

For encrypted PDFs, `inspect`, `convert`, and `audit` accept `REFLOW_PDF_PASSWORD`.
Use `--password` without a value for a hidden prompt. `--password <value>` still works,
but warns because the password is visible in the process list.
