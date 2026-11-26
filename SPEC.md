# reflow product specification (draft)

The main output of reflow is an accessible HTML version of each PDF. Tagged PDF/UA export is a
later phase.

## 1. Problem

Organizations that fall under Section 508, ADA Title II, or EN 301 549 must publish accessible
documents. For ADA Title II, the DOJ rule in 28 CFR Part 35 sets WCAG 2.1 AA as the standard.
Most published PDFs are untagged, badly tagged, or scanned images. A fix by hand in Acrobat Pro
takes 15 minutes to several hours per document.

reflow reads PDFs and makes an accessible HTML version of each one. It is built to meet
WCAG 2.1 AA, and flags what a person must check. The HTML
is fast to read on any device, also for documents of 1,000 pages or more. Automation does most
of the work, and people review only the items that the machine is not confident about. A tagged
PDF/UA file is a later phase.

### Why HTML comes first

- It is much simpler and more reliable to generate semantic HTML from a document model than to
  rewrite the tags inside the content streams of an existing PDF.
- HTML works better with screen readers, reflow and zoom (WCAG 1.4.10), mobile devices, and
  search.
- The validation tools for HTML, such as axe-core and Pa11y, are mature.

### The compliance caveat needs legal review

WCAG allows a conforming alternate version. The DOJ Title II rule appears to allow an alternate
version only when technical or legal reasons make it impossible to make the original accessible.
So an HTML alternative alone may not satisfy every customer. For that reason, the product must
make the HTML version the main experience, the one that is linked first. It must also keep
PDF/UA export on the roadmap as Phase 5. Confirm with counsel before marketing makes any
compliance claim.

## 2. Users

| User | Need |
|---|---|
| Accessibility coordinator | Ingest a document library in bulk, track its status, and see what is waiting for review |
| Content author or admin staff | Upload a PDF, publish an accessible version, and answer a few questions |
| Reviewer, internal or a vendor specialist | Work fast through a queue of low-confidence items |
| End reader, including screen-reader users | Read, navigate, and search a large document fast and accessibly |
| Developer or integrator | An API and an embeddable viewer to add to a CMS, an agenda portal, or a records portal |

## 3. Pipeline

`reflow convert` runs one conversion as a fixed sequence of passes. The sequence is in
`src/reflow/pipeline.py`, and `src/reflow/convert.py` runs it. Each pass reads a frozen `Run`, which holds the options, the worker
pool, the staging folder, and the `Document`. A pass changes only the `Document` and its fact
ledger (§5). reflow writes nothing until the options, the model provider, and the output
directory pass their checks. It builds the output in a staging folder, which replaces the output
directory only when the conversion succeeds. So a failed run leaves the previous output as it
was.

```
prepare.prepare                   source: original.pdf, working.pdf, page info, OCR
analyze.engine.build              engine: the node tree
analyze.text_evidence             evidence: OCR word grades onto the tree
analyze.evidence.witness          evidence: title, links, tag tree, bookmarks
analyze.order_evidence.apply      evidence: reading order
analyze.structure.finish          structure: tables, footnotes, sections, page labels, ids
analyze.structure_assist          opt-in: --assist-structure
analyze.label_assist              opt-in: --assist-labels, --assist-scan-fields
analyze.page_vision               opt-in: --assist-page-vision
analyze.form_repair               opt-in: --assist-form-repair
analyze.polish                    opt-in: --polish
analyze.assist.transcribe         opt-in: --assist local
analyze.enrich.describe           figures: crops and alt text
analyze.page_integrity            pages whose ink has no text
analyze.decisions_apply           opt-in: --decisions
analyze.review.queue              confidence from the facts, then the review items
output.publish                    every derived file
```

1. **Source** (`reflow.prepare`). This pass copies the input byte for byte to `original.pdf`.
   It writes `working.pdf`, which qpdf repairs, decrypts, and linearizes. Every later pass reads
   `working.pdf`. The pass records the metadata, the SHA-256 of the input, and the kind and the
   printed label of each page. A page is born-digital, scanned, mixed, or empty. reflow refuses
   a PDF whose owner forbids text extraction (exit 3), unless `--allow-restricted` is given.
   With that option, reflow converts the PDF and flags it. OCRmyPDF adds a text layer to
   image-only pages (`--ocr auto|none|ocrmypdf`). With `auto` and no OCRmyPDF installed, reflow
   flags those pages instead. Nothing writes `working.pdf` after this pass.
