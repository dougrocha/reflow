# Page-check regression tests

The page-check suite checks the converter's output on the sample PDFs in `samples/`, one page at
a time. Each check converts a real document, so the suite is slow. It is opt-in, and the default
`.venv/bin/python -m pytest -o pythonpath=src -q` run skips it.

## Run the page checks

Run quick mode while you work on a branch. Run the full suite (`-m pages`, about ten minutes) once
on main after merging.

    .venv/bin/python -m pytest -o pythonpath=src -q -m pages                   # every page check (converts every document)
    .venv/bin/python -m pytest -o pythonpath=src -q -m pages -k fw9            # only pages whose id contains "fw9" (converts fw9 only)
    .venv/bin/python -m pytest -o pythonpath=src -q -m pages -k "p17-138"      # one page
    .venv/bin/python -m pytest -o pythonpath=src -q -m pages --quick           # only the tested pages of long documents: minutes, not ten
    PYTHONPATH=src .venv/bin/python tests/pages/report.py                      # a pass, xfail or fail status per page
    PYTHONPATH=src .venv/bin/python tests/pages/report.py -v pima              # with the message for each failure and xfail
    PYTHONPATH=src .venv/bin/python tests/pages/report.py --quick --scorecard  # plus a per-document scorecard
    PYTHONPATH=src .venv/bin/python tests/pages/report.py --write              # write docs/corpus-scorecard.md (full run)

Make the committed scorecard from a full run on an idle machine.

## Add a page check

1. Convert the document:
   `uv run reflow convert samples/<file>.pdf -o /tmp/out --no-page-images`.
2. Look at the page image. After a site build, it is in
   `web/public/docs/<slug>/pages/NNNN.webp`.
3. Write the spec. Assert what is correct on the image. For a real defect, use `xfail` with a
   reason that names the defect.
4. Run `PYTHONPATH=src .venv/bin/python tests/pages/report.py -v <id>` until it shows only `pass` and `xfail`.

## Each spec describes one page

`specs/<doc>-<page>.json` describes one page of one sample document:

```json
{
  "doc": "fw9",
  "page": 1,
  "description": "...",
  "assertions": [
    {"min_recall": 96.5},
    {"heading": {"text": "Part II Certification", "levels": [3, 3]}},
    {"xfail": "why this is a known open defect",
     "contains_in_order": ["Signature of", "U.S. person", "Date", "General Instructions"]}
  ]
}
```

Specs use 1-based PDF page numbers. Each assertion is one test. The assertions check structure and
words, not markup, so an improvement to the markup does not break them.

The table lists every check kind.

