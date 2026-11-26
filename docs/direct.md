# Prompt-only transcription (`--engine direct`)

Every other pipeline runs reflow's heuristics, and a model, when there is one, answers only
narrow questions (a label, a heading, an alt text). `--engine direct` replaces analysis and
layout with one model request per page: the model reads the page and writes its transcript
itself. It exists to compare models with reflow's own pipelines, on the site and in a scored
eval. It is opt-in; the default pipeline and its output are unchanged.

```sh
env $(cat .env) reflow convert form.pdf -o out --engine direct                # Claude Haiku
env $(cat .env) reflow convert form.pdf -o out --engine direct --direct-model claude-opus-5-5
reflow convert form.pdf -o out --engine direct --direct-model glm-ocr         # local, Ollama
```

## What the model gets and writes

| `--direct-model` | Where it runs | It gets | It writes |
| --- | --- | --- | --- |
| `claude-haiku-5-5` (default), `claude-sonnet-5-5`, `claude-opus-5-5` | the Anthropic API, with your `ANTHROPIC_API_KEY` | the page picture (PNG, 1568 px on its longer side) and the PDF's own text layer | semantic HTML |
| `glm-ocr` | the local Ollama server, under the GPU lease | the page picture alone, with its `Text Recognition:` prompt | markdown, with HTML tables |
| any other Ollama model | the local Ollama server, under the GPU lease | the picture and the text layer | semantic HTML |

The text layer lets the model fix reading order and structure without misreading characters. It
is read from the source PDF, so a page with no text of its own (a scan) sends the picture alone.
The direct engine runs no OCR: the model reads the picture.

The prompt (`SYSTEM_PROMPT` in `src/reflow/analyze/direct.py`) asks for an HTML fragment of a
fixed element set: headings, paragraphs, lists, tables with captions, header cells and spans,
figures with alt text and a box, footnotes in `aside`, running headers and footers in `header`
and `footer`, form fields as `label` with `input`, `select` or `fieldset`, and `strong`, `em`,
`sup` and `a`.

### Prompt variants

`--direct-prompt` picks one of four variants of the prompt (`PROMPTS` in `direct.py`). Each
is the base rules with one addition:

| `--direct-prompt` | adds | its id |
| --- | --- | --- |
| `base` (the default) | nothing: the prompt first measured | `html-1` |
| `complete` | transcribe every entry (every row, item and index entry), never abbreviate, sample or summarize | `html-complete-1` |
| `forms` | stricter form rules: one control per place to fill in, a label holds only its caption, a fieldset for every question with choices and every titled group of fields | `html-forms-1` |
| `example` | a short example of the HTML wanted, with a heading, a list, a table and a fieldset | `html-example-1` |

The id is in the answer cache's key and in `meta.direct.prompt`, and `direct_prompt` is in the
provenance record's options. The base keeps the first prompt's id, so the answers cached then are still used.
On the site, `<slug>~direct-haiku-complete`, `~direct-haiku-forms` and `~direct-haiku-example` are
Haiku with each variant. glm-ocr has its own task prompt and ignores the variant.

**A Claude model sends each uncached page off the machine**: its picture and its text layer.
`glm-ocr` and other Ollama models send nothing unless `OLLAMA_HOST` points elsewhere.

## The model's markup is never published

`src/reflow/analyze/direct_html.py` parses the answer and builds reflow nodes from the allowed
elements only, so the result is a normal reflow document: the transcript, the variant tab, Compare,
the inspector and the page checks all read it. Markdown goes through markdown-it first.

- An element outside the set is opened and its text kept (`div`, `span`, `thead` silently; an
  unknown one such as `blink` is counted). `script`, `style`, `svg`, `iframe` and the like are
  dropped with their content. A page with a repair gets a `direct-repaired` fact (no severity).
- Only `colspan` and `rowspan` (1 to 50), `scope="row"`, `alt`, `data-box`, `type`, `checked` and
  `start` are read, each checked. A link keeps its target only when it is `http(s):` or
  `mailto:`.
- A figure's `data-box` (thousandths of the page, from its top left) becomes its box, so the
  figure is cropped from the page. Its alt is stored as an AI draft (`provenance="ai-drafted"`),
  shown with the "AI-drafted, not reviewed" badge. `alt=""` is decorative; no alt is
  `alt-missing`.
- Every node has `source="llm"`. The model's `h1` is a title candidate, as Docling's title is.

