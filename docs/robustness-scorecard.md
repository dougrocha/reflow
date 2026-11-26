# Robustness scorecard

reflow does well on PDFs that are well made inside. This page measures how it does on the rest.
The robustness harness in `tests/robustness/` damages good PDFs on purpose, the
way badly made PDFs are damaged inside, converts each damaged copy whole, and scores what reflow
publishes against the conversion of the clean file. It changes no converter behaviour.

The page checks cannot see this kind of damage. They compare the transcript with pdfium's text of
the same text layer, so a wrong `/ToUnicode` scores perfect recall. A shifted `/ToUnicode` in one
font of `samples/fw9.pdf` leaves the page pixel-identical, and every reader takes "Gpsn W-9 (Sfw.
Nbsdi 2024)" for the title.

## Running it

```sh
.venv/bin/python -m pytest -o pythonpath=src -q -m pages --robustness tests/robustness \
    [--robustness-out DIR] [--robustness-doc]
```

The suite is collected only with `--robustness`, so `pytest -m pages` and the unit suite never see
it. It holds two harnesses: the damage kinds below, and the defect injection (the last section),
which `tests/robustness/test_defects.py` runs alone in a few seconds. It takes about five minutes: about 130 conversions, as many at once as the machine's worker
budget gives it. Each run writes `scorecard.md` and `run.json` to `--robustness-out` (default: the
session's temporary folder, printed at the end). `run.json` records the SHA-256 of every input,
the seeds and the tool versions. `--robustness-doc` also writes the scorecard below. The nightly
runner (`scripts/nightly`) runs it as the `robustness` job, with the report in the job's folder.

The harness writes only its own temporary folder. It never reads or writes the page-check caches,
and the damaged files are never page-check documents.

## How a cell is scored

A cell is one damage kind on one base document. The kinds are the `KINDS` registry in
`tests/robustness/kinds.py`, the only list. Each kind has a transform, a pixel relation and an
expectation:

- `same`: the semantic projection equals the clean conversion's. The projection is, per page, the
  words in reading order, the table cells, the link texts and the form fields (kind, name and
  value), and the document's language. The words must come in the same order: the same words in
  another order is a difference, unlike the page checks' recall. reflow's own notes ("Text on
  this page was read by OCR") are left out.
- `flagged(code)`: a review fact with that code covers the damaged region, and the clean
  conversion did not raise it there. An unrelated warning never counts.
- `repaired(code)`: both. A kind whose expectation is `flagged` names `repaired` as its target,
  for later fixes to reach.

The outcomes:

| Outcome | Meaning |
| --- | --- |
| preserved (ok) | the projection is the clean one's |
| caught | the projection changed, and the kind's fact covers the damage |
| silent loss (LOSS) | the projection changed, and nothing says so |
| false alarm (ALARM) | a clean control raised a fact some kind watches for |
| invalid (INVALID) | the mutation is wrong: it changed nothing, or a kind declared pixel-same renders differently (pdfium, within 2 pixels) |
| unsupported (n/a) | the document has nothing the kind can damage |
| crashed (CRASH) | the conversion of the damaged file failed |

Each base also has a clean control: the base saved again through pikepdf, as every mutant is.
It runs through the same scoring, so a false alarm on a clean file shows.

A kind's `enforced` set names the bases on which a miss fails pytest; a bold cell below is
enforced. Every other miss is a scorecard entry, so the suite lands while most cells are red. An
invalid mutation always fails pytest, because it is a bug in the harness. When a fix makes a cell
true, add its base to the kind's `enforced` set. The first enforced cells are the ones reflow
already survives: one glyph per `Tj`, rotation, an offset CropBox, and the dropped-ligature flag. The
text witness added `tounicode-shifted` on every base. The words-missing check
added `text-in-annotation` on every base but fw9 (the scan has no text to move).

The first baseline enforced every cell that passed two full runs (on 2026-11-12, before and
after the change): `tounicode-missing`, `outlines`, `rasterized`, `shuffled-objects`, `tags-deleted`,
`tags-wrong` and `all-artifact` on every base they apply to. A cell the kind can damage that is
not enforced must say why: the kind's `unenforced` holds the reason, the scorecard prints it
under "Cells that are not enforced, and why", and the session fails on a cell that has neither.
A later change enforced the last three, each with a converter change: `tounicode-pua` on every base,
`broken-ligature` on scan and `text-in-annotation` on fw9 (the notes below say how). Every kind
is now enforced on every base it can damage. A clean control must be preserved: a false alarm
on one fails pytest, here and in the defect injection.

## Bases

| Base | What it is |
| --- | --- |
| prose | synthetic: one page of Helvetica prose with fi and fl words (`make_text_pdf`) |
| tagged | synthetic: two tagged Courier pages with a table and an outline (`make_tagged_pdf`) |
| scan | synthetic: a page image under an invisible OCR text layer (`make_scan_pdf`) |
| fw9 | `samples/fw9.pdf`: a tagged IRS form, 6 pages, links and fields |
| arxiv-p1-2 | `samples/arxiv.pdf` (fetched by `scripts/fetch_samples.py`, not committed) pages 1 and 2: an untagged pdfTeX paper |
| i9 | `samples/corpus/uscis-i9.pdf`: a tagged form, 4 pages, Type0 fonts |

## Notes on the kinds

- `tounicode-shifted` expects `font-map`, the fact the font repair leaves
  (`reflow.ingest.font_map`): the text witness reads the page, its words vote on what each
  code of the shifted font is, and the decided codes are written into the font's /ToUnicode, so the
  page reads the decoded text everywhere. The pages it changed are judged again, and once the text
  agrees with OCR they lose `text-disputed`, so that fact is no longer the kind's expect; it stays
  watched on the clean controls (`WATCHED_CODES` in `kinds.py`). Its target, `repaired(font-map)`,
  is not met on any base: a code is decided only from three votes in two different words, so a
  letter that appears in one word of the page (i9's g, j, M and z; capitals on fw9) keeps its
  shifted reading, and the `font-map` fact lists it as undecided. arxiv's fi ligature reads as two
  letters, which a one-letter swap can't decide.
- `tounicode-pua` reads the font into the Private Use Area, so the layer has no letters at all.
  On a born-digital page each private-use character counts as a unicode error of its block
  (`reflow.extract.text`, outside the symbol fonts that draw bullets from the PUA), the witness
  disputes the lines (a private-use character stands for a letter in `text_trust.compare`), and
  the font repair decides a private-use reading like an unknown letter, so the codes with enough
  votes decode and the rest keep raising `unicode-errors`. The scan base's layer is read again
  instead (`ingest.ocr` unreadable-text: no letter is left in it), so there `ocr-page` is the
  fact that catches it, and the kind expects either. Its target, `repaired(unicode-errors)`, is
  not met on any base, for the reasons `tounicode-shifted` gives, and because a digit is never
  decided.
- `broken-ligature` on scan: the page shows two plain f's, too few to learn the font's usual
  "f" from (`_wide_f` learns it from four or more), so the ligature glyph is measured against
  the width the font declares for its "f" (`/Widths`, or the standard 14 fonts' metrics:
  `GlyphNames.f_width`), with a wider margin (an upright f's hook can overhang its advance by a
  quarter; a fi is two thirds wider) and never for an italic. The scan's layer is invisible text
  reflow kept, so the ligature check now runs on every page but those reflow's own OCR wrote.
- `outlines` uses `gs -dNoOutputFonts`, which drops the tag tree too, so it is a compound kind:
  outlines and no tags.
- `shuffled-objects` moves only runs of three or more adjacent text objects that each set the text
  state they use, and keeps the last one last, so the page renders the same. fw9, arxiv and i9 have
  no such run.
- `text-in-annotation` is the partial loss: page 1's largest upright text object is drawn by a
  stamp annotation's appearance instead of the page, so the page looks the same and no text
  reader sees those words. It expects `words-missing`, which `text_trust.flag_missing`
  raises over words OCR reads in ink the text layer does not draw. The scan base has no visible
  text object to move. On fw9 the object moved is the title "Form W-9": OCR reads the large
  "W-9" as one token of two characters, which the word rule (three letters or digits) would not
  report, and the small "Form" too poorly to count, so a short token set tall (14 pt of box,
  `text_trust._tall`) is a word too. Words no reader can see (`white-text`, `tiny-text`,
  `hidden-text`) are no loss: they stay `same`, and raise no `words-missing`.
- `tiny-text` and `forms-flattened` declare their pixels changed: a 0.5 pt line is within the
  render comparison's slack, and `qpdf --generate-appearances` redraws the fields.
- `forms-flattened` expects `form-flattened`. Flattening takes the field names and values out of
  the file, so `same` can't be met. The fact comes from the traces that flattening leaves
  (`reflow.ingest.flattened`): widget annotations with no AcroForm to hold them, and form XObjects
  that mark their content `/Tx`, as a text field's appearance does. Ruled boxes and tables leave
  neither trace. Its region is the boxes of the flattened fields.

## Real files

The kinds are damage we can imagine. `samples/robustness/MANIFEST.tsv` lists real PDFs that tools
made badly, with each file's source, licence, the quirk observed in it and the `damage_id` of its
synthetic twin (`samples/robustness/README.md`). The suite converts every row whose file is here
and reports it under "Real files by the tool that made them": counts per family, with their
denominators, and each file's facts. The family comes from the producer and creator that the
converted Document records, not from the manifest.

- A row with a hand transcript is scored: preserved when the published words are the
  transcript's in its order, caught when they differ and the twin kind's fact is raised, silent
  loss otherwise. A row without one is inventory and moves no rate.
- The files were picked because they are bad. The counts describe the manifest, not prevalence.
- A file that may not be redistributed is not committed. `scripts/fetch_robustness_corpus.py`
  fetches it into the main checkout. A row whose file is not here is skipped and counted as
  absent; a file whose hash is not the manifest's fails pytest.
- The section names the families of `FAMILIES` (`tests/robustness/real.py`) that no row comes
  from, and claims nothing about them.

The manifest holds 18 rows from 12 families. Two have a hand transcript and are scored,
both veraPDF test files; the other 16 are inventory. 16 rows name a twin kind; the other two
have a quirk no kind makes (an OCR layer that misreads words, and a font that is not embedded).
The first baseline added no rows: every file in `samples/robustness/` and every file the fetch script takes
already has one.

## Latest scorecard

<!-- scorecard: generated by tests/robustness (--robustness-doc) -->
| Kind | Expect | prose | tagged | scan | fw9 | arxiv-p1-2 | i9 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `clean` | control | ok | ok | ok | ok | ok | ok |
| `tounicode-missing` | same | n/a | n/a | n/a | n/a | **ok** | **ok** |
| `tounicode-shifted` | flagged(font-map) → repaired(font-map) | **caught** | **caught** | **caught** | **caught** | **caught** | **caught** |
| `tounicode-pua` | flagged(unicode-errors or ocr-page) → repaired(unicode-errors) | **caught** | **caught** | **caught** | **caught** | **caught** | **caught** |
| `dropped-spaces` | same | **ok** | **ok** | **ok** | **ok** | **ok** | **ok** |
| `broken-ligature` | flagged(dropped-ligature) → repaired(dropped-ligature) | **caught** | n/a | **caught** | n/a | n/a | n/a |
| `outlines` | flagged(ocr-page) → repaired(ocr-page) | **caught** | **ok** | **caught** | **caught** | **caught** | **caught** |
| `rasterized` | flagged(ocr-page) → repaired(ocr-page) | **ok** | **ok** | **ok** | **caught** | **caught** | **caught** |
| `one-glyph-per-tj` | same | **ok** | **ok** | **ok** | **ok** | **ok** | **ok** |
| `shuffled-objects` | same | **ok** | **ok** | **ok** | n/a | n/a | n/a |
| `rotated` | same | **ok** | **ok** | **ok** | **ok** | **ok** | **ok** |
| `offset-cropbox` | same | **ok** | **ok** | **ok** | **ok** | **ok** | **ok** |
| `white-text` | same | **ok** | **ok** | **ok** | **ok** | **ok** | **ok** |
| `tiny-text` | same | **ok** | **ok** | **ok** | **ok** | **ok** | **ok** |
| `hidden-text` | same | **ok** | **ok** | **ok** | **ok** | **ok** | **ok** |
| `doubled-text` | same | **ok** | **ok** | **ok** | **ok** | **ok** | **ok** |
| `text-in-annotation` | flagged(words-missing) | **caught** | **caught** | n/a | **caught** | **caught** | **caught** |
| `tags-deleted` | same | n/a | **ok** | n/a | **ok** | n/a | **ok** |
| `tags-wrong` | same | n/a | **ok** | n/a | **ok** | n/a | **ok** |
| `all-artifact` | same | **ok** | **ok** | **ok** | **ok** | **ok** | **ok** |
| `wrong-lang` | same | **ok** | **ok** | **ok** | **ok** | **ok** | **ok** |
| `forms-flattened` | flagged(form-flattened) | n/a | n/a | n/a | **caught** | n/a | **caught** |

| Kind | preserved | caught | silent loss | false alarm | invalid | unsupported | crashed | met |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `clean` | 6 |  |  |  |  |  |  | 6/6 |
| `tounicode-missing` | 2 |  |  |  |  | 4 |  | 2/2 |
| `tounicode-shifted` |  | 6 |  |  |  |  |  | 6/6 |
| `tounicode-pua` |  | 6 |  |  |  |  |  | 6/6 |
| `dropped-spaces` | 6 |  |  |  |  |  |  | 6/6 |
| `broken-ligature` |  | 2 |  |  |  | 4 |  | 2/2 |
| `outlines` | 1 | 5 |  |  |  |  |  | 6/6 |
| `rasterized` | 3 | 3 |  |  |  |  |  | 6/6 |
| `one-glyph-per-tj` | 6 |  |  |  |  |  |  | 6/6 |
| `shuffled-objects` | 3 |  |  |  |  | 3 |  | 3/3 |
| `rotated` | 6 |  |  |  |  |  |  | 6/6 |
| `offset-cropbox` | 6 |  |  |  |  |  |  | 6/6 |
| `white-text` | 6 |  |  |  |  |  |  | 6/6 |
| `tiny-text` | 6 |  |  |  |  |  |  | 6/6 |
| `hidden-text` | 6 |  |  |  |  |  |  | 6/6 |
| `doubled-text` | 6 |  |  |  |  |  |  | 6/6 |
| `text-in-annotation` |  | 5 |  |  |  | 1 |  | 5/5 |
| `tags-deleted` | 3 |  |  |  |  | 3 |  | 3/3 |
| `tags-wrong` | 3 |  |  |  |  | 3 |  | 3/3 |
| `all-artifact` | 6 |  |  |  |  |  |  | 6/6 |
| `wrong-lang` | 6 |  |  |  |  |  |  | 6/6 |
| `forms-flattened` |  | 2 |  |  |  | 4 |  | 2/2 |

Run 2026-11-12T20:59:39 to 2026-11-12T21:09:34, seed 305, reflow 50b15cef2214089a0d57ab3deeb3d2aed78e2e1a; pikepdf 10.17.0 (qpdf 12.4.2), pypdfium2 5.14.0, pdfplumber 0.11.10, qpdf version 12.4.2, Ghostscript 10.08.0, OCR OcrmypdfRunner. The input hashes are in run.json.

Unsupported preconditions:

- `tounicode-missing` on prose: the font has no /ToUnicode to remove
- `tags-deleted` on prose: the document has no tag tree
- `tags-wrong` on prose: the document has no tag tree
- `forms-flattened` on prose: the document has no form fields
- `tounicode-missing` on tagged: the font has no /ToUnicode to remove
- `broken-ligature` on tagged: page 1 shows no fi or fl in a standard Helvetica or Times font
- `forms-flattened` on tagged: the document has no form fields
- `tounicode-missing` on scan: the font has no /ToUnicode to remove
- `text-in-annotation` on scan: page 1 shows no visible upright text object of eight characters or more
- `tags-deleted` on scan: the document has no tag tree
- `tags-wrong` on scan: the document has no tag tree
- `forms-flattened` on scan: the document has no form fields
- `tounicode-missing` on fw9: the font has no /ToUnicode to remove
- `broken-ligature` on fw9: page 1 shows no fi or fl in a standard Helvetica or Times font
- `shuffled-objects` on fw9: no run of three text objects that can be moved without changing what they draw
- `broken-ligature` on arxiv-p1-2: page 1 shows no fi or fl in a standard Helvetica or Times font
- `shuffled-objects` on arxiv-p1-2: no run of three text objects that can be moved without changing what they draw
- `tags-deleted` on arxiv-p1-2: the document has no tag tree
- `tags-wrong` on arxiv-p1-2: the document has no tag tree
- `forms-flattened` on arxiv-p1-2: the document has no form fields
- `broken-ligature` on i9: page 1 shows no fi or fl in a standard Helvetica or Times font
- `shuffled-objects` on i9: no run of three text objects that can be moved without changing what they draw

### Real files by the tool that made them

18 files from `samples/robustness/MANIFEST.tsv`, grouped by the family of the producer and creator that the converted Document records. They were picked because they are bad: the counts describe this manifest, not how common a quirk is. Only a file with a hand transcript is scored; the rest are inventory.

| Family | files | converted | crashed | absent | flagged | scored | met |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ABBYY | 2 | 2 | 0 | 0 | 2/2 | 0 | 0/0 |
| Acrobat Paper Capture | 1 | 1 | 0 | 0 | 0/1 | 0 | 0/0 |
| Adobe PDF Library | 2 | 2 | 0 | 0 | 2/2 | 0 | 0/0 |
| Amyuni | 1 | 1 | 0 | 0 | 0/1 | 0 | 0/0 |
| Canva | 1 | 1 | 0 | 0 | 1/1 | 0 | 0/0 |
| Microsoft Print to PDF | 2 | 2 | 0 | 0 | 2/2 | 0 | 0/0 |
| OpenOffice | 1 | 1 | 0 | 0 | 1/1 | 0 | 0/0 |
| PDFlib | 1 | 1 | 0 | 0 | 0/1 | 0 | 0/0 |
| Snagit | 1 | 1 | 0 | 0 | 1/1 | 0 | 0/0 |
| dvips | 1 | 1 | 0 | 0 | 1/1 | 0 | 0/0 |
| iText | 1 | 1 | 0 | 0 | 1/1 | 0 | 0/0 |
| veraPDF Test Builder | 4 | 4 | 0 | 0 | 1/4 | 2 | 0/2 |
| all | 18 | 18 | 0 | 0 | 12/18 | 2 | 0/2 |

"flagged" counts the files that raised a fact some damage kind watches for (dropped-ligature, font-map, form-flattened, ocr-page, text-disputed, unicode-errors, words-missing). An absent file is counted only as absent.

No file here comes from pdfTeX, Skia, cairo, Quartz, Acrobat Distiller, Microsoft Word, Microsoft Excel, Ghostscript, Adobe Designer: the scorecard makes no claim about those tools.

| File | Family | Twin | Outcome | Facts raised | Quirk |
| --- | --- | --- | --- | --- | --- |
| `safedocs-0609714` | ABBYY | tounicode-shifted | inventory | alt-missing 1, prior 39, text-disputed 1, title 1, title-suspect 1, unicode-errors 24, words-missing 1 | ABBYY via PDF-XChange, Type0 fonts with no /ToUnicode: the Hebrew reads as IPA modifier letters ('ʪʸʲʺ…'), words run together; the English case names read correctly |
| `safedocs-4516169` | ABBYY | tounicode-shifted | inventory | font-ambiguous 2, prior 44, text-disputed 3, title 1, title-suspect 1, unicode-errors 32 | ABBYY via PDF-XChange, Type0 fonts with no /ToUnicode: the Ukrainian reads as Latin Extended and IPA letters ('ȱɧɮɨɪɦɚɰɿɹ…') |
| `paper-capture-naca-ocr` | Acrobat Paper Capture | - | inventory | aligned-table 1, alt-missing 13, decorative-guessed 1, heading-from-ink 3, ocr-doubtful 228, ocr-image-text 442, ocr-in-picture 6, ocr-mark-lost 16, ocr-suggestion 6, ocr-words-omitted 9, prior 746, soft-columns 284, title 1, title-suspect 1, unread-handwriting 3, unread-text 19 | a scan under an Acrobat 9 Paper Capture OCR layer (HiddenHorzOCR font) that misreads words ('Tirne', 'rnile', '6'17 #i 5' on page 1); 436 bytes before %PDF- |
| `apl-cdc-hepb-arabic` | Adobe PDF Library | tounicode-pua | inventory | alt-missing 4, level-disagreement 1, link 3, order-disagreement 1, prior 76, role-agreement 15, role-disagreement 6, tag-alt 1, title 1, unicode-errors 11 | Arabic text layer: page 1 reads 25 U+FFFD and lam-alef ligatures out of order ('االلتهاب' for 'الالتهاب'); lines come out shuffled |
| `azblue-hca-nondiscrim-multilang` | Adobe PDF Library | tounicode-shifted | inventory | link 9, order-disagreement 1, prior 96, role-agreement 64, role-disagreement 4, soft-columns 17, tag-heading 1, title 1, unfinished-sentence 1, unicode-errors 6 | p3 Navajo tagline in a legacy Navajo font reads 'Bilag1ana bizaad doo bee y1n7[ti’' for 'Bilagáana bizaad doo bee yáníłti’' (1=á, 7=í, [=ł); Hindi, Gujarati and Khmer lose vowel signs (#124) |
| `pdfjs-issue15910` | Amyuni | tounicode-shifted | inventory | prior 5, title 1 | an Arial TrueType font with no /ToUnicode and no /Encoding: 'Allgäu Käferhofen' reads 'Allg‰u K‰ferhofen' |
| `safedocs-7122050` | Canva | tounicode-shifted | inventory | alt-missing 8, font-ambiguous 1, prior 48, text-disputed 2, title 1, unicode-errors 20, words-missing 2 | a Canva flyer distilled by Acrobat Distiller 19, fonts with no /ToUnicode: 'LEASE' and 'HOMESTEAD VILLAGE' read as '1*&8*' and 'Bka)…' with control characters; one line reads correctly |
| `ms-print-claim-form-booklet` | Microsoft Print to PDF | rasterized | inventory | alt-missing 13, decorative-guessed 1, ocr-doubtful 91, ocr-grades-unmatched 2, ocr-image-text 161, ocr-in-picture 5, ocr-page 8, ocr-words-omitted 7, prior 329, soft-columns 52, struck-word-unread 7, title 1, title-suspect 1, tracked-change-unplaced 2, unread-handwriting 2, unread-signature 2, unread-text 16 | Microsoft Print to PDF wrote all 8 pages as images with no text layer at all (pdfium reads 0 characters) |
| `safedocs-3612614` | Microsoft Print to PDF | tounicode-shifted | inventory | alt-missing 3, heading-fragment 4, prior 24, text-disputed 1, title 1, unicode-errors 5, words-missing 1 | Microsoft Print to PDF Type0 fonts whose /ToUnicode is off by 29: 'Offer valid in-store' reads '2IIHU YDOLG LQ\x10VWRUH'; other lines on the page read correctly |
| `untagged-cdc-firstline-slides-es` | OpenOffice | tags-deleted | inventory | alt-missing 12, decorative-guessed 28, link 5, ocr-page 3, prior 137, role-agreement 2, title 1, words-missing 1 | an Impress slide deck exported with no tag tree, no /Lang and no /MarkInfo; the text layer reads correctly |
| `pdfjs-issue6605` | PDFlib | tounicode-pua | inventory | prior 7, title 1, title-suspect 1 | a bank statement whose Type3 font glyphs have a 0 0 0 0 /FontBBox; page 1 reads one U+FFFD |
| `snagit-remittance-advice` | Snagit | rasterized | inventory | alt-missing 6, heading-from-ink 1, ocr-doubtful 42, ocr-grades-unmatched 1, ocr-image-text 27, ocr-in-picture 6, ocr-page 1, ocr-words-omitted 1, prior 117, title 1, title-suspect 1, tracked-change-unplaced 1 | a Snagit screen capture saved as PDF: one page image, no text layer |
| `safedocs-0117100` | dvips | tounicode-pua | inventory | alt-missing 1, label-missing 1, prior 215, title 1, unfinished-sentence 1, unicode-errors 79 | dvips Type3 bitmap fonts with no /ToUnicode: every 'c' reads as a carriage return ('Quadrati\r splines' for 'Quadratic splines') |
| `pdfjs-issue4550` | iText | tounicode-shifted | inventory | empty-transcript 1, prior 2, text-witness-unavailable 2, title 1, title-suspect 1, unicode-errors 1 | a Type1 font (MSTT31c64e) with a custom /Differences encoding and no /ToUnicode: the printed 'íú.' reads '!:.' |
| `verapdf-ua1-7.1-t01-fail-a` | veraPDF Test Builder | all-artifact | inventory | prior 5, title 1 | tagged, but a paragraph of real content is marked as an Artifact |
| `verapdf-ua1-7.21.4.1-t01-fail-a` | veraPDF Test Builder | - | inventory | prior 1, role-agreement 1, title 1 | Helvetica not embedded; the text layer reads correctly ('Go to last page') |
| `verapdf-ua1-7.21.7-t01-fail-a` | veraPDF Test Builder | tounicode-shifted | silent loss: font-map not raised; page 1 text: 2 words lost (hello world), 2 words added (7orld ello); first change at word 1: 'hello world' became 'ello 7orld' | prior 1, role-disagreement 1, text-witness-unavailable 1, title 1, unicode-errors 1 | a Type0 Identity-H font with no /ToUnicode: pdfium reads 'Hello World' as '(ELLO7ORLD', each letter as another |
| `verapdf-ua1-7.21.7-t02-fail-a` | veraPDF Test Builder | tounicode-pua | silent loss: unicode-errors not raised; page 1 text: 1 words lost (u); first change at word 1: 'u' became '(end)' | empty-transcript 1, text-witness-unavailable 1, title 1 | a /ToUnicode that maps the only glyph to <0000>: the printed 'u' reads as U+FFFE |
<!-- end of scorecard -->

## Defect injection

The damage kinds break the input. Defect injection breaks the output: it starts from a
correct conversion, injects one known defect, and measures which page checks and which review
items catch it. Equalify Iris calibrates its verifier the same way. The defects are the `DEFECTS`
registry in `tests/robustness/defects.py`, the only list; the runner is
`tests/robustness/inject.py`.

A cell is one defect in one document. Its steps:

1. The document's conversion is copied into the run's folder: the page-check cache's entry for the
   current code when it has one, read under its shared lock and never written, else a conversion
   made in the run's folder. A robustness session never prunes the cache, and its last test checks
   that every entry it copied is unchanged.
2. The defect edits the model (`document.json`) of the copy. It picks its target with the cell's
   seed among the pages the document's specs test, and it edits text only through
   `Node.set_text`. The copy is then rendered again from the model, the way a conversion publishes
   it: `review.json` from `review.queue`, the layout sidecars, the site and the passages. Nothing
   edits HTML by hand, so `document.json` and the chunks agree.
3. The defect must round-trip through `Model.read`, change `document.json`, and change the HTML of
   its page, or the cell is invalid and fails pytest.
4. The damaged page's spec assertions run on the copy and on the control (the same conversion
   rendered again with no defect, which must come out byte for byte the conversion it was copied
   from). An assertion that passes on the control and fails on the copy caught the defect. An
   assertion that fails on both is not counted.
5. A review code caught the defect when an item with that code covers the changed region and the
   control had no item of that code over it, or when it was already there and is one of the
   defect's own codes (marked "already there"). An unrelated warning that was already on the page
   never counts.

Page-check catches and review catches are scored apart. Swapped paragraphs keep every word, so
recall, which is order-blind, must never catch them: pytest fails if it does. Only an order
assertion (`contains_in_order`) or a review item can. A defect's `enforced` set names the documents
whose catch pytest holds, the cells caught when the harness landed. A run records each cell's
target, seed and `document.json` hash in `defects.json`, and the session runs every cell a second
time in a fresh folder to check that it scores the same.

The defects:

| Defect | What it changes |
| --- | --- |
| `drop-table-row` | a body row (text in two cells or more, never the first row) of a table with two body rows or more is gone |
| `demote-heading` | a section heading (h2 to h6) becomes a paragraph, and the reading order is nested again: what its section held joins the section before |
| `swap-paragraphs` | two adjacent paragraphs of five words or more trade places |
| `change-number` | one digit of a number in a paragraph, list item, cell, caption or heading becomes another digit |
| `drop-paragraph` | a paragraph of five words or more is gone: the defect recall exists to catch |

What the first run shows: the page checks catch dropped rows and dropped paragraphs where a spec
tests that table or holds a recall floor, and demoted headings only where a `heading` assertion
names that heading. They miss four of five changed digits, and no check catches a swap. The review queue
catches almost nothing, because its facts are written while the document converts, from the
converter's own evidence. A defect made after that leaves no fact behind, so the queue can only
see what `review.queue` derives again from the finished model. The one review catch, a swap on the
trifold, is an `order-disagreement` the page already carried.

The specs of the five documents' tested pages then gained assertions aimed at these defects:
a `contains_in_order` of each paragraph's, item's and heading's opening words, a
`contains_in_order` of every number with a word either side, a `heading` for each section
heading, and a `table` that names each table's row count and the longest cell of each row. Each
holds on the control, and the page checks now catch every cell. Over ten other seeds (1000 to
1009) they caught 230 of 230 cells. Every caught cell is enforced.

### The first baseline, next to Iris

The scorecard below is the first baseline of these catch rates. Its conversions are this
checkout's: the footer says where each came from, and a page-check cache entry is keyed by the
converter source, the tool versions and the PDF. The same five documents' published copies were
checked too: `bun run docs:check fw9 resnet uscis-i9 irs-w4-2026 cdc-antibiotics-trifold` said
all five were stale (seeded from an older main), and after `docs:build --stale --stage-cache` of
those five it said all five were current.

Equalify Iris published one calibration of its fidelity verifier, a vision model that compares a
page's HTML with the page image: [docs/verifier-calibration.md, "First measurement"](https://github.com/EqualifyEverything/equalify-iris/blob/eb7fff76b796f48985b69c74f454b161cffa0da2/docs/verifier-calibration.md#L63-L73)
(2026-08-26, `sonnet-4-6`, 11 pages across 3 documents). Its numbers, beside reflow's first
baseline (2026-11-12, seed 306, 5 documents):

| Defect | reflow page checks | reflow review queue | Iris verifier |
| --- | --- | --- | --- |
| clean copy passed | 5 of 5 documents | not measured | 11 of 11 pages |
| dropped table row | 3 of 3 | 0 of 3 | 100% |
| demoted heading | 5 of 5 (to a paragraph) | 0 of 5 | 4 of 7, 57% (two levels down) |
| swapped paragraphs | 5 of 5 | 1 of 5 | 3 of 5, 60% |
| changed number | 5 of 5 (any digit) | 0 of 5 | 100% (in a table cell) |
| dropped paragraph | 5 of 5 | 0 of 5 | not measured |
| every defect | 23 of 23 | 1 of 23 | 25 of 30, 83% |

Iris states "100% each" for its dropped row and changed number, without their own counts. A
clean control is scored by its spec assertions only, so the review queue has no false-positive
rate here (the clean trifold already carries an `order-disagreement`). The rows don't measure
the same thing. Iris judges any page with one general check. reflow's page
checks are assertions written for these pages, aimed at these defects, so they catch only
on a page that has a spec. The review queue is reflow's check for any document, and it is the
closer match to Iris's verifier: it catches 1 of 23, a swap whose `order-disagreement` the page
already carried.

### Latest defect scorecard

<!-- defects: generated by tests/robustness (--robustness-doc) -->
| Defect | fw9 | resnet | uscis-i9 | irs-w4-2026 | cdc-antibiotics-trifold | checks | review |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `clean` | ok | ok | ok | ok | ok |  |  |
| `drop-table-row` | **checks** | **checks** | **checks** | n/a | n/a | 3/3 | 0/3 |
| `demote-heading` | **checks** | **checks** | **checks** | **checks** | **checks** | 5/5 | 0/5 |
| `swap-paragraphs` | **checks** | **checks** | **checks** | **checks** | **checks + review** | 5/5 | 1/5 |
| `change-number` | **checks** | **checks** | **checks** | **checks** | **checks** | 5/5 | 0/5 |
| `drop-paragraph` | **checks** | **checks** | **checks** | **checks** | **checks** | 5/5 | 0/5 |

| Defect | Document | Page | What changed | Assertions failed | Review codes |
| --- | --- | --- | --- | --- | --- |
| `drop-table-row` | fw9 | 5 | dropped row '10. Corporation or LLC electing corporate status on Form 883' of table 5:e05… | `table#7`, `contains_in_order#12`, `table#14` | none |
| `drop-table-row` | resnet | 6 | dropped row 'ResNet-34 A \| 25.03 \| 7.76' of table 6:75f37dfbfde4 | `table#4`, `contains_in_order#14`, `table#15` | none |
| `drop-table-row` | uscis-i9 | 1 | dropped row 'List A \| OR \| List B List C AND' of table 1:b61b46992444 | `table#10` | none |
| `drop-table-row` | irs-w4-2026 | | unsupported: the tested pages [1] have no table with two body rows of text | | |
| `drop-table-row` | cdc-antibiotics-trifold | | unsupported: the tested pages [1] have no table with two body rows of text | | |
| `demote-heading` | fw9 | 5 | h3 'What Name and Number To Give the Requester' became a paragraph | `heading#3` | none |
| `demote-heading` | resnet | 2 | h2 '2. Related Work' became a paragraph | `heading#2` | none |
| `demote-heading` | uscis-i9 | 1 | h3 'USCIS Form I-9' became a paragraph | `heading#9` | none |
| `demote-heading` | irs-w4-2026 | 1 | h2 'Step 5: Sign Here' became a paragraph | `heading#17` | none |
| `demote-heading` | cdc-antibiotics-trifold | 1 | h2 'What are the side effects?' became a paragraph | `heading#8` | none |
| `swap-paragraphs` | fw9 | 6 | swapped 'Protect yourself from suspicious emails…' and 'Phishing is the creation and use… | `contains_in_order#6` | none |
| `swap-paragraphs` | resnet | 3 | swapped 'y = F(x, {Wi}) + x. (1)' and 'Here x and y are the input and output v…' | `contains_in_order#15`, `contains_in_order#16` | none |
| `swap-paragraphs` | uscis-i9 | 3 | swapped 'OMB No. 1615-0047 Expires 05/31/2027' and 'Instructions: This supplement must be… | `contains_in_order#4`, `contains_in_order#5` | none |
| `swap-paragraphs` | irs-w4-2026 | 1 | swapped 'Complete this step if you (1) hold more…' and 'Do only one of the following.' | `contains_in_order#10` | none |
| `swap-paragraphs` | cdc-antibiotics-trifold | 1 | swapped 'Improving the way healthcare profession…' and 'Talk with your doctor if you have… | `contains_in_order#6` | `order-disagreement (already there)` |
| `change-number` | fw9 | 2 | 1984 became 3984 in p 'Example. Article 20 of the U.S.-China income tax …' | `contains_in_order#8` | none |
| `change-number` | resnet | 7 | 32 became 37 in td '32' | `contains_in_order#9` | none |
| `change-number` | uscis-i9 | 1 | 2 became 5 in td 'Certification: I attest, under penalty of perjury…' | `contains_in_order#7`, `table#10` | none |
| `change-number` | irs-w4-2026 | 1 | 4 became 5 in h2 'Step 4: Other Adjustments' | `contains_in_order#5`, `contains_in_order#10`, `contains_in_order#11`, `heading#15` | none |
| `change-number` | cdc-antibiotics-trifold | 1 | 2 became 6 in p 'Each year in the United States, at least 2 millio…' | `contains_in_order#7` | none |
| `drop-paragraph` | fw9 | 1 | dropped 'Form (Rev. March 2024) Department of the Treasury Internal …' | `contains_in_order#16`, `contains_in_order#17` | none |
| `drop-paragraph` | resnet | 3 | dropped 'We adopt residual learning to every few stacked layers. A b…' | `min_recall#0`, `contains_in_order#15`, `contains_in_order#16` | none |
| `drop-paragraph` | uscis-i9 | 3 | dropped 'Instructions: This supplement must be completed by any prep…' | `min_recall#0`, `contains_in_order#4`, `contains_in_order#5` | none |
| `drop-paragraph` | irs-w4-2026 | 1 | dropped 'For Privacy Act and Paperwork Reduction Act Notice, see pag…' | `min_recall#0`, `contains_in_order#10`, `contains_in_order#11` | none |
| `drop-paragraph` | cdc-antibiotics-trifold | 1 | dropped 'Talk with your doctor if you have any questions about your …' | `min_recall#0`, `contains_in_order#6` | none |

Run 2026-11-12T21:09:34 to 2026-11-12T21:09:37, seed 306, reflow 50b15cef2214089a0d57ab3deeb3d2aed78e2e1a. Each document's source and PDF hash are in defects.json.

The conversions the cells read: cdc-antibiotics-trifold, fw9, irs-w4-2026, resnet, uscis-i9 from the page-check cache. A cache entry is keyed by the converter source by content, the tool versions and the PDF, so each one is what the source that ran makes of the PDF: current, in the sense of `bun run docs:check`.
<!-- end of defects -->