| check | holds when |
|---|---|
| `min_recall` | The word recall of the page transcript against the PDF text layer (pypdfium2) meets the numeric floor, or the `floor` in a reference object. |
| `contains` | Each snippet is on the page. |
| `doc_lang` | The document shell has the exact `<html lang>` tag. |
| `block_lang` | Each `{"text": "snippet", "lang": "es"}` snippet has that language on its text or on an ancestor within the page. Nested overrides apply. A `lang` of `null` wants no language on the snippet: the document's. |
| `shell_contains` | Each snippet is in the document shell, which the page checks cannot see. The shell is the header with the title, and the pages of the first chunk. |
| `contains_in_order` | The snippets are on the page, each after the previous one, in reading order. |
| `links` | Each entry is a link on the page: an `<a>` with that `text` whose `href` is the given `href`, that goes to `page` N, or (with `"internal": true`) that goes to any page of the document. Use `internal` for p17 and other long documents. A `--quick` excerpt renumbers the pages it keeps, so a page number holds only in the full run. |
| `emphasis` | Each entry `{"text": "...", "kind": "strong"}` (or `"em"`, `"red"`, `"green"`) is emphasis on the page: some `<strong>` (or `<em>`, or `<span class="ink-red">`) element whose text, normalized, equals `text`. |
| `superscripts` | Each entry `{"text": "1", "after": "each year."}` is a superscript on the page: a `<sup>` with that `text`, right after the words `after` in the same block. A `<sup>` around a note reference counts. |
| `footnotes` | The page has this many `doc-footnote` asides. |
| `noterefs` | The page has this many `doc-noteref` links. Each one points at a footnote on the page whose backlink points back. |
| `no_text` | None of the strings is on the page. Use it for `�`, letter-spaced junk and dropped words. |
| `no_blank_runs` | No run of three or more underscores is on the page: every printed blank became a field. |
| `same_block` | Each pair is in one paragraph, list item, table cell or table row. |
| `separate_blocks` | Each pair is not in one block, for example two index entries that were merged. |
| `same_entry` | A heading that holds the name is followed, before the next heading, by the text. This is a directory entry, with a name and then its address. |
| `field_labels` | Each text (a key) is the text of exactly that many labels (the value), and each label is bound with `for=` to a control. Neighbouring captions are separate labels. |
| `heading` | A heading with that text (exact, or with `"contains": true`) is at a level in `levels` (default 1-6). |
| `not_heading` | None of the texts is a heading. |
| `no_block` | None of the texts is a whole body block, such as the labels of a chart set as running text. |
| `in_figure` | Each text is a whole run of words inside some figure, in its labels or its caption. |
| `max_headings` | The page has at most that many headings. |
| `table` | Some table has at least `rows` rows and `cols` columns, and holds every cell in `cells` (exact cell text). `"exact": true` makes `rows` and `cols` equalities. The table then has exactly `rows` rows, and every row covers exactly `cols` grid columns, counting `colspan` and `rowspan`. `"row_headers": true` also requires every body row to start with a `th scope="row"`. |
| `table.cell_contains` | Each entry checks a substring within selected cells of an anchored row. For example, `{"row": {"col": 1, "text": "56×56"}, "cols": [2, 3, 4, 5, 6], "text": "3×3 max pool, stride 2"}` requires an exact whole-cell match at the anchor column of the row, then the substring in at least one selected column of that same row. Columns are zero-based logical grid positions, including spans. Text uses the same normalization as `cells`, and `cells` still requires whole-cell matches. |
| `max_table_cols` | No table on the page is wider than that many logical columns, counting `colspan` and `rowspan`. A printed form ruled into boxes can come out as one table of its page, thirty columns across and clipped on the right: its boxes are not columns. |
| `tables`, `figures` | The page has exactly that many tables or figures. |
| `unread_figures` | The page has that many unread figures (`figure.unread`): a count, or `{"min": n, "max": n}`. |
| `calendar_month` | `{"caption": "JULY 2024", "year": 2024, "month": 7}`: exactly one table has that caption, its first row is 7 `th`, and its body cells give the dates 1 to N in order, each in its weekday's column (Sunday first, from Python's `calendar`). Dates are compared as integers, so `"01"` passes; assert the printed form with `table`. |
| `review_has`, `review_lacks` | Codes of facts with a severity on the page, or on any node of it, in the `document.json` ledger (not `review.json`, which names only the first error and warning of a node). The page must have, or must not have, each. |
| `list_items` | A `ul` (or an `ol` when `"ordered"` is set) holds the items, in order (exact item text). |
| `nested_list` | The list item that starts with `parent` has a nested list that holds `items` in order. |
| `fields` | The page has exactly these controls: `{"text": n, "select": n, "checkbox": n, "radio": n}`. Kinds left out must be absent. |
| `field_group` | A fieldset whose legend is `legend` holds checkboxes, radios or text boxes labelled `options`, in order. Each label is bound to a control inside the fieldset. With `type` (`radio`, `checkbox` or `text`) every control in it is of that type; radios also share one `name` and each has a `data-pdf-field`. |
| `fieldsets_named` | `true`: every fieldset has a legend with words in it. A legend hidden from sight (`sr-only`) must have its words printed elsewhere on the page too (the heading right above the group). |
| `ruled_box` | `{"name": "MAIL TO: ...", "texts": [...]}`: a framed group (`section.ruled-box`, role `group`) whose accessible name is `name` (`null` for none) starts with the first text and holds them all in order. With `"filled": true` it is the tinted callout (`.filled-box`) instead, an `aside` when `"tag": "aside"`. |
| `labelled_once` | Each text is the label of exactly one control and is on the page exactly once. |
| `control_names` | Each text is the accessible name of at least one control (`aria-label`, else its bound label). |
| `control_order` | The texts are the accessible names of controls in this order (other controls may come between them). |
| `labels_in_names` | `true`: every control with both a bound label and an `aria-label` has the label's words in its name, or the name's in the label. |
| `no_control_names` | No control is named any of the texts. |
| `field_rows` | Each entry lists the names of the controls that share one row, in order. |
| `alt_has` | Each pattern matches the alt of at least one `<img>` on the page: a substring, or a regular expression when it starts with `^`. It fails on a page with no `<img>`. |
| `alt_not` | No `<img alt>` on the page matches any pattern. A spec that uses it must also have a `figures` or `min_figures` check, so a page that lost every picture cannot pass. |

