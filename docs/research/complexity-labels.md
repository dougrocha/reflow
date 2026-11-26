# Labelled page set for complexity routing

The page-vision pass flags a page as one or more of four kinds of complexity, and the
rules have signals of their own. Neither has been scored against labels: the page-vision study's figures (`vision-page-understanding.md`) are
observations on a few known pages. This set gives each class a written definition, labels a
held-out set of pages from those definitions, and scores both the pass and the rules on it.

The labels come from these definitions, read against the rendered page. They never come from
the model's answers. A page the definitions can't decide is listed for a reviewer, with a crop,
and is labelled from the reviewer's answer.

**Held out.** Nothing in this set tunes the pass's thresholds (`SCAN_FLAGS`, `SCAN_KINDS`,
`DENSE_SHARE`) or the prompt. The scorer reports; it changes nothing. Pages from the 11 DocAccess
documents were seen by the page-vision study and by the pass's first measurement, so the scores are also
given for the pages that neither saw (`seen` in the set).

## The unit and the labels

The unit is one PDF page. Each page gets four independent booleans, one per class, so a page may
be in several classes or none. A page is a negative for every class it is not in, and the set
deliberately holds the negatives the issue names: clean scans, digital diagrams, born-digital
pages with small images or logos, and dense born-digital tables.

Judge the page as a person sees it rendered, at a size where body text is readable. Don't use the
text layer, the converter's output or the model's answer.

## Definitions

### handwriting

**In:** the page carries at least one handwritten word, number, signature or set of initials,
made by a hand on the paper or by a pen on a screen: a filled-in form entry, a margin note, a
library or file number written on a cover, a signature on a scanned or born-digital page (a
signature image placed in a born-digital page counts).

**Out:**

- print in any typeface, however small, faint or script-like (a script font is print);
- stamps, seals and embossed marks, even when inked by hand;
- marks with no letters or digits on their own: a tick, a cross, a circle round printed text,
  an underline, a struck line, a scribble that crosses something out;
- a picture of handwriting that is part of a photo (a sign, a plaque).

Each positive also records `signature_only`: true when the only handwriting on the page is
signatures or initials. The page-vision study counted audit pp15 and 18 as false handwriting flags, but each holds
a signature image, so they are positives by this definition with `signature_only`. The scorer
reports handwriting both ways: all handwriting, and handwriting other than signatures (where a
`signature_only` page counts as a negative).

### poor_scan

**In:** the page is a raster image of paper (scanned or photographed, whether or not it has a
text layer), and at least one of these makes some of its print hard to read at normal size:

- skew of more than about 1 degree, or warped lines;
- faint, broken or filled-in strokes in body text;
- speckle, streaks, bleed-through, show-through or a dark or tinted background that lowers the
  contrast of the print;
- low resolution or blur that makes small print soft.

**Out:**

- a born-digital page, whatever pictures it holds (a photo of an old document inside a
  born-digital page is not a scan of the page);
- a clean scan: crisp, dark print on an even light background, straight, readable at normal
  size, even if it is grey-scale or has a faint frame or punch holes;
- a scan whose only flaw is in handwriting, a stamp or a photo on it.

