# Review decisions

A reviewer's answers to the review queue: a correction of an OCR word that reflow flagged,
a "keep OCR's reading", a figure's alt text, a dismissed warning, and an accept or reject of a
page-vision proposal. They live in a **decision store**, a JSONL file outside the
output folder. The converter reads it once per conversion, matches every decision to the page
again, and applies only what still matches exactly one target. A decision that no longer matches
never changes the output. It goes to review instead.

The converter never writes the store. Writers follow the protocol below: the web app and
the test helper `tests/decisions_kit.py`.

## The store file

- **Where.** The web keeps it at `<data root>/.decisions/<slug>.jsonl`. The converter
  reads only the file it is given: `reflow convert --decisions FILE`, or
  `reflow convert-many --decisions-dir DIR`, which reads `DIR/<output folder name>.jsonl` for
  each document. A missing file is an empty store. A path that exists and is not a file is an
  error (exit 2) before any work.
- **Header.** The first line is
  `{"v": 1, "kind": "store", "id": "<16 hex, random>", "slug": "<slug>", "created": "<UTC ISO>"}`.
  The `id` names this store: a deleted and recreated store has a new one.
- **Lines.** One JSON object per line, UTF-8, ending in `\n`, at most 8 KB.
- **Write protocol.**
  1. Take `<store folder>/coordination.lock` with `flock` (exclusive).
  2. If the file does not exist, create it with `O_CREAT | O_EXCL` and write the header.
  3. If the file is not empty and its last byte is not `\n`, a writer died mid-line: write one
     `\n` first, so the torn bytes become one line that does not parse.
  4. Write the record line with one `write` on an `O_APPEND` descriptor, `fsync`, release the
     lock. A record is durable when the writer reports it.
- **Read protocol.** Read the file once, up to its size at that moment, and cut it after the last
  `\n`. The bytes after it are a torn tail: counted as `torn`, not read. A complete line that is
  not a JSON object is counted `unreadable`. If the first line is not a valid header, nothing in
  the file applies and the ledger says `"error": "no-header"`.

## The snapshot

`decisions.read(path) -> Snapshot` is the one read of a conversion:
`{store: <header id>, bytes: <offset after the last complete line>, lines: <complete lines>}`, and
the parsed records. A record is **pending** for a published build when the build's ledger names
another store id, or when the record's line starts at or after `bytes`. A record appended during a
build is therefore in exactly one build's snapshot, this one or the next.

## The record

Every record but the header carries `v`, `id`, `at`, `pdf` and `kind`:

```jsonc
{"v": 1, "id": "m2k9x4qq-ab12cd34", "at": "2026-10-28T09:14:03Z",
 "pdf": "<sha256 of the source PDF, 64 hex>",
 "kind": "correct" | "keep" | "alt" | "dismiss" | "undo" | "reattach" | "proposal" | "font-map",
 "target": {"anchor": {...}, "range": {...}},   // correct, keep: anchor and range; alt: anchor
 "now": "80110",                                // correct
 "alt": {"text": "…", "decorative": false},     // alt
 "findings": ["<fingerprint>", ...],            // dismiss
 "proposal": "pv-…", "verdict": "accept" | "reject", "text": "<sha256>",   // proposal
 "map": "fm-<16 hex>", "verdict": "accept" | "reject",                     // font-map
 "supersedes": "<id>",                          // optional, correct/keep/alt
 "of": "<id>",                                  // undo
 "from": "<sha256>", "to": "<sha256>"}          // reattach
```

- `id` is `<milliseconds since the epoch, base 36>-<8 random hex>`, minted by the writer.
- `pdf` is `doc.meta["sha256"]`, the hash of the file the user gave (not of the repaired working
  copy). The writer copies it from the ledger of the build the reviewer was looking at.
- No reviewer name in v1. The ledger is written from explicit field lists, so a field the record
  carries that the ledger does not list (a later `by`, an `email`) never reaches a published file.