The doc checks read the converted document, not the page's HTML. In quick mode they read the
excerpt's page that holds the spec's source page.

| check | holds when |
|---|---|
| `review_has`, `review_lacks` | Each code is (or is not) among the review facts of the page: the facts with a severity on the page itself or on any node of the page, nested ones included, read from `document.json`. Not from `review.json`, whose items hold only the first code of each severity. |
| `title_suspect` | The document's title is (`true`) or is not (`false`) sent to review: a `title-suspect` fact on the root of `document.json`. Document-wide, like the title. |
| `toc_lacks` | None of the texts is an entry of the Contents (`manifest["toc"]`). Document-wide: in quick mode the Contents holds only the converted pages. |
| `max_figure_share` | No figure of the page covers more than this share of the page. |
| `min_figures` | The page has at least this many figures (`figures` asserts an exact count). |
| `box_on_page` | `{"name": "...", "bbox": [x0, top, x1, bottom]}`: exactly one ruled box of the page has that name, its box is `bbox` (within `tol` points, default 2) and every block it holds lies inside it. |
| `max_review_items` | The page has at most this many `review.json` items. |
| `unread_over_text` | No unread figure (writing OCR could not read, shown as a crop) overlaps a block whose text holds one of the printed strings. |
| `signature_items` | The page has exactly this many `review.json` items that target a signature (`unread-signature`). |
| `figure_order` | The entries come in this order among the page's nodes in `document.json`, skipping a figure's children. A text entry is a node whose text holds it. `"<figure kind=signature>"` is a signature figure, and it must come directly before the node of the text entry that follows it. |

The checks collapse whitespace and fold curly quotes to straight ones before they compare text.
Matching is case-sensitive. Take the expected text from the page image. The PDF text layer must
agree with it.

## Known defects are strict xfails

An assertion with an `"xfail": "reason"` key is a known defect. The xfail is strict, so the test
must fail. When a fix makes it pass, pytest reports XPASS and the run fails. Then remove the
`xfail` key from that assertion, so the check guards the fix from then on. `report.py` shows the
same state as `XPASS`.

Assertions with no `xfail` key must pass. A failing one is a regression.

An assertion with `"xfail_without_ocr": "reason"` is a strict xfail only when
`reflow.ingest.ocr.make_runner("auto")` returns no runner. With OCR available, it must pass. Both
pytest and `report.py` use this rule. In `scan-1`, the recall, `contains` and `contains_in_order`
checks use it, and the heading and list defects stay unconditional strict xfails.
`az-remittance-advice-scan-1` and `az-tucson-employee-booklet-44` use it too.

A defect in the converter is not the only cause of a failure. A missing page, a conversion error or
a bad snippet in a spec fails as an error, because xfail accepts only assertion failures.

## Recall floors sit below the measured value

`min_recall` floors sit about 1.5 points below the value measured on main when the spec was
written. Recall uses the text layer from pypdfium2, which reads some ligatures and hyphenated words
differently from pdftotext. So recall can come out lower than a pdftotext comparison. Raise a floor
when a fix raises the recall. Never lower a floor to make a test pass.

A number compares the transcript with the text layer of that same page. A scanned page has no text
layer, so name a reference page that has one:

```json
{"min_recall": {"floor": 60, "reference": {"doc": "fw9", "page": 1}}}
```

`reference.doc` is a corpus document slug, and `reference.page` is a positive, 1-based page
number. The check reads the text layer of that page with pdfium and does not convert the reference
document. Recall counts the reference words that the transcript of the tested page recovers,
including repeated words. An empty source text layer fails with `no source text on this page`.

## Quick mode converts an excerpt of each long document

`--quick` (or `REFLOW_PAGE_CHECKS_QUICK=1`) is for work on a branch. It converts documents of up to
20 pages whole, as usual. It converts each longer document from an excerpt. The excerpt is a copy
of the PDF that keeps these pages:

- the tested pages, with two pages on either side, so running headers still repeat
- the first six pages
- 40 pages spread through the document, so the font statistics see every heading size

The excerpt keeps the tag tree, the form fields and the outline for the pages it keeps.

Some results depend on the whole document, so quick mode changes two things:

- On an excerpt, it checks that a heading exists but not its level. Heading levels rank font
  sizes over the whole document.
- It skips `test_calibration.py`, which counts review items over whole documents. The exception
  is `test_queue_stays_usable`. That test still checks the 5% review-queue cap over the
  documents that quick mode converts whole, and lists the excerpted ones it skipped.

