"""reflow.analyze.anchors: the anchor a decision carries, its hashes and the finding id."""

import hashlib

from reflow.analyze.anchors import Anchor, anchor_of, crop_sha, finding_id, text_sha
from reflow.model import Node, Role, Source

TEXT = "Englewood, CO 8011O"


def test_an_anchor_holds_the_page_the_rounded_box_and_the_hashes():
    node = Node(Role.P, id="n-7", page=1, bbox=(72.04, 100.06, 300.0, 112.0), text=TEXT,
                source=Source.OCR)
    a = anchor_of(node)
    assert a == Anchor(page=1, bbox=(72.0, 100.1, 300.0, 112.0), text_sha=text_sha(TEXT),
                       crop_sha="", hint="n-7")
    assert len(a.text_sha) == 16


def test_a_figure_anchor_has_a_crop_and_no_text_hash():
    figure = Node(Role.FIGURE, page=2, bbox=(10.0, 20.0, 110.0, 220.0),
                  attrs={"crop_sha": "fedcba9876543210"})
    a = anchor_of(figure)
    assert a.text_sha == "" and a.crop_sha == "fedcba9876543210"


def test_a_node_without_a_page_or_a_box_has_no_anchor():
    assert anchor_of(Node(Role.P, text="x")) is None


def test_text_sha_is_nfc_and_whitespace_insensitive_and_sixteen_hex():
    assert text_sha("é  x\n y") == text_sha("é x y")
    assert text_sha("a") != text_sha("b")
    assert len(text_sha("a")) == 16 and int(text_sha("a"), 16) >= 0


def test_crop_sha_is_the_first_sixteen_hex_of_the_bytes_hash():
    assert crop_sha(b"abc") == hashlib.sha256(b"abc").hexdigest()[:16]


def test_a_finding_id_is_canonical_and_follows_its_evidence():
    scope = [1, [72.0, 100.0, 300.0, 112.0], "0123456789abcdef"]
    a = finding_id(scope, "ocr-suggestion", "detail", {"b": 1, "a": 2})
    assert a == finding_id(scope, "ocr-suggestion", "detail", {"a": 2, "b": 1})
    assert len(a) == 16
    assert a != finding_id(scope, "ocr-suggestion", "detail", {"a": 3, "b": 1})
    assert a != finding_id(scope, "ocr-doubtful", "detail", {"a": 2, "b": 1})
    assert finding_id(["doc"], "x", "é", None) == finding_id(["doc"], "x", "é", None)
