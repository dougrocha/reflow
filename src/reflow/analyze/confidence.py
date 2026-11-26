"""Confidence from evidence: the one place that says what a Fact is worth.

`node.confidence` is a constant chosen where the node is built (layout.py's CONF_*,
docling_layout.CONF_DOCLING, enrich.CONF_MODEL). Here that constant becomes the engine's
*prior*, recorded as a Fact so the ledger is complete, and the published confidence is a
function of the whole ledger:

    confidence = clamp(prior + Σ CODES[f.code].weight for f in facts, 0, 1)

Each role has its own review threshold (ROLE_THRESHOLD). The registry below is also the one
source of the review labels and severities: review.json carries `label`.

Calibration is a test, not a formula (tests/pages/test_calibration.py): at least 80% of the
page-check xfail pages must carry a review item on that page, and the budget-appendix and p17
item counts must leave the single digits. Tune `weight` and `ROLE_THRESHOLD` against that test.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from reflow import geometry
from reflow.analyze.constants import SPLIT_SOFT, UNHELD_WHY, reading_order_why
from reflow.extract.tables.cells import TableMethod
from reflow.model import Document, Fact, Node, Role, Source, code_of

Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class CodeSpec:
    label: str                  # what the review queue shows
    weight: float               # added to the prior; negative lowers confidence
    severity: Severity | None   # a review item on its own, regardless of the score
    # Which scope the code applies to, for the registry test: "node", "page" or "doc".
    scope: Literal["node", "page", "doc"] = "node"


# Every code a Fact may carry. A code absent here is a bug (confidence.apply raises).
CODES: dict[str, CodeSpec] = {
    # engine priors (recorded by apply itself, weight 0: the prior is the base)
    "prior": CodeSpec("Engine prior", 0.0, None),
    "table-giant-cell": CodeSpec("Table that is one giant cell", -0.3, "warning"),
    "calendar-grid-mismatch": CodeSpec("Printed month whose dates are not the month's", -0.3,
                                       "warning"),
    # what the colour key and the dated list say about each day (analyze.calendar.annotate):
    # one fact per month table or per page, never one per day. The page's two sit on its first
    # month table, so the review item points at the calendar.
    "calendar-colour-unmapped": CodeSpec("Coloured days that the colour key does not name", 0.0,
                                         "warning"),
    "calendar-list-mismatch": CodeSpec("Dated list that disagrees with the calendar's colours",
                                       0.0, "warning", "page"),
    "calendar-list-unparsed": CodeSpec("Dated list lines that could not be read", 0.0,
                                       "warning", "page"),
    "empty-transcript": CodeSpec("Non-blank page has an empty transcript", 0.0, "error", "page"),
    "ocr-missing": CodeSpec("Image-only page without OCR", 0.0, "error", "page"),
    # the prompt-only pipeline (analyze.direct)
    "direct-unread": CodeSpec("The model did not transcribe this page", 0.0, "error", "page"),
    "direct-truncated": CodeSpec("The model's transcript of this page stops early", 0.0,
                                 "warning", "page"),
    "direct-repaired": CodeSpec("Markup the model wrote outside the allowed elements was "
                                "removed", 0.0, None, "page"),
    # heuristics + AI polish (analyze.polish): what the edit was; ai-drafted is the warning
    "ai-polished": CodeSpec("Structure changed by AI polish", 0.0, None),
    "ocr-page": CodeSpec("Text recognized by OCR", -0.25, None, "page"),
    "ocr-image-text": CodeSpec("Text recovered from an image", 0.0, "warning"),
    "ocr-doc": CodeSpec("Document has image-only pages", 0.0, "error", "doc"),
    # the OCR plan: a page image band with ink and no text that was not read
    "ocr-band-missing": CodeSpec("Part of the page image holds text that was not read", 0.0,
                                 "warning", "page"),
    # the text-trust preflight (ingest.text_trust, the only writer): weight 0, so no node drops
    # under its threshold and the calibration budgets don't move; page trust is read from these
    "text-disputed": CodeSpec("OCR of the page reads other words than its text layer", 0.0,
                              "warning", "page"),
    "text-witness-unavailable": CodeSpec("Text layer looks wrong and could not be checked by OCR",
                                         0.0, "warning", "page"),
    # a form flattened into the page (ingest.flattened, the only writer): its fields are
    # drawn, so the transcript has none to fill. Weight 0: the page's text is as trustworthy
    "form-flattened": CodeSpec("Form was flattened: its fields are drawn on the page and the "
                               "PDF has none to fill", 0.0, "warning", "page"),
    # the font repair (ingest.font_map, the only writer): a decoded font stays in the queue
    # until a reviewer accepts or rejects it; weight 0, as the preflight's codes
    "font-map": CodeSpec("Text decoded from a font whose text layer was wrong; accept or reject "
                         "it", 0.0, "warning", "doc"),
    "font-ambiguous": CodeSpec("A font's text layer looks wrong and was not decoded", 0.0,
                               "warning", "doc"),
    # words OCR reads on a born-digital page that its transcript lacks (text_trust.flag_missing)
    "words-missing": CodeSpec("Words printed on the page are missing from the transcript", 0.0,
                              "error", "page"),
    # the page scan
    "scan-noisy": CodeSpec("Page image is too grainy to find pictures; check for missing figures",
                           0.0, "warning", "page"),
    "ocr-in-picture": CodeSpec("Text read inside a picture, listed under the figure; check it",
                               -0.1, "warning"),
    # unread figures: writing no text transcribes, shown as a crop
    "unread-handwriting": CodeSpec("Handwriting not transcribed", 0.0, "error"),
    "unread-text": CodeSpec("Writing OCR could not read", 0.0, "error"),
    # a signature is expected to stay unread: a check, not an error, one per signature
    "unread-signature": CodeSpec("Signature not transcribed", 0.0, "warning"),
    # cross-engine and tag agreement
    "order-disagreement": CodeSpec("Reading order disputed by another engine", -0.3,
                                   "warning", "page"),
    # Docling's order is a weaker witness than the author's tags (it misreads some multi-column
    # pages): it only corroborates other doubts, never makes a review item on its own.
    "order-disagreement-docling": CodeSpec("Reading order differs from Docling's", -0.1,
                                           None, "page"),
    "order-unchecked": CodeSpec("Reading order not checked against the tag tree", 0.0,
                                None, "doc"),
    "role-agreement": CodeSpec("Role confirmed by the tag tree", 0.1, None),
    "role-disagreement": CodeSpec("Role disputed by the tag tree", -0.1, None),
    "level-disagreement": CodeSpec("Heading level differs from the bookmarks", -0.2,
                                   "warning"),
    "levels-unreliable": CodeSpec("Heading levels follow a different scheme than the PDF's outline",
                                  0.0, None, "doc"),
    "untagged": CodeSpec("No tag covers this block in a tagged document", -0.1, None),
    "tag-alt": CodeSpec("Alt text taken from the PDF tags", 0.3, None),
    "tag-scope": CodeSpec("Header scope taken from the PDF tags", 0.2, None),
    "tag-heading": CodeSpec("Heading role taken from the tag tree", 0.2, None),
    "bookmark-level": CodeSpec("Heading level taken from the bookmarks", 0.2, None),
    "link": CodeSpec("Link from the PDF", 0.0, None),
    # a page link whose page the document does not have: published as plain text
    "link-unresolved": CodeSpec("Link to a page this document does not have, shown as plain text",
                                0.0, "warning"),
    "docling-rejected": CodeSpec("Docling's table or figure was not adopted", -0.1,
                                 "warning"),
    # heuristic self-doubt: the signals layout.py already computes
    "soft-columns": CodeSpec("Columns spanned by a table or callout", -0.15, None, "page"),
    # a ruled box of text that was not grouped, because its text or order is in doubt
    "ruled-box-doubt": CodeSpec("Text in a ruled box not shown as one group; check its order",
                                -0.1, "warning"),
    "aligned-table": CodeSpec("Table found from text alignment alone", -0.1, None),
    "header-guess": CodeSpec("Header row is a guess", -0.05, None),
    "unheld-lines": CodeSpec("Lines inside the table that no cell holds", -0.2, "warning"),
    "rare-heading-style": CodeSpec("Heading style used once or twice", -0.1, None),
    "unicode-errors": CodeSpec("Characters the font could not map", -0.2, "warning"),
    # structural flags (table_evidence.flag_structure)
    "table-collapsed": CodeSpec("Table columns probably collapsed", -0.3, "warning"),
    "heading-fragment": CodeSpec("Heading looks like a fragment or a label", -0.3,
                                 "warning"),
    "demoted-heading": CodeSpec("Styled like the headings in its column, but not a heading",
                                -0.2, "warning"),
    # text evidence (analyze.text_evidence)
    "unfinished-sentence": CodeSpec("Block stops mid-sentence", -0.3, "warning"),
    # An error: the words are demonstrably misspelled, and a person must fix them.
    "dropped-ligature": CodeSpec("Words missing the letters of a fi ligature", -0.3, "error"),
    "literal-bullets": CodeSpec("Bullet glyphs left in the text instead of a list", -0.2,
                                "warning"),
    "split-list": CodeSpec("List that continues the list above it", -0.2, "warning"),
    "empty-header-row": CodeSpec("Table header row is empty", -0.2, "warning"),
    # an OCR word mark (doubt, ins, del) that a text edit could not carry over (Node.set_text)
    "ocr-mark-lost": CodeSpec("A word mark could not be placed", -0.1, "warning"),
    # OCR word grades (ingest.ocr_trust and text_evidence.flag_ocr_marks)
    "ocr-doubtful": CodeSpec("OCR unsure of some words", -0.1, "warning"),
    "ocr-words-omitted": CodeSpec("Words OCR could not read are not shown", 0.0, "error",
                                  "page"),
    "ocr-grades-unmatched": CodeSpec("OCR word grades did not match the text", 0.0, "warning",
                                     "page"),
    # tracked changes on a scanned page (text_evidence.flag_ocr_marks)
    "struck-word-unread": CodeSpec("Struck word not transcribed", 0.0, "warning"),
    "tracked-change-unplaced": CodeSpec("Tracked change not marked in the text", 0.0, "warning",
                                        "page"),
    # a heading on a scanned page seen only on its image (headings.ink_cue)
    "heading-from-ink": CodeSpec("Heading found from the page image", 0.0, "warning"),
    # a likely OCR slip with a reading the document itself suggests (analyze.ocr_words)
    "ocr-suggestion": CodeSpec("OCR reading may be wrong", -0.1, "warning"),
    # the title (analyze.title via evidence.witness): which candidate won, for Summary
    "title": CodeSpec("Document title chosen", 0.0, None, "doc"),
    # things a person must add
    "alt-missing": CodeSpec("Figure missing alt text", 0.0, "error"),
    "label-missing": CodeSpec("Form control has no accessible name", 0.0, "error"),
    "title-suspect": CodeSpec("Document title looks wrong", 0.0, "error", "doc"),
    "extract-forbidden": CodeSpec("The PDF forbids text extraction", 0.0, "error", "doc"),
    # alt text drafts (enrich.py)
    "ai-drafted": CodeSpec("AI-drafted, not reviewed", 0.0, "warning"),
    "label-assisted": CodeSpec("Form control named by AI from the printed captions, to check",
                               0.0, "warning"),
    "field-assisted": CodeSpec("Form field found on a scanned page by AI, to check",
                               0.0, "warning"),
    "scan-fields": CodeSpec("Form fields on this scanned page were found by reading its image; "
                            "check them against the page", 0.0, "warning", "page"),
    "scan-fields-skipped": CodeSpec("Finding form fields on this scanned page failed, so it may "
                                    "have fields that are missing", 0.0, "warning", "page"),
    "structure-assisted": CodeSpec("Form line judged by AI to be a label or value, not a "
                                   "heading; to check", 0.0, "warning"),
    "ai-assist-failed": CodeSpec("AI transcription failed", 0.0, "warning", "page"),
    # a page-vision proposal a reviewer accepted and reflow applied (analyze.proposals):
    # already reviewed, so not a review item
    "vision-accepted": CodeSpec("Structure change accepted in review", 0.0, None),
    # opt-in form repair (analyze.form_repair): review items only, no text changes
    "form-repair-proposal": CodeSpec("AI form repair: text OCR missed, read by the AI", 0.0,
                                     "warning", "page"),
    "form-repair-unresolved": CodeSpec("Text OCR missed, which the AI could not read", 0.0,
                                       "warning", "page"),
    # page vision's complexity flags (analyze.page_vision): one item per page, and dense
    # tables on more than a third of the pages as one document item. They route a page to a
    # reviewer and change nothing, so they weigh nothing.
    "vision-complexity": CodeSpec("AI page check: a complex page to check against the PDF",
                                  0.0, "warning", "page"),
    "vision-dense-tables": CodeSpec("AI page check: dense tables on many pages", 0.0,
                                    "warning", "doc"),
    # the model read a block as another kind: shown in the inspector, never a review item
    "vision-block": CodeSpec("AI page check read this block as another kind", 0.0, None),
    # a figure with no alt marked decorative before drafting: a ruling, a blank band, a band
    # across the page edge, a crop repeated on several pages or a tiny one (enrich.mark_roles);
    # its reason is in the fact
    "decorative-guessed": CodeSpec("Figure marked decorative from its size, place or repetition, "
                                   "to check", 0.0, "warning"),
    "suggested-alt": CodeSpec("Suggested alt text to check", 0.0, "warning"),
    "suggested-description": CodeSpec("Suggested description to check", 0.0, "warning"),
    "suggested-alt-uncertain": CodeSpec("Uncertain suggested alt text", -0.2, "warning"),
    # a reviewer's decisions (analyze.decisions): what they settled is already reviewed,
    # so it weighs nothing and is no review item; a decision that no longer matches the page,
    # or a store that could not be read, is a warning
    "reviewed-correction": CodeSpec("Word corrected by a reviewer", 0.0, None),
    "reviewed-kept": CodeSpec("OCR reading confirmed by a reviewer", 0.0, None),
    "reviewed-alt": CodeSpec("Alt text written by a reviewer", 0.0, None),
    "decision-orphaned": CodeSpec("A reviewer's decision no longer matches the page", 0.0,
                                  "warning", "doc"),
    "decisions-other-pdf": CodeSpec("Decisions were made on another PDF and were not applied",
                                    0.0, "warning", "doc"),
    "decisions-unreadable": CodeSpec("Part of the decisions file could not be read", 0.0,
                                     "warning", "doc"),
    # language (analyze.language)
    "language-missing": CodeSpec("Document language not set", 0.0, "warning", "doc"),
    "language-mismatch": CodeSpec("Document language contradicts the text", 0.0,
                                  "warning", "doc"),
    "block-language": CodeSpec("Block in another language", 0.0, None),
    # derived by review.items, never appended to a ledger
    "low-confidence": CodeSpec("Low-confidence structure", 0.0, None),
}

# Below these, a node is a review item even with no severity-bearing fact. Structure that
# the heuristics guess (tables, columns) sits close to its prior; a single doubt crosses.
ROLE_THRESHOLD: dict[Role, float] = {
    Role.TABLE: 0.55,
    Role.TH: 0.55,
    Role.FIGURE: 0.5,
    Role.H1: 0.5, Role.H2: 0.5, Role.H3: 0.5, Role.H4: 0.5, Role.H5: 0.5, Role.H6: 0.5,
    Role.LIST: 0.45,
    Role.LI: 0.45,
    Role.P: 0.4,
    Role.FIELD: 0.5,
    Role.GROUP: 0.5,
}
DEFAULT_THRESHOLD = 0.45


@dataclass(frozen=True)
class Score:
    value: float
    reasons: tuple[str, ...]   # CODES labels of the facts that moved it, strongest first

    def below(self, role: Role) -> bool:
        return self.value < ROLE_THRESHOLD.get(role, DEFAULT_THRESHOLD)


def prior_fact(node: Node) -> Fact:
    """The node's constructor confidence, as the first Fact of the ledger."""
    return Fact("prior", node.source, f"{node.source.value} prior",
                {"value": round(node.confidence, 3)})


