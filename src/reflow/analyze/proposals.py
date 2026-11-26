"""Structure proposals and their review: a model may propose a change; only a reviewer's
accept lets it change the output, and only while the document's text is what the reviewer saw.

The life of a proposal:

    Proposal ──(reviewer: reject)──▶ REJECTED        nothing changes
        │      (no decision)    ──▶ PENDING          nothing changes
        └─────(reviewer: accept)──▶ Accepted ──apply──▶ APPLIED   the text is unchanged
                                                   └─▶ STALE     the text or the block moved on

`Accepted` is sealed: `review` is the only code that makes one, from a decision whose verdict
is accept, and `apply` takes nothing else. So a proposal that is applied without being accepted
can't be written down. `apply` first compares the document's text (`text_digest`, every node's
text in document order) with the digest the reviewer accepted, and leaves the document alone if
they differ. A heading change never touches text, and `apply` checks that too. An unread crop
takes exactly its own paragraph's text out of the transcript, into the crop's `ocr_guess`, and
`apply` checks that nothing else changed; the PDF's text layer is never touched.

A proposal names one closed change to one block by its node id: demote a heading to a paragraph,
promote a paragraph to a heading, or hide an OCR paragraph in an unread crop. Node ids are made from the page, box and text
(model.make_id), so a decision outlives a rebuild that lays the page out the same way.

Decisions live in PROPOSAL_DECISIONS_JSON in the output directory, written by `reflow review` and carried
from one conversion to the next (names.CARRIED). The file is parsed at its boundary: anything
but {proposal id: {"verdict": "accept" | "reject", "text": <digest>}} is refused.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import NewType

from reflow.analyze.decisions import Verdict
from reflow.analyze.figures import UNREAD_KINDS, make_unread
from reflow.analyze.headings import fix_heading_levels
from reflow.analyze.sections import nest_sections
from reflow.model import Document, Node, Role, SUBHEADING_ROLES, Source

ProposalId = NewType("ProposalId", str)
TextDigest = NewType("TextDigest", str)


class Change(StrEnum):
    DEMOTE_HEADING = "demote-heading"     # an h2..h6 becomes a paragraph
    PROMOTE_HEADING = "promote-heading"   # a paragraph becomes a heading
    UNREAD_CROP = "unread-crop"           # an OCR paragraph becomes an unread crop


# The role a block must have now for each change to apply to it.
APPLIES_TO: dict[Change, frozenset[Role]] = {
    Change.DEMOTE_HEADING: SUBHEADING_ROLES,
    Change.PROMOTE_HEADING: frozenset({Role.P}),
    Change.UNREAD_CROP: frozenset({Role.P}),
}


class Status(StrEnum):
    PENDING = "pending"     # no decision: nothing changes
    REJECTED = "rejected"   # nothing changes
    APPLIED = "applied"     # accepted, the text unchanged, the change made
    STALE = "stale"         # accepted, but the text or the block is not what was reviewed


def text_digest(doc: Document, without: str | None = None) -> TextDigest:
    """The document's text: every node's text in document order, roles and structure aside;
    with `without`, the text of every node but that one (what an unread crop must leave)."""
    h = hashlib.sha256()
    for node in doc.root.walk():
        if node.text is not None and node.id != without:
            h.update(node.text.encode("utf-8"))
            h.update(b"\0")
    return TextDigest(h.hexdigest())


@dataclass(frozen=True)
class Proposal:
    """One change a model proposes to one block. `word` is the model's answer (a fixed word),
    `text` the block's own text (for the reviewer; the model never writes it) and
    `document_text` the document's text when the proposal was made. `doubt` is the rule that
    already doubts an unread crop's paragraph (the second witness), else empty."""
    id: ProposalId
    change: Change
    node: str
    page: int
    was: Role
    word: str
    text: str
    document_text: TextDigest
    model: str
    doubt: str = ""

    @classmethod
    def make(cls, change: Change, node: str, page: int, was: Role, word: str, text: str,
             document_text: TextDigest, model: str, doubt: str = "") -> Proposal:
        """The id names the change, the block and its role: the same question on a rebuild is
        the same proposal, whatever the model's word or the rest of the text."""
        digest = hashlib.sha256(json.dumps([change.value, node, was.value]).encode())
        return cls(ProposalId("pv-" + digest.hexdigest()[:12]), change, node, page, was, word,
                   text, document_text, model, doubt)

    def to_dict(self, status: Status) -> dict:
        out = {"id": self.id, "change": self.change.value, "node": self.node,
               "page": self.page, "was": self.was.value, "word": self.word, "text": self.text,
               "document_text": self.document_text, "model": self.model,
               "status": status.value}
        if self.doubt:
            out["doubt"] = self.doubt
        return out


