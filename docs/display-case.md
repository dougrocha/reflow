# Text printed in capitals: shown in sentence case, kept as printed

Headings, form labels and short lines printed wholly in capitals read more easily in sentence
case. reflow changes only how they **look**. The words stay exactly as the PDF prints them.

## The rule

- The DOM, the downloaded HTML, a copy and paste, and what a screen reader reads all hold the
  words as printed: `MAIL TO: ASSESSMENT APPEALS BOARD CLERK`.
- The screen shows the sentence-case form: `Mail to: Assessment appeals board clerk`.
- These runs are eligible:
  - a heading of up to 24 words
  - a form label or a group's legend of up to 24 words
  - a paragraph of up to 8 words
- A run qualifies only when it is wholly in capitals: two capital letters or more, and no
  lower-case letter. Longer paragraphs in capitals are body text set that way on purpose, such
  as a legal warning. They stay in capitals, and so does any mixed-case text.
- Each word takes a case from one of these rules:
  - the case the document prints it in elsewhere: lower case when it appears in lower case
    (`ASSESSMENT` becomes `assessment`), or a capital when it appears capitalised in mid-sentence,
    like a name (`SAN LUIS OBISPO` becomes `San Luis Obispo`)
  - otherwise, lower case if it is a common English word (`render/casing.py`, `COMMON`)
- Acronyms are never changed. A word keeps its capitals when it:
  - holds a digit (`W-4`, `I-9`, `D430`, `1099-MISC`)
  - is a known acronym (`EIN`, `USCIS`, `IRS`; `KEEP` in `render/casing.py`)
  - is a single letter
  - is three letters or fewer and the document never prints it otherwise (`CA`, `LLC`)
- If a longer word appears only in capitals and is not a common word, it might be a name or an
  acronym. Then the whole run stays as printed. When in doubt, reflow shows the capitals.
- The first word, and the word after a `.`, `:`, `?` or `!`, starts with a capital.
- reflow leaves a heading split in two languages (`ENGLISH / ESPAÑOL`) as printed, along with
  an address set line by line.

## How it works

`render.casing.Casing` reads every word of the document once, to learn how it prints each word.
The renderer wraps each stretch of text it changes like this:

```html
<span class="sc" data-sc="Mail to"><span>MAIL TO</span></span>
```

The stylesheet (`viewer/transcript.css`) sets the inner span, the printed words, to
`font-size: 0`. It then draws the `data-sc` form with `content: attr(data-sc) / ""`. The empty
alternative after the `/` keeps screen readers from reading the drawn form. In Chromium, the
accessibility tree, `innerText`, the selection and the clipboard all hold the printed capitals.

This doesn't use `text-transform`, because Chromium gives the transformed text to the
accessibility tree and to the clipboard.

Fallbacks:
- A browser without the `content: … / ""` syntax shows the capitals (`@supports`).
- A find hit or a jump target inside a run shows the printed words, because those are what it
  matched.

The table of contents, `toc.json` and the document's name (in the manifest, the page's `<title>`
and the viewer's header) keep the printed text. The title printed on the first page is a heading
like any other, so it shows in sentence case.
