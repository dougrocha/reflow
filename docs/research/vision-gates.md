# Checks on the page-vision model's heading and row-header disputes

Measured 2026-11-02 by `tests/pages/vision_gates.py` over
the page-check harness's quick conversions (heuristic engine, OCR on). No model ran: each case is
an answer recorded in the page-vision study, so no GPU was needed.

## What is counted

- **Wrong cases** (`tests/pages/fixtures/vision-gates.json`): every heading and row-header answer
  in the page-vision report's "Every confidently wrong case" list (`vision-page-understanding.md`), from the recorded wrong answers. That
  is 28 paragraphs the model called headings, and the row-header columns of 12 calendar months
  and audit p62. The legislation SECTION lines are scored by the heading policy for them: a SECTION line that opens an
  amendment sentence holds no title, so calling it a heading is wrong. 27 of the 28 are a block
  of their own in reflow's conversion. On legislation p8, reflow joins `Code, is amended to read
  as follows:` to its SECTION line, so the pass offers no such block.
- **Required cases** (false rejections): every `heading` assertion in `tests/pages/specs` that
  is not an expected failure. That is 190, and 182 of them are blocks the pass offers. The 8
  headings of the two dental claim forms sit inside table cells, which the pass never offers, so
  they can't be disputed. Every required heading is judged as if the model proposed it,
  whatever role reflow gave it. Audit p30's `Requests for Information`, appeal p2's
  `CERTIFICATION` and the two legislation section titles (pp30 and 39) are among them.
  For row headers, the specs require only 2 (resnet pp5 and 6), so the rules' own row-header
  columns on the spec pages (22) are counted too.

A check sees the block, and the block with text just before it on the same page, as the pass
does.

## Scores

| Check | On | Wrong cases rejected | Its own targets | Required rejected |
|---|---|---|---|---|
| `continuation`: starts in lower case, or ends in a comma or semicolon | **on** | 4 of 27 | 4 of 4 | **0 of 182** |
| `form-id`: a form code beside a revision mark (`BOE-305-AH … REV. 12 (05-24)`) | **on** | 3 of 27 | 3 of 3 | **0 of 182** |
| `runs-on`: the paragraph before is prose (6+ words) that stops with no punctuation | off | 1 of 27 | 0 of 2 | 44 of 182 |
| `no-witness`: not bold over all its text, not larger than the body text, no heading ink, no heading tag | off | 26 of 27 | 16 of 16 | 4 of 182 |
| `data-column`: a column of numbers, dates or weekdays | n/a | 14 of 14 columns | 14 of 14 | 0 of 2 (specs), 0 of 22 (rules') |

The checks that are on together reject **7 of the 27** wrong heading answers, and none of the 182
required headings.

## Why each number is what it is

- **Only 7 of 27.** Most of the model's wrong headings are lines set like the body text: the
  legislation's SECTION amendment lines (4), the budget's column captions and resolution (4), the
  permit ledger's captions and running labels (7), audit p19's notice, and the run-in
  `Sec. 151.003.` paragraph. Only `no-witness` sees them, and it is off (next point). Two cases
  have no enabled check: `COLOR KEY:` is bold, so it has a witness, and `Sec. 151.003.` is caught
  only by `no-witness`.
- **`no-witness` is off, with 4 false rejections.** These are the two legislation section titles
  that the SECTION-line policy made headings (`Sec. 25A.0041. RULES RELATED TO …`, `Sec. 25A.021. ACTIONS COMMENCED
  …`) and NACA p2's `1. FUNDAMENTAL AND DERIVED UNITS`, all OCR text set at body size with no
  bold that OCR can see, plus `RECLAMOS DE BLACK LUNG Y FECA` on the Spanish medical claim form.
  On a scan the type is the weak witness, and those are the pages where the model is most
  needed. Turning it on would reject 26 of 27 wrong answers. That is a product decision, since it
  costs these 4.
- **`runs-on` is off, with 44 false rejections,** almost all in provider directories: an address
  paragraph ends with a phone number and no stop, and the next provider's name is a
  heading. The others are a title after a masthead line (NCHS, NACA) and form sections after a
  label (appeal pp1 and 2, audit p30). It also misses its own two targets (legislation pp4 and
  19), because each is the first block on its page and the sentence it finishes ends the page
  before. The pass asks about one page at a time.
- **`continuation` and `form-id` are exact on their targets** (4 of 4, 3 of 3) with no false
  rejection. There is no rule against a heading that ends in a lower-case word, which would reject
  `Requests for Information`.
- **`data-column` rejects all 14 wrong columns** (the Sunday column of each of the 12 months and
  audit p62's two numeric columns) and none of the 24 row-header columns. But the pass asks no
  row-header question yet (report issue 11), so there is no dispute for it to filter. It is in
  the code and tested, ready for that question. Two of run A's row-header cases can't be scored:
  permit pp2 and 6 were tables only in run A's hybrid run, and the heuristic engine's ledger is
  unread crops. Audit p62's column 0 is left out because reflow's table puts the row labels
  there, where run A's table had numbers.

## What the numbers could be measuring instead

- **The conversion, not the model.** A check judges reflow's block, not the model's picture. A
  case scores only where reflow's block is the one the model was shown, and the text match (the
  start of the block) found 27 of 28 wrong cases and 182 of 190 required ones. The rest are
  listed above.
- **Quick excerpts.** The harness converts excerpts of long documents. Reading order and the
  block before can differ from a whole conversion: `--whole` converts whole documents for the
  same score.
- **A small set.** The wrong cases come from 6 documents, and nothing here measures the checks
  on answers they were not written for. The required set covers every spec heading (182 blocks,
  35 documents), but only 2 required row headers.

Reproduce: `PYTHONPATH=src:tests/pages .venv/bin/python tests/pages/vision_gates.py --list`, and
`pytest -o pythonpath=src -m pages --quick tests/pages/test_vision_gate_pages.py` (one test per case).