- `range` is `{"was", "before", "after", "rule"}`: OCR's reading and up to 20 characters of the
  node's text on each side, copied from the published suggestion. Offsets are never stored.
- **Parsed once, at the boundary.** `decisions.parse_record` turns a JSON object into one typed
  record (`Correct`, `Keep`, `AltText`, `Dismiss`, `Undo`, `Reattach`, `ProposalVerdict`, `FontMapVerdict`) or
  refuses it. A record with an unknown `v` or `kind` is `unsupported` (kept in the store, listed,
  never applied). A known kind with a wrong field is `invalid`. `now` must be one non-empty line with no
  control or bidi character, at most 200 characters and at most four times the length of `was`,
  else `invalid-now` when it is matched. Nothing past the parser sees raw JSON.

## The fold

`decisions.fold(snapshot) -> Fold` is a pure function of the records in store order.

- **Duplicates.** The first record with an id wins; a later one is counted `duplicate`.
- **Families.** A decision replaces the active decision of its own family:
  - `correct` and `keep`: `(pdf, page, bbox rounded to 1 pt, text_sha, before, was, after)`, so
    a `keep` after a `correct` of the same range replaces it, and two equal address blocks on one
    page are two families;
  - `alt`: `(pdf, page, crop_sha)`;
  - `dismiss`: each fingerprint is its own family;
  - `proposal`: `(pdf, proposal id)`;
  - `font-map`: `(pdf, map id)`.
- **Supersession.** `supersedes: X` also replaces X when X is an earlier `correct`/`keep` (for a
  `correct`/`keep`) or an earlier `alt` (for an `alt`), across family keys. That is how a decision
  follows its target after it changed the node's text. A `supersedes` naming an unknown id, a later
  record or another kind is `invalid` and ignored; the record then stands alone.
- **Undo.** Each family keeps a stack. `undo {of: X}` takes X off its stack, so the decision X
  replaced is active again. Undo of an undo, a header, a `reattach`, an unknown id or a later record
  is `invalid`. A second undo of the same record does nothing.
- **Reattach.** `reattach {from: A, to: B}` makes the records made on PDF A eligible for PDF B.
  One hop: reattachments do not chain. Reattached records still go through full matching.
- The fold gives the active decisions in store order and the counts the ledger publishes.

## The PDF identity

Before anything matches, every record's `pdf` must be the current PDF's hash, or be reattached to
it. Otherwise it is orphaned `other-pdf`, and the document gets one warning,
`decisions-other-pdf`. A changed PDF never takes decisions on its own, even when its text and boxes
are the same. A converter change against the same PDF keeps the hash, so decisions keep applying.

## Anchors and matching, fail-closed

The converter mints every anchor; a writer only copies a published one. Review items for nodes and
figures carry `anchor`, and each suggestion carries `anchor`, `before`, `after` and `id`.

```python
@dataclass(frozen=True)
class Anchor:
    page: int                                   # 1-based
    bbox: tuple[float, float, float, float]     # rounded to 0.1 pt
    text_sha: str   # first 16 hex of sha256(NFC text, whitespace runs collapsed); "" for a figure
    crop_sha: str   # figures: first 16 hex of sha256(the crop's bytes); "" otherwise
    hint: str       # the node id when minted: shown to a person, never used to match
```

**Text decisions** (`correct`, `keep`) search the OCR nodes of the anchor's page, in the model as
it was before the pass changed anything. Tiers run in order:

| tier | candidate | note |
|---|---|---|
| exact | same `text_sha`, every box edge within 3 pt | |
| moved | same `text_sha`, box IoU ≥ 0.5 | ledger `moved` |
| rebased | box IoU ≥ 0.85, any text | ledger `rebased` |

- One candidate in a tier goes on to range location. Two or more stop the search: `ambiguous`,
  and weaker tiers are not tried. None lets the next tier run; after the last, `no-node`.
- The node id is never a shortcut: a node whose id is the `hint` gets no preference.
- **Range location.** `before + was + after` must occur exactly once in the raw `node.text`.
  Else `range-missing`. Text is normalized only to compare hashes, never to locate a range.