2. **Engine** (`analyze.engine.build`). This pass runs the layout engine that `--engine` names,
   and puts the tree of that engine under the root.
   - `heuristic` (default). pypdfium2 reads the text lines and drawings of each page, and
     pdfplumber finds ruled tables. Then `analyze.layout.analyze` finds the artifacts (running
     headers, footers, and page numbers), the columns and the reading order, and the blocks,
     headings, lists, paragraphs, tables, figures, and form fields. It plans each page on its
     own. After that, it ranks the heading styles over the whole document, and joins each list
     that continues across a page break.
   - `docling`. Docling's models run over the whole document. This engine needs the `docling`
     extra.
   - `hybrid`. The heuristic engine runs on every page, and Docling runs on the pages that need
     it. Those pages have soft column gutters, ruled tables, vector figures, or many short
     blocks. A Docling table or figure replaces the heuristic one only when it keeps the text of
     the page.
   - `direct`. A model reads the picture of each page and writes its transcript. It replaces the
     analysis and layout. It is opt-in and runs on the Anthropic API or on a local Ollama model
     (`--direct-model`, `docs/direct.md`).

   With `--scan-fields`, the rules in `analyze.scan_fields` read the image of each scanned page
   before layout, in a PDF that has no form fields of its own. They add the blanks, the boxes,
   and the check boxes (squares and circles) that they are sure of. `--assist-scan-fields` also
   asks a model about the doubtful ones. With every engine, the text of the tree holds no
   U+FFFD.
3. **Evidence** (`analyze.evidence.witness`, `analyze.order_evidence.apply`). The evidence pass
   chooses the title from the metadata, the first bookmark, the top heading of the first page,
   Docling's title, and the file name. It turns link annotations into inline links. Next, it
   compares the tag tree and the bookmarks with the node tree. It compares the tag tree only for
   documents of up to 300 pages. Each agreement or disagreement becomes a fact. The tags also
   supply the alt text, header scope, and heading levels that the heuristics cannot know. Then
   `analyze.order_evidence.apply` compares the reading order of each page with the order of the
   tag tree, by Kendall's tau-b. In an untagged document, it compares with Docling's order on
   the hard pages of the hybrid engine instead. A page that disagrees gets an
   `order-disagreement` fact.
4. **Structure** (`analyze.structure.finish`). This pass flags doubtful tables and headings, and
   text problems such as dropped ligatures, literal bullets, and unfinished sentences. It fills
   row headers and links footnotes to their references. It nests the flat node list into
   `SECTION` nodes, fills the page labels that the file does not declare, and makes the node ids
   unique.
5. **Opt-in assist.** These passes are off by default. Each one asks a model one narrow
   question, and checks the answer against the page before it changes anything.
   - `--assist-structure` asks whether a doubtful heading on a form is a heading, a label, or a
     value.
   - `--assist-labels` asks which printed caption labels a form field that the rules could not
     name. `--assist-scan-fields` asks the same question about the fields that it found on
     scanned pages.
   - `--assist local` transcribes hard scans and handwriting after OCR, with an on-device model.
   - `--assist-page-vision` has a local vision model (Ollama) look at each page and propose
     heading changes. A proposal changes nothing until `reflow review` accepts it, and the next
     conversion applies it only if the text is unchanged.
   - `--assist-form-repair` has a local vision model read a region of a form that OCR left
     unread. A reading that the page confirms becomes a review item. The transcript does not
     change.
   - `--polish` has a Claude model see each page and propose structural edits (heading levels,
     lists, table headers, reading order, form groups). An edit that would change the text is
     rejected (`docs/polish.md`).

   The structure, label, and scan-field questions run on a local Ollama model (`gemma4:e4b`) by
   default. With `--assist-provider anthropic`, they run on the Anthropic API with the user's
   own key. reflow caches the answers in `ai-assist-cache.json` and caps the uncached questions
   per document. Each accepted answer becomes a fact with source `assist`, and the review lists
   that fact as a warning. `docs/assist.md` lists what leaves the machine.