def score(facts: Sequence[Fact], page_facts: Sequence[Fact] = ()) -> Score:
    """The published confidence from a node's facts (and its page's, whose weights apply
    to every node on the page). Raises KeyError for a code missing from CODES."""
    prior = 1.0
    seen_prior = False
    total = 0.0
    moved: list[tuple[float, str, str]] = []
    for fact in (*facts, *page_facts):
        spec = CODES[fact.code]
        if fact.code == "prior":
            if not seen_prior:
                seen_prior = True
                prior = float(fact.data.get("value", 1.0))
            continue
        total += spec.weight
        if spec.weight:
            moved.append((-abs(spec.weight), fact.code, spec.label))
    moved.sort(key=lambda m: (m[0], m[1]))
    value = round(min(1.0, max(0.0, prior + total)), 6)   # no float noise at a threshold
    return Score(value, tuple(label for _, _, label in moved))


def page_facts_of(node: Node, facts: Sequence[Fact]) -> list[Fact]:
    """The page facts that weigh on `node`: all of them, except that one recording the regions
    it found (data["region_boxes"], four numbers per box) weighs only on nodes that overlap one
    of them, or have no box. A page OCR'd in redo mode for its textless bands keeps its own
    visible text: only what was read in the bands is OCR text."""
    out = []
    for fact in facts:
        flat = fact.data.get("region_boxes")
        if flat and node.bbox is not None and not any(
                geometry.overlap(node.bbox, tuple(flat[i:i + 4])) > 0
                for i in range(0, len(flat), 4)):
            continue
        out.append(fact)
    return out