- **Only a flagged span.** The located range must be exactly a `doubt` mark of the node, a span
  reflow itself flagged (an OCR suggestion, or a doubtful OCR word). Anything else, a wider range
  included, is `not-flagged`.
- **Satisfied.** If the range is not found but `before + now + after` occurs exactly once in one
  node that passes the box test of the exact or moved tier (hash aside), the converter already
  reads the word right: `satisfied`, nothing changes. If both readings occur, `ambiguous`.
- The page is never relaxed, and the role is not compared (`p` → `h2` still matches).
- Every text node of the page with a box is a candidate, so a node that is not OCR still makes
  an OCR twin `ambiguous`. A correction whose one candidate is not `source == ocr` is `not-ocr`.

**Figures** (`alt`) match only by picture: the same page and `crop_sha`, on exactly one figure.
Two equal crops on one page are `ambiguous`; no figure with that crop is `figure-changed`.

**Proposals** keep page vision's own fail-closed check (`proposals.apply`): an accept applies
only while the document's text digest is the one the reviewer accepted against and the block
still has the role the change is for. Otherwise the proposal is `stale` and nothing changes. A
store's proposal decisions are layered over the carried `proposal-decisions.json`, the store's
winning for the same proposal id, and pass the PDF check first.

## Findings and dismissals

A dismissal settles one **finding**, never a whole item, code or page.

- `finding_id(scope, code, detail, data)` is the first 16 hex of sha256 over the canonical JSON
  of `[scope, code, detail, data]`. `scope` is a node's `[page, bbox, text_sha]`, `["page", N]` or
  `["doc"]`. A low-confidence item's finding is `[scope, "low-confidence", reasons]`. The language
  item's is `[["doc"], issue, declared, detected]`. When the evidence changes, so does the id, and
  the old dismissal is `stale`.
- Every review item lists `findings: [{"code", "severity", "id"}]`. A group item lists its
  members' findings.
- Dismissed fingerprints go into `doc.meta["decisions"]["dismissed"]`, so `review.items` stays a
  pure function of the model. It drops a dismissed finding before it picks an item's worst fact;
  an item with nothing left is not published.
- **Errors cannot be dismissed** (`refused-error`), nor can the `decision-*` warnings.
- Dismissals are settled against the model after the edits, so a dismissal of a finding on a node
  that a correction also changed is `stale` and its finding is reviewed again.
- Dismissing a finding does not change the node's score: a low-confidence finding is its own
  finding, dismissed on its own.
- A `correct` or `keep` settles only its own suggestion: that range's `ocr-suggestion` fact and its
  `doubt` mark.

## Applying

`decisions.apply` runs in `pipeline.finish` after `page_integrity.flag_empty`, with
`confidence.apply` just before it, and before `review.queue`:

```
enrich.describe → flag_empty → confidence.apply → decisions.apply → review.queue
```

1. **Revert** what an earlier pass left (`decisions.revert`), so applying twice equals once.
2. **Check identity** and **match** every active decision against the unchanged model.
3. **Conflicts.** Two decisions whose ranges overlap in one node are both `conflict`.
4. **Apply** each node's disjoint edits at once, last range first, with one `Node.set_text`.
5. **Settle** dismissals, and record the ledger and the orphans in the model.

| kind | page | model |
|---|---|---|
| `correct` | `was` becomes `now` | a `fix` mark over `now` with `alt = was`; a `reviewed-correction` fact (`human`); that range's `doubt` mark and `ocr-suggestion` fact removed; `attrs["lines"]` edited on the line that holds it; `source` stays `ocr` |
| `keep` | unchanged | that range's `doubt` mark and `ocr-suggestion` fact removed; a `reviewed-kept` fact |
| `alt` | the reviewer's alt, `""` when decorative | `alt`, `decorative`, `provenance = "reviewer"`, `reviewed = True`; AI fields and facts removed; a `reviewed-alt` fact; `source = human` |
| `dismiss` | unchanged | the fingerprint joins `meta["decisions"]["dismissed"]` |
| `font-map` accept | unchanged: the decoded text | the `font-map` finding joins `dismissed` |
| `font-map` reject | every node the font repair changed reads the text layer again | `text` and `lines` back to `attrs["layer_text"]`, the title too when it came from one; the prior state in `attrs["undecided"]`; the `font-map` finding joins `dismissed` |