6. **Figures** (`analyze.enrich.describe`). This pass crops each figure to WebP. With
   `--alt-text local`, an on-device vision model (Qwen3-VL-8B) drafts alt
   text from the crop, the caption, and the text near the figure. `--alt-text ollama` and
   `--alt-text anthropic` ask a model server instead. reflow caches the drafts in
   `alt-text-cache.json` and still lists them for review. Then `analyze.page_integrity` flags
   the pages that have no text in the transcript. It also flags the scanned pages with ink that
   the text layer does not explain.
7. **Review** (`analyze.review.queue`). `analyze.confidence.apply` scores every node from its
   facts. The score is the prior of the engine plus the weight of each fact. Each role has its own
   threshold. Every fact that has a severity becomes a review item, and so does every
   node below the threshold of its role. With `--decisions FILE`, `analyze.decisions_apply`
   first applies a reviewer's saved decisions (`docs/review-decisions.md`). It applies only the
   ones that still match the page, and writes `decisions.json`.
8. **Publish** (`output.publish`). This pass writes these files into the staging folder:
   `document.json`, `review.json`, the per-page layout sidecars, the per-page trace of the
   heuristic engine, `passages.jsonl`, the page images, and the site. The site is `index.html`,
   `chunks/`, `toc.json`, `manifest.json`, and `assets/` (§6). "Ask a question" in the web app
   searches `passages.jsonl`. Then `output.staged` swaps the staging folder in for the output
   directory.

### Parallel work

With `--jobs N` above 1, one pool of N spawned worker processes serves the whole conversion.
The default is the CPU count minus 2. The pool reads the page info of a long document, and
extracts text and tables in runs of pages. For the heuristic engine, it plans the layout of each
page when the document has six pages or more. It also parses the pages that the tag tree and the
links need, renders the page images, and crops the figures. The parent process keeps every step
that spans pages.

Two threads in the parent process start the reads of the evidence pass early. One thread reads
the tag tree and the link annotations while the pool extracts. The other asks the pool to parse
the pages that the tags and links need. That request waits in the queue of the pool behind the
layout planning. The page images
start when the engine pass ends, and they render while the serial passes run.

The output of `--jobs N` is the same as the output of `--jobs 1`. `mise run verify-parallel`
checks that the two outputs match, with a TLA+ model of the orchestration and Hypothesis tests
on the real code.

`reflow convert-many` converts several documents at once, on threads that share one pool.
`--max-docs` sets how many, and the default is 2. A document that fails does not stop the
others. With any engine but `heuristic`, with any `--alt-text` but `none`, or with
`--assist local`, `convert-many` converts one document at a time.

### Not in the pipeline yet

These parts are not in the pipeline yet:

- axe-core validation runs against the web viewer (`bun run a11y` in `web/`), not inside
  `reflow convert`. It does not block publishing.
- The converter never writes the decision store of a reviewer. The web app saves decisions in
  it, and `reflow convert --decisions` or `reflow rerender` applies them
  (`docs/review-decisions.md`). `reflow review` saves only the accept or reject of page-vision
  proposals, in `proposal-decisions.json`.
  The web app records decisions only on a development server, or with `REFLOW_BUILDS=1`, and has
  no authentication.
- reflow does not publish to object storage or a CDN. The output is a static folder.
- Outside the opt-in AI passes (§3, item 5), no pass uses AI to repair headings and tables.
- No pass decides for good whether a figure is decorative. The figure pass guesses from the
  size, place, and repetition of a figure (`decorative-guessed`), and a model's alt-text draft
  can say decorative. A reviewer checks both.

## 4. Phases

Phase 0 and Phase 1 are built. `reflow audit` reports as text, JSON, Markdown or HTML, and
`--verapdf` merges veraPDF's PDF/UA-1 result. Phase 2 is built, except for axe-core
inside the pipeline. axe-core runs on the web viewer instead. Phase 3 has OCR, local alt text,
the opt-in assist, confidence scoring, and a decision store of reviewer decisions, which the
converter applies. For Phase 4, the web app has a review list, an inspector that records
decisions, and conversion jobs, but no service, upload, or authentication. Phase 5 is not
started.

### Phase 0: Ingest and inspect

- A loader, page classification, the document model, and `reflow inspect file.pdf` with JSON
  output.

### Phase 1: Audit the source PDF

- Checks on the original PDF, mapped to Matterhorn and WCAG (§7, "Source checks").
- A report in JSON, Markdown, or HTML, a non-zero exit code for CI, and an optional veraPDF
  merge.
