# Local assist for scans and handwriting

This file records an October 2026 research decision and its first measured run. The decision was to add an opt-in
pass in which a local vision model drafts text for hard scans and handwriting, and alt text for figures that have
none. The pass shipped as `--assist local`, and the run below was measured on 2026-10-19. The description
was later checked against the code, and the paths and facts that had gone stale were fixed.

`--assist local` is separate from the three form questions in `docs/assist.md`. Those questions ask a model through
Ollama or the Anthropic API. `--assist local` runs Qwen3-VL-8B through transformers on this machine.

## What `--assist local` does

`--assist local` runs Qwen3-VL-8B transcription after the OCR stage. It also drafts alt text, but only for figures
flagged `Figure missing alt text`. The default is `--assist none`. `reflow convert` and `reflow convert-many` both
accept the option. The Python equivalent is `convert(pdf, out_dir, assist="local")`. `--alt-text local` still turns
on figure descriptions alone.

This example runs from the root of the checkout:

```sh
PYTHONPATH=src .venv/bin/reflow convert \
	samples/docaccess/01-chem-math.pdf \
	-o /tmp/chem-assist --assist local --jobs 1
```

The local provider needs torch and transformers, and it installs neither. `--device` picks `cuda`, `mps`, or `cpu`,
and the default, `auto`, picks cuda, then mps, else cpu. On the CPU the model takes minutes per figure and needs
about 17 GB of memory, so use a GPU for long documents.

## The pass picks scanned pages, poor OCR text, and unread ink

The pass reads the OCR text layer of the working PDF, including text that the layout engine did not emit. A scanned
page qualifies in two cases. Either its text has a letter share below 0.6, or an OCR region with at least 12
characters contains letters and has a letter share below 0.6.

A node is flagged as handwritten when `node.attrs["handwritten"]` is true, or when a fact on the node has
`data["handwritten"]` set to true. A page fact with `data["handwritten"]` set to true flags the whole page. A
flagged node sends its own crop to the model.

Tesseract sets no handwriting flag. So a second heuristic looks on scanned pages for visible ink outside the OCR text
boxes. It masks each OCR box with two points of padding, skips the outer 12 points of the page, and ignores isolated
speckles and thin rules. This heuristic selects the budget signatures. It can also select drawings or numbers. The
model then reports whether the crop contains handwriting.

Since the pass shipped, a page check in `src/reflow/analyze/page_integrity.py` has added the review item "Scanned
page with handwriting-like ink", with the code `handwriting-like-ink`. The assist pass does not read that item. It
runs its own ink heuristic.

## Every draft is labelled and sent to review

Each draft is a paragraph block with the source page and the bbox of the crop. Its metadata includes
`source="llm"`, `provenance="ai-drafted"`, `reviewed=false`, `handwritten`, and `format="markdown"`. Each block has
an `ai-drafted` fact and a review item labelled `AI-drafted, not reviewed`. The HTML carries `data-provenance`,
`data-reviewed`, `data-handwritten`, and `data-bbox`, plus a visible label. The viewer shows the label because it is
part of the transcript HTML, so the pass needed no change to the web source.

The baseline blocks and figures stay. Drafts follow the baseline on the same page, so some text repeats. The bbox and
the handwriting flag describe the whole source crop, not single words. The Markdown stays escaped text with its line
breaks. Headings, tables, and mathematics get no new semantic HTML from this pass.

## Drafts are cached by image, model, and prompt

The transcription cache is `ai-assist-cache.json`, which staged reconversion carries over. The form questions in
`docs/assist.md` now write to the same file. A transcription's cache key includes the SHA-256 of the image, the model
identity, the prompt text, and the prompt version. Figure drafts use the existing `alt-text-cache.json`.

Successful results are cached, including empty transcriptions. The pass discards invalid replies and replies cut off
at 4,096 tokens, and flags them for review. Failed calls run again on reconversion.

`assist.draft` accepts an injectable `Transcriber`, and the unit tests use a fake one. Local generation shares the
existing model and a process-wide lock, including across `convert_many` runs.

## Measured run on 2026-10-19

The run did one pass per page, with transformers 5.19.0, torch 2.14.1+cu130, bf16, an RTX 3090, and no batching. It
used cached weights in offline Hugging Face mode. Other workers were busy on the machine at the time, so these are
observed wall times, not a speed-up claim. Loading the model took 51.909 seconds, and the page times below leave it
out. The budget and Carmel inputs were single-page subsets of source page 4. Chemistry and the budget already had
text layers, which automatic OCR kept. Carmel's OCR pass produced only a page number. Its PDF page 4 has the printed
page number 5.

- Chemistry p1 took 56.75 seconds in the transcription stage and 56.943 seconds in total. It gave twelve draft
  paragraphs, all flagged handwritten. The draft recovered
  `239.0g CuSO4 x (1 mol CuSO4 / 159.62g CuSO4) = 1.497 mol`, `Tf = -1.39°C`, and `Tb = 103.13°C`. A visual
  comparison found that the top-right `Key` moved to the end and that the printed `1.34m` became `1.34 M`.
  Subscripts and fractions became plain text.
- Budget p4 took 10.19 seconds in the transcription stage and 10.315 seconds in total. It gave five paragraphs, all
  flagged handwritten. The model read the president's name above `President`, and it emitted `ATTEST:`, `[illegible]`,
  and `Secretary` for the other signature. It left out the tiny signing identifier beneath the first signature. The
  visible name agrees with the source image.
- Carmel p4 took 40.20 seconds in the transcription stage and 45.505 seconds in total. The total includes 1.07
  seconds of OCR and 3.93 seconds for the separate attempt at figure alt text. It gave five paragraphs, flagged as
  printed text. It recovered `Message from the General Manager`, `June 27, 2024`, and the prose that begins
  `I believe we embark on the new year with a degree of optimism and an attitude of perseverance.` The text follows
  the source and ends at its last visible word, `live`. The printed page number was left out. The figure alt-text
  attempt failed with `empty alt text for an informative image`. The figure kept its missing-alt review error, and no
  invented alt text took its place.

All three transcription calls succeeded, and each image cache held one successful result. Reconversion recorded
`drafted=0, cached=1, failed=0` for each transcription. The reconversions took 1.165, 0.484, and 6.355 seconds in
total. Carmel's failed alt-text call ran again, so its reconversion still used the model.

The generated budget HTML was opened in a browser. Its accessibility tree had five visible
`Handwritten · AI-drafted, not reviewed` notes and both signature results. The browser console showed no errors.

This command repeats the measurement:

```sh
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONPATH=src \
	.venv/bin/python scripts/measure_ai_assist.py \
	--samples samples/docaccess \
	--output /tmp/reflow-assist-new-run
```

The output directory must hold no earlier assist conversions. The script writes the selected PDF pages, the complete
drafts in `report.json`, the generated HTML, and the caches. The 2026-10-19 run wrote its report to
`/tmp/reflow-ai-assist-gpu/report.json`, which no longer exists.
