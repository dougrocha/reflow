# Real badly made PDFs

`MANIFEST.tsv` lists real PDFs that tools made badly, grouped by the tool that made them.
The robustness harness (`tests/robustness/`, `docs/robustness-scorecard.md`) converts each one
and reports it by that tool's family. Its damage kinds say how reflow handles damage we can
imagine. These files say which quirks real tools produce.

The files were picked because they are bad. Their counts describe this manifest, never how
common a quirk is among the PDFs users upload.

## The manifest

One row per file, tab-separated. Lines that start with `#` are comments.

| Column | What it holds |
| --- | --- |
| `id` | A short name: lower-case letters, digits, dots and dashes |
| `path` | Where the file lives, from the repository root |
| `sha256` | The file's SHA-256. The harness and the fetch script refuse a file with another hash |
| `producer`, `creator` | The PDF's `/Producer` and `/Creator`, as the file states them |
| `family` | The tool family, `family_of(producer, creator)` in `tests/robustness/real.py` |
| `source_url` | Where the file was retrieved from. `-` only for a file that is already in a gitignored sample folder and has no URL on record. `ZIP#member` names one member of a remote zip |
| `retrieved` | The retrieval date |
| `license` | The licence, or why there is none to rely on |
| `redistribute` | `yes` when the licence allows us to commit the file, else `no` |
| `language` | The text's language (BCP 47; `mul` for several) |
| `pages` | The page count |
| `quirk` | The defect observed in this file, checked by hand against the rendered page |
| `damage_id` | The id of its synthetic twin in `KINDS` (`tests/robustness/kinds.py`), or empty |
| `transcript` | A hand transcript in `transcripts/`, or empty |

`tests/test_robustness_manifest.py` checks the rows: every `damage_id` is a kind (so removing or
renaming a kind that a row names fails it), each family is the one its producer and creator
give, no file that may not be redistributed is committed, and each committed file has the hash
the row records. A row leaves `damage_id` empty only when no kind makes its quirk: the Paper
Capture OCR layer that misreads words, and the veraPDF file whose only fault is a font that is
not embedded.

A row with a hand transcript is scored: the words reflow publishes must be the transcript's, in
its order, or the twin kind's fact must flag the document. A transcript is plain UTF-8 text with
a form feed between pages. A row without one is inventory: the scorecard reports the facts reflow
raised on it, and it moves no failure rate.

## Where the files live

- `files/`: committed. Only files whose licence allows redistribution, and only small ones.
- `fetched/`: gitignored. `scripts/fetch_robustness_corpus.py` downloads every row that is not
  here into the main checkout's `fetched/`, and keeps a file only when its hash is the row's.
  Every worktree reads it from there, as it reads `samples/docaccess/`. For a SafeDocs row it
  reads the zip's directory and the one member with HTTP range requests, so it never downloads
  a whole zip (each is 1 to 3 GB).
- Rows may also point at samples the repository already has: committed ones in `samples/` and
  ones in `samples/azblue/site/`, which are not redistributed here.

```sh
.venv/bin/python scripts/fetch_robustness_corpus.py --list   # each row, and whether it is here
.venv/bin/python scripts/fetch_robustness_corpus.py          # fetch missing files that have a recorded URL
```

## Sources and licences

- **veraPDF corpus** (`verapdf-*`, committed in `files/`). From
  https://github.com/veraPDF/veraPDF-corpus (branch `staging`), PDF/UA-1 "fail" test files.
  © the veraPDF consortium, licensed under Creative Commons Attribution 4.0 International
  (https://creativecommons.org/licenses/by/4.0/). The files are unchanged. The veraPDF Test
  Builder made them by hand to break one rule each, so their family is the test builder, not a
  real authoring tool.
- **pdf.js test corpus** (`pdfjs-*`, fetched). From the `text` tests of
  https://github.com/mozilla/pdf.js/blob/master/test/test_manifest.json. The repository is
  Apache-2.0, but these PDFs came from users' bug reports and carry no licence of their own, so
  they are not committed.
- **SafeDocs CC-MAIN-2021-31-PDF-UNTRUNCATED** (`safedocs-*`, fetched). From
  https://digitalcorpora.org/corpora/file-corpora/cc-main-2021-31-pdf-untruncated/. The files
  were picked from its Tika metadata (`tika-20230714.csv.gz`, streamed and filtered, never
  stored): a damaged font, at least 20% unmapped characters, or an out-of-vocabulary rate of
  0.85 or more over 200 tokens. The `license` column names each file's original URL from
  `cc-provenance-20230303.csv.gz`. Their copyright belongs to whoever published them, so they
  are not committed. Common Crawl's terms of use apply: https://commoncrawl.org/terms-of-use.
- **Samples already in the repository**: the Navajo notice
  (`samples/azblue/site/`, not redistributed here), and committed public-domain files from `samples/` and
  `samples/corpus/` (see `samples/README.md` for their sources).

## What the sources did not yield

- **Old arXiv papers made with dvips.** Five papers from 1993 to 2002 (hep-th/9711200,
  astro-ph/9805201, math/0211159, cond-mat/9501001, gr-qc/9310026) are served as PDFs that arXiv
  made again with Ghostscript 9.22 or 10: their text layers read correctly, and cond-mat/9501001
  uses Type 1 fonts, not Type 3. The dvips Type 3 row comes from SafeDocs instead.
- **Chrome (Skia) and Firefox (cairo).** Among the files the SafeDocs filter kept, none from Skia
  or cairo was an English file of one to four pages with 30% or more unmapped characters. pdf.js's Skia file (issue 7878) reads correctly. These producers
  stay without a row until a file shows their quirk.
