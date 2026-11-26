# Opt-in AI assist for forms

`reflow convert` can ask a model three questions about a form. Each question has its own flag, all
three are off by default, and you turn each one on per conversion. By default the model runs on
your own machine through Ollama. The three questions share one provider, one model, one cache
file, and the privacy terms below.

The model sees only the cases that the rules leave in doubt, and the rules decide everything else.
Each answer is a choice from a fixed list. Reflow checks the answer against the page before it
changes anything, records it with `source=assist`, and lists it in the review queue as a warning
to check.

| Flag | Question | Answers | Limit on uncached questions per document (default 25) |
| --- | --- | --- | --- |
| `--assist-labels` | Which printed caption labels this form field? | an offered candidate id, or `none` | `--assist-labels-max-calls` |
| `--assist-structure` | Is this doubtful heading of a form a heading, a label, or a value? | `heading`, `label`, `value`, `unsure` | `--assist-structure-max-calls` |
| `--assist-scan-fields` | Is this place on a scanned form a field, a checkbox, printed text, or nothing? | `field`, `checkbox`, `label`, `none` | `--assist-scan-fields-max-calls` |

`--scan-fields` runs the scanned-form rules alone and calls no model.
[Question 3](#question-3-finds-fields-on-scanned-forms) describes both scanned-form flags.

`--assist local` is a separate flag. It reads the writing OCR could not read with a local vision
model. [Reading unread crops](#reading-unread-crops) below covers it.

`--alt-text` drafts alt text for figures. [Figure alt text](#figure-alt-text) below covers it.

`--engine direct` has a model transcribe every page itself, with none of the rules.
[docs/direct.md](direct.md) covers it.

## Figure alt text

`--alt-text` is off by default (`none`). Then every figure without alt text is listed in
review.json as `alt-missing`, and its `<img alt>` says where the picture is: `Picture on page 4
under “Message from the General Manager”; no description yet`. The heading is the nearest printed
heading before it. Only printed text goes into this fallback.

Turn drafting on per conversion, on `convert` and `convert-many`:

| `--alt-text` | Where the model runs | Default model (`--alt-text-model` to change) |
| --- | --- | --- |
| `ollama` | a local Ollama server (`OLLAMA_HOST`, default `localhost:11434`); nothing leaves the machine | `qwen3-vl:8b` (`ollama pull qwen3-vl:8b`) |
| `anthropic` | the Anthropic API with your own `ANTHROPIC_API_KEY` | `claude-haiku-5-5` |
| `local` | this process, through torch and transformers (docling extra, about 17 GB of VRAM) | `Qwen/Qwen3-VL-8B-Instruct` |

**`anthropic` sends each figure off the machine.** Per uncached figure it sends the figure's crop
(WebP) and its context: the caption, the nearest heading and paragraph, and the words printed
within 40 points of it. Figures can show faces, names and signatures.

Figure description and transcription have separate providers. `--assist local` always reads
through Ollama, whatever `--alt-text` picks.

### What is never sent

A figure is drafted only when it has no alt of its own. These are never sent to a model:

- a figure whose alt came from the PDF's tags, or that the tags mark decorative;
- an unread crop of writing (`unread`), which keeps reflow's own note;
- a figure that needs no description. Before drafting, a ruling or thin band, a blank band, a
  crop that repeats on 3 or more pages (a logo or letterhead) and a tiny figure (under 48 x 48
  points, or under 0.5% of its page) get the role `decorative`, `logo` or `icon`. One with no
  alt of its own is marked decorative with an empty alt. Its `decorative-guessed` warning in
  review.json names the reason, so a person can check it.

Every figure's role (`content`, `decorative`, `logo` or `icon`) is stored as
`attrs.figure_role` in document.json.

### Every draft is labelled and checked

A draft is stored with `provenance="ai-drafted"` and `reviewed=false`. The transcript shows the
badge "AI-drafted, not reviewed" beside it, on screen and in print. The draft is only the
`<img alt>` and the "Image description" details. Find skips the badge and the description
(`data-generated`), and Ask never quotes a drafted alt. Every draft is listed in review.json as a
warning: `suggested-alt`, `suggested-description` for a chart, diagram, map or table, or
`suggested-alt-uncertain`.

A draft is marked uncertain when it names a number, a date or a quotation that the figure's
context does not hold. Numbers are compared without thousands separators, decimal commas or
trailing zeros, and dates as (year, month, day), so `1,497` matches `1497` and `1/2/2025` matches
`January 2, 2025`. The check never edits the text. It cannot check any other claim, such as a
colour or what a photo shows.

Answers are cached in `alt-text-cache.json` in the output directory by crop, context, provider,
model and prompt version. A rebuild sends only new or changed figures. A fully cached run needs no
server and no key.

AI alt text is always off for the fast suite, the page checks and the web tests. The sample builds turn it on only with
`REFLOW_ALT_TEXT` (see `web/README.md`).

### Speed and accuracy

Measured on 85 figures from 8 samples on an RTX 3090, 2026-11-01. Each draft was graded against
the figure. The full report, with candidates that are not defaults, is
[docs/research/2026-11-alt-text-models.md](research/2026-11-alt-text-models.md).

| Route | Seconds a figure, median | Video memory | Right, partly, wrong (of 85) |
| --- | --- | --- | --- |
| `--alt-text ollama` (`qwen3-vl:8b`) | 2.1 s | 11.3 GB | 55, 9, 21 |
| `--alt-text local` (Qwen3-VL-8B) | 4.7 s | 17.5 GB | 50, 15, 20 |
| `--alt-text ollama --alt-text-model gemma4:e4b` | 0.7 s | 5.7 GB | 38, 22, 25 |

About one draft in four states a wrong number, name or label, mostly on charts and tables.
`suggested-alt-uncertain` catches about half of those. So the sample builds keep alt text off by
default: every draft needs a person before it replaces the fallback.

The 94 samples hold 873 figures to draft (1,060 figures, less 114 unread crops and 73 skipped
before drafting). A first full pass takes about 31 minutes through Ollama and about 61 minutes
in-process. Later passes replay `alt-text-cache.json`.

## Set up a provider

`--assist-provider` picks the provider and `--assist-model` picks the model. Both apply to all
three questions.

To run the model on your machine with Ollama, the default provider:

1. Start Ollama with `ollama serve`, or start its system service.
2. Pull the default model with `ollama pull gemma4:e4b`.

To use the Anthropic API instead:

1. Install the package into the venv with `uv sync --extra anthropic`. A sync removes every extra
   it isn't given, so name all the ones you use: `uv sync --extra docling --extra anthropic`.
2. Set `ANTHROPIC_API_KEY`. To keep it out of your shell, put `ANTHROPIC_API_KEY=...` in the
   checkout's gitignored `.env`, and run the command as `env (cat .env) reflow convert ...` in fish
   (`env $(cat .env) ...` in bash).
3. Add `--assist-provider anthropic` to `reflow convert`. The default model is `claude-haiku-5-5`.

If a form holds personal data, read
[what each question sends](#each-question-sends-one-page-crop-and-a-small-json-description) before
you use `anthropic`.

## How each provider sends a question

The Ollama provider is `OllamaChoiceProvider` in `analyze/ask.py`, which sends through `OllamaVision`
in `analyze/vision.py`, and it uses only the Python standard library. For each question it calls
`POST {OLLAMA_HOST or http://localhost:11434}/api/chat` with these settings:

- the crop as base64 in `images`
- the answer schema in `format`
- `think` false, `temperature` 0, and `seed` 0
- `keep_alive` 10 minutes
- a timeout of 180 s per question

The `qwen3-vl` tags ignore `think: false`. For those tags, the request ends with an assistant turn
that closes an empty think block.

Decision models (`clef-flash` and `d1`, listed in `DECISION_MODELS`) use `POST /v1/systemone`
instead. The request puts the same JSON description in its `state`, with the crop and one `choice`
question. The
reply carries a `confidence` from 0 to 1 that says how concentrated the probabilities are. Reflow
stores the confidence in the cache entry and in the fact's `data.confidence`, and applies no
threshold to it.

The `anthropic` provider uses the Anthropic SDK. Reflow imports the package only when a question
has to go to the API.

## A failed question keeps the rules' result

If Ollama is not reachable or the model is not pulled, the pass stops. Its message tells you to run
`ollama serve` and `ollama pull <model>`. With `anthropic`, a missing package or a missing key also
stops the pass.

A timeout, an HTTP error, or an answer outside the enum is a failed question. Reflow counts it in
`failed`, and the rules' result stands. A conversion that only replays cached answers needs neither
provider.

## Each question sends one page crop and a small JSON description

Each question sends a PNG crop of the rendered page at 2 pixels per point. The crop covers the
place in question plus 12 points of margin. With it goes a small JSON description, which each
question's section below lists in detail.

The crop is the rendered page, so the model sees any text, form values, or handwriting inside it.
Reflow never sends the PDF, other pages, the file name, or anything that identifies you or the
document. Forms often hold personal data.

With `ollama`, the request goes to the Ollama server on your machine (`localhost:11434`, or
`OLLAMA_HOST`). Nothing leaves the machine unless `OLLAMA_HOST` points at another machine.
Requests to Ollama never go through a proxy (`HTTP_PROXY` and the like are ignored), and a host
counts as this machine only when every address it resolves to is loopback: `127.a.8.8.8.8.nip.io`
is 8.8.8.8, not this machine (`vision.loopback`). A request to this machine connects only to a
loopback address, resolved again as it connects, so a name that changes in between is refused.
With `anthropic`, the request goes to the Anthropic API under the API terms of the owner of
`ANTHROPIC_API_KEY`. For forms with personal data, leave the flags off or stay on `ollama`.

## Cached answers replay without a model

Reflow caches answers in `ai-assist-cache.json` in the output directory and carries the file over
between conversions. The cache key includes these parts:

- the PDF's sha256
- the page
- the full question, with its texts and boxes
- the model
- the prompt version

So a changed layout, a different model, or a changed prompt asks again, and the answers of
different models never mix. A conversion that replays the cache makes no call and needs no
credentials. Tests never reach a model or the network. They use a scripted provider, a faked HTTP
layer, or the cache.

## Question 1 asks which caption labels a field

`--assist-labels` turns on this question.

### The pass asks only about unnamed or guessed controls

The pass asks about a form control (a field or a group) in three cases:

- The control has a `label-missing` fact.
- The rules named the control from nearby text or a table header (`label` is `printed-nearby` or
  `cell-header`).
- The control is a text box bound to a printed label that is only the tail of a wrapped caption.
  Such a label starts in lower case, like "code (if any)", or closes a bracket it never opened.

In the second and third cases, the control needs at least two candidate captions. The rules' own
name is one of the candidates, so the model can keep it. The pass never asks about a control that
has a tooltip or any other bound printed label.

### The model sees the control and up to 8 nearby captions

Each question sends these items:

- The crop that covers the control and its candidate captions.
- JSON with the control's kind, its PDF name (/T), its tooltip (/TU), and its box, the crop's page
  and box, and up to 8 candidates. Each candidate has an id (`c1`, `c2`, and so on), its printed
  text, and its box.
- A fixed system prompt (`SYSTEM_PROMPT` in `analyze/label_assist.py`) and a schema whose only
  property is `candidate`, an enum of the offered ids plus `none`.

The candidates are the document's text nodes within 100 points of the control, each up to 400
characters long. The model reads at most 200 characters of each candidate. A longer candidate keeps
its first 133 characters and its last 62, so the closing words of a wrapped caption stay visible.
The JSON holds no field values.

### An answer counts only when its caption is within 60 points

The reply must be `{"candidate": "<id>"}`, and the candidate's box must lie within 60 points of the
control. If not, the pass drops the answer and counts it as rejected. `none` changes nothing.

An accepted answer makes these changes:

- It sets the control's accessible name (`attrs.aria`) to the candidate's full text, and it sets
  `attrs.label` to `assist`.
- It replaces the `label-missing` fact with a `label-assisted` fact (`source=assist`), whose data
  holds the model and the chosen node id.
- It puts a warning in the review queue.

The HTML control carries `data-label-source="assist"`, and the `document.json` meta gets a
`label_assist` object of counts.

### Results on 51 hand-labelled fields

`tests/label_assist/labels.json` holds 51 hand-labelled pairs of a field and its caption. They come
from fw9, the I-9, and the DocAccess assessment appeal form. The pass asks about only 12 of the 51
fields, and the rules name the rest.

| Answers | Fields named right |
| --- | --- |
| Rules only | 46/51 (90%) |
| Rules plus `gemma4:e4b` | 47/51 |
| Rules plus the `claude-haiku-5-5` reference answers | 48/51 (94%), at about 4.5 s a question |

The reference answers were recorded once. The
[local model benchmark](#local-models-compared-on-the-same-48-questions) has the other local models.
Answers on borderline controls can vary between runs.

Two fields are out of reach, and the pass never asks about either of them:

- The I-9 "Document Title 1". Its long tooltip belongs to the wrong section but still names the
  field.
- The DocAccess "No" checkbox. Its printed label also holds the text of the next question.

## Question 2 asks if a heading is a heading, a label, or a value

`--assist-structure` turns on this question. Forms are full of lines set large or bold that are not
titles, such as an address, a fee, "Mail:", or a sentence of instructions. The rules catch many of
them (`is_label_value`, `_is_form_label`), and this pass asks about the rest.

### The pass asks only about unconfirmed headings that read like form content

The pass asks about a heading only when all of these conditions are true:

- The heading is h2 to h6, never the h1 title.
- The document has at least one form control.
- The PDF's tags do not confirm the heading. It has no `role-agreement`, `tag-heading`, or
  `bookmark-level` fact.
- The heading reads like form content.

A heading reads like form content when a control lies within 36 points of it. It also reads like
form content when its text does one of these things:

- ends with a colon
- ends with a full stop and has six or more words
- holds a run of underscores, a dollar amount, or a ZIP code
- is 100 characters or longer

### The model sees the line and up to six objects around it

Each question sends these items:

- The crop that covers the line, the lines before and after it within 150 points, and the controls
  within 150 points. The crop holds at most six objects around the line.
- JSON with `line`, `image`, and `around`. `line` has the text up to 160 characters, the font size,
  and the box. `image` has the page, the crop box, and the pixels per point. `around` has up to six
  entries. Each entry has its position (before, after, or control), the role or control kind, the
  text up to 160 characters, and the box.
- A fixed system prompt (`SYSTEM_PROMPT` in `analyze/structure_assist.py`) and a schema whose only
  property is `kind`.

The JSON holds no PDF field name, tooltip, or value.

### A demotion needs a control or a line within 60 points

`heading` and `unsure` change nothing. `label` and `value` apply only when a control or another
line lies within 60 points of the line. They never apply to a heading with sub-headings, a heading
the tags confirm, or the document's last heading.

An accepted answer makes these changes:

- It turns the heading into a paragraph, with `attrs.form_text` set to `label` or `value`.
- It merges the section that the heading opened into the section above.
- It renumbers the heading levels.
- It adds a `structure-assisted` fact (`source=assist`), whose data holds the model and the kind.
  Review shows the fact as a warning.

The `document.json` meta gets a `structure_assist` object of counts.

### Results on 36 hand-labelled lines

`tests/label_assist/structure.json` holds 36 hand-labelled lines from the DocAccess appeal form, the
I-9, and five public sample forms. Those five are not redistributed in this
repository. Each line is a heading that the rules produced and that the trigger asks about. So the set
measures the hard lines, not all headings. A line is right when a `heading` stays a heading and a
`label` or `value` does not.

| Answers | Lines right |
| --- | --- |
| Rules only | 19/36 (53%) |
| Rules plus the `claude-haiku-5-5` reference answers | 33/36 (92%) |
| Rules plus `gemma4:e4b` | 34/36, with no heading wrongly demoted |

The [local model benchmark](#local-models-compared-on-the-same-48-questions) has all the models.

## Question 3 finds fields on scanned forms

A scanned form is an image with an invisible OCR text layer. The PDF names no fields, so a screen
reader user cannot tell where to write. Two flags find the fields:

- `--scan-fields` finds the fields from the page image with rules alone.
- `--assist-scan-fields` implies `--scan-fields`. It also asks the model about the places the rules
  are unsure of, then asks question 1 about the fields it found.

The model can only accept a place that the rules drew. It never draws a place of its own. With both
flags off, the output is unchanged. Reflow refuses `--engine docling` with either flag, because
docling produces no page lines. `hybrid` works, and a page that got fields keeps the heuristic
layout.

### The rules alone are the safer choice

For a scanned form, use `--scan-fields`. A wrong field tells a screen reader user that the form asks
for something it does not. A missed field only leaves the page as it was. The model cuts misses but
adds wrong fields. In the last measurement it found more fields on the synthetic forms (97% against
92%) but added more false ones (8 against 1). On the 1970 census scan it accepted 97 of 104 doubtful
candidates, and nearly all of them were false. So add `--assist-scan-fields` only when missed fields
cost more than wrong ones, and only if you will check the review queue.

Neither flag is on by default, for two reasons. Field names are right for only 82% of the fields
found on the synthetic forms and 32% on the real scans. And reading a page costs up to about a
second, which a long scanned report would pay on every page.

### Only image-only scans with OCR lines are read

- Reflow skips a document with any fillable (AcroForm) field, because its form is the PDF's own.
- A page is eligible when it is an image-only scan with OCR lines. The page image must cover at
  least 90% of the page, or 20% when OCR recognized the page in this run. At least half of the
  page's lines must come from the invisible text layer.
- Reflow never reads a born-digital page.
- A scan without OCR (`--ocr none`, or no OCR engine) has no lines to bind captions to.

### The rules sort candidates into certain and doubtful

On an eligible page, the rules in `analyze/scan_rules.py` look for these marks:

- underlines, which can be solid, dashed, or dotted
- blank ruled cells
- rows of single-character boxes (comb fields)
- small empty squares and circles (checkboxes)

A candidate with no doubt is certain and becomes a field. `--scan-fields` leaves out a candidate
with doubts, and `--assist-scan-fields` asks about it. The doubts are `Doubt` and `DOUBT_TEXT` in
the code:

- no caption beside it
- a table cell
- a line long enough to be a separator
- ink inside it
- overlapping text
- a run of only two boxes
- a ring set like a list bullet
- too few other candidates on the page

A page with fewer than two certain candidates does not count as a form, and the pass asks nothing
about it.

### How the rules read paper scans

**Dashed rules.** The rules join four or more collinear dashes into one rule. Each dash is at least
2.5 pt long, and the dashes are at most 13 pt apart. The space just above and below the dashes must
be clear, which keeps out the bars of "T" and "E" and text baselines.

**Faint dotted rules.** The rules chain specks into a rule the same way. A speck is 14 gray levels or
more under the local mean, and a dotted rule has eight or more evenly pitched specks. A dotted rule
is never a wall of a box. A dotted rule that runs up to a small box or to text, or to within 60 pt
of a page number, is a leader and not a blank.

**Labels left of a box.** A square counts as captioned by words that end before it with leader dots
between them, up to 160 pt away. It also counts as captioned by an OCR line that runs on over it,
or by its row's label. The forms planner (`analyze/forms.py` and the `analyze/form_*.py` modules it uses) gives a scan-detected checkbox with no
label on its right the words on its left. The planner also reads rows that print labels first
("Yes [ ] No [ ]"). It names the later boxes of a row by the first box's label and position. These
planner changes apply only to rows with a scan-detected box.

**Circles.** A circle is a small ring of ink 4.5 to 8.5 pt across. Its middle is clear, though a
tick may be in it. Nearly all of its circumference is ink, and it has room on both sides. A circle
is a checkbox. A ring in a line of words that is set like the line's own type is doubtful as a
likely bullet (`Doubt.BULLET`). Such a ring is under 0.8 of the height of the capitals beside it,
or closer than 0.67 capital heights to the first letter. Very small rings (under 4.9 pt) are not
circles at all. Neither are rings with ink on both sides in running text.

**Worksheet line numbers.** A caption made only of one to three digits and an optional letter ("1a",
"8b") is a line number. The planner skips it and takes the entry text further left, with the lines
of that entry that wrap above it (`LINE_NUMBER`, `_left`, and `_entry` in `analyze/form_labels.py`). This
rule applies to any form, not only to scans.

### Scan fields join the page before layout

Reflow adds the fields after text extraction and before layout. So the forms rules bind captions,
group checkboxes, and build controls as they do for a PDF's own form. Before those rules run,
reflow removes from the page's lines the OCR noise that the fields explain. Examples are the `O` or
`]` that OCR makes of a checkbox, and the leader dots before an underline.

If reading a page fails, reflow skips the page and adds a `scan-fields-skipped` fact, which is a
warning in `review.json`. The conversion goes on.

### The model sees the outlined candidate and the OCR lines near it

Only `--assist-scan-fields` sends anything. For each doubtful candidate that is not in the cache, it
sends these items:

- The crop that covers the candidate and the OCR lines near it, with the candidate outlined in red.
- JSON with `candidate`, `image`, and `nearby`. `candidate` has the shape (underline, cell, comb,
  square, or circle), the box, and the doubts in plain words. `image` has the page, the crop box,
  and the pixels per point. `nearby` has up to 6 OCR lines within 72 points, each up to 160
  characters, with boxes.
- A fixed system prompt (`SYSTEM_PROMPT` in `analyze/scan_fields.py`, `SCAN_PROMPT_VERSION` 3) and
  a schema whose only property is `kind`.

`candidate` does not say the kind that the rules propose, because that made the model agree with the
rules. The caption question that follows sends what question 1 sends.

### Answers must match the candidate's shape

- The pass accepts `field` only for a text shape (underline, cell, or comb), and `checkbox` only for
  a square or a circle. It rejects and counts the opposite answer.
- `label` means printed text, not a blank, and drops the candidate. `none` leaves the candidate out.
- An accepted candidate becomes a field with `attrs.detected = "assist"` and a `field-assisted` fact
  (`source=assist`). The fact's data holds the model, the shape, the doubts, and the confidence if
  the model gave one. Review shows the fact as a warning.
- A certain candidate gets `attrs.detected = "rules"` and no fact of its own. Instead, each page that
  got fields carries one `scan-fields` page fact: "Form fields on this scanned page were found by
  reading its image; check them against the page".
- The `document.json` meta gets a `scan_fields` object of counts. Its keys include `mode`, `model`,
  `pages`, `certain`, `doubtful`, `unasked`, `asked`, `cached`, `accepted`, `dropped`, `rejected`,
  `none`, `failed`, `skipped`, `calls`, and `injected`. The CLI prints one line of these counts.

### Scan fields are HTML inputs that never write to the PDF

The fields are real `<input type="text">` and `<input type="checkbox">` controls. A control gets a
`<label for>` when a printed caption was bound, and an `aria-label` when only the model chose a
caption. Each control carries `data-field-source="scan"`. The form note tells the reader that the
fields were found by reading the scanned page, so some may be missing or in the wrong place.

A scan has no PDF field to write. So the controls carry no `data-pdf-field`, the manifest does not
say `fillable`, and the viewer offers no "Download filled PDF". The browser keeps what you type, and
your text goes into the HTML transcript that you download or print. Each field keeps its page and
box, so a later change can stamp the values onto the scan.

### Five scanned forms make up the evaluation set

`tests/scan_fields/fields.json` holds the hand-labelled fields of five scanned forms in two groups.
Some pages of the real forms are held out.

**Synthetic forms.** `scripts/make_scan.py` rendered three scans from born-digital PDFs, with no
skew, noise, or handwriting. The scans are `samples/scan.pdf` (the W-9, six pages, fields on page
1), `samples/scan-i9.pdf`, and `samples/scan-w4.pdf`. The labels were seeded from the source widgets
and checked by eye. The eval scores 201 fields, 180 text fields and 21 checkboxes.

**Real scans.** These are two paper scans of US Census Bureau questionnaires. `samples/README.md`
lists their sources and hashes.

- `samples/scan-census-1970.pdf`, pages 1 and 2. These are 400 dpi CCITT images with no text layer,
  dashed write-in rules, and fill-in circles.
- `samples/scan-census-1960.pdf`, pages 2 and 4. These are 600 dpi JPEG images on grey paper with a
  poor OCR layer, square checkboxes, and dotted write-in lines.

Skew is under 0.1 degree. One person measured the write-in blanks and squares by eye on the page
images, 78 on the 1970 form and 135 on the 1960 form. The 1970 form's circles were seeded from the
rules' own detections, then swept by eye. On page 1 the seeds were folded into one template per
person row. Page 2 was seeded after the circle rules were frozen. The 24 circles of the office-use
strip are optional. So a missed circle is a real miss, but circle precision is not an independent
measure.

**Hold-out pages.** Page 1 of `scan-census-1960.pdf` (70 fields) and page 2 of
`scan-census-1970.pdf` (14 blanks and 93 circles) were set aside before the rules were tuned on
these forms. The eval reports them apart (`HOLD`) from the tuned pages (`TUNE`). They are other
pages of the same two forms, not other forms.

The eval matches each detected field to a label of the same kind whose centre lies inside the
field's box, grown by 3 points.

### Rules-only results, measured 2026-10-23

Each cell gives the fields found, then the false fields in brackets. The header gives the number of
labelled fields.

| W-9 (23) | I-9 (130) | W-4 (48) | 1970 census (568) | 1960 census (135) | TUNE (529) | HOLD (174) |
| --- | --- | --- | --- | --- | --- | --- |
| 23 (0) | 115 (0) | 46 (1) | 494 (30) | 111 (2) | 454 (23) | 151 (9) |

On the synthetic forms, precision is 184/185 (99%) and recall is 184/201 (92%). The rules find
167/180 text fields and 17/21 checkboxes. Captions are right for 151/184 fields (82%).

On the real scans, the rules find 605 of 703 fields, with 32 false. They find 49/94 text fields
(1970: 34/75, 1960: 15/19), 460/493 circles, and 96/116 squares, with no false checkbox. Captions
are right for 169/494 fields on the 1970 form and 22/111 on the 1960 form, 32% together. The OCR
layers are bad ("Stoles", "K1nder9orten"), and a caption counts only if it contains the labelled
text.

On scanned prose, the 12 pages of `samples/naca-ocr.pdf` (a NACA report) give 0 certain circles.
They give 137 other certain candidates, and none of them is a real field. These false candidates are
why `--scan-fields` stays opt-in.

Reading a page takes about 0.8 s for a 2478 by 1696 pixel page and 0.4 s for a letter page.

### Model results from 2026-10-22 predate the current rules

The assist was last measured on 2026-10-22 with `gemma4:e4b`. The rules at that time had no dashed
rules, dotted rules, left labels, or circles. The cached answers are keyed to the candidates that
those rules drew, so they no longer replay.

At that time, on the synthetic forms, the model raised recall from 91% to 97% (194/201), for 8 false
fields instead of 1. Of the 25 candidates it was asked about, it accepted 11 of the 14 real fields
and 8 of the 11 others. So it tells real fields from false ones only weakly, and `gemma4:12b` did no
better. On the 1970 form, the rules then proposed none of the dashed blanks. There the model
accepted 97 of 104 doubtful candidates and added 123 false fields. Kind questions took a median of
0.3 s, and caption questions took 0.4 s.

An earlier prompt told the model the kind that the rules proposed, and the model answered "field"
for 37 of 38 questions. The current prompt leaves the kind out.

### Known limits

These limits are accepted and left unfixed.

- **Large-letter counters read as squares.** In large type, the counters of "o", "e", and "a" read
  as small closed boxes. This happens in DejaVu from 14 pt, Liberation Sans from 16 pt, and
  Liberation Sans Bold from 12 pt. One rendered 18 pt page had 66 of them. Neither nearby ink nor
  corner ink separates a counter from a printed checkbox. The 1960 census squares have letters 1.4
  pt away, and corner ink on real squares overlaps the counters' range. A page counts as a form only
  when it has two or more certain candidates. A prose page with these counters has dozens, so that
  limit does not filter it out.
- **Dense-row circles.** The rules read a circle as a letter when ink lies within 2 pt of it, which
  means its label touches it. They also read it as a letter when ink lies within one ring diameter
  on both sides in a line of words. On synthetic rows with labels and options 3 to 5 pt apart, the
  rules find 1 of 3 circles, and none of them is certain. With a label within 2 pt, they find none.
  On the real forms, this costs 3 of the 1970 form's circles. If either rule were looser, round
  letters in prose would come back as circles.
- **Lone-glyph cases.** A lone "o", "O", or "0" stays a certain circle when it is alone on a line.
  It also stays a certain circle at a line's end after words with no letter taller than the
  x-height ("Marque si o" in DejaVu at 14 pt). The rules judge a bullet in the middle of a row of
  options ("○ one   ○ two") by its size only, not by its gap. A marker font whose ring is as tall as
  the capitals, set further than a word space from the text, makes the same ink as a drawn circle.
  The bullet thresholds were set on one printer's circles (the 1970 census), with a margin of about
  0.07 on each side. So a form that prints small circles beside much larger labels loses them to the
  doubtful list.

### Other open limits

- **Contents pages.** Under a heading "Contents" or "Table of contents", a dotted rule that runs
  from words to unread ink is a leader. So a form with that heading can lose dotted fields when OCR
  misses their printed units ("cm", "mm", "No. 1"). A contents page with no recognized heading and
  no readable page numbers can still produce false fields.
- **One rule, two captions.** The 1970 name line is one dashed rule under "First name" and "Middle
  initial". The 1960 name line is one dotted rule under three captions. One label matches, and the
  others are misses, 8 on 1970 page 1 and 1 on 1960.
- **Dashed boxes.** The 1970 "Print exact relationship" and "Print tribe" blanks are boxes of
  dashed rules, with 16 misses on page 1. The rules build boxes from solid rules only. A trial that
  let dashed walls close boxes found none of these boxes and lost two correct underlines.
- **Blanks beside circles.** The 1970 "Age" blanks (8) and some "Month" and "Year" blanks (5) carry
  ink or OCR text from the circles beside them, so they stay doubtful.
- **Faint or merged marks.** The rules miss 33 of the 1970 form's 493 circles and 20 of the 1960
  form's 116 squares. These marks are too faint or merged with a neighbour, and 3 of the circles
  read as a lone glyph. Looser thresholds put false fields back on the W-9.
- **Synthetic-form misses.** The rules never propose the four I-9 "alternative procedure"
  checkboxes. They also miss the stacked List A to C cells with no caption of their own, and the
  W-4 exemption checkbox. They miss 14 W-4 captions, either because there is no line number to skip
  or because OCR fused the leader dots and the number.
- **A small label set.** The real-scan labels are one person's measurement of two forms in one
  pass. They show the failure modes, not a rate to expect on other forms.

## Page vision proposes heading changes for review

`--assist-page-vision` (off by default) shows a local vision model each page with
reflow's blocks outlined and numbered, and asks which kind each block is. It runs only through
Ollama on this machine (`--assist-page-vision-model`, default `qwen3-vl:8b`), and asks at most
`--assist-page-vision-max-pages` uncached pages per document (default 100). The page-vision study
(`docs/research/vision-page-understanding.md`) measured what the model gets right and wrong.

The model only proposes. Nothing it says changes the output until a reviewer accepts it.

- **The answer is closed.** It is `{"blocks": [{"id": <an offered block id>, "role": <one of 12
  fixed words>}], "complexity": {"dense_table", "diagram", "poor_scan", "handwriting": true or
  false}}`, parsed once. Any other key, type or string, such as a word outside the list, an id
  that was not offered, a repeated id, a missing flag or a field of text, rejects the whole
  answer, and that page makes no proposal and no flag. The model never writes text.
- **A disagreement becomes a proposal.** A heading (h2 to h6) that the model calls something
  else becomes a proposal to demote it to a paragraph. A paragraph it calls a heading becomes a
  proposal to promote it, to the level of the heading before it. The title is never proposed.
  The proposals go to `page-vision-proposals.json` beside the conversion.
- **A promotion the page's own evidence contradicts is not proposed.** A block
  that starts in lower case or ends in a comma or semicolon is a piece of a sentence, and a form
  code beside a revision mark (`BOE-305-AH (P1) REV. 12 (05-24)`) is a form's identifier. The
  model calls both headings with confidence. Such a promotion is listed under `gated` in
  `page-vision-proposals.json` and is never shown for review. Only the checks that reject none
  of the headings the page specs require are on. `docs/research/vision-gates.md` has the counts,
  and the checks that are off.
- **A complex page is one review item.** A page the model calls complex gets one
  `vision-complexity` warning, which joins the page's other page facts in one review item.
  Handwriting and poor scan count only on a scanned or mixed page: the page-vision study saw small print and small
  images on born-digital pages called handwriting. Refused flags are counted in the summary and
  in `page-vision-proposals.json`. When dense tables are found on more than a third of the
  document's pages (the audit: 42 of 93), they become one `vision-dense-tables` item for the
  document, naming the pages, instead of one per page.
- **A block the model reads as another kind is for the inspector only.** It gets a
  `vision-block` fact with no severity and no weight. The inspector shows it, the review queue
  doesn't, and its confidence doesn't move.
- **Until a proposal is accepted, the pass changes no role, text or order.** With the
  `vision-*` facts taken out, `document.json`, the transcript and the inspector's layout are
  byte for byte those of a run without the flag. The only other changes are the review items
  above, and the version stamp, which is a digest of the output.
- **A reviewer decides.** `reflow review OUT` lists the proposals. `reflow review OUT --accept
  ID` or `--reject ID` records a decision in `proposal-decisions.json`, which is carried from
  one conversion to the next.
- **An accepted change waits for the text check.** The next `reflow convert --assist-page-vision
  -o OUT` replays the answers from the cache, with no model call. Before it applies an accepted
  proposal, it checks that the document's text is the text the reviewer accepted it against,
  and that the block is still a heading to demote or a paragraph to promote. If either has
  changed, for example because OCR read the page differently, the proposal is `stale` and
  nothing changes. The applied change carries a `vision-accepted` fact.

There is no mode that changes structure first and warns after. A rejected or unreviewed
proposal leaves the output as it would be without the pass.

### Unread crops from page vision (off by default)

`--assist-page-vision-unread` adds one more kind of proposal. Where the model
calls an OCR paragraph `handwriting`, `noise` or `figure_label`, and a rule already doubts it,
the pass proposes hiding that paragraph in an unread crop. Three rules count as doubt:
OCR's grade (`text_quality.node_gate`), no word of two letters or more printed anywhere else
in the document, or a box within 12 pt of a figure. reflow has no dictionary, so the document
stands in for one.

- **Nothing changes until a reviewer accepts.** `reflow review` lists the crop with the rule
  that doubts it. Unreviewed or rejected, the output is byte for byte a run without the pass.
- **An accepted crop takes out only its own paragraph's text.** The paragraph becomes an
  unread figure with reflow's note, a crop of the page, and the OCR reading in `ocr_guess`.
  `proposals.apply` checks that no other text changed.
- **The text leaves the transcript, Find, Ask and the review queue.** Find skips the note,
  `passages.jsonl` holds no passage for the crop, and the crop is an `unread-*` review item.
  The inspector (`layout/`, `trace/`) and `document.json` keep the OCR reading. The PDF and
  its text layer are never touched.

It is off because it hides real text too often. On 2026-11-02 it ran with `qwen3-vl:8b` on the
11 DocAccess documents and the 28 committed samples. All 31 published ones were current
(`docs:check`). 21 of the documents hold OCR paragraphs, 3,040 in all, and the model was asked
about every page that holds one. It called 197 junk, and 178 of those were eligible.
I checked each eligible paragraph by eye against its page:

| Verdict | Count | What it is |
| --- | ---: | --- |
| Wrong: real text read correctly | 36 | 18 NACA chart ticks and labels ("80", "o Experimental points"), 5 typed lines on the budget ("$-0-", "; and", the DocuSign ID), 4 handwritten ledger entries OCR read right ("SHELL", "5699 S"), 3 typed legislation phrases, the W-9's "W-9" and "What's New" |
| Printed text OCR garbled | 61 | mostly the legislation's line numbers and NACA's symbols and axis ticks. A crop would show them, but labelled handwriting or noise |
| Right | 81 | rules, specks, halftone, chart curves, and misread handwriting |

The second witness barely filters: 178 of the 197 junk calls pass it. Its rules were built to
keep OCR text out of navigation, not to judge whether text is real. "Low letter share" fires on
every number, and "starts in lowercase" fires on every fragment, so a correctly read chart
tick passes it. Those two account for 22 of the 36 wrong hides. Before this is turned on, the
witness has to be a rule that tells junk from short real text. No such rule exists in reflow
today.

`scripts/measure_vision_unread.py` reproduces the count. The model failed on no page and
rejected no answer. 3 pages with more than 90 blocks (NACA 1, EPA 2) were not asked, so the
count is a floor for those documents. Two runs over the budget's 11 asked pages gave the same 5
eligible paragraphs.

## Reading unread crops

Writing that OCR could not read is a crop of the page (an unread figure). A default
conversion asks no model, and each crop says so: "Not transcribed: no model was asked to read
it. Check the original page." `--assist local` reads each crop with a local vision
model through Ollama (`qwen3-vl:8b`, `providers.TRANSCRIPTION_MODEL`), under the GPU lease
(`src/reflow/analyze/assist.py`).

- **A page with crops sends its crops, not the page.** Each crop is read alone, so its reading
  can go under it. A page without crops is sent whole as before, when OCR read little of it.
  A signature is asked only for its signer's name (below).
- **Each reading is checked against its crop before it is shown.** It is rejected when it reads
  nothing but `[illegible]`, when a run of up to 8 words comes more than 3 times back to back
  (a repetition loop), when it reached the token limit, or when it has more letters than the
  crop's ink can hold: one letter for every 8 dark pixels of the crop at 144 dpi, plus 10.
  Readings that passed by eye on the transcription benchmark's crops used 20 (a 7 pt footer) to 475 dark pixels a
  letter, so only a gross invention is caught.
- **One reread.** A rejected crop is read once more, told which check failed. If that reading
  fails too, the crop stays as it was.
- **A reading that passes goes under its crop**, labelled "AI-drafted, not reviewed" and listed
  in review.json. The crop stays, so a reader can check the reading against it, and its alt
  says what it is ("Handwriting.") instead of "not transcribed".
- **"Not transcribed" is the last resort, and says why:** no model was asked to read it, the
  model was unavailable, or no model reading passed the checks. An unavailable Ollama is asked
  once; every crop after it says so, and the conversion goes on.
- **Cache.** Each reading is cached in the carried `ai-assist-cache.json` by the crop's PNG, the
  model, the prompt and, for a reread, what it was told. A rerun replays it, reaches the same
  verdict and makes no call.

What leaves the machine: nothing, unless `OLLAMA_HOST` points elsewhere. Per crop the local
server gets one PNG of the crop at 144 dpi and the fixed transcription prompt. A signature crop
never leaves it: with `OLLAMA_HOST` on another machine, no signature is sent, and the request
that reads one refuses any server whose addresses are not all loopback (`local_only`).

### Signatures

A signature is not text to transcribe, so it reads as "Signature." (or "Signature above
“President”.", when OCR read every word of the line it signs), with no "Not transcribed" note,
in every conversion. Under `--assist local`, each signature crop goes to the model on this
machine alone, asked for the name it spells or `[illegible]` (`assist.SIGNATURE_PROMPT`, a
plain-text answer). A name is never guessed: it is drafted under the crop, like any reading, only
when the printed line the signature signs has it in a name field (`assist.signer`):

- the line has a name field ("Name:", "Print Name", "Name of Applicant:", "By:";
  `assist.NAME_FIELD`), and the name is the field's own value: the capitalized words right
  after it, ended by any other mark ("/", ","), a word in lower case, or a role word. A blank
  field ("Name: ____ Title: Managing Attorney") names no one. Printed words with no field
  could as well be a role ("Managing Attorney", "Human Resources"), so they name no one;
- the reading is two to four words of letters, never `[illegible]`, OCR-like fragments or a
  non-answer ("Unknown", "No signature", "Not legible", "Cannot read" and the like);
- it is that printed name word for word, in order: each word like its printed word (`difflib`
  ratio 0.8), spelled as printed, or an initial, its first letter. One word at least is more
  than an initial, so a first name alone ("John" for "John Smith") is no name, and a name is
  never pieced from two ("John Doe" from "John Smith/Jane Doe");
- no word is a role or a form's own word ("President", "Secretary", "Date", "Signature",
  "Applicant", "Witness", "Title", "Owner", "Chief Executive" and the like:
  `assist.NOT_NAMES`).

Anything else keeps "Signature" alone, and the crop's `why` records the rejected reading. The
model's certainty is not used. On real crops shifted slightly (an 8 pt pad), qwen3-vl:8b gave
another signer's surname at 0.907 under one signature (that signer's hand-lettered label was
inside the crop) and a misspelt surname at 0.82 under another, while right names scored 0.863,
0.949 and 0.998: no cutoff tells a sure name from a guess, and a wrong name under a person's signature
is worse than "Signature". Neither signature page measured here prints a name (budget p4 prints "President" and
"Secretary"; legislation p1's names are handwritten), so today no name is drafted on either. The
reading is cached by the crop's PNG, the model and the prompt.

### Formulas

The prompt asks for each formula or line of calculation as LaTeX between dollar signs, and the
renderer (`render/mathml.py`) shows it as MathML, in the same AI-drafted block and under the
same crop. The model's text is untrusted: it is parsed with a small grammar (fractions, roots,
sub- and superscripts, Greek letters, degrees, arrows, upright text) and the markup is written
from the parsed tree through a fixed list of MathML elements. The web viewer's chunk sanitizer
keeps those elements and no other MathML, and a test holds the two lists equal.

- **A formula the grammar does not read stays as the escaped text the model wrote**, dollar signs
  included. So does a reading with no formula that reads, and a price ("$5 to $10") is never
  one.
- **The reading stays checkable.** `node.text` (document.json, review.json) holds the LaTeX the
  model wrote; the page shows it as math under the crop it was read from, with the same
  "AI-drafted, not reviewed" badge. A formula's text content is its plain text ("ΔTf=(2)(1.86°C/m)"),
  so find and a reader without MathML still get it.
- **A line that is LaTeX from end to end with no dollar signs** is one formula too. Math written
  as plain text ("T_b = 102.06°C") is left as the model wrote it.
- **The ink check does not count a command's letters** (`\mathrm` is not on the crop).

`scripts/bench_transcription.py` measures it on its benchmark pages: `run none` and
`run ollama:qwen3-vl:8b`, then `compare` grades each transcript with the pages' spec words.

## Form repair reads what OCR left unread, for review

`--assist-form-repair` (off by default) finds the places on an OCR'd page where
OCR left printed text unread, and asks a local vision model to read only that region. On a scan
of the ADA dental claim form, Tesseract reads fields 1 to 23 and the services grid but none of
the ten labels in the grid's header (24 to 31, with 29a and 29b). The pass finds that header
from the page and `qwen3-vl:8b` reads all ten labels from a crop of it. It runs only through
Ollama on this machine (`--assist-form-repair-model`, default `qwen3-vl:8b`), and makes at most
`--assist-form-repair-max-calls` uncached calls per document (default 24, rereads included).

- **Only OCR text is repaired.** A page is looked at only when OCR read it in this conversion,
  part of its image went unread, or it holds a kept OCR layer. Born-digital text is the PDF's
  own: such a page is never rendered or sent, so a born-digital document makes no call.
- **A region comes from the page's evidence** (`analyze/form_regions.py`). Unread ink is a
  glyph-sized piece of ink that no text in the PDF's text layer covers and no picture holds.
  Unread glyphs that line up are a gap. A gap in a ruled cell becomes the whole ruled row, rule
  to rule and as far as both rules run, so no cell or label is cut. Any other gap is lines of
  text, padded by half a line. Each region records why it was chosen, the text already read in
  it, the cells and the cells with ink, the field numbers missing from the page's run (23, then
  32: 24 to 31 are missing), and the checkbox squares, answer circles and comb boxes the
  scan-field rules found in it. A region taller than a third of the page is a whole form and is
  not read. At most six regions a page and 24 a document.
- **The answer is typed.** A ruled row is asked for `{"fields": [{"id", "label", "widget"}]}`:
  the printed field number (`29a`, or empty), the printed words, and one of `none`, `text`,
  `code_boxes`, `checkbox`, `radio`, `signature` or `unknown`. There is no slot for a value, so
  a blank is never filled in. Lines of text are asked for `{"lines": [...]}`. Any other key or
  type rejects the answer.
- **The answer is checked against the page** (`analyze/form_repair.py`). It is rejected when a
  field number repeats, runs out of order, or can't be there (neither read in the region nor
  missing from the page's run, nor a sub-field of one of those), when a number the text layer
  read in the region is dropped, when it has fewer fields than the row has cells with ink (the
  dental header's ten inked cells are its ten labels), when it has far fewer letters than the
  unread ink or far more than the page holds there, or when it repeats itself. A checkbox or
  radio where the rules found no square or circle becomes `unknown`, and is called code boxes
  read as checkboxes where they found comb boxes.
- **One targeted reread.** A rejected region is read once more, told which check failed. A
  timeout or the token limit counts as an attempt. If the second reading fails too, the region
  is unresolved. An unavailable Ollama stops the pass and leaves every region unread; the
  conversion goes on.
- **Nothing changes the transcript.** Each page with regions read gets one review item that
  lists every region in page order, with its reading or the checks every reading failed, and
  points at every region's box. It is `form-repair-proposal` when a reading passed, else
  `form-repair-unresolved`. The document's text and structure are those of a conversion
  without the flag, byte for byte. Every region, its evidence, each attempt with its problems
  and seconds, and what was skipped go to `form-repair-proposals.json` beside the conversion.
- **Cache.** Each attempt's raw reply is cached by the PDF's hash, the page, the region's box,
  task and evidence, the model and the prompt version in the carried `ai-assist-cache.json`. A
  rerun replays it and makes no call.

`scripts/eval_form_repair.py` compares region reads with whole-page reads on the same model;
the numbers are in `docs/research/2026-11-form-repair.md`.

What leaves the machine: nothing, unless `OLLAMA_HOST` points elsewhere. Per region the local
server gets one PNG crop of the region at 144 dpi and the task's fixed prompt; a reread also
gets the names of the checks the last reading failed.

## Local models compared on the same 48 questions

`scripts/bench_assist_models.py` asks every question of the label and structure evals directly
through `OllamaChoiceProvider.choose`. It uses the crops and candidates that the converter produces.
That makes 12 label questions and 36 structure questions, 48 in all, so the timings hold no
conversion time and no cache hit.

The raw data is in two files. `docs/assist-bench-runs.csv` has one row per model, with power and
VRAM. `docs/assist-bench-questions.csv` has one row per request, with the answer, confidence,
latency, tokens, Ollama's timing fields, and error.

### Test conditions

The benchmark ran on 2026-10-21 on one RTX 3090 (24 GB) with Ollama 0.40.7. The desktop shared the
GPU, and other applications used about 2.3 GB of VRAM and 5 to 35% utilisation. Every model got the
same prompt and crop, and ran with these settings:

- temperature 0 and seed 0
- `think` false
- `keep_alive` 30 minutes
- its default tag
- Ollama's default context, 32768 tokens, or 16384 tokens for the decision models

Each model got one interleaved pass. The pass unloaded the model, asked one warm-up question and
discarded it, then asked the 48 questions. Accuracy is the rules plus that model's answers, replayed
through the evals.

### Results

| Model | Download size, quantization | Labels (of 51) | Structure (of 36) | Headings wrongly demoted | Latency, median and p95 | Cold first request | Peak VRAM above idle | Failed | Rejected |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `gemma4:e4b` (default) | 6.6 GB, Q4_K_M | 47 | 34 | 0 | 0.32, 0.44 s | 6.3 s | 5.7 GB | 0 | 2 |
| `ornith-1.5:9b` | 6.6 GB, Q4_K_M | 47 | 33 | 2 | 0.52, 0.68 s | 5.1 s | 7.7 GB | 0 | 2 |
| `qwen3-vl:8b` | 6.2 GB, Q4_K_M | 47 | 32 | 1 | 0.92, 0.98 s | 4.8 s | 11.5 GB | 0 | 3 |
| `gemma4:12b` | 8.0 GB, Q4_K_M | 46 | 33 | 1 | 0.76, 0.93 s | 5.6 s | 9.9 GB | 0 | 1 |
| `clef-flash:9b` | 10 GB, Q8_0 | 46 | 29 | 4 | 0.36, 0.52 s | 7.5 s | 13.9 GB | 0 | 3 |
| `d1:3b` | 3.7 GB, Q8_0 | 46 | 25 | 8 | 0.15, 0.32 s | 3.4 s | 4.4 GB | 5 | 2 |
| `bonsai-2:9b` | 3.2 GB, Q2_0 | 45 | 30 | 2 | 0.50, 0.62 s | 3.5 s | 4.9 GB | 0 | 1 |
| rules only | | 46 | 19 | | | | | | |
| `claude-haiku-5-5` (reference) | API | 48 | 33 | 1 | about 4.5 s | | | 0 | 1 |

- The cold first request is the first request after `ollama stop`, with the model files mostly in
  the page cache. A first load from disk is slower. `gemma4:e4b` took 89 s right after it was
  pulled.
- Only `d1:3b` failed. It returned HTTP 500 `failed to process mtmd chunk` for 5 of 48 requests, on
  the wider structure crops. Nothing timed out, and no reply was malformed. A rejected answer is one
  that the geometry checks or the tree checks dropped.
- Prompt evaluation takes most of the time. The answer is 8 to 13 tokens. For `gemma4:e4b`, a
  0.33 s request is 0.21 s of prompt evaluation (942 tokens) and 0.09 s of generation. The image for
  `qwen3-vl` takes about 1,800 tokens.
- The VRAM figures include the key-value cache of the default context. A smaller `num_ctx` would
  lower them, but this was not measured.
- Energy is not the cost. `gemma4:e4b` uses about 0.03 Wh per question on the GPU board, well under
  a cent per 1,000 questions at $0.15 per kWh. The reference model costs about $0.11 to $0.26 per
  1,000 questions at Haiku's API prices.
- Ten questions, asked again right after each pass, got the same answer from all seven models.
- Decision-model confidence carries some signal but is a weak filter. If `clef-flash` dropped its
  answers below 0.7, the answers it keeps would be right 88% of the time instead of 74%, at the
  cost of 13 correct answers.
  Even with a threshold, neither decision model beats `gemma4:e4b`.

### Why `gemma4:e4b` is the default

`gemma4:e4b` has the best combined total (47 + 34) and no wrongly demoted heading. It is the fastest
chat model at 0.32 s, and it needs 5.7 GB of VRAM. Its lead over `ornith-1.5:9b` and `gemma4:12b` is
one answer, so treat the three as equals and choose by speed and memory.

With under about 6 GB of VRAM free, `bonsai-2:9b` is usable for `--assist-structure` only. It lifts
structure from 19 to 30, but it costs one label. Don't use `d1:3b`. It fails on 10% of the crops,
gains nothing on labels, and demotes eight real headings.

### One pass cannot separate close results

The benchmark is one pass of 48 questions on a shared GPU. So it does not establish differences
under about 20% in latency, or differences of one or two answers in accuracy. The hand-labelled sets
are small (51 and 36), and the structure set holds only the rules' own hard cases. The energy figure
is GPU board power only. The `d1:3b` failures look like an image-handling limit of its runner in
Ollama 0.40.7.

## Run the evaluations and the benchmark

The three evals replay committed caches and call no model, unless you add `--record`.

Re-recording the `claude-haiku-5-5` reference answers calls the Anthropic API with your own key.

### Score the label question

```sh
.venv/bin/python scripts/eval_label_assist.py
```

The script reports the rules-only accuracy. Then it replays `tests/label_assist/answers-cache.json`
and reports the accuracy of the rules plus the model.

- To replay a different model's cached answers, add `--model <name>`.
- To make real calls through `--provider` and merge the answers into the cache, add `--record`.

### Score the structure question

```sh
.venv/bin/python scripts/eval_structure_assist.py
```

The script scores the rules only, then the rules plus `tests/label_assist/structure-cache.json`. It
takes the same `--model`, `--provider`, and `--record` options as the label eval.

### Score the scanned-form fields

```sh
.venv/bin/python scripts/eval_scan_fields.py
```

The script converts each form with `--scan-fields`. Then it converts each form with
`--assist-scan-fields` and replays `tests/scan_fields/answers-cache.json`.

- To score the rules alone and skip the replay, add `--rules-only`.
- To list the misses and false fields on the held-out pages, add `--show-holdout`. Don't add it
  while you tune the rules.
- To make real calls, add `--record`. The script appends one row per call to
  `docs/assist-scan-fields-calls.csv`, and the eval's latency report reads that file.

### Run the model benchmark

The benchmark needs a running `ollama serve`.

1. Convert the eval forms once and keep the questions:
   `.venv/bin/python scripts/bench_assist_models.py extract`
2. Ask every model all 48 questions: `.venv/bin/python scripts/bench_assist_models.py run`
3. Write the report and the CSV files: `.venv/bin/python scripts/bench_assist_models.py report`

`report` writes `docs/assist-bench-runs.csv` and `docs/assist-bench-questions.csv`. To write them
to a different place, add `--csv-prefix <path>`.

### Run the alt-text benchmark

`scripts/bench_alt_text.py` converts the benchmark's samples once per alt-text route and times each
figure. Run one route per command, for example
`.venv/bin/python scripts/bench_alt_text.py run ollama:qwen3-vl:8b` or `... run local`. Then
`... report` writes the per-figure CSV and a blind grading sheet. The method is in
[docs/research/2026-11-alt-text-models.md](research/2026-11-alt-text-models.md#method).
