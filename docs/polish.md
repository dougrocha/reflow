# Heuristics + AI polish (`--polish`)

`--polish` runs reflow's own pipeline first, then has a Claude model look at each page and fix its
structure. The model sees the page's picture and reflow's transcript of that page, and answers
with **edits to reflow's nodes**, never with HTML or text. It is opt-in, off by default, and
runs on the Anthropic API with your own `ANTHROPIC_API_KEY`.

```sh
env $(cat .env) reflow convert form.pdf -o out --polish                       # Claude Haiku
env $(cat .env) reflow convert form.pdf -o out --polish --polish-model claude-sonnet-5-5
```

On the site it is the variant "Heuristics + Haiku polish" (`<slug>~polish-haiku`).

## What the model reads and may change

Per page, `src/reflow/analyze/polish.py` sends the picture (PNG, 1568 px on its longer side) and
the page's blocks in reading order as JSON: each block's short id (`b1`, `b2`...), its role and
its text (clipped to 300 characters), a list's first items, a table's size and first row, and a
field's kind and label. The model answers with a list of edits (structured output):

| edit | what it does |
| --- | --- |
| `heading` | a paragraph or a heading gets level 1 to 6 |
| `paragraph` | a heading becomes a paragraph |
| `list` | two or more consecutive paragraphs become one list; a printed bullet or number moves off the item's text, as reflow's list rule moves it |
| `table_header` | a table's first row becomes header cells |
| `move` | a block goes right after another block, or first on the page (reading order) |
| `group` | two or more consecutive form fields go in a fieldset whose legend is the paragraph or heading right before them |

## Every edit is checked

An edit is applied only when:

- the blocks it names are on the page and have the roles it needs (a `list` takes paragraphs in
  a row, a `group` fields in a row with their title just before them, and so on);
- the page's normalized words are the same before and after it: the text and list marker of
  every node, in order. A `move` changes the order by design, so it compares them as a multiset.
  No edit may invent or drop a word.

An edit that fails is rejected and counted. The edits of an answer are applied in order, each on
the page as the ones before it left it. Then the heading levels and sections are rebuilt.

Every node an edit changed or made is labelled AI-drafted, as the other assist drafts are
(`provenance="ai-drafted"`, `reviewed` false, an `ai-drafted` fact and an `ai-polished` fact
that says what the edit was). The transcript shows the "AI-drafted, not reviewed" badge on it,
and the review queue lists it.

## Requests, cache and cost

The polish pass shares the direct engine's machinery (docs/direct.md): one request per page, 4
at a time; a failed page asked again up to twice; answers cached by (model, prompt id, page
picture, transcript) in `~/.cache/reflow/direct`; and a spending cap per run,
`--polish-max-dollars` (default 5). A page left unread at the cap, or whose request failed, stays
as the rules made it. Since the transcript is part of the key, a change to reflow's own output
for a page asks again.

`document.json` records the run in `meta.polish`: the model and prompt, and for each page its
tokens, dollars, seconds and edits (applied, rejected and by kind), with the totals.
"How this transcript was made" shows the cost per 1,000 pages, the document's exact cost and
the edits.

**What leaves the machine**, for each uncached page: its picture and the transcript JSON above.

A model's edits are in no key, so a conversion with `--polish` never reuses a saved model
(reflow.saved), and `docs:build` calls its provenance unverifiable, as it does any variant a model
answered for.

## Results

See the table in docs/direct.md ("Haiku experiments"), which scores this pass against the
heuristics, the Haiku assist steps and Haiku direct with each prompt variant.