- The audit is useful on its own. It lets customers measure their backlog before they convert
  anything.

### Phase 2: HTML for born-digital PDFs, the first product to ship

- Layout analysis builds the model. The model becomes chunked semantic HTML and page images.
- A static viewer (§6) that you can open locally after `reflow convert file.pdf -o out/`.
- axe-core validation inside the pipeline.

### Phase 3: Scanned documents, AI enrichment, and confidence

- OCR, confidence scoring, and an LLM that writes alt text and repairs headings and tables.
  OCR, confidence scoring, and AI alt text are built. The AI repair of headings and tables is
  built only as the opt-in passes of §3.
- The review decisions. They are not saved in a sidecar file `file.reflow.json` keyed by node
  id, as first planned. They are saved in a JSONL decision store, and each decision is matched
  to the page again by an anchor (`docs/review-decisions.md`).

### Phase 4: Review UI and service

- A FastAPI service for upload, job status, the review queue, and publishing.
- A reviewer UI built for the keyboard first. It shows the page image with overlaid boxes,
  reading-order numbers, and tag types. It lets the reviewer edit alt text and table headers.
  The web app has the inspector and records the decisions about OCR words, alt text, and
  warnings. It does not edit table headers or reading order.
- An embeddable viewer, share links, and an API for CMS integrations.

### Phase 5: Tagged PDF/UA export

- Write the structure tree and the MCIDs back into the original PDF, from the same model that
  the HTML came from. A simpler path renders the HTML to a tagged PDF, for example with the
  PDF/UA output of WeasyPrint. That path does not keep the original layout.
- Validate the result with veraPDF against PDF/UA-1.

### Out of scope for now

- Inputs other than PDF, such as DOCX and PPTX. They could come later through the same model.
- Automatic fixes for problems that need human judgment, such as meaning that only color
  carries, or a meaningful sequence in an ambiguous design. reflow flags these problems and
  does not claim to fix them.

## 5. Document model (sketch)

The model is in `src/reflow/model.py`. Coordinates are PDF points with a top-left origin.
Page numbers start at 1.

```python
class Node:
    role: Role              # doc, section, h1..h6, p, list, li, table, tr, th, td, figure,
                            # caption, link, note, form, field, group, box, artifact
    id: str                 # f"{page}:{sha1(rounded bbox, text)[:12]}": survives re-runs and
                            # a change of role
    page: int | None
    bbox: tuple[float, float, float, float] | None
    text: str | None
    attrs: dict             # alt, lang, scope, colspan, level, label...
    children: list[Node]
    confidence: float       # 0..1, written only by analyze.confidence.apply
    source: Source          # heuristic | tags | docling | ocr | llm | assist | human
    facts: list[Fact]       # the evidence about this node
    spans: list[Span]       # inline links and note references, as offsets into text
    marks: list[Mark]       # inline emphasis and OCR doubt, as offsets into text

class Fact:                 # who found what, never what it is worth
    code: str               # "order-disagreement", "alt-missing", "label-assisted"...
    source: Source
    detail: str             # the reason a reviewer reads
    data: dict              # measurements, JSON scalars only

class PageInfo:             # number, size, rotation, kind, label, char_count, image_coverage,
                            # and the page's facts
class Document:             # root: Node, pages: list[PageInfo], meta: dict
```

Every pass reads and writes this tree (§3). The facts work as a ledger. An engine or a pass
appends what it found, and `analyze.confidence.CODES` is the one place that gives each code its
label, severity, and weight. The confidence of a node is the prior of its engine plus the
weights of its facts. `analyze.review` reads the review items from the same facts. The HTML
renderer, the review queue, and the inspector in the web app all read the tree. So detection,
decisions, and output stay separate, and each one can be tested on its own. `document.json` is
the serialized `Document`.

## 6. Web viewer and performance (documents of 1,000 pages or more)

### Principles

- **Do the heavy work at conversion time, not in the browser.** The browser receives static
  HTML and images that are already split, not a PDF that it has to parse.
- **Don't list-virtualize the accessible view.** With windowing, only the visible rows exist in
  the DOM. Windowing breaks screen-reader browse mode, heading navigation, and Ctrl+F, and those
  are what the accessible view is for. Keep the content in the DOM, and let the browser skip
  rendering it instead.