- **Print only what the page says.** A correction changes only a span reflow itself flagged.
- **OCR's reading is kept** in the `fix` mark's `alt` (the page shows it), the fact, the ledger and
  the store.
- **A font map** is a decision on the whole repair, not on a word: its id is the
  `font-map` fact's `map`, a hash of the patched codes. When the repair changed nothing, or
  changed other codes, the record is `stale`. A reject restores only nodes whose text layer
  reading has the same length as the decoded text, which is every node the patch changed, since
  it swaps one letter for one letter.
- **The title** follows a correction of the heading it came from; otherwise the ledger entry says
  `title_unchanged`. Node ids never change, so heading anchors do not move.
- **The record of the change.** The first decision on a node stores its prior state in
  `attrs["undecided"]`; the prior title goes to `root.attrs["undecided_title"]`.
  `revert(apply(m)) == m`, compared by `Document.to_json`, is a test. Replaying a store on a saved
  model uses `revert`.
- **Determinism.** Without `--decisions`, the output is what it was. The pass is serial and reads
  only the model, the crops and the snapshot, so `--jobs N` equals `--jobs 1`.

## Provenance

- **Inline.** A correction renders as
  `<span class="reviewed-fix" data-was="8011O" title="Corrected by a reviewer. OCR read: 8011O">80110</span>`,
  dotted underline in the viewer CSS.
- **Page note.** `<p class="review-note" role="note" data-generated>A reviewer corrected 1 word on
  this page. OCR read ‘8011O’; the page shows ‘80110’.</p>` after the page label, at most five
  words then a count. Find skips it (`data-generated`); the corrected word is page text and is
  found.
- **Figures.** `figure_alt` checks the `reviewer` source first; the `<img>` gets
  `data-alt-by="reviewer"` and no AI badge. A reviewer's alt is never an Ask passage.
- **Ask.** A corrected word stays quotable. A passage line gains `f: [[start, end, was], ...]`
  only when it holds a correction.
- **The ledger**, `decisions.json`, a public file written whenever a decisions file was given:

  ```jsonc
  {"v": 1, "source": {"sha256": "…"},
   "store": {"id", "bytes", "lines", "torn", "unreadable", "duplicate",
             "invalid": [...], "unsupported": [{"id", "v", "kind"}], "error"},
   "applied":   [{"id", "kind", "page", "bbox", "node", "was", "now", "tier", "title_unchanged"}],
   "satisfied": [{"id", "page", "node", "now"}],
   "dismissed": [{"id", "page", "code", "finding"}],
   "refused":   [{"id", "finding", "reason"}],
   "orphaned":  [{"id", "kind", "page", "bbox", "reason", "was", "now", "text_now"}],
   "stale":     [{"id", "kind", "finding"}],
   "proposals": [{"id", "proposal", "verdict"}],
   "fonts":     [{"id", "map", "verdict", "nodes"}]}   // only when a font-map record applied
  ```

- `reflow convert` prints `decisions: 2 applied, 1 dismissed, 1 orphaned`.
- The ledger also lives in the model, as `document.json`'s `meta["decisions"]`, so a saved model
  publishes the same `decisions.json`. With a missing or empty store, `document.json` differs from
  a conversion without the option only by that key.

## When a decision no longer matches

