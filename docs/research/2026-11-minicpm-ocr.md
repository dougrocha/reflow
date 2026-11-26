# MiniCPM OCR on hard scans and handwriting

Measured 2026-11-01 on an RTX 3090 (24 GB), with Ollama 0.40.13, OCRmyPDF 17.13 with
Tesseract 5.5. Everything ran locally, and no API model was called.

## Recommendation

**Adopt neither MiniCPM route, and change no default.** Both MiniCPM routes are fast and small,
but both put text on the page that the page does not have. The bar is that a model that
invents text fails, whatever its speed.

- **MiniCPM-o 4.5** (`openbmb/minicpm-o4.5`, with the `--assist local` prompt) **invents text.**
  On the scanned budget's small-print notice, it replaced the dial-in number with a toll-free
  number of its own, turned the meeting website into another address, changed one digit of the
  passcode, and replaced the contact with a name and email of its own. None of these are on the
  page. On the permit
  ledger it wrote the columns one after another, so no value stays with its row. It also gave
  all 21 fees as 16.00, though 9 of the 20 rows have another fee.
- **MiniCPM-OCR 1.0** (OpenBMB's Ollama GGUF guide, prompt `Text Recognition:`) **is the
  fastest by far.** It takes 1.3 s a page at 4.9 GB, reads the most handwritten ledger cells
  (262 of 305) and reads every map label. But it **moves values to the wrong rows and repeats
  rows.** On ledger p3, from the ninth of 21 rows on, each name sits beside the next row's
  values, and the last row appears three times. It also left out the whole small-print notice
  column on budget p5. It misspelt both signers' names on budget p4. Its text
  is LaTeX (`\frac{1 mol}{82.034 g}`, `T_f`) and table tags (`<fcel>`, `<nl>`), which reflow
  would show to a reader as written.
- **The baseline, Qwen3-VL-8B in-process (`--assist local`), stays the best route.** It is the
  only route that marked the unreadable signatures `[illegible]`, as the prompt asks. Its printed
  text is nearly exact: a character error rate of 0.2% on the pages it read. It needs 18.4 GB,
  takes 14 s a page, and failed on ledger p3, where it ran out of tokens after 112 s and the
  pass discarded the draft. On ledger p1, the one ledger page it read whole, it made up names
  and repeated a row.
- **Qwen3-VL-8B through Ollama (`qwen3-vl:8b`), added as a fourth route, is the strongest
  candidate for a smaller GPU.** It is the same model family at 11.6 GB, and it is 2.6 times as
  fast (5.5 s a page). It keeps every ledger value in its row and reads every map label, but it
  misreads names and numbers. It is the one route that read both budget signatures right. It
  also has no wiring yet: `--assist` has no Ollama or model option. See "Open decisions".

No new route is wired into `reflow convert`. The benchmark's transcriber,
`assist.OllamaTranscriber`, is experimental and is not reachable from the CLI. Its text would
be a draft, `ai-drafted`, `reviewed=false`, in review, and next to OCR's reading, as every
`--assist` draft is.

## What was measured

### Pages

These are eight hard pages from the DocAccess demo samples. Each is cut out of its sample as a
one-page PDF.

| Page | What is hard |
| --- | --- |
| chem-math p1 | Handwritten answers and working: formulas, fractions, a "Key" note |
| permit ledger p1 | A 1987 ledger: a printed header over 20 handwritten rows, with struck-out old permit numbers |
| permit ledger p3 | The same ledger, 21 rows in block capitals and cursive |
| legislation p5 | A printed scan with tracked changes (struck "[or]", underlined insertions) and margin line numbers |
| scanned budget p4 | Two signatures: a DocuSign script name and a pen signature |
| scanned budget p5 | A pen signature, a notary stamp, and a small-print public notice column |
| Carmel p4 | A photo above a typed letter (OCR'd text) |
| brochure map p2 | A scanned map: 25 printed labels, one set at an angle |

### Routes

| Route | How it runs |
| --- | --- |
| Tesseract (baseline A) | The default conversion (`--ocr auto`). Four of the pages (chem-math, ledger p1, budget p4 and p5) keep a vendor text layer under `--ocr auto`. So each page was also read with `ocrmypdf --force-ocr -l eng`, shown as "Tesseract (forced)". |
| `--assist local` (baseline B) | `Qwen/Qwen3-VL-8B-Instruct`, transformers 5.19, bf16, in-process |
| MiniCPM-OCR 1.0 | OpenBMB's guide followed as written: the Q8_0 model, the F16 4x projector and the Q8_0 DSpark draft, imported with its Modelfile (`num_ctx 8192`, temperature 0, `draft_num_predict 15`). Prompt `Text Recognition:`, plain text back, `think: false`, `top_k 1`. |
| MiniCPM-o 4.5 | `openbmb/minicpm-o4.5` (Q4_K_M, 8.2B), with the same `TRANSCRIPTION_PROMPT` and JSON answer as `--assist local`. It ran at the tag's default context of 4,096 tokens. |
| `qwen3-vl:8b` (added) | The same prompt and JSON through Ollama, to tell the model apart from the runtime |

The image is not quite the same for every route. `LocalProvider` shrinks each image to at most
1,280 pixels a side (`MAX_IMAGE_SIDE`) before the model sees it. The Ollama routes send the full
rendering at `assist.SCALE` (1,224 by 1,584 for a letter page, 2,016 by 1,224 for the ledger),
and each model then resizes the image in its own way. So a difference between the in-process
model and `qwen3-vl:8b` is partly a difference in the input. The comparison measures each route
as it runs, not the model alone.

Ollama 0.40.13 was already installed, which is newer than the guide's 0.40.12, so no upgrade was
needed. Both MiniCPM models ran on a second Ollama server started for the benchmark on port
18499, with its own model store in `~/.cache/reflow-bench209`. The main Ollama store was not
changed. `qwen3-vl:8b` ran on the main server.

The MiniCPM-OCR SDK pipeline was not run. That pipeline is the PP-DocLayoutV3 layout model, then
recognition of each region. It needs a Paddle or torch layout model in a separate environment.
The routes above already settle the question for a draft beside OCR, so it is left as a
follow-up. It might fix the row shifts on the ledger, because it would crop each table cell.

### Method

`scripts/bench_transcription.py run ROUTE` converts each page through `reflow.convert.convert`
with `assist="local"`, with convert's transcriber swapped for the route's. So each route
receives exactly the crops that `--assist` sends (`assist.regions`). Then the whole page goes to
the same transcriber once more, rendered at `assist.SCALE` as a scan is rendered. This way every
route reads every page whole, and the scores use the whole-page text.

`--assist` itself selects only five of the eight pages: chem-math, both ledger pages and both
budget pages. Legislation p5, Carmel p4 and the brochure map have usable OCR text, so the pass
sends nothing for them. On 2026-10-19 Carmel p4 was selected, because OCR then read only its
page number.

- **Ground truth.** A reference transcript of each page, checked against the page image at 4x
  to 7x zoom, and corrected by hand where it was wrong.
  - **The ledgers.** Truth for both pages was read cell by cell from the page.

  The truth and the runs are not in the repository.
- **Printed text.** The character error rate is computed per printed line against the best place
  for that line in the output. It ignores case, spaces and markup (Markdown, LaTeX commands,
  HTML or OTSL tags), so reading order and layout cost nothing.
- **Handwriting.** Each distinct handwritten cell of the truth counts as read when the output
  holds it with at most 20% of its characters edited. There are 320 cells: 15 on chem-math, and
  145 and 160 on the ledgers. A cell counts wherever it appears on the page, so a date that recurs
  is one cell. The three signatures are graded only by hand, because their names are also printed
  on the page. Every ledger row was also graded by hand.
- **Junk and invented text.** "Wrong facts" are the numbers, emails and web addresses in the
  output that are not on the page exactly. Each one was then checked by hand as a misreading or
  an invention.
- **GPU.** Before each timed run, nvidia-smi listed only desktop apps, about 1.7 GB used at
  idle. No other benchmark ran.
  Video memory is the peak above that idle reading, sampled every 250 ms through the whole run.
  Each harness unloaded its own Ollama model at the end. Afterwards no model was loaded on either
  server, and nothing of the benchmark was on the GPU.

## Results

### Speed and memory

| Route | Seconds a page, whole page: median, max | First load | Video memory above idle | Pages failed |
| --- | --- | --- | --- | --- |
| Tesseract (forced) | 3.8, 6.1 s (CPU) | none | none | 0 |
| `--assist local`, Qwen3-VL-8B in-process | 14.3, 112.0 s | 10.5 s | **18.4 GB** | 1 (ledger p3: 4,096-token limit) |
| `qwen3-vl:8b`, Ollama | 5.5, 14.9 s | 4.3 s | 11.6 GB | 0 |
| MiniCPM-OCR 1.0, Ollama | **1.3, 3.9 s** | 2.0 s | **4.9 GB** | 0 |
| MiniCPM-o 4.5, Ollama | 4.6, 11.4 s | 1.9 s | 6.6 GB | 0 |

- The time is the model's time per page, not the conversion's.
- The model files were in the page cache, so a first load from disk takes longer.
- In the `--assist` pipeline, one ledger p1 crop hit the 4,096-token limit for both
  `qwen3-vl:8b` and MiniCPM-o 4.5. One ledger p3 crop did the same for the in-process model.
  Each was discarded and flagged `ai-assist-failed`, and OCR's text stayed.

### Quality

The whole page, as read by each route.

| Route | Printed text, error rate (all pages / pages without a ledger) | Handwritten cells read, chem-math and ledgers (of 320) | Wrong facts on printed text | Ledger rows moved, repeated or lost | Signatures (budget p4, p5) |
| --- | --- | --- | --- | --- | --- |
| reflow today (`--ocr auto`) | 4.8% / 4.8% | 14 | 6 | no rows, junk text | none; "UtaScore 40" |
| Tesseract (forced) | 3.8% / 4.2% | 79 | 7 | no rows, junk text | none |
| `--assist local` (Qwen3-VL-8B) | **0.2% / 0.2%** (7 pages) | 112 (p3 failed) | 3 misread | p1: 1 repeated, 1 lost | the president right; `[illegible]`; `[illegible signature]` |
| `qwen3-vl:8b`, Ollama | 2.2% / 0.7% | 261 | 5 misread | **none** | **both p4 signers right**; the p5 signer (right) |
| MiniCPM-OCR 1.0 | 5.8% / 5.7% | **276** | 0 (but a column left out) | p1: 5 moved, 1 added; p3: 13 moved, 2 repeated | both p4 names misspelt (wrong); none |
| MiniCPM-o 4.5 | 3.1% / 4.2% | 259 | **4 invented** | p1: all 20 (columns written one after another), 9 fees made up; p3: 3 moved | both p4 names misspelt (wrong); `[Signature]` |

The per-page numbers are in `docs/transcription-bench-pages.csv`. Its cell counts for the two budget pages are the signature names, which match the printed names as well. Error rates on the printed text
of single pages:

- **Brochure map.** Tesseract reads the labels at 18.6% to 19.1%. Every model reads them at
  0.0% to 0.5%.
- **Budget p5.** MiniCPM-OCR is at 32.7%, because it left out the notice column. MiniCPM-o 4.5
  is at 2.1% while inventing four facts, so the error rate alone hides the invention.
- **Legislation p5.** All routes are at 1.9% or less. No route marks the struck "[or]" as
  struck. MiniCPM-o 4.5 writes it as plain "[or]", both Qwen routes as "[ex]" (the strike line
  read as letters), Tesseract as "[e¥]", and MiniCPM-OCR leaves it out.

### Where each route goes wrong

**Inventions: text with no source on the page.** Only MiniCPM-o 4.5 does this outright.
- **The notice on budget p5.** It gave the toll-free number, the website, the passcode and the
  contact details described above. Its pipeline crop of the same page invented a different toll-free
  number. The page is easy for every other model: the in-process Qwen got all of the
  notice right except one digit of a phone number and one email address.
  Outside the notice it misread one digit of the envelope ID.
- **The ledger.** All 21 fees given as 16.00, and a valuation of 400,000.00 where the page has
  400.00.

**Values in the wrong row.** A reader of a ledger can't tell these from right answers.
- **MiniCPM-OCR.** On p3, from row 9, each name sits beside the next row's values. The last row
  appears three times, under three names. On p1, rows 13 to 17 each sit
  beside the next row's values, and it adds a row of its own: "Restaurant & Flower Shop 5111 Decumwith
  87/88-10 16.00 10/20/87".
- **Qwen3-VL-8B in-process, ledger p1.** It writes one bakery row twice, and the row after
  it is lost. It gives every permit number as "87/87-…", mixing the struck and the
  new numbers. It gives names the page does not hold ("Alberts", "One Heart", "Dale & Denny"
  twice, "Department Center"), and a fee of "Double S. 3.000.00 done".
- **`qwen3-vl:8b` through Ollama.** It keeps every row together. But it misreads names
  ("Gelato", "Bryon", "Audience", "Bud" twice) and some digits (11/11/88 for 1/11/88, 4/15/88
  for 4/5/88).

**Guessed signatures.**
- The prompt says "Do not guess signatures or names". The in-process Qwen followed it, with
  `[illegible]` for the second p4 signer and `[illegible signature]` for the p5 signer.
- `qwen3-vl:8b` read all three signatures right.
- Both MiniCPM models wrote wrong names, misspelling both p4 signers. The guide's prompt
  for MiniCPM-OCR has no such instruction, and it can't flag handwriting.

**Chem-math.** All four models read the handwritten key nearly perfectly: every formula, the
1.497 mol, the 82.034 g, and all four boxed answers. MiniCPM-OCR wrote it in LaTeX. Only
`qwen3-vl:8b` and the in-process model's crop kept the "Key" note.

### Against the success criteria

| Criterion | MiniCPM-OCR 1.0 | MiniCPM-o 4.5 |
| --- | --- | --- |
| Handwriting: no more made-up words than Qwen3-VL-8B, and at least as many right | More cells right (276 against 112, with the in-process model's p3 lost). But more rows moved and repeated, and wrong signature names. **Fails.** | Inventions on budget p5 and made-up fees. **Fails.** |
| Printed: word recall no lower than Tesseract | Word recall was not measured. The character error rate is 5.8% against 3.8%, and the notice column is left out. **Fails.** | Word recall was not measured. The character error rate is 3.1% against 3.8%, but with invented facts. **Fails.** |
| Clearly lower video memory than 17 GB, or faster | 4.9 GB, 11 times as fast. **Passes.** | 6.6 GB, 3 times as fast. **Passes.** |

The page checks of the harness (`-m pages`) convert with default options, so they don't see
`--assist`. They were not part of this comparison. The CER and cell scores above replace them for
these pages.

## Licences

The upstream cards and repository say Apache-2.0 for MiniCPM-OCR 1.0 (weights, code and docs)
and for MiniCPM-o 4.5. The Ollama tag `openbmb/minicpm-o4.5` ships no licence text, as the
MiniCPM-V tags did in an earlier comparison. Using the SDK pipeline would also need a check of the
PP-DocLayoutV3 licence. None of this matters until a route is adopted.

## Open decisions

1. **An Ollama route for `--assist`.** `qwen3-vl:8b` through Ollama is faster and uses 6.8 GB
   less video memory than the in-process model. It keeps ledger rows together and read the
   signatures. It is not cleaner, though: it misreads names, and it read signatures that the
   prompt says not to guess. Wiring it would mean an opt-in `--assist ollama` (with a model
   option) that uses `assist.OllamaTranscriber`. It would not be a default.
2. **Selection misses some pages.** `--assist` does not select the brochure map, where every
   model reads the labels and Tesseract makes junk headings of them. It also doesn't select
   pages whose junk OCR text has a high enough letter share. That is a change to
   `assist.regions`, separate from the model.
3. **A struck word is not marked as struck.** No route marks the struck "[or]" in the
   legislation. MiniCPM-o 4.5 writes plain "[or]", as if it were in force, and the Qwen routes
   write "[ex]".

## Run it again

```sh
scripts/bench_transcription.py run none
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 scripts/bench_transcription.py run local
scripts/bench_transcription.py run ollama:qwen3-vl:8b
OLLAMA_HOST=127.0.0.1:18499 OLLAMA_MODELS=~/.cache/reflow-bench209/ollama ollama serve &
scripts/bench_transcription.py run ocr:minicpm-ocr --host http://127.0.0.1:18499
scripts/bench_transcription.py run ollama:openbmb/minicpm-o4.5 --host http://127.0.0.1:18499
scripts/bench_transcription.py score --json /tmp/bench209/score.json
```

Run them with `.venv/bin/python`. The harness reads the DocAccess demo samples, which are not
redistributed here, and writes to `/tmp/bench209`. The hand-checked truth it scores against is
not in the repository. To import MiniCPM-OCR, follow OpenBMB's
`docs/ollama_deploy.md` into the second server.