### Accessible HTML view (primary)

- **Chunking.** The converter splits the document into runs of whole pages
  (`render/chunks.py`). The first chunk keeps within 30 KB, because the viewer fetches it first.
  A later chunk ends at a page boundary once it reaches 150 KB. It also ends at a page that
  opens a top-level section, once it has 40 KB. The output looks like this:
  ```
  out/
    index.html          # shell: title, lang, skip link, contents, the first chunks inline
    toc.json            # every heading, with its chunk and anchor
    manifest.json       # chunks, pages and their sizes, contents, version: for other front ends
    chunks/0001.html …  # semantic fragments, each also a standalone page
    pages/0001.webp …   # page renders, 800 px, and 0001-1200.webp for srcset (up to 300 pages)
    figures/            # figure crops, p0001-0.webp …
    assets/             # viewer.css, viewer.js
    review.json         # the review queue
    decisions.json      # only with --decisions: what the reviewer's decisions did
    original.pdf        # the input, byte for byte, for download
  ```
  The output folder also keeps working files that are not for the reader. They are
  `working.pdf` (the linearized copy from qpdf), `document.json`, the per-page records in
  `layout/` and `trace/`, the alt-text and assist caches that the next run reuses, and
  `passages.jsonl`. "Ask a question" in the web app searches `passages.jsonl`.
- **Loading.** The shell inlines the first chunk for a fast first paint. The viewer prefetches
  the next chunks when the reader nears the end of the current one, with an
  IntersectionObserver sentinel. A jump from the TOC or from search loads the target chunk
  directly. A chunk stays in the DOM once it has loaded.
- **Render cost.** Each page wrapper has `content-visibility: auto` and
  `contain-intrinsic-size`. So off-screen content costs almost nothing to render, but it stays
  in the DOM and in the accessibility tree.
- **Page parity.** A marker wraps each page, such as
  `<div class="pg" id="p37" data-page="37"><p class="pg-label"><span>Page 37</span>…`. So a
  "see page 37" reference works, and so does a `#p37` deep link.
- **Search, not built yet.** The static viewer has no search. The plan is full-document search,
  with a prebuilt static index or a backend endpoint. A Pagefind index works on a CDN with no
  server. Results will deep-link to `#p37` or to heading anchors. The web app has its own find
  over the chunks it has loaded, and "Ask a question" (`docs/ask.md`).
- **Images.** Each image has `loading="lazy"`, an explicit width and height so that the layout
  does not shift. The alt text is the reviewer's, the author's, a model draft (only when
  `--alt-text` is on, labelled as AI-drafted), or a plain description of where the figure is.
- **Progressive enhancement.** Without JavaScript, the TOC links to the standalone chunk pages,
  because each chunk is also a full HTML page. JavaScript adds continuous scrolling.
- **Accessibility of the shell.** The shell has landmarks and a skip link. When a chunk loads
  from the TOC or from search, focus moves to the target heading. The shell uses `aria-live`
  only for status messages, reflows at 320 CSS px, and uses no animation.

### "View original" (secondary)

By default, "view original" shows page images. reflow already renders each page during the
conversion, so it keeps the renders as WebP files and loads them lazily. The browser needs no
PDF engine, scrolling is instant, and a CDN can serve the images.

PDF.js is an option for true PDF rendering and zoom. To use it:

- Serve the linearized PDF with HTTP range requests (`Accept-Ranges`).
- Set `disableAutoFetch: true`, and `disableStream` as needed, so that only the bytes of the
  pages in view load.
- Use the lazy page rendering and the canvas recycling that the PDF.js viewer has built in.

Only if editing or annotation features come later, consider PDFium compiled to WASM for faster
rendering of complex pages. EmbedPDF is one option, and Nutrient and Apryse are commercial ones.

### Performance budgets (1,000-page reference document)

| Metric | Budget |
|---|---|
| Time to first readable content (4G) | < 1.5 s |
| Initial transfer (shell and first chunk) | < 200 KB |
| Main-thread work per chunk append | < 50 ms |
| Memory after scrolling the full document | < 300 MB |
| Jump to any page from the TOC or search | < 1 s |

Only the initial transfer is measured, by `bun run a11y`. No test measures the other rows yet.