Never applied, never deleted, always visible. The reasons are `other-pdf`, `no-node`,
`ambiguous`, `range-missing`, `not-flagged`, `conflict`, `not-ocr`, `invalid-now`, `figure-changed` and
`refused-error`. An orphaned correction, keep or alt becomes its own review item
(`decision:<id>`, role `decision`, the anchor's page and box, no node), because the page is
uncertain again. A dismissal whose finding is gone is listed `stale`, not queued. Orphans never
fail a conversion.

## Recording decisions from the web

The web app records decisions through `GET` and `POST /api/decisions/<slug>`
(`web/src/lib/decisions/`). It follows the write protocol and the fold above in TypeScript, and
`web/src/lib/decisions/fold.test.ts` folds every file in `tests/fixtures/decisions/` and compares
the result with its `.expected.json`, as the converter's test does, so the two cannot drift.

- **Routes.** `GET` answers the folded store, with each decision's status for the published build
  (`pending`, `applied`, `satisfied`, `dismissed`, `orphaned`, `stale`, `refused`, `undone`), the
  count of pending records, the build's `source.sha256` and the ledger's `store` snapshot. `POST`
  takes one choice (`accept`, `keep`, `other`, `alt`, `dismiss`, `undo`, `reattach`) and answers
  201 `{"decision": "<id>", "pending": n}` only after the record is durable. A decision never starts
  a build. The reviewer decides several things and then rebuilds, and every build (the page's button
  or `bun run docs:build`) passes `--decisions-dir <data>/.decisions`.
- **The gate.** The builds switch (`REFLOW_BUILDS=1`, or a development server) and no other. Off,
  every `/api/decisions` route is an empty 404.
- **The boundary.** Every route, reads included, requires a loopback `Host` (`localhost`,
  `127.0.0.1` or `[::1]`, with an optional port). A `POST` needs an `Origin` equal to `Host`, a
  `Sec-Fetch-Site` of `same-origin` when it is sent, `application/json`, and at most 8 KB. The slug
  must be a known document. *This stops* a cross-site page, a DNS-rebinding page and a page on
  another origin of the same machine. *It does not stop* a program that can reach the port and sends
  `Host: localhost` with a matching `Origin`: on the machine that is any local process, and on a
  server that listens on a network interface it is any host on that network. So the server listens
  on `127.0.0.1` (`bun run dev` does; the production line in `docs/builds.md` passes `-H`), and a
  reviewer on another machine needs an authenticated design that this does not provide.
- **Bound to the build the reviewer saw.** The client sends the manifest's `v` (`rev`), the review
  item's id and the id of the suggestion or finding it chose. The server reads `manifest.json`,
  `review.json` and `decisions.json` without the cache, between two reads of the publication record,
  and answers 409 if a publication is under way or the generation changed. A `rev` that is not the
  snapshot's, or an item, suggestion or finding that is not in it, is also a 409, and nothing is
  written. The record's anchor and range come from the published suggestion and its `pdf` from
  `decisions.json`, never from the client. A document built before decisions (no `decisions.json`)
  is a 409: rebuild it once.
- **No reviewer name.** The body has no name field (a field the server does not know is a 400),
  and the record holds no `by`.

### The controls

All of them render only when the builds are on (`useBuilds()` is not null) and the page knows the
build it shows (the manifest `v`, sent as `rev`). They are real buttons and labelled fields, and
every result goes to a polite live region.

- **Inspector, under "Review issue".** An OCR suggestion offers *Accept ‘…’*, *Keep ‘…’* and
  *Another reading…* (one labelled field, with its length limit in its description). A figure
  offers an *Alt text* textarea, a *Decorative* checkbox and *Save alt text*. Each warning finding
  gets a *Dismiss: <label>* button; an error gets none, and the panel says "Errors are fixed, not
  dismissed". An orphaned decision offers *Re-decide* (links to the page's other issues with
  `?supersedes=<id>`, so the new choice replaces it, and offers *Use the previous alt text* for an
  orphaned alt) and *Remove* (an undo). `decisions-other-pdf` offers *Try them on this PDF*.
- **A decided node.** A node that has a `reviewed-correction`, `reviewed-kept` or `reviewed-alt`
  fact says "Corrected by a reviewer. OCR read ‘…’." with *Change* and *Undo*.