Paper colour alone (yellowed, grey paper) counts only when it lowers the contrast of the print.
A bitonal photocopy whose print is crisp is a clean scan, even when its handwriting is thickened
or speckled (a reviewer's decision on the permit ledger).

### dense_table

**In:** the page's tabular data, counted over every grid on the page together, has at least 40
data cells, and at least one grid has at least 4 columns. A grid is any arrangement of values in
aligned rows and columns, ruled or not: a financial statement, a ledger, a calendar month, a form
laid out as a table of entries. Count data cells, not header cells; an empty cell in a grid that
is being filled in (a ledger row) counts.

**Out:**

- prose in columns, a table of contents, an index, a list of names in two columns, a directory
  of addresses: text that is not read across a row;
- a form with fields scattered over the page, not in a grid;
- a chart, even one with a data table drawn inside it, unless the table itself meets the count;
- a grid inside a screenshot of a software screen (a reviewer's decision on RFP p10).

### diagram

**In:** the page holds a chart, graph, flowchart, organisation chart, timeline graphic, map,
floor plan, technical drawing or schematic that carries information a reader needs, and it
covers at least about a tenth of the page.

**Out:**

- photographs (of a place, a person, equipment, a plaque), even ones with labels;
- logos, seals, icons, decorative art, borders and a cover's background picture;
- screenshots of software screens and forms;
- tables, even ruled or coloured ones;
- a diagram smaller than about a tenth of the page (a small icon-like chart in a margin).

## The rules' own signals

Declared here before any scoring, from what the default conversion (no assist) already publishes
in `document.json`. None was tuned on the set. Each is scored on its own, and the union is
scored as the rules' signal for the class.

| Class | Signal | What the converter publishes |
|---|---|---|
| handwriting | `unread-pen` | an unread crop with `handwritten: true` on the page (facts `unread-handwriting`, `unread-signature`) |
| poor_scan | `scan-noisy` | the page fact `scan-noisy` |
| poor_scan | `unread-text` | an unread crop with `handwritten: false` on the page (fact `unread-text`): print OCR could not read |
| dense_table | `table` | any table on the page |
| dense_table | `table-dense` | the page's tables, together, have at least 40 cells (`rows` × `columns`, header rows included, since the rules don't separate them reliably) and one has at least 4 columns: the definition above applied to the rules' tables |
| diagram | `vector-figure` | a figure that is not an unread crop, found as a vector drawing |
| diagram | `figure` | any figure that is not an unread crop |

The rules have no handwriting signal for a born-digital page and none for a diagram that is not a
figure. That is part of what the scores show.

## The set

`tests/complexity/labels.json` holds 95 pages from 24 documents, each with its four labels,
`signature_only`, a note on the deciding detail, and `seen`: `vision study, first measurement` for the 56 pages of the
11 DocAccess documents, which the page-vision study and the pass's first measurement looked at
(`vision study` is the study, `first measurement` the measurement), `vision study` for the NASA cover, and empty for the 38
pages neither saw. The pages were picked from the rendered pages before any model run, to hold
the hard cases this set was built for and positives and hard negatives of each class. Four pages
the definitions left open were put to a reviewer with a crop and labelled from the answers (noted
on each page): permit p2 (not a poor scan), NACA p3 (handwriting: a faint pencil number along the
edge), RFP p10 (not a dense table) and Carmel p10 (not a poor scan). For these four only, Claude
Haiku and Sonnet were given the definitions and the crops as reference labellers. Both agreed on
permit p2, both read NACA p3 as handwriting (the reviewer then changed that label), Sonnet called
Carmel p10 a poor scan (the reviewer kept it as not), and RFP p10 turned on the screenshot rule
the reviewer set. No model labelled any other page.

`scripts/eval_complexity.py` scores the set. It replays `tests/complexity/answers-cache.json`
and calls nothing; `--record` asks a local Ollama server for missing answers. See its docstring.

## Results

Measured 2026-11-02 on the RTX 3090: `qwen3-vl:8b`
through local Ollama, prompt version 2, one answer per page, recorded once with
`scripts/eval_complexity.py --record` (94 pages answered in 14 minutes, conversions included;
naca p8 offers 91 blocks, one over `MAX_BLOCKS`, so the pass asks nothing and it counts as not
flagged, as in a real conversion). On every page the scorer's gated flags equal what the pass
itself wrote to `page-vision-proposals.json`. Replay: `.venv/bin/python scripts/eval_complexity.py
--pages`, about 5 minutes of conversions and no model call.

Positives and negatives over the 95 pages: handwriting 19 and 76 (6 of the 19 are signatures
only), poor scan 5 and 90, dense table 19 and 76, diagram 24 and 71.

### All 95 pages

| Class | Predictor | Precision | Recall | TP | FP | FN | TN |
|---|---|--:|--:|--:|--:|--:|--:|
| handwriting | vision raw | 72% | 68% | 13 | 5 | 6 | 71 |
| handwriting | vision gated | 79% | 58% | 11 | 3 | 8 | 73 |
| handwriting | rules: unread-pen | 75% | 16% | 3 | 1 | 16 | 75 |
| handwriting, signatures not counted | vision raw | 44% | 62% | 8 | 10 | 5 | 72 |
| handwriting, signatures not counted | vision gated | 57% | 62% | 8 | 6 | 5 | 76 |
| handwriting, signatures not counted | rules: unread-pen | 25% | 8% | 1 | 3 | 12 | 79 |
| poor_scan | vision raw = gated | 14% | 40% | 2 | 12 | 3 | 78 |
| poor_scan | rules: scan-noisy | - | 0% | 0 | 0 | 5 | 90 |
| poor_scan | rules: unread-text (= any) | 6% | 20% | 1 | 15 | 4 | 75 |
| dense_table | vision raw = gated | 83% | 53% | 10 | 2 | 9 | 74 |
| dense_table | rules: table (= any) | 40% | 32% | 6 | 9 | 13 | 67 |
| dense_table | rules: table-dense | 33% | 11% | 2 | 4 | 17 | 72 |
| diagram | vision raw = gated | 95% | 83% | 20 | 1 | 4 | 70 |
| diagram | rules: vector-figure | 33% | 17% | 4 | 8 | 20 | 63 |
| diagram | rules: figure (= any) | 40% | 96% | 23 | 35 | 1 | 36 |

### The 38 pages neither earlier measurement saw

| Class | Predictor | Precision | Recall | TP | FP | FN | TN |
|---|---|--:|--:|--:|--:|--:|--:|
| handwriting | vision raw = gated | 33% | 25% | 1 | 2 | 3 | 32 |
| handwriting | rules: unread-pen | 50% | 25% | 1 | 1 | 3 | 33 |
| poor_scan | vision raw = gated | 20% | 33% | 1 | 4 | 2 | 31 |
| poor_scan | rules: unread-text (= any) | 0% | 0% | 0 | 6 | 3 | 29 |
| dense_table | vision raw = gated | 75% | 60% | 3 | 1 | 2 | 32 |
| dense_table | rules: table (= any) | 33% | 60% | 3 | 6 | 2 | 27 |
| diagram | vision raw = gated | 91% | 77% | 10 | 1 | 3 | 24 |
| diagram | rules: figure (= any) | 46% | 92% | 12 | 14 | 1 | 11 |

None of the unseen handwriting is signatures only, so the second handwriting variant is the same
here. `--pages` lists every page each predictor gets wrong.

### What the numbers say, and what they rest on

- **Diagram is the one class the pass gets right on both counts**: 20 of its 21 flags are
  diagrams, and it finds 20 of the 24. The false flag is the census cover's triangle art. The
  misses are the audit's map (p6), two NACA graph pages (p6, and p8, which the pass doesn't ask)
  and the census's two line charts on p22. The rules' `figure` finds 23 of the 24, but 35 of the
  58 pages it fires on hold no diagram (photos, logos, cover pictures, the ruled lines of the
  audit's statements), so it can't stand in for the pass.
- **Dense table: precise, half the recall.** 10 of 12 flags are right (the false two are a
  3-column report list and a 3-column prose table). It misses 9 of 19: four audit statements
  (pp21, 23, 27, 29), the calendar, the RFP's number table, the rotated ledger list (permit p8),
  the census business-cycle table and the 1960 census fuel grid. The rules find tables on only
  6 of the 19, because aligned statements mostly stay paragraphs; the definition's count applied
  to their tables (`table-dense`) finds 2.
- **Handwriting: the gate trades recall for precision.** Raw, the pass finds 13 of 19 and is
  right on 13 of 18 flags. The scan gate removes 2 false flags (appeal p1, the NASA cover) and 2
  true ones (the signature images on audit pp15 and 18), so it reads 11 of 14 right and finds 11
  of 19. Its 3 remaining false flags are all on clean scans of forms (budget p2, census 1970 p1,
  the W-9 scan). Its misses: permit p2, audit p12's signatures, the hand-lettered map (brochure
  p3), three NACA library and margin notes (pp2, 3, 8). Not counting signatures, the gated pass
  finds 8 of 13 and is right on 8 of 14. The rules' unread pen crops find 3 of 19: they see
  coloured or tall writing only.