The first load of the viewer has a soft target of 200 KB. It counts the document, scripts,
styles, manifest, and first chunk, compressed. `bun run a11y` warns above 200 KB, and gives an
early warning above 180 KB, but it does not fail. The initial transfer in the table, the HTML of
the shell and the first chunk, stays a hard limit.

## 7. Checks

### Source checks (Phase 1, on the original PDF)

| ID | Check | WCAG |
|---|---|---|
| SRC-META-001 | Tagged (`MarkInfo/Marked`, StructTreeRoot) | 1.3.1 |
| SRC-META-002 | Title present and `DisplayDocTitle` set | 2.4.2 |
| SRC-META-003 | Language set | 3.1.1 |
| SRC-META-004 | Security doesn't block assistive technology | 4.1.2 |
| SRC-TXT-001 | Real text present (not image-only) | 1.4.5 |
| SRC-TXT-002 | Fonts embedded and mapped to Unicode | 1.3.1 |
| SRC-FIG-001 | Figures have alt text | 1.1.1 |
| SRC-TBL-001 | Tables have header cells | 1.3.1 |
| SRC-FRM-001 | Form fields have tooltips | 4.1.2 |

`src/reflow/checks/source.py` maps each check to its Matterhorn Protocol 1.1 checkpoint. No
checkpoint maps to SRC-TXT-001.

### Output checks (Phase 2 and later, on the generated HTML)

These checks are meant to block publishing. None of them runs inside the pipeline yet (§8).

| ID | Check | WCAG |
|---|---|---|
| OUT-AXE | axe-core passes on every chunk, with the wcag2a, wcag2aa, and wcag21aa tags | various |
| OUT-H-001 | Exactly one h1, and no skipped heading levels | 1.3.1, 2.4.6 |
| OUT-IMG-001 | Every informative image has alt text that is not empty and not a file name. A decorative image has `alt=""` | 1.1.1 |
| OUT-TBL-001 | Data tables have `<th>` with `scope` or `headers`. Complex tables have a caption or a summary | 1.3.1 |
| OUT-LNK-001 | Link text is descriptive: no "click here", and no raw URL without context | 2.4.4 |
| OUT-LANG-001 | `lang` on `<html>`, and `lang` on foreign-language passages | 3.1.1, 3.1.2 |
| OUT-ORD-001 | Reading-order confidence is at or above the threshold, or a person approved the order | 1.3.2 |
| OUT-CLR-001 | Text contrast is at least 4.5:1 in the reflow CSS | 1.4.3 |
| OUT-RFL-001 | No horizontal scroll at 320 px (Playwright screenshot test) | 1.4.10 |
| OUT-REV-001 | Zero unresolved review items | None |

## 8. Architecture and package layout

