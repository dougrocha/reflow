"""Where a reviewer's decision points: the anchor the converter mints (docs/review-decisions.md).

An anchor says which page, which box and which text or picture a review item was made from.
Writers copy a published anchor into a record; they never build one. `hint` is the node id when
the anchor was minted: shown to a person, never used to match.

Also the finding id: the fingerprint of one fact (docs/review-decisions.md, "Findings").
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from reflow.model import Node, Role

# The match tolerances (docs/review-decisions.md, "Anchors and matching").
EXACT_EDGE_PT = 3.0     # every edge of the box within this many points
MOVED_IOU = 0.5         # same text, box overlap at least this
REBASED_IOU = 0.85      # any text, box overlap at least this
CONTEXT_CHARS = 20      # characters of text kept on each side of a suggestion
BBOX_DECIMALS = 1       # an anchor's box is rounded to 0.1 pt
SHA_CHARS = 16          # hex digits of every hash a record carries

DOC_SCOPE: list[Any] = ["doc"]


@dataclass(frozen=True)
class Anchor:
    page: int                                   # 1-based
    bbox: tuple[float, float, float, float]     # rounded to 0.1 pt
    text_sha: str   # first 16 hex of sha256(NFC text, whitespace runs collapsed); "" for a figure
    crop_sha: str   # figures: first 16 hex of sha256(the crop's bytes); "" otherwise
    hint: str       # the node id when minted: shown to a person, never used to match

    def to_dict(self) -> dict[str, Any]:
        return {"page": self.page, "bbox": list(self.bbox), "text_sha": self.text_sha,
                "crop_sha": self.crop_sha, "hint": self.hint}


def text_sha(text: str) -> str:
    """The hash that compares two nodes' text: NFC, runs of whitespace collapsed to one space,
    ends trimmed. Used only to compare; a range is always located in the raw text."""
    norm = re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:SHA_CHARS]


def crop_sha(data: bytes) -> str:
    """A picture's identity: the hash of the crop file's bytes."""
    return hashlib.sha256(data).hexdigest()[:SHA_CHARS]


def rounded(box: Any) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = (round(float(v), BBOX_DECIMALS) for v in box)
    return (x0, y0, x1, y1)


def anchor_of(node: Node) -> Anchor | None:
    """The anchor of a node a decision can answer, or None for a node with no page or box."""
    if node.page is None or node.bbox is None:
        return None
    figure = node.role == Role.FIGURE
    return Anchor(node.page, rounded(node.bbox),
                  "" if figure else text_sha(node.text or ""),
                  str(node.attrs.get("crop_sha") or ""), node.id)


def node_scope(node: Node) -> list[Any]:
    """The scope of a node's findings: its page, rounded box and text hash."""
    box = list(rounded(node.bbox)) if node.bbox else None
    return [node.page, box, text_sha(node.text or "")]


def page_scope(number: int) -> list[Any]:
    return ["page", number]


def finding_id(scope: list[Any], code: str, detail: Any, data: Any) -> str:
    """The first 16 hex of sha256 over the canonical JSON of [scope, code, detail, data]. When
    the evidence changes, so does the id."""
    body = json.dumps([scope, code, detail, data], sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:SHA_CHARS]