The full suite converts `bluejourney-directory` (2,640 pages) whole. It adds about as much time as
`maricopa-directory`.

## The scorecard sums up each document

`report.py --scorecard` prints one row per document, with these columns:

- `pages`: the number of page checks
- `pass`, `xfail`, `fail` and `xpass`: the assertion counts
- `mean recall` and `mean floor`: the mean measured recall and the mean `min_recall` floor over
  its page checks
- `review err` and `review warn`: the errors and warnings in its `review.json`
- `convert s` and `pages/s`: the conversion seconds and the pages converted per second

The harness records the timing next to each conversion, in `page-checks-timing.json`. A cached
conversion with no timing shows `-` until `--reconvert` converts the selected documents again. The
seconds are a sum of wall-clock stage times, so they move with machine load.

In quick mode, the review counts and speed of a long document describe its excerpt, and its row is
marked `*`.

`--write [PATH]` writes the same table as markdown. The default path is `docs/corpus-scorecard.md`.

## Conversions are cached and shared by every worktree

The suite converts each document once, into `.pytest_cache/reflow-page-checks/<doc>/<hash>/` in the
main checkout, or into `reflow-page-checks-quick/` for an excerpt. Every worktree shares this cache.
`REFLOW_PAGE_CHECKS_CACHE` overrides the directory.

The hash covers these inputs:

- every file under `src/reflow`, including code, rendering templates and viewer assets
- the PDF bytes
- whether an OCR runner is available
- the versions of Python, of the packages the conversion runs and of the OCR programs
  (`reflow.stagecache.tool_versions`)

A branch therefore reuses whatever another branch with the same code and packages already
converted, and a dependency upgrade converts again. The suite converts only the documents whose
pages are selected.

The harness publishes a conversion to the cache with a rename, so a concurrent run never reads a
partial entry. If another worktree published an entry first, the harness keeps that entry. If an
OCR runner was available but failed on some pages, the run uses the conversion but does not cache
it, and prints a warning. Without that rule, a timeout under load would satisfy later runs.

Conversions run without page images (`--no-page-images`). This changes nothing in the transcript.

## A cached stage must publish what an uncached conversion does

A warm run is served whole from this cache, so it never reads the stage cache
(`reflow.stagecache`). `test_stage_equivalence.py` checks the stage cache on its own. It converts
fw9 and the scanned `scan-w4` without the stage cache, then cold and warm through a private stage
root. Then it compares every published file. It never reads or writes the shared caches. It
also plants a stale entry in each stage, which the check must catch, and edits each stage's
module, which must make that stage miss and still publish the same output. The only exclusions
are OCR's own noise between two OCR runs (`OCR_NOISE` in `tests/stage_equivalence.py`, each entry
with its reason). To see the check fail on a stale entry, run it with one planted:

    REFLOW_STAGE_EQUIVALENCE_PLANT=extract .venv/bin/python -m pytest -o pythonpath=src -q \
        -m pages --quick -k "fw9 and publish_what" tests/pages/test_stage_equivalence.py

## AZ Blue site samples are gitignored

The `az-*` specs use the gitignored PDFs in `samples/azblue/site/`. A worktree reads missing samples
from the main checkout. `SOURCES.tsv` in that folder records where each file came from. A missing
sample is a prerequisite error, not a reason to skip its checks.

These samples cover the Medicare supplement application and outline, the Mandarin SBC, the
bilingual ACA guide, the Tucson employee booklet and member guide, the scanned medical remittance,
the Spanish formulary, the BlueDental manual, and the bilingual advance-directives notice.

Check the source page before you use a printed page number or a note from the collection. The
outline matrix is on PDF page 3, page 44 of the employee booklet is a logo cover, and page 10 of
the Mandarin SBC is Chinese. The ACA guide has Spanish and English sections, and its checks test
the English document tag and the Spanish block tags separately.

The permission-restricted `tsmc-ppo-mandarin.pdf` stays outside the corpus. The harness uses the normal
permission checks of the conversion and does not turn on restricted extraction.

## Why the reading-order threshold is 0.92 (#58)

`order_evidence.TAU_MIN` is 0.92. It was chosen from tau against the tag tree, over the tagged
page-check documents. The two pages with a `contains_in_order` xfail score 0.026 and 0.891. The 17
passing pages score 0.82 to 1.0, and six of them are below 0.97. The two groups overlap, so no
threshold separates them. The module docstring has the figures. `test_calibration.py` asserts that
each xfail page keeps its reading-order review item.
