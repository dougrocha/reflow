# Local models for figure alt text

Measured 2026-11-01 on an RTX 3090 (24 GB), with Ollama 0.40.13. Everything ran locally. No
route sent anything off the machine, and no API model was called.

## Recommendation

**Keep figure alt text off for the sample builds, and keep `qwen3-vl:8b` as the `--alt-text
ollama` default.** No route passes the sample builds' gate: every route has a draft graded wrong on 17 to 34
of the 85 figures, and the gate asks for no number or name the page lacks on every figure. So the
sample builds do not get the follow-up pass, and no default changes.

- `qwen3-vl:8b` through Ollama is the best route to draft alt text by hand
  (`--alt-text ollama`). It has the most drafts graded right (55 of 85), with 21 graded wrong
  against 20 for the in-process model. It takes 2.1 s a figure, half the time of
  the in-process model (4.7 s), with 11.3 GB of video memory instead of 17.5 GB.
- MiniCPM-V 4.7 35B-A3B matches it on accuracy (54 right, 20 wrong) and is faster (1.3 s a
  figure). It is the only candidate that keeps up with the baseline, but it needs 18.8 GB, so it
  does not run on a smaller GPU, which was the point of this comparison.
- MiniCPM-V 4.6 meets this comparison's success criteria on paper: 17 wrong, no failures, 2.5 GB, and
  0.55 s a figure. But only 14 of its 85 drafts were graded right. 54 are true but too generic to
  replace the fallback alt ("The document shows total pension expense and other related financial
  figures."). Use it on a small GPU only if a generic draft is worth a reviewer's time.
- MiniCPM-V 4.7 1B is the least accurate: 34 drafts wrong, mostly misread numbers on tables and
  charts.

## What was measured

The earlier baseline holds. `--alt-text local` runs `Qwen/Qwen3-VL-8B-Instruct` in-process
(torch and transformers, bf16), and `--alt-text ollama` runs `qwen3-vl:8b`
(`enrich.LOCAL_MODEL` and `enrich.ALT_OLLAMA_MODEL`). `gemma4:e4b` is the default only of the
form questions, but it was asked for as a comparison, so it ran too.

**Skipped: `minicpm-v:8b` (MiniCPM-V 2.6).** Its weights are under the MiniCPM Model License,
which allows free commercial use only after registration, so it was not benchmarked.

**Licence of the candidates.** The upstream model cards of MiniCPM-V 4.6 and 4.7 say Apache-2.0.
The Ollama tags ship no licence text (`ollama show --license` prints nothing for all three), so
check the upstream terms before you adopt one.

### Figures

85 figures from 8 documents, all drafted by every route. A figure is drafted only when it has no
alt of its own, so the count is what reflow would send to a model:

| Document | Figures drafted | What they are |
| --- | --- | --- |
| Carmel budget (`da-timelines-difficult-graphics`), named in the plan | 21 | photos, plaques, pie charts, timelines, small timeline icons |
| Comprehensive financial audit (`da-financial-audit-comprehensive`), named in the plan | 37 | photos, logo, signatures, maps, bar charts, table snippets |
| Scanned budget (`da-scanned-budget-handwriting`), named in the plan | 1 | a notary stamp |
| RFP with diagrams (`da-complex-rfp-diagrams`) | 12 | charts, map, flowcharts, screenshots of tables |
| NCHS QuickStats (`nchs-bar-chart`) | 2 | a pie chart and a bar chart |
| Project Firstline slides (`cdc-firstline-slides-es`) | 12 | photo crops, background fragments, a logo |
| Accident insurance flyer (`az-accident-insurance-flyer`) | 0 | its one figure is the coloured band across the page edge, marked decorative before drafting |
| Antibiotics tri-fold (`cdc-antibiotics-trifold`) | 0 | its three photos carry tagged alt, so they are never drafted |

The flyer and the tri-fold give nothing to draft, so the photos the comparison asks for come from the
Firstline slides.

### Method

`scripts/bench_alt_text.py` converts every document once per route, into a fresh output folder,
through `reflow.convert.convert` with the options the CLI passes (`alt_text`, `alt_text_model`).
It wraps the provider that convert makes to time each figure, and changes nothing else.

- **First load.** Before the documents, the route's model is loaded once and timed: for Ollama,
  `ollama stop` and then an empty `/api/generate`; for `local`, the model load. The model files
  were in the page cache, so a first load from disk is slower.
- **Video memory.** nvidia-smi samples `memory.used` every 250 ms, and the figure is the peak
  above the idle reading taken before the load. For `local` it is the same as
  `torch.cuda.max_memory_reserved` (17.1 GB).
- **GPU.** Before each route nvidia-smi listed only desktop apps, about 1.4 GB used at idle. No other benchmark ran.
  The first MiniCPM-V 4.6 run left its model loaded, because the harness looked for the name
  `minicpm-v4.6` while Ollama reported `minicpm-v4.6:latest`. The model stayed loaded through the
  two MiniCPM-V 4.7 runs (2.5 GB, counted in their idle reading). The harness now matches the
  `:latest` name. MiniCPM-V 4.6 then ran again on its own, and its table row is from that rerun.
  When the work finished, no model was loaded and nothing of the benchmark was left on the GPU.
- **Cache.** A second run of the `qwen3-vl:8b` route into the same folders made 0 model calls.
  `alt-text-cache.json` replayed all 73 of its figures.
- **Grading.** One grader graded every draft blind to the route, with the routes shuffled under
  letters for each figure: right, partly right (true but too generic, or a small misreading), or
  wrong (a made-up or misread number, name, label or object, or a value given to the wrong
  series). Each grade was checked against the crop, and against the page when the crop cut off its
  legend. Two "wrong" grades were checked again by hand (a timeline triangle that points up, and a
  pension expense of $255,921,172 read as $2,559,211,772); both stand.
  `docs/assist-bench-*.csv` hold the form questions' benchmark, not alt text. So no reference
  answers exist for figures, and every draft was graded against the figure itself.

## Results

| Route | Seconds a figure, median and p95 | First load | Video memory above idle | Right | Partly | Wrong | Flagged `suggested-alt-uncertain` | Failed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `--alt-text local` (Qwen3-VL-8B, bf16) | 4.69, 7.79 s | 13.9 s | 17.5 GB | 50 | 15 | 20 | 42 | 0 |
| `--alt-text ollama` `qwen3-vl:8b` | 2.09, 3.30 s | 2.7 s | 11.3 GB | **55** | 9 | 21 | 44 | 0 |
| `gemma4:e4b` (the comparison) | 0.74, 1.18 s | 5.6 s | 5.7 GB | 38 | 22 | 25 | 38 | 0 |
| `minicpm-v4.6` | **0.55, 0.77 s** | 2.8 s | **2.5 GB** | 14 | 54 | **17** | 37 | 0 |
| `openbmb/minicpm-v4.7:1b` | 0.74, 1.00 s | 1.8 s | 2.2 GB | 23 | 28 | 34 | 41 | 0 |
| `openbmb/minicpm-v4.7:35b-a3b` | 1.30, 1.90 s | 15.4 s | 18.8 GB | 54 | 11 | 20 | 38 | 0 |

- Of 85 figures. The draft counts by flag, and the per-figure seconds and grades, are in
  `docs/alt-text-bench-figures.csv` (no draft text). Every route drafted every figure: no failed
  draft, no figure left `alt-missing`, and every answer was valid JSON for the schema without
  changing `OllamaVision`.
- MiniCPM-V 4.7 did not reason at length despite the worry that it would. With `think: false` its answers
  are about 145 tokens, like the other models'.
- Ollama's context was 32,768 tokens for `qwen3-vl:8b`, `gemma4:e4b` and `minicpm-v4.6`, and
  8,192 for the two MiniCPM-V 4.7 tags. These are their defaults. Part of each memory figure is the key-value cache.
- The time is the model's. A conversion's other stages are the same for every route.

### Where the drafts go wrong

| Route | RFP (12) | Audit (37) | Carmel (21) | Slides (12) | NCHS (2) | Notary stamp (1) |
| --- | --- | --- | --- | --- | --- | --- |
| `local` | 5 / 0 / **7** | 21 / 9 / 7 | 11 / 4 / 6 | 10 / 2 / 0 | 2 / 0 / 0 | right |
| `qwen3-vl:8b` | 5 / 2 / 5 | 23 / 4 / 10 | 13 / 2 / 6 | 11 / 1 / 0 | 2 / 0 / 0 | right |
| `gemma4:e4b` | 6 / 4 / 2 | 17 / 8 / 12 | 6 / 5 / 10 | 7 / 5 / 0 | 2 / 0 / 0 | wrong |
| `minicpm-v4.6` | 0 / 8 / 4 | 4 / 26 / 7 | 7 / 9 / 5 | 1 / 10 / 1 | 1 / 1 / 0 | right |
| `minicpm-v4.7:1b` | 2 / 3 / 7 | 6 / 12 / **19** | 8 / 8 / 5 | 5 / 5 / 2 | 2 / 0 / 0 | wrong |
| `minicpm-v4.7:35b-a3b` | 7 / 3 / 2 | 21 / 0 / 16 | 13 / 7 / **1** | 10 / 1 / 1 | 2 / 0 / 0 | right |

Right / partly / wrong. The wrong drafts fall into a few kinds:

- **Numbers misread or given to the wrong series.** A one-row table of $255,921,172 becomes
  $2,559,211,772 or $2,592,172. The 2023 10-digit calls (9,602) become 184,281, which is the 911
  count. A variance of +$421,085 is read as a decrease.
- **Trends reversed.** "2023 is higher in every category" when one category fell.
- **Text made up in photos and logos.** "Lake County" or "Smear County" on a sign, "GFA" for the
  GFOA logo, "Harold L. Ices" for Ickes on a plaque.
- **Flowchart branches swapped.** Yes and No reversed on the RFP's response-plan diagram.

Photos without text are the easy case: on the slides, `qwen3-vl:8b` and the in-process model
have no wrong draft. The errors are in the charts, tables, diagrams and any text in the picture
(signs, plaques, logos, signatures), where the alt text matters most.

### The uncertain flag catches about half the wrong drafts

`suggested-alt-uncertain` marks a draft that names a number, a date or a quotation the figure's
context does not hold. For `qwen3-vl:8b` it caught 11 of the 21 wrong drafts. The other 10 were
flagged `suggested-alt` or `suggested-description`. It also flagged 33 drafts that were right or
partly right, because a number read correctly off a chart is usually not in the context either.
The flag points a reviewer at numbers. It does not separate right drafts from wrong ones.

## Sample drafts

The same figure from each route.

**RFP, Figure 1, a bar chart of calls by type and year** (p2):

| Route | Grade | Alt |
| --- | --- | --- |
| `local` | wrong | Bar chart showing annual call volumes for Kitsap dispatch floor from 2018 to 2023, broken down by call type. (Its description gives the 2018 total as 173,336, the 911 count.) |
| `qwen3-vl:8b` | wrong | Bar chart showing annual call volumes for 911, 10-Digit, Non-emergency, and Outgoing calls processed on the dispatch floor from 2018 to 2023. (Its description gives 2023 10-digit calls as 184,281.) |
| `gemma4:e4b` | wrong | Bar chart displays the number of calls processed on the dispatch floor from 2018 through 2023. (Its description gives the 2018 total as 173,336.) |
| `minicpm-v4.6` | partly | The chart displays calls processed on the dispatch floor over years from 2018 to 2023, showing totals for 911, 10-digit, non-emergency, outgoing. |
| `minicpm-v4.7:1b` | partly | Bar chart showing calls processed on the dispatch floor for Kitsap in 2018-2023, categorized by 911, 10-digit, and non-emergency calls. |
| `minicpm-v4.7:35b-a3b` | right | Bar chart showing annual call volumes processed on the dispatch floor from 2018 to 2023, categorized by call type. |

**Audit, a one-row table: "Total pension expense $255,921,172 $93,583"** (p62):

| Route | Grade | Alt |
| --- | --- | --- |
| `local` | right | Total pension expense for the period is $255,921,172, with a related amount of $93,583. |
| `qwen3-vl:8b` | wrong | Total pension expense shows $2,559,211,772 and $93,583 in financial figures. |
| `gemma4:e4b` | right | Total pension expense for the period ending June 30, 2022, was $255,921,172 and $93,583. |
| `minicpm-v4.6` | partly | The document shows total pension expense and other related financial figures. |
| `minicpm-v4.7:1b` | wrong | Pension expense table showing total pension expense for the period ending June 30, 2022, with amounts of $25,912 and $9,358. |
| `minicpm-v4.7:35b-a3b` | wrong | The table displays the total pension expense for two different periods, showing amounts of $2,592,172 and $93,583. |

**Firstline slides, the Project Firstline logo** (p14):

| Route | Grade | Alt |
| --- | --- | --- |
| `local` | partly | Project Firstline logo featuring a shield with medical icons and text identifying it as CDC's national training collaborative for healthcare infection (cut at 150 characters) |
| `qwen3-vl:8b` | right | Project Firstline logo with shield icon and text identifying it as CDC's National Training Collaborative for Healthcare Infection Prevention & (cut at 150 characters) |
| `gemma4:e4b` | partly | Project Firstline logo featuring a shield and text promoting healthcare infection prevention. |
| `minicpm-v4.6` | partly | The image shows a section heading for PROJECT FIRSTLINE with a shield logo and related text. |
| `minicpm-v4.7:1b` | wrong | Project FirstLine logo featuring a shield with people, representing CDC's healthcare injection prevention and control initiative. |
| `minicpm-v4.7:35b-a3b` | right | Project FIRSTLINE logo for CDC's National Training Collaborative on healthcare infection prevention. |

**Carmel, a pie chart of 2024-25 revenues** (p8). Every route but one is wrong. Their
descriptions give a value to the wrong category, merge two categories under one amount, invent a
category ("Revenue Tax"), or call Treatment Fees the second-largest source when Property Tax is
bigger. `minicpm-v4.7:35b-a3b` is partly right: its values are right, but it merges two labels
and leaves one out.

## The decision for the sample builds

The plan asked for these steps:

1. **Speed gate.** The time per figure is in the table above. The gate is "no number or name the
   page lacks" on every figure, and no route passes it. The closest, `qwen3-vl:8b`, has 21 wrong
   drafts of 85 (25%), and its uncertain flag misses 10 of them.
2. **Recount.** The 94 published samples (`web/.reflow`, as built on 2026-11-01) hold 1,060
   figures. 114 are unread crops of writing. 73 are skipped before drafting: 57 repeated on 3 or
   more pages, 13 tiny, 1 ruling, 1 blank, 1 bleed. That leaves 873 to draft (epa-ghg-inventory
   alone has 270). At the measured means, the first full pass takes about 31 minutes with
   `qwen3-vl:8b` through Ollama (2.16 s a figure), and about 61 minutes in-process (4.21 s).
   Later passes replay the cache.
3. **Sample builds**, and 4. **the follow-up pass.** Not done, because the gate did not pass. The
   builds keep alt text off unless `REFLOW_ALT_TEXT` is set. Nothing changes in
   `web/scripts/build-docs.ts`.
5. **The reviewed run.** The Carmel, budget and audit samples were converted with
   `--alt-text ollama` (`qwen3-vl:8b`), the same as `REFLOW_ALT_TEXT=ollama`. Three drafts per
   document were set aside for a person to judge. No generated output is committed. The drafts in this report are quoted as samples.

A pass on the sample builds would put a wrong number or name on about one in four drafted charts,
tables and photos. Each would be labelled AI-drafted and listed for review, but about half would
not be flagged uncertain. Until a reviewer checks the drafts, the fallback alt ("Picture on page 4
under …; no description yet") is the honest one.

## What would change the decision

- **A narrower pass.** Draft only figures the model calls a photo, and drop any draft that quotes
  text from the picture. On the slides, the two Qwen3-VL routes had no wrong draft. The charts,
  tables and signs, where they fail, would keep the fallback.
- **A larger crop for tables.** Several misread numbers come from one-row table snippets cropped
  without their headers. Sending the crop with the surrounding rows might help.
- **One grader.** Each draft was graded once, so a difference of 2 or 3 wrong drafts between
  routes is within the noise. The gap to MiniCPM-V 4.7 1B (34) and the number of generic MiniCPM-V
  4.6 drafts (54) are not.

## Run it again

```sh
.venv/bin/python scripts/bench_alt_text.py run ollama:qwen3-vl:8b --rerun-cached
.venv/bin/python scripts/bench_alt_text.py run local
.venv/bin/python scripts/bench_alt_text.py run ollama:minicpm-v4.6
.venv/bin/python scripts/bench_alt_text.py report
```

The harness needs `ollama serve` and the gitignored samples in the main checkout. It writes to
`/tmp/altbench` by default. `report` writes the per-figure CSV and a blind grading sheet with its
key.