def apply(doc: Document) -> dict[str, int]:
    """Set `node.confidence` on every node from its ledger. The only writer of confidence
    after the engines ran. Idempotent: a node that already has a "prior" fact keeps it.
    Returns the count of nodes per role that fell below their threshold."""
    page_facts = {p.number: p.facts for p in doc.pages}
    below: dict[str, int] = {}
    for node in doc.root.walk():
        if node is doc.root:
            continue
        if not any(f.code == "prior" for f in node.facts):
            node.facts.insert(0, prior_fact(node))
        node.facts.extend(legacy_facts(node))
        result = score(node.facts, page_facts_of(node, page_facts.get(node.page, ())))
        node.confidence = result.value
        if node.role != Role.ARTIFACT and result.below(node.role):
            below[node.role.value] = below.get(node.role.value, 0) + 1
    return below


NEEDS_CODES = {"alt": "alt-missing", "label": "label-missing"}


def legacy_facts(node: Node) -> list[Fact]:
    """Facts for what engines record only in attrs: attrs["needs"], attrs["warn"] and the
    attrs["why"] entries that carry a Fact's code (UNHELD_WHY, the soft reading order: see
    model.Why). Codes the node's ledger already holds are not repeated."""
    attrs, source = node.attrs, node.source
    have = {f.code for f in node.facts}
    why = [w for w in attrs.get("why", []) if isinstance(w, str)]
    found: list[Fact] = []

    def add(code: str, detail: str = "", **data: object) -> None:
        if code not in have:
            have.add(code)
            found.append(Fact(code, source, detail, dict(data)))

    needs = attrs.get("needs")
    if needs:
        if needs not in NEEDS_CODES:
            raise KeyError(f"unknown attrs['needs'] value: {needs!r}")
        add(NEEDS_CODES[needs])
    warn = attrs.get("warn")
    if warn:
        if warn not in CODES:
            raise KeyError(f"unknown attrs['warn'] value: {warn!r}")
        add(warn)
    if node.role == Role.TABLE:
        if attrs.get("method") == TableMethod.ALIGNED:
            add("aligned-table", why[0] if why else "")
        if source == Source.HEURISTIC and attrs.get("header_rows"):
            add("header-guess", f"{attrs['header_rows']} header row(s) guessed")
    marked = {code_of(w) for w in why}   # the layout rules that wrote a code beside their words
    if "unheld-lines" in marked:
        add("unheld-lines", UNHELD_WHY)
    if "soft-columns" in marked:
        add("soft-columns", reading_order_why(SPLIT_SOFT))
    errors = attrs.get("unicode_errors", 0)
    if errors and errors > 0:
        add("unicode-errors", f"{errors} characters the font could not map", count=errors)
    return found