- **Poor scan is not measured well by either.** The pass flags 14 pages and 2 are poor scans
  (brochure p3, NACA p12); it still misses both census 1960 pages, the hard case this set was built for, and the
  brochure cover. 6 of its 12 false flags are the permit ledger, which the reviewer labelled a clean
  bitonal scan: had the ledger counted as poor, the pass would be right on 8 of 14 flags and find
  8 of 11. So this precision mostly measures that one decision. The rules have nothing: the
  `scan-noisy` fact never fires on these pages, and 15 of the 16 pages with an `unread-text`
  crop are not poor scans: the crop marks writing OCR could not read, not a poor page.

Limits, which every figure above carries:

- **Size.** 95 pages, 24 documents. A positive is worth 1/5 of poor-scan recall, about 1/19 of
  handwriting or dense-table recall and 1/24 of diagram recall. On the 38 unseen pages the
  classes hold 4, 3, 5 and 13 positives, too few to compare with the whole set. The 4 unseen
  handwriting pages are all NACA library marks and margin notes, two of them faint, the hardest
  kind in the set.
- **Balance.** Negatives outnumber positives 3 to 1 or more in every class (18 to 1 for poor
  scan), so a few false flags move precision a lot.
- **Not independent.** 57 pages were seen before: 56 from the 11 DocAccess documents, which
  the page-vision study and the pass's first measurement looked at, and the NASA cover. The
  pass's scan gate was
  built from their false flags (appeal p1, audit pp9, 15, 18, the NASA cover).
- **One labeller, one run.** The labels are one reader's application of the definitions, with
  four pages decided by a reviewer from a crop. The model answered each page once; a second run may differ.
- **Clustered.** Pages of one document share a scanner and a layout: the 6 permit pages decide
  most of the poor-scan precision, and the audit decides most of the dense-table recall.

The set is held out. These scores report on the pass's thresholds (`SCAN_FLAGS`, `SCAN_KINDS`,
`DENSE_SHARE`) and change none of them.
