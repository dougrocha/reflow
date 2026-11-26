# Readable scans: what readers get today, against local models

Measured 2026-11-12 on an RTX 3090 (24 GB), with Ollama 0.40.13. The GPU was idle before each
run (`nvidia-smi`: 1.6 GB used by the desktop; `ollama ps`: nothing loaded). Everything ran
locally, and no API model was called. The "Haiku, direct" column reads the site's published
`direct-haiku` builds, which were made before this measurement. The site has those builds only
for three public sample forms.

## Summary

- **Readers get none of the words these page checks look for.** On the ten pages, the default conversion
  shows 0 of the 37 words and orders the specs ask for, and 19 of the known junk strings. The
  junk is "8USINES3", "ap ted", "w1th", "¢" for bullets, "revene", "HOWTO", "J. LI N E", and
  "Not transcribed" crops.
- **A local model reads them.** `qwen3-vl:8b` through Ollama, reading each page whole, gives 31
  of the 37 and no junk. MiniCPM-V 4.7
  (35B-A3B) and GLM-OCR give 28 each.
- **`qwen3-vl:8b` is the model to wire first.** It has the lowest error rate on printed lines
  (CER 2.1%), and it read the most handwritten ledger cells among the local routes run here
  (264 of 323). It is the only local route that read both budget signatures. It takes 6.6 s a
  page and 9.6 GB of video memory. MiniCPM-V 4.7 needs 19.6 GB and invents more: 114 wrong
  facts against qwen's 46. GLM-OCR is the fastest (2.9 s, 3.7 GB), but its printed text is
  poor (CER 17.9%), and it failed on ledger p1.
- **The permit ledger stays hard.** Every model misses most of its names.

## What was measured

### Pages and specs