- **Document page.** A *Decisions* section beside the issue list holds *Rebuild*, the line "N
  decisions will apply at the next build" and every decision with its status in words, a link to
  its page in the inspector, *Undo* and *Change*. An issue whose decision waits for the next build
  carries a "Decided: …" badge. N counts the decisions that change the next build: an undone
  decision that no build has seen does not count.
- **What the reader hears.** "Saved. Takes effect when you rebuild." after a write. A 409 shows
  the server's sentence and a *Reload* button, and writes nothing.
- **Items with no node.** An orphaned decision has role `decision`, a page and a box. The
  inspector draws the box and selects no node; the page preview draws the box alone.

The web app's browser checks ran on a scratch data root (`REFLOW_DATA_ROOT`): accepting ‘80110’
wrote one line, a rebuild showed `.reviewed-fix` `80110` with `data-was="8011O"` and the note "OCR
read ‘8011O’", an undo and a rebuild brought back `8011O` with its doubt mark, a tab that was open
before a rebuild got the reload sentence and wrote nothing, and an alt text became the figure's
`alt` with `data-alt-by="reviewer"`.

## Applying decisions without a full conversion

A decision changes only the tail of the model: nothing before the decisions pass reads a store. So
the converter does not repeat the model for a new store; it replays the store on the model it
saved.

- **The store is not part of the model key** (`Options.decisions` is not compared), so a
  conversion with `--decisions` and `--stage-cache` saves its model like any other, and the next
  conversion of the same PDF restores it.
- **One replay for both paths.** `pipeline.decide(run)` is the tail of `pipeline.finish`:
  `decisions.revert`, then, when the run has a decisions file, `confidence.apply` and
  `decisions.apply`, then `review.queue`. `saved.Reuse.restore` runs it through
  `pipeline.redecide` on every restored model (the previous output or the stage root), then
  rewrites `document.json` and `review.json` in the stage. The saved model may hold the decisions
  of the build that made it; `revert` takes them back first. A model with none, restored by a run
  with no store, is left as it is. `Reuse.close` records the digest of what the stage then holds.
- **The page images are carried** when they cannot differ. On a restore from the previous
  output, if its `build.json` render key equals this run's and its `pages/` holds exactly the
  files this run would write, `pages/` is cloned into the stage and nothing is rendered.
  Otherwise they are rendered as always.
