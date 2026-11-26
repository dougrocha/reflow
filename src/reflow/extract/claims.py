"""What a witness says about a page: the claims the extract readers (reflow.extract.tags,
reflow.extract.links) and the other engines produce, and that reflow.analyze.evidence fuses into
the heuristic tree. Kept apart from the fusion so a reader imports no analysis code."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from reflow.model import BBox, Role, Source


@dataclass(frozen=True)
class Claim:
    """One thing a witness says about a region of a page.

    `role` is None when the witness only knows the region exists and where it falls in the
    reading order (a tagged /P with no useful role, a Docling text item). `text` is a text
    key for matching when the geometry is ambiguous (the first and last few words).
    `attrs` carries what the witness knows: {"level": 2}, {"scope": "row"},
    {"alt": "..."}, {"href": "https://..."}, {"cells": [[...]]}, {"actual_text": "..."}."""
    bbox: BBox
    role: Role | None = None
    text: str | None = None
    attrs: dict = field(default_factory=dict)


@dataclass
class PageClaims:
    """A witness' view of one page: the claims in the witness' reading order."""
    page: int
    order: list[Claim]
    # True when the witness is well formed on this page (a tag tree with real roles, not a
    # /Div soup; a Docling result that covered the page). Ill-formed witnesses still match
    # regions but never fill attributes, and their disagreement weighs less.
    trusted: bool = True


class WitnessKind(StrEnum):
    """Which witness: the fusion branches on it, and a Fact's detail calls the witness by its
    value ("the tag tree says ...")."""
    TAG_TREE = "tag tree"
    BOOKMARKS = "bookmarks"


class Witness(Protocol):
    """A source of claims. `pages()` lists the pages it has anything to say about."""
    source: Source
    kind: WitnessKind

    def pages(self) -> set[int]: ...
    def claims(self, page: int) -> PageClaims | None: ...