```
src/reflow/
  cli.py              # typer: inspect | convert | rerender | review | convert-many | audit | doctor
  convert.py          # convert and convert_many: prepare, the model passes, publish, Summary
  pipeline.py         # the order of the model passes (§3): read_pages, finish, Model
  saved.py rerender.py   # reuse of a saved model, and decisions applied without a full conversion
  run.py              # Options (validated once), Workers (the pool and the pdfium lock), Run
  prepare.py          # the source pass: original.pdf, working.pdf, OCR, the Run
  model.py            # Node tree, facts and spans (§5), and their (de)serialization
  geometry.py text_quality.py   # box arithmetic and the OCR text checks every stage shares
  providers.py        # the AI providers and their default models
  output.py           # the staging folder and swap, publish (names.py: every output name)
  stagecache.py       # opt-in cache (--stage-cache) of ingest, OCR and extraction
  steps.py            # the opt-in AI steps, declared once; the options as a record, `uncovered`
  provenance.py       # what a published document was converted from
  ingest/             # load, repair and linearize (qpdf), classify pages, OCR (OCRmyPDF)
  extract/            # text lines and drawings (pypdfium2), tables, form fields, tags,
                      # links, figure crops (crops.py), the pool's page chunks (parallel.py),
                      # the witnesses' claims
  analyze/
    engine.py         # the engine pass: heuristic, docling or hybrid
    layout.py         # the heuristics that build the Node tree: analyze runs the stages in order
    pageplan.py       # per-page layout planning on the worker pool
    artifacts.py columns.py blocks.py breaks.py lists.py worksheets.py headings.py
    figures.py forms.py
                      # the layout stages. constants.py holds the tunables they share
    form_cuts.py form_options.py form_labels.py form_groups.py form_plan.py form_place.py
    form_common.py    # forms.py's parts: line cutting, option labels, label search, groups and
                      # legends, node building and the plan, placing in the flow; what they share
    docling_layout.py hybrid.py   # the Docling and hybrid engines
    evidence.py order_evidence.py title.py prefetch.py   # the evidence pass
    structure.py      # the structure pass: row headers, notes, sections, labels, ids
    row_headers.py notes.py sections.py page_numbers.py   # its steps
    table_evidence.py text_evidence.py   # checks that become facts
    scan_rules.py scan_fields.py   # fields on scanned forms
    structure_assist.py label_assist.py assist.py   # the opt-in assist passes
    page_vision.py form_repair.py polish.py   # more opt-in AI passes (--assist-page-vision,
                                              # --assist-form-repair, --polish)
    direct.py direct_html.py   # --engine direct: a model transcribes each page
    decisions.py decisions_apply.py   # a reviewer's saved decisions (docs/review-decisions.md)
    language.py       # the language of the document, its pages and its blocks
    enrich.py         # figure alt text (local, Ollama or Anthropic model, or none)
    page_integrity.py # pages whose ink has no text in the transcript
    confidence.py review.py   # scoring from facts, the review queue
  checks/
    source.py         # Phase 1 PDF checks (SRC-*)
    doctor.py         # which optional tools this machine has
  render/
    site.py           # render_html: writes the shell, the chunk pages, toc.json and the assets
    shell.py          # the shell and the standalone chunk page, built as escaped f-strings
                      # (markupsafe), with no template engine
    html.py           # Node tree to semantic HTML: inline text (links.py) and each block
    chunks.py toc.py manifest.py   # the chunks of whole pages, the contents, manifest.json
    pages.py          # each page to WebP (pypdfium2) at each srcset width
    sidecars.py
    passages.py       # passages.jsonl, for "Ask a question" in the web app
  viewer/             # small vanilla JS and CSS, no framework: the chunk loader and contents
tests/                # unit tests. pages/ holds the page checks on the sample PDFs
web/                  # Next.js app (bun): document pages, viewer, inspector, review list,
                      # "Ask a question" (docs/ask.md), conversion jobs (docs/builds.md),
                      # a11y check (scripts/a11y-check.ts)
```

Each package and top-level module is a layer, and a module imports only its own layer and the
ones before it: the base (model, geometry, text_quality, leaders, patterns, pdfobjects, names,
cachedir, pool, budget, providers, device, pageimages, identity, tessdata, fontcensus,
progress_lines), then ingest, stagecache, extract, checks, run, steps, prepare, analyze,
pipeline, viewer, render, output, provenance, saved, convert, rerender and cli.
`tests/test_layers.py` holds the order, imports inside functions included, so a cycle between
stages cannot come back.

These parts are not built yet: a search index (`render/search.py`, Pagefind), the HTML output
checks inside the pipeline (`checks/output/`), the service (`review/`, Phase 4), and PDF/UA
export (`export/pdfua.py`, Phase 5).

## 9. Technology choices

| Concern | Choice | Notes |
|---|---|---|
| Environment and packaging | uv, Python 3.12 or later | `uv run reflow`, `uv tool install` |
| PDF object access | pikepdf (qpdf) | MPL-2.0. Repairs, linearizes, and reads tags and metadata |
| Text extraction | pypdfium2. pdfplumber stays for ruled tables, and for the page parse that the tag tree and links need | About 6x faster than pdfplumber: 19 ms against 124 ms per page on p17 |
| Page rasterization | pypdfium2 | Apache and BSD licenses. Fast. Avoids the AGPL of PyMuPDF |
| Layout analysis | Heuristics by default. Docling is optional (`--engine docling`, `hybrid`) | Docling is MIT. It gives reading order, tables, and headings |
| OCR | OCRmyPDF and Tesseract | System dependencies |
| AI enrichment | Alt text: Qwen3-VL-8B on the machine (transformers), or a model on Ollama or the Anthropic API (`--alt-text`). Assist passes: Ollama (`gemma4:e4b`) by default, or the Anthropic API with the user's own key | Optional and off by default. The default runs fully offline |
| HTML generation | Escaped f-strings, with MarkupSafe | No template engine |
| Search | Pagefind (not built yet) | A static index that a CDN can serve |
| Questions in the web app | BM25 over `passages.jsonl`. Ollama (`gemma4:e4b`) by default, or the Anthropic API with the user's own key | The server checks the quote against the document before it shows the answer (`docs/ask.md`) |
| Viewer | Vanilla JS and CSS with no framework, in every output. The Next.js app in `web/` | Small, fast, and easy to audit for accessibility |
| "View original" | Page images in WebP. PDF.js is optional | Range requests, `disableAutoFetch` |
| Validation | axe-core through Playwright (`bun run a11y`). veraPDF for Phase 1 (`reflow audit --verapdf`, optional) and Phase 5 | |
| Service (Phase 4) | FastAPI, a job queue such as arq or RQ, and S3-compatible storage | Planned, not built. None of them is a dependency. The web app runs its own builds (`docs/builds.md`) |
| Tests | pytest and Hypothesis, page checks on the sample PDFs, a TLA+ model of parallel conversion, `bun test` for the web app, and a Playwright accessibility check | No hosted CI. Every check runs on the developer's machine (`scripts/affected`) |