- **The result equals a full build.** `tests/test_rerender.py` converts with a store S0, then with
  S (more decisions and an undo of one of S0's) into the same folder, which must reuse the model,
  and compares it with a conversion of the same PDF with S, stage cache off: `document.json`,
  `review.json`, `decisions.json`, `passages.jsonl`, `index.html`, `chunks/`, `layout/`,
  `figures/`, `trace/` and the manifest's `v` are byte-identical, on a born-digital PDF with
  figures and on `samples/scan.pdf`. An undone correction brings OCR's reading, its doubt mark
  and its review item back.

`reflow rerender <out> --decisions FILE [--stage-cache DIR] [--source PDF [--engine E]
[--alt-text P]] [--progress-json]` does the replay without the PDF at hand and without any
conversion option. `build.json` records the conversion's
options (`options`) and what its model key hashed (`model.inputs`: code, tools, options, OCR data);
its `source.name` is the name the PDF had, which the key hashes with the PDF's bytes, so the
command links `<out>/original.pdf` under that name. It runs `convert` with the recorded options and
`must_reuse`, which raises before any work if the saved model cannot be proved. That proves the
output true to the PDF and options it was made from, not that they are still the ones a caller
would convert. `--source` says which: the command runs only when that PDF has the recorded name
and bytes and `--engine` and `--alt-text` (their defaults when not given) are the recorded ones,
and exits 4 with `pdf` or `options` otherwise. `docs:build --rerender` passes the sample's PDF and
its own options, so a sample replaced since the conversion, or another `REFLOW_ENGINE` or
`REFLOW_ALT_TEXT`, sends the job to a full build.

| exit | meaning |
|---|---|
| 0 | done; prints the review and decisions lines of `convert` |
| 2 | `<out>` holds no conversion (no readable `build.json`, `document.json` or `original.pdf`), `FILE` is not a readable file, or `<out>` holds foreign files |
| 4 | a full conversion would build another model: `pdf` (`original.pdf`, or the `--source` PDF, is not the converted PDF), `options`, `code`, `tools` (their versions, or the OCR runner's data), `record` (a `build.json` from before options were recorded) or `model`; `<out>` is unchanged |

With `--progress-json` it prints `start`, the JSON `progress` lines and `ok` or `FAILED` (with the
reason after `CannotRerender:`), as `convert-many` does. A rerender always renders the site with
the current renderer, so a render-only change needs no full conversion either. Any change to the
code the model runs (`reflow.pipeline`, `reflow.prepare` and what they import) is exit 4. The
reason is deliberate: a model made by other code is not the model a full build makes.

Measured on `samples/docaccess/09-scanned-budget-handwriting.pdf` (12 pages), scratch folder,
warm machine: a full `reflow convert --stage-cache` took 6.8 s (stages: ocr 3.9 s, extract 0.6 s,
layout 0.2 s, figures 0.5 s, publish 0.1 s, 5.6 s in all); `reflow rerender` with a one-decision
store took 2.1 s, of which the stages are restore 0.02 s, decisions 0.03 s, review 0.03 s and
publish 0.07 s. What is left is starting Python and loading the converter.

### From the web app

The document's Decisions section has an *Apply decisions* button beside *Rebuild*, shown only when
builds are on and a decision is waiting. It queues a `rerender` job (`POST /api/builds {slug,
kind: "rerender"}`, [builds.md](builds.md#a-job-is-a-build-or-a-rerender)), which runs
`bun scripts/build-docs.ts --rerender <slug>`: `reflow rerender` on the document's store, then the
public files and the carried audit are published. Nothing else starts one: recording a decision
never does. A conversion given a decisions file records `build.json` even without the stage cache,
so a page build can be rerendered from its own output. When the saved model cannot be proved (no
`build.json`, from a build before this; or exit 4), the job runs a full build, which applies the store too. The
browser check ran on a scratch data root: a conversion of `da-scanned-budget-handwriting` with
`--stage-cache` took 7.8 s wall (6.96 s as a job); accepting ‘80110’ on page 5 and pressing *Apply
decisions* gave a job labelled "Applying decisions" that took 1.7 s with no OCR (the converter's
own stages: restore 0.01 s, decisions 0.0 s, review 0.01 s, publish 0.03 s), and
`/view/da-scanned-budget-handwriting?page=5` then showed `.reviewed-fix` `80110` with
`data-was="8011O"`.

## Changes from the first plan

The code moved after the store was first planned. The web app's recording and applying follow these:

- **The saved model.** The decisions pass is a model pass, so a model built with decisions
  holds them. A store is not a model input, so a conversion given `--decisions` saves and reuses a
  model like any other; a reused model is made a valid base again by `revert` and then takes the
  store (see "Applying decisions without a full conversion").
- **Proposal decisions** are one more record kind, `proposal`, so the web can record
  an accept or reject in the same store. `reflow review --accept/--reject` still writes the carried
  `proposal-decisions.json`; the store's decision wins for the same proposal.
- **Anchor tolerances, measured** (step 1 of the plan). 1,795 decisions minted on two older builds
  of eight OCR documents and matched against a newer one: 1,203 exact,
  2 moved, 56 rebased, 27 figures; 367 `no-node`, 48 `range-missing`, 5 `not-ocr`, 19
  `figure-changed`, none ambiguous. All 56 rebased matches and 50 exact or moved ones were
  checked by eye against the page images: no false application. The closest same-text pair on a
  page sits 3.26 pt apart, so the 3 pt edge tolerance must not grow.
- **Figure identity.** The crop's hash is stored on the figure as `attrs["crop_sha"]` when the crop
  is made, so the review queue can publish a figure's anchor without reading files.
- `build.json` and `publish(run, model)` already exist; the web app's *Apply decisions* builds on them.