Each page below now has page-check assertions (`tests/pages/specs/`). The assertions
check for words a reader should get (`contains`, and `contains_in_order` for the maternal
form's row) and for junk a reader must not get (`no_text`). The ones that fail today are `xfail`. The assertions
that already pass guard against a regression, such as the old chem-math junk ("DgCusoux",
"NaCzMsOr"), which the unread crops removed.

| Page | Spec | What is wrong today |
| --- | --- | --- |
| chem-math p1 | docaccess-chem-math-1 | The purple key is 17 crops marked "Not transcribed". |
| permit ledger p1 | docaccess-permit-1 | The ledger is 2 crops; the header reads "8USINES3 NAfvtE". |
| legislation p1 | docaccess-legislation-1 | "ap ted", "w1th", no "HB 40" and no date; every signature is a crop. |
| legislation p5 | docaccess-legislation-5 | The struck "[or]" is a placeholder "[]". |
| budget p4 | docaccess-budget-4 | Each signature is "Signature, not transcribed.", with no name. |
| brochure p1 | docaccess-brochure-1 | "¢" for bullets; "mouining", "Barterr"; the web address is a crop; no title. |
| remittance p1 | az-remittance-advice-scan-1 | "revene", "ERAsinthe"; claim totals without decimal points. |
| claim booklet p1 | az-claim-form-instruction-booklet-1 | "HOWTO COMPLETEYOUR"; "Out-of-Network" is a crop. |
| claim booklet p4 | az-claim-form-instruction-booklet-4 | The callouts read "J. LI N E 24a". |
| maternal form p1 | az-maternal-risk-assessment-1 (new) | "not known) EDD: (From LMP U/S)" is read before "LMP:". |
| naca p2 | naca-2 | The stacked printed fractions are a crop labelled "Handwriting". |

Every crop that stays should say why. The chem-math spec checks that with `alt_not` on an
alt that ends at "not transcribed.".

### Readings

`scripts/bench_transcription.py compare` grades every reading of a page with the words in its
spec: the words found, and the junk left. A one-word item is found whatever its case, markup or
spacing ("CuSO_4" and "CuSO 4" are CuSO4). A phrase keeps its spaces, so "ERAsinthe" is not
"ERAs in the". An ordered run (the maternal row) counts as one item, found only in its order.

| Reading | How it was made |
| --- | --- |
| reflow today | The transcript a reader gets: the default conversion of the page (`--ocr auto`), read as the page checks read it, with each picture's alt |
| `qwen3-vl:8b` | The whole page through Ollama, with `--assist`'s transcription prompt and JSON answer |
| `openbmb/minicpm-v4.7:35b-a3b` | The same |
| `glm-ocr` | The whole page through Ollama, with the OCR model's own prompt (`ocr:` route) |
| Haiku, direct | The site's `direct-haiku` build (`web/public/docs/<slug>~direct-haiku`), made earlier. Only three public sample forms have one |

The model readings are of the whole page, not of reflow's crops. They show what each model can
read, before reflow chooses what to send. The model readings are not wired into the transcript
yet. The first two follow-ups below do that.

## Results

### Words found and junk left

Each cell is words found / words checked, then the junk strings left.

| Page | reflow today | qwen3-vl:8b | minicpm-v4.7 | glm-ocr | Haiku, direct |
| --- | --- | --- | --- | --- | --- |
| chem-math p1 | 0/7, 1 | 7/7 | 7/7 | 7/7 | — |
| permit p1 | 0/6, 3 | 2/6 | 1/6 | 0/6 (failed) | — |
| legislation p1 | 0/4, 3 | 4/4 | 4/4 | 3/4 | — |
| legislation p5 | 0/1 | 0/1 | 0/1 | 1/1 | — |
| budget p4 | 0/2 | 2/2 | 0/2 | 1/2 | — |
| brochure p1 | 0/6, 5 | 6/6 | 6/6 | 5/6 | — |
| remittance p1 | 0/8, 3 | 8/8 | 7/8 | 8/8 | 8/8 |
| claim booklet p1 | 0/2, 2 | 2/2 | 2/2 | 2/2 | 2/2 |
| claim booklet p4 | 2 junk | — | — | — | — |
| maternal p1 | 0/1 | 0/1 | 1/1 | 1/1 | 0/1 |
| **All** | **0/37, 19 junk** | **31/37, 0** | **28/37, 0** | **28/37, 0** | **10/11, 0** |

On claim booklet p4 the spec checks only junk.

The models' misses:

- **The permit ledger** (all). qwen reads "BUSINESS NAME" and the first permit number, but none of the four
  business names. glm-ocr's reading failed.
- **The struck "[or]"** on legislation p5. qwen and MiniCPM write the bracket without the word.
  glm-ocr writes "[or]".
- **The signatures** on budget p4. qwen reads both signers' names, though the
  prompt says not to guess signatures (the signature follow-up decides when a name is shown). MiniCPM reads neither.
- **The maternal form row.** qwen and Haiku read the row in order but drop its parentheses
  ("□ not known EDD:"), so the strict order check misses them. MiniCPM and glm-ocr read it as
  printed.

### Against the hand-checked truth (`score`)

The same runs, scored on the eight pages of an earlier transcription comparison against a
hand-checked truth (not in the repository). CER is the character error rate on printed lines, and
handwritten cells count the cells of handwriting read. Junk words match nothing on the page.
Wrong facts are numbers, emails and addresses that are not on the page.

| Route | Mean CER | Handwritten cells | Junk words | Wrong facts |
| --- | --- | --- | --- | --- |
| reflow today (`--ocr auto`) | 4.9% | 9/323 | 53 | 35 |
| `qwen3-vl:8b`, whole page | 2.1% | 264/323 | 42 | 46 |
| `minicpm-v4.7:35b-a3b`, whole page | 2.8% | 237/323 | 90 | 114 |
| `glm-ocr`, whole page | 17.9% | 149/323 | 22 | 24 |

Today reflow reads only 9 of the 323 handwritten cells. On 2026-11-01 it read 15. reflow now hides
OCR's junk reading of handwriting behind a crop, and a crop counts as not read.

### Speed and memory

| Model | Seconds a page (whole page) | Video memory (`ollama ps`) |
| --- | --- | --- |
| `qwen3-vl:8b` | 6.6 | 9.6 GB |
| `minicpm-v4.7:35b-a3b` | 5.1 | 19.6 GB |
| `glm-ocr` | 2.9 | 3.7 GB |

The benchmark's own peak above idle is not reliable for this run. A conversion with
`qwen3-vl:8b` ran beside the glm-ocr run.

## What follows

The follow-ups, in the order that should move the most specs from xfail to pass:

1. Read unread crops with `qwen3-vl:8b` through Ollama, checked against the crop. A
   crop that stays says why. This should move chem-math, the brochure's web address, the claim
   cover, and the legislation date.
2. Re-read lines of doubtful OCR print (permit header, legislation p1, remittance,
   brochure, claim callouts, the struck "[or]").
3. Signatures read as "Signature", with the name when it is legible (budget p4,
   legislation p1).
4. No "Handwriting" label without a sign of writing (naca p2).
5. One region per piece of writing (budget p4, chem-math).
6. "¢" bullets and the maternal form's row order.
7. Formulas as math.

## Run it again

```sh
P=.venv/bin/python
W=/tmp/bench-scans
$P scripts/bench_transcription.py run none --work $W
$P scripts/bench_transcription.py run ollama:qwen3-vl:8b --work $W
$P scripts/bench_transcription.py run ocr:glm-ocr --work $W
$P scripts/bench_transcription.py run ollama:openbmb/minicpm-v4.7:35b-a3b --work $W
$P scripts/bench_transcription.py compare --work $W
$P scripts/bench_transcription.py score --work $W
```

Check `nvidia-smi` and `ollama ps` first.