After the pages, one model-free step joins what runs on across a page break: a table whose next
page starts with a table of as many columns (a repeated header row is dropped), a list followed
by a list of the same kind, and a paragraph that goes on (the hybrid engine's rule). The joined
node keeps its first page and lists every page in `attrs.pages`.

## Requests, retries, cache and the spending cap

- Pages run in parallel: 4 requests at a time to the Anthropic API, one at a time to Ollama.
- A page whose request fails or whose answer is empty is asked again on its own, up to twice.
- Each answer is cached by (model, prompt version, page image hash, text hash), one file per
  answer in `~/.cache/reflow/direct` (`REFLOW_DIRECT_CACHE` to move it), which every conversion on
  the machine shares. A cached page makes no request, needs no key, and costs nothing.
- `--direct-max-dollars` (default 5) caps what a run spends on uncached pages. A request starts
  only while the dollars spent, plus the most a request can cost (a full text layer in, 16,000
  tokens out) for each request in flight and itself, stay within the cap, so a run never passes
  it. At the cap the run sends nothing more, keeps the pages it finished and every cached
  page, and flags each page it left unread with a `direct-unread` error. Run it again to go on:
  the finished pages come from the cache.
- Dollars come from a per-model price table (`PRICES` in `direct.py`, Anthropic's list prices,
  October 2026: Haiku 5.5 $0.10 and $0.50, Sonnet 5.5 $2 and $10, Opus 5.5 $4 and $20 per
  million tokens in and out). Ollama models cost nothing.

`document.json` records the run in `meta.direct`: the model, provider and prompt, and for each
page its tokens in and out, dollars and seconds (a cached page keeps the numbers of the request
that made it), with the totals, the pages left unread and why, and what this run spent.
`reflow convert` prints the totals. "How this transcript was made" shows the run's cost **per
1,000 pages** as its headline ("about $1.25 per 1,000 pages": its dollars divided by its pages,
times 1,000), and the document's exact cost and tokens in small text below it. The eval table
gives the same figure.

The page checks, the fast suite and the web tests never call a model: their direct tests use a
fake reader.

## Scored eval

`scripts/eval_direct.py` has each model transcribe the spec pages of the page-check suite
(`tests/pages/specs`), then scores the transcripts with the suite's own checks. Reflow's heuristic
pipeline (`default`) on the same pages is the baseline. Each document is cut to an excerpt of its
chosen pages, as the quick page checks cut one. Every assertion passes or fails, its `xfail` mark
ignored, and a heading's level is not checked. The checks are grouped by what they test (`FAMILIES`
in the script; "other" holds the rest). Seconds are model time per page for a direct row, and the
whole conversion's wall time per page for the others. Dollars use the price table above, as
dollars per 1,000 pages.

Besides `default` and a direct model, a row can be `assist:<model>` (the heuristics with every
assist step and the alt text on that model: the site's "Haiku" variant), `polish:<model>` (the
heuristics, then `--polish`, docs/polish.md) or `<model>/<prompt>` (a direct model with a prompt
variant). An assist row counts the requests it sends, so give it a fresh `--out` (the default
temporary folder is).

```sh
env $(cat .env) .venv/bin/python scripts/eval_direct.py --sample 64 \
    --models default claude-haiku-5-5 claude-sonnet-5-5 claude-opus-5-5 glm-ocr
```

The pages are read first, through the shared answer cache, under one spending cap for each model's
run (`--max-dollars`, default 5). Then each excerpt is converted from the cache. A second run costs
nothing and rebuilds the same table. `--failures PATH` writes each failed check, and `--out DIR`
keeps the conversions.

### First results (measured 2026-11-04)

Every model, on the same 64 specs spread evenly over the suite (`--sample 64`: 403 checks on 49
documents). No page was left unread. Sonnet and Opus ran on the sample only, because all 184 pages
would cost Opus about $9.40, past the $5 cap. The last column was dollars for the sample when the
table was first written (Haiku $0.08, Sonnet $1.63, Opus $3.28); it now gives them per 1,000
pages, as the site does.

| model | text | headings | blocks | tables | lists | forms | figures | other | all | s/page | $/1,000 pages |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| default | 95% | 88% | 79% | 88% | 89% | 92% | 100% | 89% | 91% | 1.2 | — |
| claude-haiku-5-5 | 72% | 68% | 76% | 71% | 58% | 31% | 71% | 44% | 65% | 5.3 | $1.27 |
| claude-sonnet-5-5 | 74% | 71% | 76% | 88% | 63% | 38% | 71% | 44% | 68% | 9.8 | $25 |
| claude-opus-5-5 | 75% | 73% | 76% | 82% | 74% | 38% | 76% | 34% | 68% | 13.1 | $51 |
| glm-ocr | 62% | 21% | 65% | 38% | 26% | 15% | 33% | 33% | 42% | 3.1 | $0 |
| checks | 127 | 94 | 34 | 34 | 19 | 13 | 21 | 61 | 403 | | |

