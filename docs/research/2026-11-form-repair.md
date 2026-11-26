# Form repair: region reads against whole-page reads

Measured on 2026-11-04 with `scripts/eval_form_repair.py`. Model `qwen3-vl:8b` through the local Ollama server, temperature 0, seed 0,
`num_ctx` 8192. The model was the only one on the GPU during the run. Each condition ran once,
so these are one sample per page, not rates.

## Set-up

The pages are born-digital pages turned into 200 dpi image-only scans. Their own text layer is
the truth, and reflow's OCR has to read them again. Reflow converts each scan without the pass,
and `form_regions` routes the gaps OCR left. A page's truth is the native words inside its
regions. The truth never reaches the router, the prompts or the checks.

| Page | Kind | Regions routed |
| --- | --- | --- |
| dental-claim-form p1 | the issue's repro (development) | 2: the services header, the missing-teeth and diagnosis row |
| nurse-on-call-flyer p1 | small print | 0 |
| tier-1a-rx-list p1 | full-width text over a drug table | 0 |
| employee-application-spanish p1 | a Spanish form | 6 small text gaps |
| ma-vision-flyer-bilingual p2 | English and Spanish | 0 |
| deceased-member-affidavit p1 | a short form | 0 |
| maternal-risk-assessment p1 | a kept old OCR layer | 0 |
| maternal-risk-assessment p2 | a blank page | 0 (`native-text`) |

At 200 dpi, Tesseract read everything on four of the six scans, so they make no call. The
same is true of the kept layer and the blank page. Those six pages are the zero-call controls.
Only two pages had gaps to score, so the totals below come from 69 words on two pages.

## Results

| Condition | Complete | Added words | Field errors | Unresolved | Calls | Seconds | Tokens |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ocr (main, no pass) | 20/69 = 29% | 7 | 9/9 | – | 0 | 0 | 0 |
| page/current | 54/69 = 78% | 8 | 6/9 | – | 2 | 48 | 5,470 |
| page/targeted | 15/69 = 22% | 28 | 9/9 | – | 2 | 55 | 6,536 |
| region/raw | 61/69 = 88% | 4 | 5/9 | – | 9 | 9 | 519 |
| region/checked | 41/69 = 59% | 4 | 5/9 | 1 | 9 | 9 | 519 |

The conditions:

- **ocr:** what a conversion on main holds in the regions.
- **page/current:** the assist's whole-page transcription prompt.
- **page/targeted:** form repair's prompt and schema, sent with the whole page.
- **region/raw:** the first reading of each region.
- **region/checked:** only the readings that passed the checks, as review would see them.

### Dental claim form (54 truth words)

- **Region reads** got all 54 words on the first reading, with no added words, in 3 calls (the
  third a reread). The header reading kept all ten labels in order, including `29a. Diag. Pointer` and
  `29b. Qty.`, and passed every check.
- **The checks rejected a correct reading.** The second region (field 33, *Missing Teeth
  Information*, and field 34, *Diagnosis Code List Qualifier*) was read correctly twice. Both
  readings were rejected as `incomplete`: the row's diagnosis-code boxes A to D count as inked
  cells, and the reading had fewer fields than inked cells. That is why `region/checked`
  drops to 34/54 and leaves 1 unresolved region. The check errs toward review, as designed, but
  this was a false rejection.
- **The current whole-page prompt also read the whole header here** (54/54). In the issue's
  pilot the same model dropped 29a and 29b from the full page; this scan and this run did not.
  It cost 24 times the tokens of the region reads (5,470 against 519) and added 8 words that
  are printed nowhere on the page.
- **The targeted prompt on the whole page hit the 4,096-token limit** on this form of about 100
  fields and returned nothing. A field list for a whole form is too long for one bounded reply.

### Spanish employee application (15 truth words)

- **Region reads** got 7 of 15 words, with 4 added words. The six gaps are short runs inside
  form lines. A crop of a few words has little context, and the model misread some of them.
- **The current whole-page prompt** hit the 4,096-token limit on this dense two-column form and
  returned nothing.
- **The targeted prompt on the whole page** got all 15 words, but with 28 added words.

### Field errors

There are nine numbered fields inside the dental regions. Every condition scores at least five
errors, because pdfium's truth lines for the header are column fragments
(`25. Area 26.` / `of Oral Tooth`). A multi-line label in the truth matches no condition's
one-line label. Read this column only for which numbers are found, not for whole labels.

## What this supports

- Routing found the issue's dental header from evidence alone. Reading only the routed regions
  was the most complete condition (88% raw), added the fewest words, and cost the least by an
  order of magnitude.
- Both whole-page conditions hit the token limit on one dense form each, which a region read
  can't.
- No condition is correct everywhere. The checks reject some correct readings (the
  diagnosis-code row) and pass some wrong ones (the Spanish gaps' misreadings pass the letter
  bounds). Readings stay review items, and the transcript keeps the OCR baseline.
- The held-out set is too small to rank anything. Four of its six scans had no gaps at 200 dpi.

Run it again with `scripts/eval_form_repair.py` (about 2 minutes on this machine). Check
`nvidia-smi` and `ollama ps` first. Every run starts with an empty cache, so the calls and
seconds are measured, not replayed.
