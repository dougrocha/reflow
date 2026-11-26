"""Evidence fusion: what other witnesses say about the heuristic tree.

The heuristic engine owns the text and the backbone of the tree (reading order, blocks,
tables, figures). Other sources are *witnesses*: each one states, per page, what it saw,
as a sequence of claims in its own reading order. This module matches those claims to the
heuristic nodes by geometry and settles what they change.

Each step is split in two. A *decision* is a pure function of the tree and the evidence: it
returns frozen records (`Promote`, `Demote`, `Retag`, `Note`, `Annotation`) and touches no
node. The tree is edited in one place per kind of edit, which applies the records in the
order they were decided: `apply_outline` for the roles, joins and removals of the title and
the headings, `apply_fusion` for the facts and the attributes a witness fills. `witness` is
the shell that reads the PDF's own structure (bookmarks, tag tree, link annotations) and
calls the steps in the order they depend on each other.

  * `title_apply` (decides with `decide_title` and `title.title_page_headings`): the winning
    heading becomes the H1, a cover line joined to it is merged in, a title page that repeats
    the cover has its headings made paragraphs;
  * `tag_headings` (decides with `plan_tag_headings`): a trusted tag tree sets heading roles
    and levels, joins the pieces of a split heading and demotes headings it calls body text;
  * `outline_toc`: marks the headings the bookmarks name (attributes only);
  * `fuse` (decides with `plan_fusion`): appends Facts (reflow.model.Fact), which
    reflow.analyze.confidence scores and reflow.analyze.review reports, and fills attributes
    the heuristics cannot know and a witness is authoritative on, when the node has none:
    `alt` from a tag tree's /Alt, `scope` from /TH. It changes no role.

Witnesses: the tag tree (reflow.extract.tags) and the bookmarks (headings only). Adding a
witness is one class with `claims()`; no consumer changes. Link annotations are not fused: they
become spans (reflow.extract.links.attach_links). Docling's reading order is compared in reflow.analyze.order_evidence.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from reflow import geometry
from reflow.analyze import title
from reflow.analyze.marks import drop_emphasis
from reflow.analyze.headings import demote, fix_heading_levels, is_section_heading, is_sentence
from reflow.text_quality import fold, node_gate, squash
from reflow.extract.claims import Claim, PageClaims, Witness, WitnessKind
from reflow.extract.links import attach_links, read_links
from reflow.extract.pagecontent import reader_for
from reflow.extract.tags import BookmarkWitness, TagWitness, read_bookmarks, shared_tags
from reflow.model import BBox, Document, Fact, HEADING_BY_LEVEL, HEADING_LEVEL, HEADING_ROLES, Node, Role, Source

if TYPE_CHECKING:
    from reflow.analyze.engine import Backbone
    from reflow.run import Run

# Two claims (or a claim and a node) are the same region when this share of the smaller box
# lies in the larger (the hybrid engine's REPLACE_SHARE, which covers IoU >= 0.5).
MATCH_SHARE = 0.5


@dataclass
class Match:
    """A heuristic node and the claims that cover it (usually one)."""
    node: Node
    claims: list[Claim]
    share: float  # the best geometric share


@dataclass
class FusionReport:
    """What `fuse` did, for the conversion summary and the inspector."""
    pages_compared: int = 0
    facts: dict[str, int] = field(default_factory=dict)          # code → count appended
    filled: dict[str, int] = field(default_factory=dict)         # attr → count filled


def witness(run: Run, backbone: Backbone) -> FusionReport | None:
    """What the PDF's own structure and the other engines say about the tree, as facts; and
    the title, chosen from the same evidence and written to `root.attrs["title"]`.

    The title (reflow.analyze.title): the metadata title, the first bookmark when it
    points at page 1, the top heading of the first page with headings, Docling's title and
    the file name are scored; the winner is the title, the root ledger says which one won
    (Fact "title", data.origin, which Summary.title_origin reads) and why a weak one is
    doubtful ("title-suspect"). A heading that wins becomes the tree's H1 and the headings
    under it move up a level; a metadata or bookmark title leaves the tree alone.

    The tag tree and the bookmarks are then fused into the tree (`fuse`): facts on the nodes
    they confirm or dispute, and the alt text and header scopes they state. The link
    annotations become inline spans.

    The steps run in the one order their data dependence allows, as reflow.analyze.structure
    does for the passes after it: choose_title reads the tree as the engine left it →
    title_apply before every step below (the H1 and the levels it sets are what the tag
    tree's decisions start from) → attach_links after title_apply, which may join the
    heading's text (a span is an offset in it) → tag_headings before outline_toc and fuse,
    which read the final roles (outline_toc also reads the headings' text after a join) →
    outline_toc → fuse last, so every fact compares a witness with the tree as it is
    published."""
    doc = run.doc
    bookmarks = read_bookmarks(run.working)   # pikepdf: no pdfium lock
    first = bookmarks[0].title if bookmarks and bookmarks[0].page == 1 else None
    choice = title.choose_title(doc.root.children, doc.meta.get("title"), first, run.pdf.stem,
                                backbone.pages or ())
    title_apply(doc.root.children, choice)
    doc.root.attrs["title"] = choice.text
    doc.root.attrs["title_evidence"] = choice.evidence()
    scanned, run.state.links_scan = run.state.links_scan, None
    links = read_links(run.working, reader=reader_for(run), scanned=scanned)   # annotations → inline spans
    if links:
        attach_links(doc.root.children, links, {p.number for p in doc.pages})
    doc.root.facts += choice.facts
    doc.root.facts.append(Fact("title", Source.HEURISTIC, f"from the {choice.origin}",
                               {"origin": choice.origin, "score": round(choice.score, 2),
                                "pages": choice.pages}))
    witnesses: list[Witness] = []
    tagged = shared_tags(run)             # one bounded, failure-safe read, shared with order_evidence
    if tagged:
        tags = TagWitness(tagged)
        witnesses.append(tags)
        tag_headings(doc, tags)           # a trustworthy tag tree sets heading role and level
    if bookmarks:
        texts = {p.number: [line.text for line in p.lines] for p in backbone.pages or ()}
        witnesses.append(BookmarkWitness(bookmarks, texts))
        outline_toc(doc, bookmarks)       # bookmarks that name the headings are the contents
    return fuse(doc, witnesses)


# Edits to the outline. A decision returns these and changes nothing; `apply_outline` makes them.

@dataclass(frozen=True)
class Promote:
    """The heading the title came from becomes the H1 (the renderer prints it once, in the
    shell). A title joined from two lines (`lead` is the one above) is merged into the heading,
    with the text `joined`, and the lead is removed so it is read once."""
    node: Node
    lead: Node | None
    joined: str


@dataclass(frozen=True)
class Demote:
    """A heading becomes a paragraph (reflow.analyze.headings.demote), for `reason`."""
    node: Node
    reason: str


@dataclass(frozen=True)
class Retag:
    """A tag tree's verdict on a block: its new role, the blocks folded into it (`join`, the
    block first; the others are removed), whether its emphasis is dropped (a heading's style
    is its level), and the "tag-heading" fact that records the change."""
    node: Node
    role: Role
    join: tuple[Node, ...] = ()
    plain: bool = False
    fact: Fact | None = None


@dataclass(frozen=True)
class Note:
    """Why a block the tag tree calls a heading stays a paragraph (an entry in its `why`)."""
    node: Node
    why: str


OutlineEdit = Promote | Demote | Retag | Note


def apply_outline(nodes: list[Node], edits: Sequence[OutlineEdit], *, relevel: bool) -> None:
    """Make the decided edits in the order they were decided, then (when `relevel`) make the
    heading levels gap-free again (fix_heading_levels). This is the one place that changes a
    role, joins or removes nodes in the outline, and the one that calls fix_heading_levels.

    Each stage that decides from the levels the one before it left (the title, the title page,
    the tag tree) applies its edits and levels them once. Levelling two stages once does not
    keep the output: a heading demoted between two passes changes the stack the second starts
    from, and fix_heading_levels is not idempotent over its own result, so the passes are not
    merged."""
    gone: set[int] = set()
    for edit in edits:
        if isinstance(edit, Promote):
            node = edit.node
            if edit.lead is not None:
                # The heading takes the lead's text, spans and marks, then the joined title (a
                # no-op unless whitespace was normalized). It traces to both printed lines.
                node.join_inline([edit.lead, node])
                node.set_text(edit.joined)
                merge_ocr(node, [edit.lead, node])
                node.facts += edit.lead.facts
                node.bbox = geometry.union([b for b in (edit.lead.bbox, node.bbox) if b])
                gone.add(id(edit.lead))
            node.role = Role.H1
            drop_emphasis(node)
        elif isinstance(edit, Demote):
            demote(edit.node, edit.reason)
        elif isinstance(edit, Retag):
            if edit.join:
                _join(list(edit.join))
                gone.update(id(p) for p in edit.join[1:])
            edit.node.role = edit.role
            if edit.plain:
                drop_emphasis(edit.node)
            if edit.fact is not None:
                edit.node.facts.append(edit.fact)
        else:
            edit.node.attrs.setdefault("why", []).append(edit.why)
    if gone:
        nodes[:] = [n for n in nodes if id(n) not in gone]
    if relevel:
        fix_heading_levels(nodes)


def decide_title(nodes: Sequence[Node], choice: title.TitleChoice) -> Promote | None:
    """The heading the title came from, to make the document's H1. The heading is `choice.nodes`
    (the last node; the one before it is the line joined in front of it): the winning heading, or the
    one heading that prints a cover title's words. None when the title came from no heading (a
    metadata title, a cover joined from lines that are not one heading) or the heading is a
    section heading."""
    if not choice.nodes:
        return None
    node = choice.nodes[-1]
    if (not any(n is node for n in nodes) or node.role not in HEADING_ROLES
            or is_section_heading(node.text)):
        return None
    lead = None
    joined = ""
    if len(choice.nodes) > 1:
        lead = choice.nodes[0]
        if not any(n is lead for n in nodes):
            return None
        joined = " ".join((n.text or "").strip() for n in choice.nodes)
    return Promote(node, lead, joined)


def title_apply(nodes: list[Node], choice: title.TitleChoice) -> Node | None:
    """Make the heading the title came from the document's H1 (`decide_title`) and re-level the
    headings beneath it, then make the headings of a title page that repeats the cover's title
    paragraphs (`title.title_page_headings`, decided on the tree the promotion left) and
    re-level again. Returns the promoted heading, None when none was."""
    promotion = decide_title(nodes, choice)
    if promotion is not None:
        apply_outline(nodes, [promotion], relevel=True)
    demotions = [Demote(n, title.TITLE_PAGE_WHY) for n in title.title_page_headings(nodes, choice)]
    if demotions:
        apply_outline(nodes, demotions, relevel=True)
    return promotion.node if promotion is not None else None


# Tagged headings. A tag tree is believed about headings when, over the blocks it covers
# and has a role for, at least TAG_AGREE_SHARE sit in the same kind (heading or text) as ours,
# it names a heading, and its headings are not all one level (when there are TAG_LEVELS_MIN or
# more of them): a tree that tags everything /P, or every heading /H1, is an export default,
# not an outline.
TAG_AGREE_SHARE = 0.75
TAG_COMPARED_MIN = 3
TAG_LEVELS_MIN = 4
# Our block must lie this much inside the tagged element to be what it says.
TAG_INSIDE = 0.8
# ...or this much, when the block's centre lies in the element and the layout split the element
# in several blocks of one size: our line boxes reach above the glyphs a tag's box is made of (an Arabic
# title's tops sit 11 points under ours).
TAG_INSIDE_CENTRED = 0.55
# A heading is body text only when the tagged /P is about that block alone; a /P much larger
# than it is a paragraph that opens with a bold run-in heading.
TAG_WHOLE = 2.0
# ...and only when it is not set in a larger size than the body text (a cover title the
# tags left as /P is still a heading).
TAG_BODY_SIZE = 1.1
# A claim's text key ends with this many characters of the element's text (tags._KEY_TAIL).
TAG_TAIL_MIN = 20
# Bookmarks become the contents when this share of at least OUTLINE_MIN of them name a heading.
OUTLINE_MIN = 3
OUTLINE_MATCH_SHARE = 0.7


@dataclass(frozen=True)
class TagHeadings:
    """What `tag_headings` did: whether the tree was believed, and the changes made."""
    trusted: bool = False
    why: str = ""
    flat: bool = False          # every heading has one tagged level: roles are believed, levels are not
    promoted: int = 0
    demoted: int = 0
    merged: int = 0
    releveled: int = 0


@dataclass(frozen=True)
class TagHeadingPlan:
    """The decision of `plan_tag_headings`: the report and the edits that make it true, in the
    order `apply_outline` makes them. A tree that is not believed still demotes the headings it
    calls part of a list item or a paragraph (`_body_line_demotions`), and its levels are left
    as they are; `report.trusted` says whether the levels are made gap-free."""
    report: TagHeadings
    edits: tuple[OutlineEdit, ...]


def _tag_pairs(nodes: Sequence[Node], tags) -> list[tuple[Node, Claim]]:
    """Each heading or paragraph with a box and the best tagged claim that has a role and
    covers it, for every page the tag tree reads, in reading order."""
    by_page: dict[int, list[Node]] = {}
    for node in nodes:
        if node.page is not None and node.bbox is not None and (
                node.role in HEADING_ROLES or node.role == Role.P):
            by_page.setdefault(node.page, []).append(node)
    pairs = []
    for number in sorted(tags.pages()):
        claims = tags.claims(number)
        if claims is None or not by_page.get(number):
            continue
        for match in match_claims(by_page[number], claims.order):
            claim = _role_claim(match)
            if claim is not None and KIND.get(claim.role) in ("heading", "text"):
                pairs.append((match.node, claim))
    return pairs


def _believe_tags(pairs: Sequence[tuple[Node, Claim]], role_of) -> tuple[bool, str]:
    """Whether the tag tree's headings are to be believed, and why not or why. A tree that
    gives every heading one level still says which blocks are headings, so it is believed
    for that (see `_flat`) but not for their levels. `role_of` is a block's role at this point."""
    heading_claims = [c for _, c in pairs if KIND.get(c.role) == "heading"]
    if not heading_claims:
        return False, "the tag tree names no heading"
    if len(pairs) < TAG_COMPARED_MIN:
        return False, "too few tagged blocks to compare"
    agree = sum(KIND.get(role_of(n)) == KIND.get(c.role) for n, c in pairs)
    if agree < TAG_AGREE_SHARE * len(pairs):
        return False, f"the tag tree agrees with {agree} of {len(pairs)} blocks"
    if _flat(heading_claims):
        return True, f"the tag tree agrees with {agree} of {len(pairs)} blocks, but gives every heading the same level"
    return True, f"the tag tree agrees with {agree} of {len(pairs)} blocks"