## 10. Key risks

1. **Reading order and complex tables.** Automated tools fail here. To reduce the risk, reflow
   scores confidence, queues doubtful items for review, and never guesses silently when
   confidence is low.
2. **Quality of AI alt text, and hallucination.** The mitigations are constrained prompts that
   use the surrounding text, confidence thresholds, human approval for charts and diagrams, and
   a stored `source=llm`, so that edits can be audited.
3. **Data sensitivity.** Customer PDFs can contain personal data (PII) or controlled
   unclassified information (CUI). The mitigations are a pluggable provider, a local-only mode,
   no training on customer data, and retention controls.
4. **Legal position of HTML alternates.** See the caveat in §1. The mitigations are a legal
   review and the PDF/UA export of Phase 5.
5. **System dependencies.** These are Tesseract, Ghostscript, Java for veraPDF (optional, `audit --verapdf`),
   and Chromium for Playwright. The mitigation is `reflow doctor`. A Docker image is planned and
   does not exist yet.

## 11. Success metrics

- For Phase 2, 100% of the born-digital fixtures pass axe-core with zero violations, and manual
  spot checks judge the HTML faithful to the source.
- For Phase 3, at least 85% of the nodes are accepted automatically because they score above
  the threshold. The median review time is under 5 minutes per document.
- The viewer meets the §6 budgets on a 1,000-page document. This is a target. No performance
  test exists, and the repository has no CI. `bun run a11y` measures the initial transfer on the
  converted sample documents, run by hand.
- Screen-reader smoke tests pass for every release, with NVDA on Chrome and with VoiceOver on
  Safari. This is a target. No such tests exist yet.

## 12. Scope and assumptions of the proof of concept

reflow is a proof of concept (POC). These defaults hold until a decision changes them:

- A local CLI only (`reflow convert file.pdf -o out/`), plus a static viewer that opens from
  disk or from `python -m http.server`.
- Born-digital PDFs come first. OCR, local alt text, and the opt-in assist are built as well.
  OCR uses OCRmyPDF by default when it is installed.
- There is no service, authentication, or storage. `review.json` and the Review page of the web
  app list the review items. The web app records reviewer decisions in a local decision store,
  and the converter applies them (§3).
- WCAG 2.1 AA is the target. The legal position is deferred.
- Success means that a 1,000-page PDF converts to HTML that passes axe-core and meets the §6
  budgets.

### Questions deferred until after the POC

1. **Deployment.** Is reflow a CLI and a library only, or a hosted multi-tenant service from
   the first day?
2. **AI.** Is an external LLM allowed, or are local models required? The assist passes have an
   answer. They use a local model through Ollama by default, and the Anthropic API only with the
   user's own key.
3. **Corpus.** What do the real inputs look like? Candidates are agendas and minutes, scanned
   forms, long reports, and financial statements. The answer sets which analyzers to build
   first.
4. **Review staffing.** Do customers review their own documents, or do we provide specialists,
   as hosted remediation vendors do?
5. **Legal.** Confirm the position of HTML alternates against remediated PDFs for the target
   customers.
6. **Target standard.** The target is WCAG 2.1 AA now. Should it also be WCAG 2.2 AA?
