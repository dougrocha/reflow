# Vision-model page understanding against the heuristic and docling layout

Measured on an RTX 3090 24 GB with Ollama 0.40.13,
docling 2.142.1, OCRmyPDF 17.13 with Tesseract 5.5. Two independent runs, **run A** and
**run B**, each built its own prototype of the pass without seeing the other's work. This
report merges their findings.

Everything ran locally. No page image or document text went to a hosted service. GPU work was
serialised through one lock file. Each batch recorded `nvidia-smi` first. Besides the two runs,
only desktop apps were on the GPU (about 1.7 GB). Run A saw one unexplained idle reading of
10.9 GB with no compute process listed, before its hybrid calendar run, and discarded that
delta.

## Answer

**A local vision model is useful as a checker that routes pages and disputes structure for
review. It does not close the structure gap by itself, and it must not be
allowed to apply its answers on its own word.**

- The two prototypes changed no output. Both left headings, Contents, tables and reading order
  untouched in verify mode (run A checked that all 22 content trees match; run B's page checks
  match the default run exactly). Neither produced a word of output text: the answers are ids
  and words from a fixed list, and over about 300 pages no answer used a word outside the list.
  The verdicts themselves can still be wrong, as the next points show.
- It is good at **page complexity** and at **junk text**. It flags handwriting that no rule
  flags, such as the chem-math working, and map labels as figure labels. It flags diagrams and
  the permit ledger. It leaves the born-digital directory and appeal form almost alone.
- It is **confidently wrong in predictable ways**: continuation lines and column captions called
  headings, small or faint print called handwriting, numeric and Sunday columns called row
  headers, and form identifiers called headings. Self-reported confidence does not separate
  these from right answers.
- The October **junk headings and Contents entries are already gone on main**. Both found 0 on the
  permit, chem-math and brochure, since the unread crops were added. So the study's headline criterion is met
  by the rules, and the vision pass can only be measured on what remains: OCR junk kept as
  paragraphs, missed tables and headings, order, and routing.
- **No expected failure turns into a pass**, and no page check gets worse in verify mode.
- **Cost:** on a 3090, a median of 4.0 s a page over every page (run B, mean 5.2 s), and a
  median of 6.8 to 8.1 s on selected hard pages (run A). 9 GB of video memory at a 16k context,
  11.3 GB at 32k. That is fine for an opt-in pass on one document or on hard pages. It is too slow for
  the default conversion, which takes 0.4 s a page.

Recommendation: keep it experimental and off by default. The next step is the routing signal
and a junk-text witness, both feeding review only (issues 1 to 4 below).

**Hard rule for every follow-up: review before any change.** The model only proposes or checks
structure. Nothing it says changes the output until a reviewer accepts it: no heading
promoted or demoted, no paragraph hidden in an unread crop, no page handed to Docling. Before an
accepted change is applied, reflow checks that the document's text is unchanged. A review
warning added *after* a change does not meet this rule. So run B's `apply` mode, which demotes a
heading on its own and then warns, is out of scope and must not be carried forward. This rule
is issue 0 below and comes before any implementation.

## The model

`qwen3-vl:8b` through Ollama for both, picked from two earlier model comparisons. It had the most correct drafts
in the first and invented no text in the second, it uses about 11.5 GB (against 17.5 GB in-process), and it
is Apache-2.0 upstream. The two runs disagree on the tag's own licence text. Run A saved
the Apache-2.0 text from `ollama show --license`,
while run B and the first comparison report the command printing nothing. Recheck on the installed Ollama and
keep the upstream card with the adoption record. Run B also tried `minicpm-v4.6`, Apache-2.0 upstream and 2.3 GB.
It is 4 times as fast, but it missed every handwritten line and map label, called 20 of 23
pages diagrams, looped into the token cap on 2 pages and demoted the budget's title to a running
header. It is not a small-GPU route for this pass; try `gemma4:e4b` next.

## Two prototypes, two designs