@dataclass(frozen=True)
class Decided:
    """A reviewer's decision on one proposal, and the document text they saw it against."""
    verdict: Verdict
    text: TextDigest


@dataclass(frozen=True)
class Decisions:
    by_id: Mapping[ProposalId, Decided]

    def get(self, pid: ProposalId) -> Decided | None:
        return self.by_id.get(pid)


_SEAL = object()


@dataclass(frozen=True)
class Accepted:
    """A proposal a reviewer accepted, with the text digest they accepted it against. Made only
    by `review`; constructing one anywhere else raises TypeError."""
    proposal: Proposal
    reviewed_text: TextDigest
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _SEAL:
            raise TypeError("an Accepted proposal comes only from a reviewer's accept "
                            "(proposals.review)")


@dataclass(frozen=True)
class Reviewed:
    status: dict[ProposalId, Status]   # the undecided (PENDING) and the REJECTED
    accepted: list[Accepted]           # the rest, to `apply`


def review(proposals: Iterable[Proposal], decisions: Decisions) -> Reviewed:
    status: dict[ProposalId, Status] = {}
    accepted: list[Accepted] = []
    for p in proposals:
        decided = decisions.get(p.id)
        if decided is None:
            status[p.id] = Status.PENDING
        elif decided.verdict is Verdict.REJECT:
            status[p.id] = Status.REJECTED
        else:
            accepted.append(Accepted(p, decided.text, _SEAL))
    return Reviewed(status, accepted)


def _promoted_role(flat: list[Node], node: Node) -> Role:
    """A promoted paragraph is a sibling of the heading before it (h2 with none)."""
    before = None
    for n in flat:
        if n is node:
            break
        if n.role in SUBHEADING_ROLES:
            before = n.role
    return before or Role.H2


def _unread_crop(node: Node, p: Proposal) -> None:
    """Turn the paragraph into an unread crop as figures.make_unread makes them: a figure
    of its box, reflow's note as its alt and its review code, OCR's reading kept only as
    `ocr_guess`. The node keeps its id, page and box."""
    pen = p.word == "handwriting"
    kind, alt, code = UNREAD_KINDS[pen]
    guess = node.text or ""
    why = [f"figure: a reviewer accepted the AI page check's reading as "
           f"{p.word.replace('_', ' ')}, and {p.doubt or 'a rule doubts its OCR'}"]
    node.role = Role.FIGURE
    node.spans, node.marks, node.children = [], [], []
    node.set_text(None)
    make_unread(node, kind, alt, code, pen, guess, why)


