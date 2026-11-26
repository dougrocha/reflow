"""The document title, chosen from evidence rather than font size alone.

The one title chooser: it takes the largest-font heading on the first page with headings only as
one candidate among several, so naca-ocr.pdf does not publish "6'17 #i 5". This module scores
the candidates and records why, and reflow.analyze.evidence calls it.

Candidates, each with its own prior: the XMP/Info title cleaned of the tool prefix and file
extension it carries (meta), the first bookmark when it sits on page 1, the H1 Docling found, the
title the first two pages print in their largest top line or their centred capitals (cover, read
from the page's lines, so it needs no heading), the largest heading on the first text page
followed by the remaining headings, and the file name as the last resort. A display title set
over two sizes ("Prior Authorization Request for" above "Medications, DME, and Medical Devices")
is joined into one candidate, so the title is not just its larger half. Each is scored on letter
share, OCR quality, unicode errors, word count, position and agreement with the others, and every
line a joined title is made of must pass the OCR gate. A smaller line under a cover title joins it
only when the text shows the title goes on (a line that runs on, a bare "Section 19"), so a
subtitle stays out. The page's spelling beats a metadata title it corrects, a first bookmark
whose words it prints and more, and a metadata title naming a year the cover does not print; a
section heading is penalised. The name of a part ("Cover", "Table of Contents") and a web address
are never the title, and the display lines of a cover set out of reading order (a tall "2026"
beside a stack of smaller lines) reject every candidate that holds them. The best wins; a weak,
OCR-read or one-word winner, a cover title joined over very different sizes, and a cover whose
largest lines are out of reading order carry Fact("title-suspect") on the document (severity
error, so it shows in review.json), and the lines that formed the title and every rejected
candidate are published as `title_evidence`.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from reflow.analyze.constants import CALLOUT_LABEL, DOCLING_TITLE_SIZE
from reflow.analyze.headings import is_numbered_heading, is_section_heading
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent
from reflow.model import BBox, Fact, HEADING_BY_LEVEL, HEADING_ROLES, Node, Role, SUBHEADING_ROLES, Source, is_ocr
from reflow.text_quality import node_gate, ocr_gate, ocr_noise_reason, squash

MIN_LETTER_SHARE = 0.6
# Punctuation ordinary titles use ("Form W-9 (Rev. March 2024)"); left out of the letter share
# so it measures letters against digits and stray symbols, not against brackets and hyphens.
TITLE_PUNCTUATION = frozenset("()[]-–—.,:;'’\"/&")
MAX_WORDS = 30
MIN_WORDS = 1
SUSPECT_SCORE = 0.5

# Titles that tools write when the author set none. The whole trimmed title must match, so
# "Document Management Guide" and "PDF Accessibility Primer" stay usable.
GENERIC_TITLE = re.compile(r"untitled( document)?|document\d*|pdf", re.IGNORECASE)
# The name of a part of the document, which a first bookmark or a heading often is: "Cover",
# "Table of Contents". The title is what that part prints, not its name.
PART_NAME = re.compile(r"(?:front\s+)?cover(?:\s+page)?|(?:table\s+of\s+)?contents|title\s+page",
                       re.IGNORECASE)
# A web address ("azblue.com/bluedentalprovider") is where to read more, not the title.
WEB_ADDRESS = re.compile(r"(?:https?://|www\.)\S+|[\w-]+(?:\.[\w-]+)*\.(?:com|org|gov|net|edu)"
                         r"(?:/\S*)?", re.IGNORECASE)
# A year a metadata title names ("Audit Committee Charter 2015"): when the cover prints another
# and not this one, the metadata is a stale copy of an older edition.
YEAR = re.compile(r"\b(?:19|20)\d\d\b")
# What an authoring tool puts around the title it was given: "Microsoft Word - Kitsap 911 -
# Request for Proposal.docx". `clean_meta` strips both ends.
TOOL_PREFIX = re.compile(r"microsoft\s+(?:word|powerpoint|excel)\s+-\s+", re.IGNORECASE)
FILE_EXTENSION = re.compile(r"\.(?:docx?|pdf|indd|pptx?|xlsx?|rtf|odt)$", re.IGNORECASE)
# A title cannot open with a dash or a closing bracket: the text before it was cut off.
FRAGMENT_START = re.compile(r"^[-–—.,:;)\]}|/\\]")
BRACKETS = {")": "(", "]": "[", "}": "{"}
# A metadata title that is a working file name ("Final Employee Booklet 04102026",
# "Exhibit 131 Medical Remittance Advice_92723"): an underscore, or a date or job stamp of six
# or more digits. Form and publication numbers are shorter ("Form 1040", "CMS-10780").
FILE_NAME_META = re.compile(r"_|\d{6,}")
# The last word of a line that runs on into the title below it ("… Request for" / "Medications,
# DME…"; "Protect your quality of life by" / "protecting your eyes.").
RUN_ON_WORDS = frozenset("a an and at by for from in of on or our the to with your".split())
LEAD_MAX_WORDS = 12
LEAD_SKIP = (Role.ARTIFACT, Role.FIGURE)
PRIOR = {"meta": 0.6, "bookmark": 0.6, "cover": 0.55, "heading": 0.5, "docling": 0.5,
         "filename": 0.2}
# A line that is only a section's label, whose name is set under it ("Section 19").
SECTION_LABEL = re.compile(r"(?:section|part|chapter|unit|module)\s+(?:\d+|[IVX]+)", re.IGNORECASE)
# A title that opens with its section number ("Section 1 – Project Description", "5 Requirements").
SECTION_NUMBER = re.compile(r"(?:section\s+)?\d{1,2}(?:[.)]\s*|\s)(?=\S)", re.IGNORECASE)

# The cover: pages whose lines can print the title, how big and how high a display title is,
# and how close the lines of one title sit.
COVER_PAGES = (1, 2)
HEADING_REACH = 2          # pages past the first heading a title candidate may sit
COVER_TOP = 0.4            # share of the page height the title starts in
DISPLAY_RATIO = 1.3        # display size over body size
TOUCH = 0.5                # lines of one title: gap at most this × the title's size
OVERLAP = 0.5              # ...and at most this × the size of overlap (a tight leading)
WRAP_SIZE = 0.1            # a wrapped title line: size within this share of the title's
ALIGN = 0.5                # a smaller title line: an edge or centre within this × its size
MIXED_SIZES = 2.0          # a joined title whose largest line is over this × its smallest
SUBTITLE_GAP = 1.0         # a smaller line this near under the title is recorded as its subtitle
CENTRE = 0.05              # a centred head: line centre within this share of the page width
SAME_LINE = 0.5            # a line is the same printed line as a node when this much is inside
AGREEMENT = 0.3


@dataclass(frozen=True)
class TitleLine:
    """One printed line (or tree node) that formed a title candidate."""
    page: int | None
    bbox: BBox | None
    text: str
    ocr: bool
    gate: str | None = None   # why this line may not be navigation (text_quality.ocr_gate)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"page": self.page, "bbox": list(self.bbox) if self.bbox else None,
                             "text": self.text, "ocr": self.ocr}
        if self.gate:
            d["gate"] = self.gate
        return d


@dataclass(frozen=True)
class Candidate:
    text: str
    origin: str            # "meta" | "bookmark" | "cover" | "heading" | "docling" | "filename"
    page: int | None = None
    font_size: float | None = None
    unicode_errors: int = 0
    ocr: bool = False
    confidence: float = 1.0
    lead: str | None = None  # the line joined in front of the heading (see lead_node)
    lines: tuple[TitleLine, ...] = ()        # every printed line that formed the candidate
    subtitle: TitleLine | None = None        # cover: the smaller line below, not joined
    nodes: tuple[Node, ...] = field(default=(), repr=False, compare=False)  # (lead, heading)
    doubtful: str | None = None              # the first gate reason over the contributors
    suspect: str | None = None               # why the title, if it wins, goes to review
    named: bool = False                      # the line a stale metadata title names (stale_meta)


@dataclass
class TitleChoice:
    text: str
    origin: str
    score: float
    lead: str | None = None  # title_apply merges this line into the heading
    facts: list[Fact] = field(default_factory=list)   # to append to doc.root.facts
    rejected: list[tuple[Candidate, str]] = field(default_factory=list)
    lines: tuple[TitleLine, ...] = ()
    subtitle: TitleLine | None = None
    nodes: tuple[Node, ...] = field(default=(), repr=False)  # the heading (and lead) to promote

    @property
    def pages(self) -> list[int]:
        return sorted({line.page for line in self.lines if line.page is not None})

    def evidence(self) -> dict[str, Any]:
        """What formed the title and what lost, as plain JSON for `root.attrs["title_evidence"]`."""
        return {"origin": self.origin, "text": self.text,
                "lines": [line.to_dict() for line in self.lines],
                "subtitle": self.subtitle.to_dict() if self.subtitle else None,
                "rejected": [{"text": c.text, "origin": c.origin, "reason": reason,
                              "lines": [line.to_dict() for line in c.lines]}
                             for c, reason in self.rejected]}


def _is_docling_title(node: Node) -> bool:
    return node.role == Role.H2 and node.attrs.get("font_size") == DOCLING_TITLE_SIZE


def _errors(node: Node) -> int:
    return int(node.attrs.get("unicode_errors", 0) or 0)


def _node_line(node: Node) -> TitleLine:
    gate = node_gate(node) or ("low-confidence heading" if node.confidence < 0.5 else None)
    return TitleLine(node.page, node.bbox, (node.text or "").strip(), is_ocr(node), gate)


def _heading_candidate(node: Node, origin: str, lead: Node | None = None) -> Candidate:
    """A heading as a candidate. A joined title (lead above its heading) is made of both nodes:
    its errors are summed, its confidence the lowest, it is OCR when either is, and it is
    doubtful when either fails the OCR gate."""
    parts = (lead, node) if lead is not None else (node,)
    lines = tuple(_node_line(n) for n in parts)
    text = " ".join(line.text for line in lines)
    return Candidate(text, origin, node.page, node.attrs.get("font_size"),
                     sum(_errors(n) for n in parts), any(is_ocr(n) for n in parts),
                     min(n.confidence for n in parts), lines[0].text if lead is not None else None,
                     lines, None, parts, next((l.gate for l in lines if l.gate), None))


def lead_node(nodes: list[Node], top: Node) -> Node | None:
    """The line just above the title heading that is the first half of the title: a short
    paragraph or heading on the same page with no closing punctuation, that ends in a word
    which runs on ("for", "by", "of") or that the heading continues in lowercase."""
    idx = next((i for i, n in enumerate(nodes) if n is top), None)
    if idx is None:
        return None
    prev = next((n for n in reversed(nodes[:idx]) if n.role not in LEAD_SKIP), None)
    if prev is None or prev.page != top.page or prev.role not in (Role.P, *HEADING_BY_LEVEL):
        return None
    lead, text = (prev.text or "").strip(), (top.text or "").strip()
    words = lead.split()
    if not words or not text or len(words) > LEAD_MAX_WORDS or lead[-1] in ".!?:;":
        return None
    runs_on = words[-1].casefold() in RUN_ON_WORDS
    return prev if runs_on or text[0].islower() else None


def clean_meta(text: str) -> str | None:
    """The metadata title without the tool prefix and the file extension an authoring tool wrote
    around it ("Microsoft Word - Kitsap 911 - Request for Proposal.docx"); None when what is left
    is a fragment (unbalanced brackets, a dash or closing bracket first); "" when it is one word
    that had a file extension, the file name itself ("Microsoft Word - memo.docx")."""
    text = TOOL_PREFIX.sub("", squash(text), count=1)
    bare = FILE_EXTENSION.sub("", text).strip()
    if bare != text and " " not in bare:
        return ""
    text = bare
    stack: list[str] = []
    for ch in text:
        if ch in "([{":
            stack.append(ch)
        elif ch in BRACKETS and (not stack or stack.pop() != BRACKETS[ch]):
            return None
    if stack or FRAGMENT_START.match(text):
        return None
    return text


def _line_reason(line: Line) -> str | None:
    """Why this printed line may not be a title (the lexical checks of `score`, then the OCR gate)."""
    return (_text_reason(line.text.strip(), line.unicode_errors, line.ocr)
            or ocr_gate([line]))


def _title_line(line: Line) -> TitleLine:
    return TitleLine(line.page, line.bbox, line.text.strip(), line.ocr, ocr_gate([line]))


def _body_size(pages: Sequence[PageContent]) -> float:
    """The character-weighted modal line size of these pages."""
    weight: Counter[float] = Counter()
    for page in pages:
        for line in page.lines:
            if not line.rotated:
                weight[round(line.size, 1)] += len(line.text.strip())
    return max(weight.items(), key=lambda kv: (kv[1], kv[0]))[0] if weight else 0.0


def _gap_below(upper: Line, lower: Line) -> float:
    return lower.bbox[1] - upper.bbox[3]


def _aligned(upper: Line, lower: Line) -> bool:
    tol = ALIGN * lower.size
    return (abs(upper.bbox[0] - lower.bbox[0]) <= tol or abs(upper.bbox[2] - lower.bbox[2]) <= tol
            or abs(upper.bbox[0] + upper.bbox[2] - lower.bbox[0] - lower.bbox[2]) <= 2 * tol)


def _runs_on(text: str) -> bool:
    words = text.split()
    return bool(words) and (text.endswith(":") or words[-1].casefold() in RUN_ON_WORDS)


def _continues(upper: Line, lower: Line, main: Line, display: float) -> bool:
    """Whether `lower`, right under `upper`, is the next line of the title `main` heads: a wrapped
    line of the title's size, or a smaller line of display size set in line with it when the text
    shows the title goes on: the line above runs on into it ("INTRODUCING A NEW OPTION:"), it runs
    on itself ("Appointment of" under "MEDICARE ADVANTAGE"), or the line above is only a section
    label ("Section 19" over "Claim Submission"). A smaller line that does none of these is a
    subtitle ("For the Fiscal Year Ended June 30th, 2023")."""
    if not -OVERLAP * main.size <= _gap_below(upper, lower) <= TOUCH * main.size:
        return False
    if abs(lower.size - main.size) <= WRAP_SIZE * main.size:
        return True
    above, text = upper.text.strip(), lower.text.strip()
    return (display <= lower.size < main.size and _aligned(upper, lower)
            and (_runs_on(above) or _runs_on(text) or SECTION_LABEL.fullmatch(above) is not None))


def tangled_lines(page: PageContent, body: float) -> list[tuple[Line, Line]]:
    """Pairs of display lines of the page where one line's box holds most of the other's: a large
    word set beside a stack of smaller lines ("2026" left of "OUTLINE OF" over "COVERAGE"), which
    the line reader joins to the wrong row, so the words of either line are out of reading order."""
    lines = [l for l in page.lines if not l.rotated and l.text.strip()
             and l.size >= DISPLAY_RATIO * body and _line_reason(l) is None]
    found = []
    for outer in lines:
        for inner in lines:
            if inner is outer:
                continue
            w = min(outer.bbox[2], inner.bbox[2]) - max(outer.bbox[0], inner.bbox[0])
            h = min(outer.bbox[3], inner.bbox[3]) - max(outer.bbox[1], inner.bbox[1])
            area = (inner.bbox[2] - inner.bbox[0]) * (inner.bbox[3] - inner.bbox[1])
            if (w > 0 and h > 0 and area > 0 and w * h >= SAME_LINE * area
                    and inner.bbox[1] > outer.bbox[1] and inner.bbox[3] < outer.bbox[3]):
                found.append((outer, inner))
    return found


def _display_title(page: PageContent, body: float) -> tuple[list[Line], Line | None] | None:
    """The page's largest line of at least DISPLAY_RATIO × body in its top COVER_TOP that passes the
    line checks, with the touching lines of its title (a kicker above it of display size that shares
    an edge or the centre with it, so a label set in the corner beside the title stays out; the lines
    below it that `_continues` accepts) and the smaller line below it, which is not joined."""
    lines = [l for l in page.lines if not l.rotated and l.text.strip()]
    display = DISPLAY_RATIO * body
    big = [l for l in lines if l.size >= display
           and l.bbox[1] <= COVER_TOP * page.height and _line_reason(l) is None]
    if not big:
        return None
    main = max(big, key=lambda l: (l.size, -l.bbox[1]))
    reach = TOUCH * main.size
    joined = [main]
    while above := [l for l in lines if l not in joined and l.size >= display
                    and -OVERLAP * main.size <= _gap_below(l, joined[0]) <= reach
                    and _aligned(l, joined[0])]:
        joined.insert(0, max(above, key=lambda l: l.bbox[3]))
    while below := [l for l in lines
                    if l not in joined and _continues(joined[-1], l, main, display)]:
        joined.append(min(below, key=lambda l: l.bbox[1]))
    after = [l for l in lines if l not in joined and l.size < main.size
             and 0 <= _gap_below(joined[-1], l) <= SUBTITLE_GAP * main.size]
    return joined, min(after, key=lambda l: l.bbox[1]) if after else None


def _centred_head(page: PageContent) -> list[Line] | None:
    """The topmost line of the page when it is centred, has two words or more and is all capitals
    ("BUILDING PERMIT APPLICATIONS")."""
    lines = [l for l in page.lines if not l.rotated and l.text.strip()]
    if not lines:
        return None
    head = min(lines, key=lambda l: l.bbox[1])
    text = head.text.strip()
    centre = (head.bbox[0] + head.bbox[2]) / 2
    if (abs(centre - page.width / 2) <= CENTRE * page.width and len(text.split()) >= 2
            and text.upper() == text and any(ch.isalpha() for ch in text)):
        return [head]
    return None


def cover_candidates(pages: Sequence[PageContent]) -> list[Candidate]:
    """The title the first two pages print, read from their lines (not from headings, which a
    cover often lacks): the largest display line of the top of either page with the lines that
    belong to it, else the centred capitals that open page 1. At most one candidate."""
    cover = [p for p in pages if p.number in COVER_PAGES and p.lines]
    if not cover:
        return []
    body = _body_size(cover)
    found = [(p, d) for p in cover if (d := _display_title(p, body)) is not None]
    if found:
        # The larger title wins; page 1 on a tie (max keeps the first).
        page, (joined, subtitle) = max(found, key=lambda pd: max(l.size for l in pd[1][0]))
        sub = _title_line(subtitle) if subtitle else None
    else:
        first = next((p for p in cover if p.number == 1), None)
        joined = _centred_head(first) if first else None
        if not joined:
            return []
        page, sub = first, None
    lines = tuple(_title_line(l) for l in joined)
    # The largest line and the lines joined under it: a kicker above is set small by design.
    sizes = [l.size for l in joined]
    under = sizes[sizes.index(max(sizes)):]
    suspect = (f"the title joins lines set in sizes from {min(under):g} to {max(under):g} pt, "
               f"so it may hold a slogan or a kicker" if max(under) > MIXED_SIZES * min(under)
               else None)
    return [Candidate(" ".join(l.text for l in lines), "cover", page.number,
                      max(sizes), sum(l.unicode_errors for l in joined),
                      any(l.ocr for l in joined), 1.0, None, lines, sub, (),
                      next((l.gate for l in lines if l.gate), None), suspect)]


def _plain_words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.casefold()))


def stale_meta(meta: str, pages: Sequence[PageContent]) -> Line | None:
    """The cover line that a stale metadata title names: the metadata names a year the cover pages
    do not print while they print another ("Audit Committee Charter 2015" over a charter of "MARCH
    2026"), and a line in the top of a cover page holds every other word of it ("AUDIT,
    COMPLIANCE, AND RISK COMMITTEE CHARTER"). None when the metadata names no year, the cover
    prints it, or no line holds the rest."""
    years = set(YEAR.findall(meta))
    cover = [p for p in pages if p.number in COVER_PAGES]
    printed = set(YEAR.findall(" ".join(l.text for p in cover for l in p.lines)))
    rest = _plain_words(YEAR.sub(" ", meta))
    if not years or not printed or years & printed or len(rest) < 2:
        return None
    for p in sorted(cover, key=lambda p: p.number):
        top = [l for l in p.lines if not l.rotated and l.bbox[1] <= COVER_TOP * p.height]
        for l in sorted(top, key=lambda l: l.bbox[1]):
            if rest <= _plain_words(l.text) and _line_reason(l) is None:
                return l
    return None


def candidates(nodes: list[Node], meta_title: str | None, bookmark_title: str | None,
               filename: str, pages: Sequence[PageContent] = ()) -> list[Candidate]:
    """Every candidate, in preference order on a tie. Docling's title comes before the
    heading candidate, so on equal scores the Docling H2 wins."""
    out: list[Candidate] = []
    if meta_title and meta_title.strip():
        cleaned = clean_meta(meta_title)
        raw = squash(meta_title)
        if cleaned is None:
            out.append(Candidate(raw, "meta", doubtful="the metadata title is a fragment "
                                 "(unbalanced brackets, or a leading dash or bracket)"))
        elif not cleaned:
            out.append(Candidate(raw, "meta", doubtful="generic title from the tool that wrote "
                                 "the file: its file name"))
        elif (named := stale_meta(cleaned, pages)) is not None:
            years = ", ".join(sorted(set(YEAR.findall(cleaned))))
            out.append(Candidate(cleaned, "meta", doubtful=f"the metadata title names {years}, "
                                 f"which the cover does not print; it prints "
                                 f"{named.text.strip()!r}"))
            out.append(Candidate(named.text.strip(), "cover", named.page, named.size,
                                 named.unicode_errors, named.ocr, lines=(_title_line(named),),
                                 named=True))
        else:
            out.append(Candidate(cleaned, "meta"))
    if bookmark_title and bookmark_title.strip():
        out.append(Candidate(bookmark_title.strip(), "bookmark"))

    docling = next((n for n in nodes if _is_docling_title(n) and n.text and n.text.strip()),
                   None)
    if docling is not None:
        out.append(_heading_candidate(docling, "docling"))

    out.extend(cover_candidates(pages))

    headings = [n for n in nodes if n.role in HEADING_ROLES and not _is_docling_title(n)
                and n.text and n.text.strip()]
    if headings:
        first_page = headings[0].page
        top = max((n for n in headings if n.page == first_page),
                  key=lambda n: n.attrs.get("font_size", 0) or 0)
        lead = lead_node(nodes, top)
        out.append(_heading_candidate(top, "heading", lead))
        if lead is not None:   # the heading alone, for when the lead may not be part of a title
            out.append(_heading_candidate(top, "heading"))
        # The other headings stand in when the top one is refused, but only those within
        # HEADING_REACH pages of it: a heading of page 8 ("QUESTIONS?") is a section's, not
        # the document's title.
        out.extend(_heading_candidate(n, "heading") for n in headings
                   if n is not top and (n.page or 0) <= (first_page or 0) + HEADING_REACH)

    out.append(Candidate((filename or "").strip(), "filename"))
    return out


def _words(text: str) -> frozenset[str]:
    return frozenset(word.casefold() for word in text.split())


def _text_reason(text: str, unicode_errors: int, ocr: bool) -> str | None:
    """The lexical checks every candidate and every cover line must pass."""
    if unicode_errors > 0:
        return f"{unicode_errors} unicode errors"
    if ocr:
        if sum(ch.isalpha() for ch in text) < 3:
            return "OCR fragment has fewer than 3 letters"
        if reason := ocr_noise_reason(text):
            return reason
    if CALLOUT_LABEL.match(text):
        return "a callout label (REMEMBER, NOTE...), not a title"
    chars = [ch for ch in text if not ch.isspace() and ch not in TITLE_PUNCTUATION]
    if not chars:
        return "no letters"
    share = sum(ch.isalpha() for ch in chars) / len(chars)
    if share < MIN_LETTER_SHARE:
        return f"letter share {share:.2f} below {MIN_LETTER_SHARE}"
    return None


def score(c: Candidate, others: list[Candidate]) -> tuple[float, str | None]:
    """(score 0..1, rejection reason or None). A rejection scores 0; agreement with another
    candidate (same words) adds, and a section heading is worth half."""
    text = c.text.strip()
    if not text:
        return 0.0, "no text"
    if c.doubtful:
        return 0.0, c.doubtful
    if c.unicode_errors > 0:
        return 0.0, f"{c.unicode_errors} unicode errors"
    if c.origin in ("heading", "docling") and c.confidence < 0.5:
        return 0.0, "low-confidence heading"
    if reason := _text_reason(text, 0, c.ocr):
        return 0.0, reason
    if GENERIC_TITLE.fullmatch(text):
        return 0.0, "generic title from the metadata or the tool that wrote the file"
    if PART_NAME.fullmatch(text):
        return 0.0, "the name of a part of the document (its cover, its contents), not its title"
    if WEB_ADDRESS.fullmatch(text):
        return 0.0, "a web address, not a title"
    if c.origin == "meta" and FILE_NAME_META.search(text):
        return 0.0, "metadata title is a working file name (an underscore or a long number)"

    value = PRIOR[c.origin] + (AGREEMENT if c.named else 0.0)
    if c.origin in ("heading", "cover") and (
            is_section_heading(text) or is_numbered_heading(text) or SECTION_NUMBER.match(text)):
        value /= 2
    if not MIN_WORDS <= len(text.split()) <= MAX_WORDS:
        value /= 2
    words = _words(text)
    # A cover and a heading read the same printed line: that is one witness, not two.
    if any(o.origin != c.origin and {o.origin, c.origin} != {"cover", "heading"}
           and {o.origin, c.origin} != {"cover", "docling"} and _words(o.text) == words
           for o in others if o.text and o.text.strip()):
        value += AGREEMENT
    return min(value, 1.0), None


def _edits(a: str, b: str) -> int:
    """0 for equal words, 1 for one inserted, dropped or changed letter, 2 for anything further."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > 1:
        return 2
    i = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    if len(a) == len(b):
        return 1 if a[i + 1:] == b[i + 1:] else 2
    short, long = (a, b) if len(a) < len(b) else (b, a)
    return 1 if short[i:] == long[i + 1:] else 2


def corrects(cover: str, meta: str) -> bool:
    """The page prints the metadata title with a typo fixed: each cover word is, in order, a word of
    the metadata title within one edit, and at least one differs ("CICP Provider Directory" for
    "CICP Provider Director January 2024"). A cover that only shortens the metadata is no
    contradiction ("Form W-9" over "Form W-9 Request for ...")."""
    cw, mw = cover.casefold().split(), meta.casefold().split()
    if not cw or len(cw) > len(mw):
        return False
    dist = [_edits(c, m) for c, m in zip(cw, mw)]
    return max(dist) <= 1 and 1 in dist


def _same_line(a: TitleLine, b: TitleLine) -> bool:
    """One printed line seen twice (a cover line and a tree node): same page, and the smaller box
    at least SAME_LINE inside the other."""
    if a.page != b.page or a.bbox is None or b.bbox is None:
        return False
    w = min(a.bbox[2], b.bbox[2]) - max(a.bbox[0], b.bbox[0])
    h = min(a.bbox[3], b.bbox[3]) - max(a.bbox[1], b.bbox[1])
    smaller = min((a.bbox[2] - a.bbox[0]) * (a.bbox[3] - a.bbox[1]),
                  (b.bbox[2] - b.bbox[0]) * (b.bbox[3] - b.bbox[1]))
    return w > 0 and h > 0 and smaller > 0 and w * h >= SAME_LINE * smaller


TITLE_PAGE_WORDS = 12   # the longest text block a title page prints: names and dates, no prose


# Why a title page's headings are paragraphs (the `why` that reflow.analyze.headings.demote writes).
TITLE_PAGE_WHY = "a title page: it repeats the cover's title, it opens no section"


def title_page_headings(nodes: Sequence[Node], choice: TitleChoice) -> list[Node]:
    """The headings of a title page that become paragraphs: pure, it changes no node. A
    report prints its title on a picture cover and again, in plain capitals with the date and
    the names that sign it, on the page behind ("SAN MATEO COUNTY MOSQUITO ... DISTRICT" / "ANNUAL
    COMPREHENSIVE FINANCIAL REPORT" / "FOR FISCAL YEAR ENDED JUNE 30, 2023"): those lines are the
    cover again, not sections, and the outline lists the title once. Only a cover page behind the
    title's own, whose text is all short blocks, has its headings listed; they keep their words as
    paragraphs (reflow.analyze.headings.demote), and the levels beneath need to be set again."""
    pages = set(choice.pages)
    if not pages:
        return []
    words = _plain_words(choice.text)
    found: list[Node] = []
    for number in COVER_PAGES:
        if number <= min(pages) or number in pages:
            continue
        on_page = [n for n in nodes if n.page == number and n.text and n.role != Role.ARTIFACT]
        heads = [n for n in on_page if n.role in SUBHEADING_ROLES]
        if (any(_plain_words(n.text) == words for n in heads)
                and all(len(n.text.split()) <= TITLE_PAGE_WORDS for n in on_page)):
            found += heads
    return found


def choose_title(nodes: list[Node], meta_title: str | None, bookmark_title: str | None,
                 filename: str, pages: Sequence[PageContent] = ()) -> TitleChoice:
    """The title to publish and the facts explaining it. The filename fallback always
    carries Fact("title-suspect"), as does a winner scoring under SUSPECT_SCORE or read by OCR."""
    cands = candidates(nodes, meta_title, bookmark_title, filename, pages)
    scored: list[tuple[float, str | None]] = []
    for i, c in enumerate(cands):
        scored.append(score(c, cands[:i] + cands[i + 1:]))
    bonus = [0.0] * len(cands)

    # One printed line, one verdict: a line that failed the gate rejects every candidate that
    # contains it, whichever path (cover line or tree node) read it. So does a display line of a
    # cover whose words are out of reading order (tangled_lines).
    cover_pages = [p for p in pages if p.number in COVER_PAGES and p.lines]
    body = _body_size(cover_pages)
    tangled = [(p, pair) for p in cover_pages for pair in tangled_lines(p, body)]
    gated = [line for c in cands for line in c.lines if line.gate]
    for _, (outer, inner) in tangled:
        why = (f"{outer.text.strip()!r} is set around {inner.text.strip()!r}, so their words are "
               f"out of reading order")
        gated += [replace(_title_line(outer), gate=why), replace(_title_line(inner), gate=why)]
    for i, c in enumerate(cands):
        if scored[i][1] is None:
            for line in gated:
                if any(_same_line(line, own) for own in c.lines):
                    scored[i] = (0.0, line.gate)
                    break

    # The page's spelling wins: a cover that corrects the metadata title replaces it, and a cover
    # that prints the first bookmark's words and more ("Section 19 Claim Submission" over the
    # bookmark "19 Claim Submission") replaces the bookmark.
    for i, named in enumerate(cands):
        for j, cover in enumerate(cands):
            if (cover.origin != "cover" or scored[i][1] is not None
                    or scored[j][1] is not None):
                continue
            if ((named.origin == "meta" and corrects(cover.text, named.text))
                    or (named.origin == "bookmark"
                        and _plain_words(named.text) < _plain_words(cover.text))):
                scored[i] = (0.0, f"the page prints it as {cover.text!r}")
                bonus[j] = AGREEMENT

    totals = [(min(v + bonus[i], 1.0), reason) for i, (v, reason) in enumerate(scored)]
    accepted = [i for i, (_, reason) in enumerate(totals) if reason is None]
    if accepted:
        # Strictly greater keeps the earlier candidate on a tie.
        best = accepted[0]
        for i in accepted[1:]:
            if totals[i][0] > totals[best][0]:
                best = i
    else:
        best = len(cands) - 1  # the filename, always the last candidate
    winner, best_score = cands[best], totals[best][0]

    rejected: list[tuple[Candidate, str]] = []
    for i, c in enumerate(cands):
        if i == best:
            continue
        reason = totals[i][1] or f"score {totals[i][0]:.2f} lost to the {winner.origin}"
        rejected.append((c, reason))

    facts: list[Fact] = []
    data = {"origin": winner.origin, "score": round(best_score, 2)}
    if not accepted:
        facts.append(Fact("title-suspect", Source.HEURISTIC,
                          f"no title candidate passed the checks; published the file name "
                          f"{winner.text!r}", data))
    elif winner.origin == "filename":
        facts.append(Fact("title-suspect", Source.HEURISTIC,
                          f"the file name {winner.text!r} is the title; the metadata and "
                          f"headings were rejected or weaker", data))
    elif best_score < SUSPECT_SCORE:
        facts.append(Fact("title-suspect", Source.HEURISTIC,
                          f"the best title {winner.text!r} scores {best_score:.2f}, under "
                          f"{SUSPECT_SCORE}", data))
    elif winner.ocr:
        facts.append(Fact("title-suspect", Source.HEURISTIC,
                          f"the title {winner.text!r} was read by OCR", data))
    elif winner.suspect:
        facts.append(Fact("title-suspect", Source.HEURISTIC, winner.suspect, data))
    elif winner.origin in ("cover", "heading", "docling") and len(winner.text.split()) == 1:
        facts.append(Fact("title-suspect", Source.HEURISTIC,
                          f"the title {winner.text!r} is one word, which may be only the "
                          f"largest word of the title", data))
    elif tangled and max(pair[0].size for _, pair in tangled) >= (winner.font_size or 0):
        page, (outer, inner) = tangled[0]
        facts.append(Fact("title-suspect", Source.HEURISTIC,
                          f"the cover sets {outer.text.strip()!r} around {inner.text.strip()!r} "
                          f"on page {page.number}, out of reading order, so its title is not "
                          f"known; published {winner.text!r}", data))

    # What title_apply makes the H1: the winner's own heading, or the one heading that prints
    # a cover title's words.
    holder = winner
    if winner.origin == "cover":
        twins = [c for c in cands if c.origin in ("heading", "docling") and c.nodes
                 and squash(c.text) == squash(winner.text)]
        # The title's own words are printed again further in (a running head, a contents page):
        # the heading on the cover's page is the one.
        pages = {line.page for line in winner.lines}
        twins = [c for c in twins if c.page in pages] if len(twins) > 1 else twins
        holder = twins[0] if len(twins) == 1 else Candidate("", "cover")
    return TitleChoice(text=winner.text, origin=winner.origin, score=best_score,
                       lead=holder.lead, facts=facts, rejected=rejected, lines=winner.lines,
                       subtitle=winner.subtitle, nodes=holder.nodes)