Both are behind `--assist-page-vision verify`, off by default, and follow the assist pattern:
rules propose, the model chooses from a closed schema keyed to reflow's block ids, reflow
checks the answer, and every finding is a `source=assist` review item.

| | Run A | Run B |
|---|---|---|
| Picture | plain page PNG, longest side at most 1,600 px | page with every offered block outlined and numbered (set of marks), longest side at most 1,400 px |
| Roles | 7: heading, paragraph, list, table, figure, label, value | 12: those plus caption, figure_label, handwriting, noise, header_footer |
| Order | choose current, top-to-bottom, or unsure | any permutation of the ids, dropped if it names under 80% of them |
| Table headers | header row and column indices | header row and column counts |
| Complexity | 4 booleans plus missing-table | 4 booleans; handwriting or poor scan rejected on a born-digital page with no image or OCR |
| Pages | spec-covered pages plus 4 chart and table pages: 39 per engine | every page (219), plus a `hard` replay (95 pages) and the 4 committed samples |
| Apply mode | none | demotes an OCR-page heading that the tags don't confirm, and only then warns. Demoted 1 in the corpus, correctly. This breaks the review-first rule, so it is not carried forward |
| Contract | loopback Ollama enforced, cached answers checked again, provenance input registered | answers cached by PDF, page, proposal, model and prompt version, `num_ctx` 16k |

The vocabulary may decide much of the outcome, but these runs don't prove it. Run A's list had no
`handwriting` or `noise`, so its model had no way to call chem-math's working handwriting. It
also chose `label` where `value` was available and right (run A's errors 1 to 7), which is a wrong
choice, not a missing word. Run B's model called all 15 lines handwriting. But the two designs
changed the vocabulary, the numbered outlines, the resolution, the prompt and the proposal at
once, so no single change can be credited. Building on run B's vocabulary and picture is a
promising hypothesis, to be tested by the ablations of issue 10.

## Baselines (both runs agree)

All 11 `samples/docaccess` PDFs, 220 pages, converted fresh for each run. The seeded web
documents were stale, so neither used them. Page checks are the 218 spec assertions run on the
complete outputs.

| Route | Pass / xfail / fail | Time, 220 pages | Video memory |
|---|---|---|---|
| default heuristic | 206 / 12 / 0 | 88 s (0.4 s a page) | none |
| `--engine hybrid --alt-text local --ocr auto` | 188 / 12 / 18 | 577 s (2.6 s a page) | 19.9 GB peak |
| default + vision verify, every page (run B) | 206 / 12 / 0 | 1,238 s wall in all (about 1,150 s above default), of which 1,140 s model time | 9 GB (16k context) to 11.3 GB (32k context) |

The **hybrid baseline is worse than the default** on these documents, before any vision. Its 18
failures:

- the calendar's 12 month tables are merged into 7, losing the month and weekday header
  semantics (11 checks);
- the permit ledger's unread crops are replaced by Docling tables, losing the unread-writing
  evidence on pp2 and 3;
- `4. VALUE` is lost as a heading on the appeal form;
- `3.2 INTERFACES` comes out at the wrong level on RFP p12;
- two Carmel alt-text checks are policy differences, since drafting replaces the "no
  description" fallback.

In exchange, hybrid finds more tables on the audit (32 against 22) and on the permit ledger (5
against 1). Whether its audit headers are better is unsettled, because the two runs
measured different things. Run A counted distinct rows holding column-scoped `th`, which go up
from 33 to 40. Run B counted tables whose leading row is all `th`, which go down from 20 to 4.
The calendar shows the gap: run A counts 12 header rows and run B 0. A follow-up must settle one
definition of a scoped header before it compares header quality.

Per-document headings, Contents entries, table counts, review codes, times and memory are in
both reports, and their headings, Contents and table counts agree.

## What the vision pass found

### Complexity routing: the strongest signal

| Class | Caught | Misses and false flags |
|---|---|---|
| handwriting | Against run A's 9 known pages (chem-math p1; permit pp1, 2, 3, 6, 8; legislation p1; budget pp4, 5): run A 6 of 9 (heuristic) and 7 of 9 (hybrid); run B 7 of 9. chem-math p1 is caught by both, and no rule flags it | Different misses. Run A missed budget p5, plus permit pp2 and 3 on the heuristic run and permit p3 on the hybrid run. Run B missed permit pp2 and 3. False flags: audit pp9, 15, 18 and the NASA cover (born-digital with small images); budget p2 (printed resolution on a `mixed` page); appeal p1 (already rejected by run B's page check) |
| diagram | the RFP's 4 diagram pages, both brochure map pages, the census and NASA charts | none clear |
| poor scan | the permit ledger, brochure p3 | missed scan-census-1960, the clearest poor scan in the corpus |
| dense table | the ledger, the calendar, the audit statements | 42 of the audit's 93 pages: true, but a flood for a human queue |