def apply(doc: Document, accepted: Accepted, seen: TextDigest | None = None) -> Status:
    """Make an accepted change, if the document's text is the text the reviewer accepted it
    against and the block is still the kind the change is for (a heading to demote, a paragraph
    to promote or crop; an earlier change may have renumbered a heading's level); else STALE,
    and nothing changes. `seen` is the document's text before this run's accepted changes
    (`settle` passes it): an unread crop accepted earlier in the run takes its own text out,
    which is not a change to the text the reviewer saw."""
    if not isinstance(accepted, Accepted):
        raise TypeError("only an Accepted proposal can be applied")
    p = accepted.proposal
    before = text_digest(doc)
    if (seen or before) != accepted.reviewed_text:
        return Status.STALE
    flat = list(doc.flow())
    node = next((n for n in flat if n.id == p.node), None)
    if node is None or node.role not in APPLIES_TO[p.change]:
        return Status.STALE
    word = p.word.replace("_", " ")
    if p.change is Change.UNREAD_CROP:
        if node.bbox is None:
            return Status.STALE
        expected = text_digest(doc, without=node.id)
        _unread_crop(node, p)
        node.fact("vision-accepted", Source.HUMAN,
                  f"unread crop accepted in review ({p.model} read it as {word})",
                  proposal=p.id, model=p.model, word=p.word, was=p.was.value)
        if text_digest(doc) != expected:   # only this paragraph's text leaves the transcript
            raise AssertionError(f"applying {p.id} changed text beyond its paragraph")
        return Status.APPLIED
    if p.change is Change.DEMOTE_HEADING:
        node.role = Role.P
        why = f"paragraph: a reviewer accepted the AI page check's reading as {word}"
    else:
        node.role = _promoted_role(flat, node)
        why = "heading: a reviewer accepted the AI page check's reading as a heading"
    node.attrs.setdefault("why", []).append(why)
    node.fact("vision-accepted", Source.HUMAN,
              f"{p.change.value.replace('-', ' ')} accepted in review ({p.model} read it as "
              f"{word})", proposal=p.id, model=p.model, word=p.word, was=p.was.value)
    fix_heading_levels(flat)
    doc.root.children = nest_sections(flat)
    if text_digest(doc) != before:   # a change never touches text
        raise AssertionError(f"applying {p.id} changed the document's text")
    return Status.APPLIED


# -- the decisions file ------------------------------------------------------------------------


def parse_decisions(raw: object) -> Decisions:
    """The decisions file's JSON, or ValueError for anything else."""
    if not isinstance(raw, dict):
        raise ValueError("decisions must be an object of proposal id → decision")
    out: dict[ProposalId, Decided] = {}
    for pid, entry in raw.items():
        if not (isinstance(pid, str) and pid.startswith("pv-")):
            raise ValueError(f"not a proposal id: {pid!r}")
        if not (isinstance(entry, dict) and set(entry) == {"verdict", "text"}):
            raise ValueError(f"{pid}: a decision is exactly {{verdict, text}}")
        verdict, text = entry["verdict"], entry["text"]
        if verdict not in (v.value for v in Verdict):
            raise ValueError(f"{pid}: verdict must be accept or reject, not {verdict!r}")
        if not (isinstance(text, str) and len(text) == 64
                and all(c in "0123456789abcdef" for c in text)):
            raise ValueError(f"{pid}: text must be a sha256 digest")
        out[ProposalId(pid)] = Decided(Verdict(verdict), TextDigest(text))
    return Decisions(out)


def load_decisions(path: Path) -> Decisions:
    """The decisions in `path`; none when it doesn't exist. ValueError when it is not valid."""
    try:
        body = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Decisions({})
    try:
        raw = json.loads(body)
    except ValueError as e:
        raise ValueError(f"{path}: not JSON ({e})") from None
    try:
        return parse_decisions(raw)
    except ValueError as e:
        raise ValueError(f"{path}: {e}") from None


def save_decisions(path: Path, decisions: Decisions) -> None:
    body = {pid: {"verdict": d.verdict.value, "text": d.text}
            for pid, d in sorted(decisions.by_id.items())}
    path.write_text(json.dumps(body, indent=1) + "\n", encoding="utf-8")


__all__ = ["Accepted", "Change", "Decided", "Decisions", "Proposal",
           "ProposalId", "Reviewed", "Status", "TextDigest", "apply", "load_decisions",
           "review", "save_decisions", "text_digest"]