On all 184 spec pages (1117 checks on 72 documents), `default` passes 92% and Haiku 68%, for
$0.24 (about $1.30 per 1,000 pages).

What the numbers say:

- **Reflow's rules lead on every family.** The gap is widest on forms. Each model writes fewer
  controls than the PDF's AcroForm holds (fw9 page 1: 6 of 15 text fields), and none of them put
  a question's choices in a `fieldset`, though the prompt asks for it.
- **The models skip words.** Text checks fail mostly on word recall. A dense index page came back
  from Haiku as one header row (`p17-138`: 0.6% recall), although the prompt says to transcribe
  every word and never to summarize. glm-ocr reads the picture alone and writes markdown, so it
  has no headings to rank and no form fields.
- **Some checks test reflow's own conventions**, which the direct element set cannot express:
  `lang` on a block in another language, `ins` and `del` in legislation, links to a page of the
  document, the review queue, calendar day statuses, signature figures and the "no description
  yet" alt. They count as failures for every model, and they make up most of "other".
- Opus is the most even of the models, but not better overall than Sonnet, at twice the price and
  1.3 times the time.

### Haiku experiments (measured 2026-11-04)

Every way to make a transcript with Haiku (claude-haiku-5-5), on the same 64-spec sample as the
table above (403 checks on 49 documents, 64 pages). No page was left unread. The rows:
`default` is reflow's heuristics; `assist` is the heuristics with every assist step and the alt
text on Haiku (the site's "Haiku" variant); `polish` is the heuristics, then the polish pass
(docs/polish.md, the site's "Heuristics + Haiku polish"); the last four are Haiku direct with
each prompt variant (`base` is the direct row above, from the cache). Seconds per page are model
time for a direct row and the conversion's wall time for the others.

| row | text | headings | blocks | tables | lists | forms | figures | other | all | s/page | $/1,000 pages |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| default | 95% | 88% | 79% | 88% | 89% | 92% | 100% | 89% | 91% | 1.3 | — |
| assist (Haiku) | 95% | 88% | 79% | 88% | 89% | 77% | 95% | 89% | 90% | 4.3 | $0.37 |
| polish (Haiku) | 94% | 83% | 79% | 88% | 89% | 92% | 100% | 89% | 89% | 4.3 | $0.62 |
| direct, base | 72% | 68% | 76% | 71% | 58% | 31% | 71% | 44% | 65% | 5.3 | $1.27 |
| direct, complete | 74% | 65% | 71% | 76% | 63% | 31% | 71% | 34% | 64% | 5.2 | $1.26 |
| direct, forms | 70% | 64% | 76% | 76% | 63% | 38% | 67% | 34% | 63% | 5.3 | $1.31 |
| direct, example | 74% | 63% | 79% | 79% | 68% | 15% | 67% | 34% | 64% | 5.5 | $1.33 |
| checks | 127 | 94 | 34 | 34 | 19 | 13 | 21 | 61 | 403 | | |

The whole run spent $0.31: assist $0.023, polish $0.040, and about $0.08 for each new prompt
variant (the base answers came from the cache).

What the numbers say:

- **No prompt variant wins overall**, so `base` stays the default. Each one helps where it aims
  and loses elsewhere: `complete` lifts text and tables but loses blocks and "other"; `forms`
  lifts forms from 31% to 38% (still far under the rules' 92%) and loses text; `example` has the
  best blocks, tables and lists of the four but drops forms to 15%.
- **The polish pass does not beat the rules.** On the 64 pages Haiku proposed 148 edits
  (47 heading levels, 44 headings to paragraphs, 20 moves; no list, table or group edit): the
  checks rejected 37 and applied 111. They fix three of `default`'s failures (a missed heading,
  a heading that is body text, and a page that should have none) and cause nine new ones: seven
  real headings demoted (NACA titles, the index letters of p17-138), one body line promoted, and
  one move that put a line out of order. Headings fall from 88% to 83%, text from 95% to 94%,
  and the rest is unchanged. The "AI-drafted, not reviewed" badge on each edited node is text in
  the page too: on docaccess-directory-2 it now sits inside a checked line.
- **The assist steps cost the least** ($0.37 per 1,000 pages) and change only forms and figures:
  they lose 2 of the 13 form checks and 1 of the 21 figure checks.
- Every Haiku row costs between $0.37 and $1.33 per 1,000 pages.