Born-digital documents stay quiet: 0 flags on the 20-page directory, 1 on the appeal form, and
on the resnet paper only its 3 result tables. Run A's 7 form, calendar and directory pages got 0
flags, but 2 hybrid answers there were rejected, so that is not a measured false-positive rate.
There is no independently labelled set for poor scans or dense tables. These are observations,
not precision and recall.

### Junk text kept as paragraphs

Run B's saved records hold 126 disputes ending in `handwriting`, `noise` or `figure_label`, 115
of them on paragraphs. (Run B's report says 112 and "4 wrong", but neither number can be
reproduced from the data, and its error list names at least 7 wrong paragraph verdicts: appeal
p1 twice, audit p15 once, budget p2 four times.) Treat what follows as examples, not as a
measured precision:

- all 15 handwritten lines of chem-math called handwriting, and the printed question text not;
- 23 garbled ledger lines called handwriting and 8 one-glyph lines called noise;
- 35 legislation margin fragments called noise, plus 4 paragraphs and 2 figures called handwriting;
- 5 brochure map labels called figure labels.

The rules have already moved most junk into unread crops. This is what the crops miss. Run A's
7-role vocabulary could not express any of it.

### Headings and tables

The model surfaces structure that reflow lacks:

- chem-math's printed title as a heading;
- `BUILDING PERMIT APPLICATIONS` on the ledger;
- 27 ledger crops as tables;
- 234 audit paragraphs as table rows (reflow has 22 tables there);
- the budget's and Carmel's missed section titles.

Run B judged the table calls right on every snippet, and hybrid's Docling tables on the same
pages agree.

It is also where the model is least trustworthy. Run A's binary audit of blocks that the specs
pin down:

| Route | Heading TP / FN / FP / TN |
|---|---|
| heuristic baseline | 32 / 0 / 0 / 154 |
| heuristic + model's choices | 31 / 1 / **13** / 141 |
| hybrid baseline | 26 / 0 / 0 / 37 |
| hybrid + model's choices | 25 / 1 / **9** / 28 |

Applied, the model's heading choices would add 9 to 13 wrong headings and lose one right one,
`CERTIFICATION` on appeal p2.