def _flat(heading_claims: Sequence[Claim]) -> bool:
    """Whether the tagged headings carry no level information: enough of them, all at one level."""
    levels = {c.attrs.get("level") for c in heading_claims if not c.attrs.get("generic")}
    return len(heading_claims) >= TAG_LEVELS_MIN and len(levels) <= 1


def tag_headings(doc: Document, tags) -> TagHeadings:
    """Set heading role and level from the tag tree when it is believed: decide
    (`plan_tag_headings`), apply the decisions and, when the tree was believed, make the levels
    gap-free like every outline (`apply_outline`, which calls fix_heading_levels)."""
    plan = plan_tag_headings(doc.root.children, tags)
    apply_outline(doc.root.children, plan.edits, relevel=plan.report.trusted)
    return plan.report


def plan_tag_headings(nodes: Sequence[Node], tags) -> TagHeadingPlan:
    """What the tag tree says about the headings, when it is believed (`_believe_tags`): pure, it
    changes no node (`nodes` is the root's children).

    Only headings are touched, on the pages the tree covers, block by block:
      * a paragraph inside a tagged heading becomes a heading at the tagged level; several of
        our blocks inside one tagged heading (a title the layout split in two) are joined;
      * a heading inside a tagged /P is a paragraph: the tree says it is body text;
      * a heading we already have keeps its place and takes the tagged level.
    A bare /H gives the role but not the level (the /Sect nesting it is read from is a
    guess): a block it promotes takes the level of the heading before it. Tagged level 1 is
    the title's, so when the tree uses /H1 on more than one page every level moves down one. The
    document's title (our one H1) is never demoted or re-levelled. Each change leaves a
    "tag-heading" fact on the block. A tree that gives every heading one level (`flat`) is
    believed for which blocks are headings, not for their levels or for which are not: it
    promotes and joins, it does not demote.

    The decisions are made one block after another and each reads the ones before it (the
    level of the heading before a block, the roles of a heading's pieces), so the roles those
    decisions give are kept in `now` while the tree stays as it was."""
    pairs = _tag_pairs(nodes, tags)
    edits: list[OutlineEdit] = list(_body_line_demotions(nodes, tags))
    now: dict[int, Role] = {id(e.node): e.role for e in edits if isinstance(e, Retag)}

    def role_of(node: Node) -> Role:
        return now.get(id(node), node.role)

    demoted = len(edits)
    promoted = merged = releveled = 0
    trusted, why = _believe_tags(pairs, role_of)
    if not trusted:
        return TagHeadingPlan(TagHeadings(False, why, demoted=demoted), tuple(edits))
    flat = _flat([c for _, c in pairs if KIND.get(c.role) == "heading"])
    order = {id(n): i for i, n in enumerate(nodes)}
    pairs.sort(key=lambda pair: order[id(pair[0])])
    h1 = next((n for n in nodes if role_of(n) == Role.H1), None)
    h1_pages = {n.page for n, c in pairs if c.attrs.get("level") == 1
                and not c.attrs.get("generic") and KIND.get(c.role) == "heading"}
    shift = int(len(h1_pages) > 1 and not flat)      # /H1 on several pages: sections, not just the title
    covered: dict[int, list[Node]] = {}
    inside: set[int] = set()
    loose: dict[int, list[Node]] = {}
    for node, claim in pairs:
        if node is h1:
            continue
        if _within(node.bbox, claim.bbox):
            covered.setdefault(id(claim), []).append(node)
            inside.add(id(node))
        elif _within(node.bbox, claim.bbox, centred=True):
            loose.setdefault(id(claim), []).append(node)
    for key, pieces_in in loose.items():       # only the pieces of a heading the layout split in blocks
        pieces = sorted(covered.get(key, []) + pieces_in, key=lambda n: order[id(n)])
        if (len(pieces) > 1 and _adjacent(order, pieces)
                and len({p.attrs.get("font_size") for p in pieces}) == 1):
            covered[key] = pieces
            inside.update(id(n) for n in pieces_in)
    # A style whose tagged headings all carry one tagged level is only echoed by the tags: they
    # rank it exactly as we do, but cannot see the chapters above it (every section in Pub 17 is
    # an /H1), and re-levelling just the blocks that lie inside their element would split
    # siblings. Such headings keep their level; a style the tags split between levels is
    # re-levelled as the tags say.
    tagged: dict[object, set] = {}
    for node, claim in pairs:
        if (id(node) in inside and role_of(node) in HEADING_ROLES and KIND.get(claim.role) == "heading"
                and not claim.attrs.get("generic") and "style_level" in node.attrs):
            tagged.setdefault(node.attrs["style_level"], set()).add(claim.attrs.get("level"))
    gone: set[int] = set()
    last = 2
    sizes = sorted(n.attrs["font_size"] for n in nodes if role_of(n) == Role.P
                   and n.attrs.get("font_size"))
    body = sizes[len(sizes) // 2] if sizes else 0.0
    for node, claim in pairs:
        if node is h1 or id(node) in gone or id(node) not in inside:
            continue
        was = role_of(node)
        if (KIND.get(claim.role) == "heading" and was in HEADING_ROLES and len(covered[id(claim)]) == 1
                and len(tagged.get(node.attrs.get("style_level"), ())) == 1):
            continue
        role, plain = was, False
        join: tuple[Node, ...] = ()
        if KIND.get(claim.role) == "heading":
            said = claim.attrs.get("level")
            parts = covered[id(claim)]
            # The tags never make OCR text navigation that the OCR gate refuses: a block that
            # fails it is not promoted, and the pieces of a heading are not joined when one does.
            if (was == Role.P or len(parts) > 1) and any(node_gate(p) for p in parts):
                if was == Role.P and (reason := node_gate(node)):
                    edits.append(Note(node, f"paragraph: {reason} (the tag tree has it as a heading)"))
                continue
            # Nor a sentence: a flyer's offer tagged /H3 is not navigation.
            if was == Role.P and is_sentence(node.text):
                edits.append(Note(node, "paragraph: a sentence (the tag tree has it as a heading)"))
                continue
            if claim.attrs.get("generic") or said is None or flat:
                # The tags give no level: keep ours, the deepest of the pieces of the heading that
                # already were one, or else the level of the heading before it.
                level = max((_level(role_of(p)) or 0 for p in parts), default=0) or last
            else:
                level = min(max(said + shift, 2), 6)
            if len(parts) > 1:
                if not _adjacent(order, parts):
                    continue            # pieces of one heading with other blocks between
                if parts[0] is node:
                    join = tuple(parts)
                    gone.update(id(p) for p in parts[1:])
                    merged += len(parts) - 1
            role, plain = HEADING_BY_LEVEL[level - 1], True
            if was == Role.P:
                promoted += 1
            elif was != role:
                releveled += 1
        elif (claim.role == Role.P and was in HEADING_ROLES and not flat
              and (node.attrs.get("font_size") or 0) <= TAG_BODY_SIZE * body
              and geometry.area(claim.bbox) <= TAG_WHOLE * geometry.area(node.bbox)):
            role = Role.P
            demoted += 1
        else:
            if role in HEADING_ROLES:
                last = _level(role) or last
            continue
        now[id(node)] = role
        if role in HEADING_ROLES:
            last = _level(role) or last
        fact = None
        if role != was:
            fact = Fact("tag-heading", Source.TAGS, f"the tag tree has this as {claim.role.value}",
                        {"was": was.value, "now": role.value})
        edits.append(Retag(node, role, join, plain, fact))
    report = TagHeadings(True, why, flat, promoted, demoted, merged, releveled)
    return TagHeadingPlan(report, tuple(edits))


def _body_line_demotions(nodes: Sequence[Node], tags) -> list[Retag]:
    """A heading the tag tree has as part of a list item or as the end of a paragraph (the
    layout took a bullet's line, or the top of a column that continues a paragraph, for a
    heading) is body text, whether or not the tree is believed in general: authors who tag
    little else right still tag their lists and paragraphs. Only a block that starts in lower
    case is considered (a numbered form label tagged /LI is a heading; a line that continues a
    sentence is not). It must lie inside the element and either fill it (a list item not much
    larger than the block) or be what the element ends with and not what it opens with. The
    document's title is never demoted. Pure: the paragraphs it decides are returned as edits."""
    title = next((n for n in nodes if n.role == Role.H1), None)
    by_page: dict[int, list[Node]] = {}
    for node in nodes:
        if node.page is not None and node.bbox is not None and node.role in HEADING_ROLES \
                and node is not title:
            by_page.setdefault(node.page, []).append(node)
    found: list[Retag] = []
    for number in sorted(tags.pages()):
        claims = tags.claims(number)
        if claims is None or not by_page.get(number):
            continue
        for match in match_claims(by_page[number], claims.order):
            node, text = match.node, squash(match.node.text or "")
            if not text[:1].islower():
                continue            # a heading may open a list item; a continuation never starts in lower case
            for claim in match.claims:
                if claim.role not in (Role.LI, Role.P) or not _within(node.bbox, claim.bbox):
                    continue
                whole = geometry.area(claim.bbox) <= TAG_WHOLE * geometry.area(node.bbox)
                tail = (claim.text is not None and "…" in claim.text and len(text) >= TAG_TAIL_MIN
                        and text[-TAG_TAIL_MIN:] == claim.text[-TAG_TAIL_MIN:]
                        and text[:TAG_TAIL_MIN] != claim.text[:TAG_TAIL_MIN])
                if (whole and claim.role == Role.LI) or tail:
                    found.append(Retag(node, Role.P, fact=Fact(
                        "tag-heading", Source.TAGS,
                        "the tag tree has this as " + ("a list item" if whole else "the end of "
                                                       + ("an item" if claim.role == Role.LI else "a paragraph")),
                        {"was": node.role.value, "now": Role.P.value})))
                    break
    return found


def _within(box: BBox, claim: BBox, *, centred: bool = False) -> bool:
    """Whether a block lies in a tagged element: nearly all of it, or with `centred` most of it
    with its centre."""
    share = geometry.share(box, claim)
    if share >= TAG_INSIDE:
        return not centred
    x, y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return centred and share >= TAG_INSIDE_CENTRED and claim[0] <= x <= claim[2] and claim[1] <= y <= claim[3]


def _adjacent(order: dict[int, int], parts: list[Node]) -> bool:
    """Whether `parts` follow one another in the reading order, on one page."""
    spots = [order[id(p)] for p in parts]
    return (spots == list(range(spots[0], spots[0] + len(spots)))
            and len({p.page for p in parts}) == 1)


def _join(parts: list[Node]) -> None:
    """Fold the blocks after the first into it: text joined, boxes united."""
    first = parts[0]
    kept = [p for p in parts if p.text]
    text = " ".join(squash(p.text) for p in kept)
    first.join_inline(kept)
    first.set_text(text)
    merge_ocr(first, parts)
    first.bbox = geometry.union([p.bbox for p in parts])


def merge_ocr(node: Node, parts: list[Node]) -> None:
    """The OCR grading of a node joined from `parts` (text_evidence.flag_ocr_marks): "ungraded"
    when any part is, else "graded" when any part is, else none."""
    found = {p.attrs.get("ocr") for p in parts}
    if "ungraded" in found:
        node.attrs["ocr"] = "ungraded"
    elif "graded" in found:
        node.attrs["ocr"] = "graded"


def outline_toc(doc: Document, bookmarks: list) -> int:
    """Mark the headings the bookmarks name, so the contents lists the author's outline.

    A bookmark names a heading when its title and the heading's text start alike on the
    bookmark's page (case, spacing and dashes folded: _outline_key). When at least OUTLINE_MATCH_SHARE of at least
    OUTLINE_MIN bookmarks do, each matched heading gets attrs["outline"] = {"title", "level"}
    (the bookmark's title and its depth, 1 at the top) and the root attrs["toc"] = "bookmarks";
    render.toc.toc_entries then lists exactly those headings. Otherwise nothing is marked and
    the contents lists the headings. Returns the number matched."""
    headings: dict[int, list[Node]] = {}
    for node in doc.root.children:
        if node.role in HEADING_ROLES and node.text and node.page is not None:
            headings.setdefault(node.page, []).append(node)
    taken: set[int] = set()
    found: list[tuple[Node, object]] = []
    usable = [b for b in bookmarks if b.page is not None and b.title.strip()]
    for bookmark in usable:
        title = _outline_key(bookmark.title)
        for node in headings.get(bookmark.page, ()):
            text = _outline_key(node.text)
            if id(node) not in taken and node.role != Role.H1 and (
                    text.startswith(title) or title.startswith(text)):
                taken.add(id(node))
                found.append((node, bookmark))
                break
    if len(usable) < OUTLINE_MIN or len(found) < OUTLINE_MATCH_SHARE * len(usable):
        return 0
    for node, bookmark in found:
        node.attrs["outline"] = {"title": squash(bookmark.title),
                                 "level": bookmark.level}
    doc.root.attrs["toc"] = "bookmarks"
    return len(found)


def match_claims(nodes: Sequence[Node], claims: Sequence[Claim]) -> list[Match]:
    """Pair each node that has a bbox with the claims covering it (share >= MATCH_SHARE of
    the smaller box). A claim may cover several nodes (a tagged /P the heuristics split in
    two) and a node several claims (two tagged /P the heuristics joined); both are kept, the
    best (highest IoU) first. The order comparison works on the pairs."""
    matches = []
    boxes = [(claim, claim.bbox) for claim in claims]
    for node in nodes:
        if node.bbox is None:
            continue
        nx0, ntop, nx1, nbottom = node.bbox
        node_area = geometry.area(node.bbox)
        scored = []
        for claim, box in boxes:
            # A share of at least MATCH_SHARE (> 0) needs the boxes to overlap: skip the
            # claims that are clear of the node before measuring anything.
            if box[2] <= nx0 or box[0] >= nx1 or box[3] <= ntop or box[1] >= nbottom:
                continue
            # geometry.share(node, box), share(box, node) and iou(node, box), from one overlap.
            shared = geometry.overlap(node.bbox, box)
            box_area = geometry.area(box)
            part = shared / node_area if node_area else 0.0
            whole = shared / box_area if box_area else 0.0
            if max(part, whole) >= MATCH_SHARE:
                union_area = node_area + box_area - shared
                scored.append((shared / union_area if union_area > 0 else 0.0, claim))
        if scored:
            scored.sort(key=lambda pair: -pair[0])   # stable: the witness' order breaks ties
            matches.append(Match(node, [c for _, c in scored], scored[0][0]))
    return matches


# What a node is, for deciding whether a witness' role contradicts it. Roles in the same kind
# agree: a heuristic paragraph under a tagged /Note, /Caption or /LI is not a disagreement.
KIND = {**{r: "heading" for r in HEADING_ROLES}, Role.P: "text", Role.NOTE: "text",
        Role.CAPTION: "text", Role.LI: "text", Role.TABLE: "table", Role.FIGURE: "figure",
        Role.LIST: "list"}
# A heading's level is compared after removing the document's usual offset between the
# witness' levels and ours (a promoted title moves every other heading down one).
LEVEL_OFFSET_MIN = 3
# When more than this share of at least LEVEL_DOC_MIN compared headings disagree, the outlines
# follow different schemes: one document-level fact, no per-heading warnings.
LEVEL_DOC_MIN = 8
LEVEL_DOC_SHARE = 0.25


def _level(role: Role) -> int | None:
    return HEADING_LEVEL.get(role)


def _outline_key(text: str) -> str:
    """A bookmark title or a heading with no spaces and one kind of dash, casefolded: a
    bookmark may run two words together ("deplanificación familiar") or set "-" for the
    heading's "–"."""
    return re.sub(r"[\s\u2010-\u2015]", lambda m: "" if m[0].isspace() else "-", text).casefold()


@dataclass(frozen=True)
class Annotation:
    """A Fact to append to a node and, with it, the attributes a witness fills on it (`filled`
    names the one the report counts)."""
    node: Node
    fact: Fact
    attrs: tuple[tuple[str, object], ...] = ()
    filled: str | None = None


@dataclass(frozen=True)
class FusionPlan:
    """The decision of `plan_fusion`: the annotations, in the order `apply_fusion` makes them."""
    annotations: tuple[Annotation, ...]
    pages_compared: int


def apply_fusion(plan: FusionPlan) -> FusionReport:
    """Append the planned facts and fill the planned attributes, in order: the one place `fuse`
    changes the tree. Returns what it did."""
    report = FusionReport(pages_compared=plan.pages_compared)
    for note in plan.annotations:
        note.node.attrs.update(dict(note.attrs))  # type: ignore[typeddict-item]
        note.node.facts.append(note.fact)
        report.facts[note.fact.code] = report.facts.get(note.fact.code, 0) + 1
        if note.filled is not None:
            report.filled[note.filled] = report.filled.get(note.filled, 0) + 1
    return report


def fuse(doc: Document, witnesses: Iterable[Witness], *,
         pages: Iterable[int] | None = None) -> FusionReport:
    """Record every witness' agreement with the tree as Facts, and fill the attributes a
    trusted witness is authoritative on: `plan_fusion` decides, `apply_fusion` appends and
    fills. `pages` limits the work; by default every page a witness knows about. Nothing here
    changes node.confidence (that is confidence.apply's job) or a node's role.

    Per page and witness, the page's nodes with a box are matched to its claims by geometry
    (`match_claims`), then for each node and the best claim that has a role:
      * same kind of block: Fact "role-agreement"; a different kind (a heading the tags call
        a /P, a paragraph the tags call a /Table): "role-disagreement". Only a trusted
        witness is compared; a claim with no role (a /Div) says nothing;
      * a heading both name: its level is compared with the claim's after the document's
        usual offset between the two is removed; a bookmark's level that fits is "bookmark-level",
        one that does not is "level-disagreement" (data: ours, theirs, offset) only when the
        heading also nests differently from the one before it; when more than LEVEL_DOC_SHARE
        of the compared headings do, one "levels-unreliable" fact on the root replaces them;
      * a figure with no alt text takes the claim's /Alt unless it is junk (`useful_alt`),
        with attrs["alt-source"] = "tags" (Fact "tag-alt");
      * a header cell takes the /Scope of the tagged header at its position (geometry inside
        the matched table, the text as a check; none when two tagged headers could be it)
        when that scope is "row" or "both" (Fact "tag-scope"); "col" is what the renderer
        assumes already; a cell in the table's header rows keeps it, as it names a column.
    A node on a page where a trusted tag tree exists but that no claim covers gets "untagged".
    The reading-order comparison is analyze.order_evidence's."""
    return apply_fusion(plan_fusion(doc, witnesses, pages=pages))


def plan_fusion(doc: Document, witnesses: Iterable[Witness], *,
                pages: Iterable[int] | None = None) -> FusionPlan:
    """What `fuse` says about the tree, as annotations: pure, it changes no node. A figure's alt
    and a header's scope are filled only where the node has none, so the nodes a witness has
    filled are remembered (`filled`) for the witnesses after it."""
    edits: list[Annotation] = []
    filled: set[tuple[int, str]] = set()
    pages_compared = 0
    wanted = None if pages is None else set(pages)
    by_page: dict[int, list[Node]] = {}
    for node in doc.root.walk():
        if (node.page is not None and node.bbox is not None and node.role != Role.ARTIFACT
                and not node.attrs.get("figure_label")):  # a label is part of its figure
            by_page.setdefault(node.page, []).append(node)
    for witness in witnesses:
        found: list[tuple[Node, Match, PageClaims]] = []
        for number in sorted(witness.pages()):
            if wanted is not None and number not in wanted:
                continue
            claims = witness.claims(number)
            if claims is None or not claims.order:
                continue
            pages_compared += 1
            nodes = by_page.get(number, [])
            matches = match_claims(nodes, claims.order)
            matched = {id(m.node) for m in matches}
            for match in matches:
                found.append((match.node, match, claims))
            if claims.trusted and witness.kind is WitnessKind.TAG_TREE:
                for node in nodes:
                    if id(node) not in matched and node.role in KIND:
                        edits.append(Annotation(node, Fact("untagged", witness.source,
                                                           "no tag covers this block")))
            _fill_scopes(edits, filled, witness, nodes, claims)
        offset = _level_offset(found)
        conflicts, compared = _level_conflicts(found, offset)
        if compared >= LEVEL_DOC_MIN and len(conflicts) > LEVEL_DOC_SHARE * compared:
            # The two outlines are built on different schemes: say so once, not per heading.
            edits.append(Annotation(doc.root, Fact(
                "levels-unreliable", witness.source,
                f"the {witness.kind} and this outline disagree on {len(conflicts)} of "
                f"{compared} heading levels; levels were not compared one by one",
                {"disagree": len(conflicts), "compared": compared})))
            conflicts = set()
        for node, match, claims in found:
            _compare(edits, witness, node, match, claims, offset, conflicts)
    return FusionPlan(tuple(edits), pages_compared)


def _note(edits: list[Annotation], node: Node, fact: Fact) -> None:
    edits.append(Annotation(node, fact))


def _role_claim(match: Match) -> Claim | None:
    return next((c for c in match.claims if c.role is not None), None)


def _level_pairs(found: list[tuple[Node, Match, PageClaims]]) -> list[tuple[Node, int, int]]:
    """(node, our level, the witness' level) for the headings both name, in document order."""
    pairs = []
    for node, match, claims in found:
        claim = _role_claim(match)
        if claims.trusted and claim and _level(node.role) and claim.attrs.get("level"):
            pairs.append((node, _level(node.role), claim.attrs["level"]))
    return pairs


def _level_offset(found: list[tuple[Node, Match, PageClaims]]) -> int:
    """The usual (our level - the witness' level) over the headings both name, 0 when fewer
    than LEVEL_OFFSET_MIN agree to say."""
    gaps = [ours - theirs for _, ours, theirs in _level_pairs(found)]
    if len(gaps) < LEVEL_OFFSET_MIN:
        return 0
    return max(set(gaps), key=gaps.count)


def _sign(n: int) -> int:
    return (n > 0) - (n < 0)


def _level_conflicts(found: list[tuple[Node, Match, PageClaims]],
                     offset: int) -> tuple[set[int], int]:
    """The ids of the headings whose level really disagrees, and how many were compared.

    One offset cannot describe a document whose sections are outlined differently (a form
    whose tags put every "Line N" two levels below the part, ours one), so a level that is off
    the offset only counts when the heading also stands in a different relation to the
    heading before it (deeper, same, shallower) in the two outlines. The first heading has
    no predecessor and is never flagged."""
    pairs = _level_pairs(found)
    conflicts: set[int] = set()
    for (_, pours, ptheirs), (node, ours, theirs) in zip(pairs, pairs[1:]):
        if ours - theirs != offset and _sign(ours - pours) != _sign(theirs - ptheirs):
            conflicts.add(id(node))
    return conflicts, max(len(pairs) - 1, 0)


def _compare(edits: list[Annotation], witness: Witness, node: Node, match: Match,
             claims: PageClaims, offset: int, conflicts: set[int]) -> None:
    claim = _role_claim(match)
    if claim is None or not claims.trusted:
        return
    ours, theirs = KIND.get(node.role), KIND.get(claim.role)
    if ours is None or theirs is None:
        return
    if witness.kind is WitnessKind.BOOKMARKS and ours not in ("heading", "text"):
        return                      # a bookmark's band can lie over a table or a figure
    if ours != theirs:
        if ours in ("list", "table") and (match.share < MATCH_SHARE or theirs == "text"):
            return                  # a /LI, /P or /TD inside our list or table is a member, not a rival
        if theirs in ("list", "table", "figure") and match.share < MATCH_SHARE:
            return                  # our block lies inside a tagged list, table or figure (a chart's labels)
        _note(edits, node, Fact("role-disagreement", witness.source,
                                 f"the {witness.kind} says {claim.role.value}, not {node.role.value}",
                                 {"ours": node.role.value, "theirs": claim.role.value}))
        return
    level, said = _level(node.role), claim.attrs.get("level")
    if level is None or said is None:
        _note(edits, node, Fact("role-agreement", witness.source,
                                 f"the {witness.kind} agrees it is {claim.role.value}"))
        return
    data = {"ours": level, "theirs": said, "offset": offset}
    if level - said == offset:
        code = "bookmark-level" if witness.kind is WitnessKind.BOOKMARKS else "role-agreement"
        _note(edits, node, Fact(code, witness.source,
                                 f"the {witness.kind} give level {said}", data))
    elif id(node) in conflicts:
        _note(edits, node, Fact("level-disagreement", witness.source,
                                 f"the {witness.kind} give level {said}, this is level {level}",
                                 data))
    else:       # off the usual offset, but nested the way the witness nests it
        _note(edits, node, Fact("role-agreement", witness.source,
                                 f"the {witness.kind} agrees it is {claim.role.value}"))


JUNK_ALT = frozenset({"image", "picture", "figure", "graphic", "photo", "photograph", "img",
                      "logo", "icon", "untitled", "image not available", "null", "none"})
_FILE_ALT = re.compile(r"^[\w\- .()]*\.(png|jpe?g|gif|bmp|tiff?|svg|webp|emf|wmf|pdf)$", re.I)


def useful_alt(alt: object) -> str | None:
    """The alt text if it says something: not empty, not a generic word ("image", "figure"),
    not a file name, not "untitled" or "image 3"."""
    if not isinstance(alt, str):
        return None
    text = squash(alt)
    plain = re.sub(r"[\W_]+", " ", text).strip().casefold()
    plain = re.sub(r"\s*\d+$", "", plain)         # "Image 3", "figure_12"
    if not plain or plain in JUNK_ALT or _FILE_ALT.match(text):
        return None
    return text


def _header_claim(cell: Node, scoped: list[Claim]) -> Claim | None:
    """The tagged header that is this cell: the one over its box (the most overlap), when the
    text agrees (or one side has none). Without a box on the cell, the one tagged header with
    its text. None when two tagged headers could be it."""
    text = fold(cell.text or "")
    if cell.bbox is not None:
        near = [(geometry.iou(cell.bbox, c.bbox), c) for c in scoped
                if max(geometry.share(cell.bbox, c.bbox), geometry.share(c.bbox, cell.bbox))
                >= MATCH_SHARE]
        if not near:
            return None
        near.sort(key=lambda pair: -pair[0])
        if len(near) > 1 and near[0][0] - near[1][0] < 0.1 \
                and near[0][1].attrs.get("scope") != near[1][1].attrs.get("scope"):
            return None
        claim = near[0][1]
        said = fold(claim.text or "")
        if text and said and text != said and text not in said and said not in text:
            return None
        return claim
    same = [c for c in scoped if fold(c.text or "") == text]
    return same[0] if text and len(same) == 1 else None


def _fill_scopes(edits: list[Annotation], filled: set[tuple[int, str]], witness: Witness,
                 nodes: list[Node], claims: PageClaims) -> None:
    """Alt text for figures, /Scope for header cells, as annotations. Only a trusted witness
    fills. `filled` holds the (node, attribute) pairs already planned, which are no longer
    empty."""
    if not claims.trusted:
        return
    figures = [n for n in nodes if n.role == Role.FIGURE and not n.attrs.get("alt")
               and (id(n), "alt") not in filled]
    for figure in figures:
        for claim in claims.order:
            alt = useful_alt(claim.attrs.get("alt"))
            if (alt and claim.role == Role.FIGURE
                    and geometry.iou(figure.bbox, claim.bbox) >= MATCH_SHARE):
                filled.add((id(figure), "alt"))
                edits.append(Annotation(
                    figure, Fact("tag-alt", witness.source, "alt text from the tag tree"),
                    (("alt", alt), ("alt-source", "tags")), "alt"))
                break
    headers = [c for c in claims.order if c.role == Role.TH and c.attrs.get("scope")]
    if not headers:
        return
    for table in (n for n in nodes if n.role == Role.TABLE):
        inside = [c for c in headers if geometry.share(c.bbox, table.bbox) >= MATCH_SHARE]
        # A cell of the header rows names a column, whatever the tags say: a tagger who calls
        # a table's first column its row headers tags the corner cell ("Directory for:") the
        # same, and a header band with a row header in it is no band (render.tables.header_rows).
        rows = [tr for tr in table.children if tr.role == Role.TR]
        in_header = {id(cell) for tr in rows[:int(table.attrs.get("header_rows", 0))]
                     for cell in tr.children}
        for cell in table.walk():
            if (cell.role != Role.TH or "scope" in cell.attrs or id(cell) in in_header
                    or (id(cell), "scope") in filled):
                continue
            header = _header_claim(cell, inside)
            if header is not None and header.attrs["scope"] in ("row", "both"):
                filled.add((id(cell), "scope"))
                edits.append(Annotation(
                    cell, Fact("tag-scope", witness.source,
                               f"scope {header.attrs['scope']} from the tag tree"),
                    (("scope", header.attrs["scope"]),), "scope"))