Table headers are no better. Run A found Sunday date columns proposed as row headers on all 12
calendar months, numeric audit columns, and ledger data columns. Two of run A's 39 hybrid page
answers were rejected (appeal p1 and calendar p1), holding 8 wrong header decisions between
them (run A's errors 74 to 81). Run B found 2 of 5 audit header disputes point at
real defects (data rows marked as header), but one integer cannot say which rows.

**Policy conflict: legislation SECTION lines.** The model calls the `SECTION n.` lines headings
(run B: 81 disputes). But reflow's specs for legislation
p5 and p24 require **no headings** on amendment pages. Run B counted these disputes as right;
run A counted them as wrong against the specs. Whichever is right, it is a product
decision, and it must be settled before any heading answer is tuned or applied (issue 5).

### Reading order: not demonstrated

The two runs disagree here, and the evidence is weak either way.

- **Run B** let the model propose any order. On the 8 pages where it disagreed with reflow
  (Kendall's tau below 0.92), run B judged the model's order right on all 8 from the block boxes.
  For example, on brochure p2 the title comes first, and on legislation p37 the margin fragments
  come last.
- **Run A** offered only reflow's order or a top-to-bottom order, and scored against the PDF tag
  tree on the 15 page runs (8 distinct pages) that had enough tagged anchors. The model always
  chose the current order, so it showed no improvement.

Neither has an independent order reference for untagged pages. The 8 wins are plausible but
were judged by the same run that built the pass, from coordinates and not from the
page. Treat a model order as a third witness to `order_evidence` at most (issue 8), measured on a
labelled set first.

### Review-queue load

Verify mode adds a lot of review work. On every page, run B's pass added 639 per-block doubts
(366 on the audit alone), 8 order doubts, 5 table doubts and 82 complexity pages. On the
selected pages, run A's hybrid + verify run pushed RFP's review share to **6.36%, over the 5% cap**
(the heuristic route stayed under it). One review item per page is the minimum before any user
sees this (issue 1).

## Every confidently wrong case

The two lists overlap but are counted differently. Run A lists 83 contradictions with an
independent source (page spec, text layer, grid). Many recur across the two engines and the 12
calendar months, 8 were rejected before use (errors 74 to 81), and 12 are the legislation SECTION
and amendment lines, which are wrong against the specs (see the policy conflict above). Run B
lists 20 clear errors over about 700
disputes and leaves out debatable cases. Merged, by kind:

| Kind | Examples (doc, page) | Found by |
|---|---|---|
| Continuation or body lines as headings | `court.` (legislation p9), `appeals;` (p40), `Code, is amended to read as follows:` (pp4, 8, 19), `NOW, THEREFORE, BE IT RESOLVED…` (budget p2), `This page intentionally left blank.` (audit p19), `COLOR KEY:` (calendar) | both |
| Column captions and running labels as headings | ledger captions `DATE OF APPLICATION`, `VALUATION`, `PERMIT #`, `FEE` (permit pp1, 6); `BUILDING PERMIT` (p6); budget p8 `Adopted`, `Actual 6/30/2024` | both. Run B calls the ledger captions debatable, run A counts them against `max_headings=0` |
| Form identifiers as headings or noise | `BOE-305-AH (P1–P3) REV. 12 (05-24)` (appeal pp1 to 3) | both |
| A real heading demoted | `CERTIFICATION` → paragraph (appeal p2) | Run A |
| Printed or faint text as handwriting | `Form Effective July 1, 2024` (appeal p1), `$-0-`, `; and` (budget p2), `xiY` (audit p15), `Rev 2 i` (NASA cover) | Run B |
| Handwriting flag on born-digital pages | audit pp9, 15, 18; NASA p1; appeal p1 (rejected by the page check) | Run B |
| Handwriting missed | budget p5 (both engines); permit pp2, 3 (run A's heuristic run) | Run A |
| Data columns as row headers | Sunday columns on all 12 calendar months; audit p62 numeric columns; ledger columns (permit pp2, 6, hybrid) | Run A |
| Header rows wrong | appeal p1 authorisation prose as a header row; audit p91 header counted as 0; calendar header rows empty (rejected) | both |
| Missed table | `missing_table=false` beside a visible handwritten ledger (permit pp1, 8) | Run A (run B's model called the same crops tables) |
| Calculated values as labels | 10 chem-math answers such as `82.034 g` and `Th = -1.39°C` | Run A (a wrong role choice: `value` was available, as above) |
| Signatures half wrong | legislation p1: 2 of 4 signature figures called noise | Run B |
| Poor scan missed | scan-census-1960 p2 | Run B |

The pattern: the model reads emphasis, capitals and position as "heading", reads small or faint
print as handwriting, and reads the first column of any grid as row headers. Tables, junk
glyphs, map labels and diagram pages are where it is rarely wrong.

## Cost per page

| Route | Seconds a page | Video memory | 220 DocAccess pages |
|---|---|---|---|
| default | 0.4 (CPU) | none | 88 s |
| hybrid + local alt text | 2.6 | 19.9 GB | 577 s |
| vision verify, every page (run B, measured) | model time: median 4.0, mean 5.2; wall: mean 5.6 | 9 GB | 1,238 s wall (1,140 s model time) |
| vision verify, selected hard pages (run A, measured) | median 8.1 (heuristic) and 6.8 (hybrid); p95 32 and 24 | 11.3 GB | not run on every page |
| vision verify, `hard` set (run B, 95 of 219 pages) | **projected** from the every-page run: 38% of its model time | 9 GB | about 430 s of model time, projected. The measured replay took 82.5 s wall from cache, which is not an uncached cost |

Time follows answer length at about 40 tokens a second: 2.5 s for a sparse RFP page, 15 s for
the 47-block calendar. The first page of each batch pays a 5 to 8 s model load. Answers are
cached by proposal, so a rebuild that leaves the layout alone costs nothing.

**Every page or hard pages?** Run it on every page only when a user turns it on for one
document: the 93-page audit took about 8 minutes. For the sample builds or any corpus, the
`hard` page set (scanned, mixed, OCR-read, at least 30% image cover, or Docling-routed) is the
cheaper choice, but it loses findings. Comparing run B's `vision` and `vision-hard` records:

- **Kept:** every handwriting flag on scanned or mixed pages (chem-math, the ledger, the
  budget), the junk-text findings on chem-math, the permit, the legislation and the brochure,
  and all 8 order disputes.
- **Lost:** the audit's 234 table-row disputes and 42 dense-table pages, the calendar's
  dense-table flag, RFP p2's diagram flag, 15 junk-role disputes (13 on the audit, including
  chart axis labels, and 2 on the appeal form), the three audit handwriting flags (which were
  false).

So the hard set keeps the scan and handwriting findings and drops the born-digital table and
diagram findings. Those need a trigger that runs without the model (issue 7). Never in the
default conversion.

## Success criteria

| Criterion | Result |
|---|---|
| Hybrid and vision measured on the same pages against default, with numbers | **Met** (both runs), with the header-count definitions still to be reconciled |
| Removes junk headings and Contents entries on the handwriting and map pages, or flags them, with no new failing checks | **Met by main before the pass.** 0 junk entries on main. The pass disputes the junk left as paragraphs (115 paragraph disputes in run B's records, at least 7 of them wrong; precision not measured), with 0 new failing checks |
| Complexity flags catch the FAQ's pages without flooding born-digital ones | **Partly met.** Handwriting, diagrams and the ledger are caught, and the directory and appeal form stay quiet. Handwriting caught on 6 or 7 of 9 known pages, with different misses per run (budget p5, permit pp2 and 3). The poor scan scan-census-1960 is missed. Floods: dense table on the audit, and handwriting on small-image born-digital pages and on budget p2. Hybrid + verify breaks the 5% review cap on the RFP |
| Cost per page known well enough to choose every page or hard pages | **Met:** hard pages for corpora, with the coverage loss above. Every page only on request. The hard-set cost is projected, not measured uncached |

## Proposed issues, ranked

Issue 0 is a requirement on all the others.

| # | Issue | Size | Risk | Why |
|---|---|:---:|---|---|
| 0 | **Review before any change from page vision**: a model answer becomes a proposal that a reviewer accepts before it changes the output, whether that is a heading change, a crop, or handing a page to Docling. reflow checks the text is unchanged before applying an accepted proposal. No apply mode that changes first and warns after | S | Low | The hard rule. Run B's `apply` mode and issue 7's routing would otherwise change structure on the model's word, and hybrid replacement already loses required evidence (issue 6) |
| 1 | **Page-vision complexity flag as one review item per page**, with handwriting and poor scan allowed only on scanned or mixed pages, and a dense table on more than a third of a document's pages turned into one document note. Keep per-block facts for the inspector only | S | Low | The strongest signal. On these runs the stricter page check would remove four more false handwriting flags (audit pp9, 15, 18 and the NASA cover) on top of appeal p1, which the current check already rejects. It would not remove budget p2, a `mixed` page. Aggregation cuts the load, but it does not by itself meet the 5% cap: run A's hybrid RFP baseline already has 14 of 283 items, so one more item is 5.3%. Measure the combined queue against the cap |
| 2 | **Labelled routing set**: a held-out, reviewer-labelled set of pages for handwriting, poor scan, dense table and diagram, including clean scans and digital diagrams as negatives | M | Low | Precision and recall for routing are not measured today, and issue 1's thresholds and any skip-or-trust decision need them |
| 3 | **Junk-text verdict as a second witness for the unread crops**: where the model calls an OCR paragraph handwriting, noise or figure label *and* a rule already doubts it (OCR grade, no dictionary words, beside a figure), propose an unread crop, accepted by a reviewer per issue 0. The text stays in the PDF's layer, and nothing is rewritten | M | Medium | Candidates, not results: run B's records hold 15 such paragraph calls on chem-math, 31 on the permit and 39 on the legislation. Neither run tested this combined rule, so the issue must measure how many are eligible, how many would be hidden wrongly, and that the text survives |
| 4 | **Semantic gates** before a heading or table-header dispute is shown: continuation evidence in context (the previous line ends mid-sentence, the block starts in lower case, it ends in a comma or semicolon), form-id patterns, and numeric, date or Sunday columns as row headers. Measure false rejections on the required headings of the specs, such as audit p30's `Requests for Information`, before turning a gate on | M | Medium | It targets most of the confidently wrong list. A blanket "ends in a lower-case word" rule would reject real headings |
| 5 | **Product decision: heading policy for legislation SECTION lines**, which the specs forbid on amendment pages while the model promotes them | S | Low | It blocks any tuning of heading answers |
| 6 | **Protect calendar, form and unread-writing structure from hybrid replacement**, with these 16 structural regressions as fixtures, and one agreed definition of a scoped header row | M | Medium | Hybrid is the issue's baseline and fails 18 checks before any vision. It is not a uniformly stronger engine |
| 7 | **A trigger for born-digital tables the rules miss**: a cheap check that runs without the model (rows of aligned numbers kept as paragraphs) proposes Docling for a page, and the model's table-row disputes add evidence where the pass ran. Docling's tables become a proposal under issue 0, not a replacement | M | Medium | The audit's 234 rows sit on pages the hard set skips, so a model-only trigger would never fire there. Measure the audit's table count, and watch issue 6's regressions |
| 8 | **Reading order as a third witness in `order_evidence`**, with the model allowed a full permutation, scored first on a labelled order set | M | Medium | Run B's 8 favourable disputes are not independently verified, and run A's restricted design showed nothing |
| 9 | **One prototype**: start from run B's set-of-marks picture, 12 roles and `hard` page set, without its apply mode, and add run A's contract (loopback-only Ollama, revalidating cached answers, coverage counters, the provenance input) | M | Low | Carries issues 1, 3, 4 and 7. Neither prototype is merged as it stands |
| 10 | **Prompt, picture and model ablations** on the cached pages: vocabulary, the outlines, no snippets, a lower resolution, and `gemma4:e4b` for a small GPU | M | Low | The two designs changed several things at once, and MiniCPM-V 4.6 failed the job |
| 11 | **Row-level table-header question** (which rows are headers, by row id) | L | Medium | A single count can't be applied. 2 of 5 audit disputes point at real defects |
| 12 | **Anthropic provider for the pass** (the user's own key) | S | Medium | Same pattern as `label_assist`, but it sends a whole page, so the privacy note must say so. Last, because local works |

A vision model only ever proposes or checks structure, and its proposals go to review before
they change anything. No issue above lets it write or rewrite text.

## Reproducing

The two prototypes' benchmark and scoring scripts are not in the repository.
`scripts/eval_complexity.py` now scores the page-vision pass.

Both runs locked the GPU around each batch with `flock`, recorded `nvidia-smi` inside the lock,
and unloaded `qwen3-vl:8b` with `ollama stop` afterwards.
