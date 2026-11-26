from pathlib import Path

import pikepdf
import pytest
from conftest import make_pdf
from pikepdf import Array, Dictionary, Name, String

from reflow.extract.links import offsets_of, read_links

SAMPLES = Path(__file__).parent.parent / "samples"

# make_pdf's "text" page draws "Hello accessible world" at (72, 720), Helvetica 12.
HELLO = [70, 715, 110, 735]        # user space, covers "Hello"
ACCESSIBLE = [104, 715, 160, 735]  # covers "accessible"


def _annotate(path: Path, build) -> Path:
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        build(pdf)
        pdf.save(path)
    return path


def _link(rect, action) -> Dictionary:
    return Dictionary(Type=Name.Annot, Subtype=Name.Link, Rect=Array(rect), A=action)


def _uri(url: str) -> Dictionary:
    return Dictionary(S=Name.URI, URI=String(url))


def _goto(pdf, page_index: int, top: float) -> Dictionary:
    dest = Array([pdf.pages[page_index].obj, Name.XYZ, 0, top, 0])
    return Dictionary(S=Name.GoTo, D=dest)


def test_uri_link_covers_known_words(tmp_path):
    path = make_pdf(tmp_path / "uri.pdf", ["text"])
    _annotate(path, lambda pdf: pdf.pages[0].obj.__setitem__(
        Name.Annots, Array([_link(HELLO, _uri("https://example.com"))])))

    links = read_links(path)

    assert list(links) == [1]
    (link,) = links[1]
    assert link.kind == "uri"
    assert link.target == "https://example.com"
    assert link.words == ("Hello",)
    assert link.dest_top is None


def test_goto_resolves_to_page_and_destination_top(tmp_path):
    path = make_pdf(tmp_path / "goto.pdf", ["text", "text"])

    def build(pdf):
        pdf.pages[0].obj[Name.Annots] = Array(
            [_link(ACCESSIBLE, _goto(pdf, 1, 500))])

    _annotate(path, build)

    (link,) = read_links(path)[1]
    assert link.kind == "page"
    assert link.target == "#p2"
    assert link.words == ("accessible",)
    # XYZ top 500 on a 792-point page is 292 points from the top.
    assert link.dest_top == pytest.approx(292.0)


def test_named_destination_through_name_tree(tmp_path):
    path = make_pdf(tmp_path / "named.pdf", ["text", "text"])

    def build(pdf):
        target = Array([pdf.pages[1].obj, Name.FitH, 700])
        pdf.Root[Name.Names] = Dictionary(
            Dests=Dictionary(Names=Array([String("intro"), target])))
        pdf.pages[0].obj[Name.Annots] = Array([
            _link(HELLO, Dictionary(S=Name.GoTo, D=String("intro")))])

    _annotate(path, build)

    (link,) = read_links(path)[1]
    assert link.target == "#p2"
    assert link.dest_top == pytest.approx(92.0)


def test_legacy_dests_dictionary_resolves_name(tmp_path):
    path = make_pdf(tmp_path / "legacy.pdf", ["text", "text"])

    def build(pdf):
        pdf.Root[Name.Dests] = Dictionary(
            intro=Array([pdf.pages[1].obj, Name.Fit]))
        pdf.pages[0].obj[Name.Annots] = Array([
            _link(HELLO, Dictionary(S=Name.GoTo, D=Name.intro))])

    _annotate(path, build)

    (link,) = read_links(path)[1]
    assert link.target == "#p2"
    assert link.dest_top is None


def test_rotated_page_maps_rectangle_to_displayed_words(tmp_path):
    path = make_pdf(tmp_path / "rot.pdf", ["text"])

    def build(pdf):
        pdf.pages[0].obj[Name.Rotate] = 90
        pdf.pages[0].obj[Name.Annots] = Array(
            [_link(HELLO, _uri("https://example.com"))])

    _annotate(path, build)

    (link,) = read_links(path)[1]
    assert link.words == ("Hello",)


def test_launch_and_javascript_actions_are_dropped(tmp_path):
    path = make_pdf(tmp_path / "launch.pdf", ["text"])
    launch = Dictionary(S=Name.Launch, F=String("run.exe"))
    script = Dictionary(S=Name.JavaScript, JS=String("app.alert(1)"))
    _annotate(path, lambda pdf: pdf.pages[0].obj.__setitem__(
        Name.Annots, Array([_link(HELLO, launch), _link(ACCESSIBLE, script)])))

    assert read_links(path) == {}


def test_pages_filter_limits_result(tmp_path):
    path = make_pdf(tmp_path / "pages.pdf", ["text", "text"])

    def build(pdf):
        for page in pdf.pages:
            page.obj[Name.Annots] = Array([_link(HELLO, _uri("https://example.com"))])

    _annotate(path, build)

    links = read_links(path, pages=[2])
    assert list(links) == [2]
    assert links[2][0].words == ("Hello",)


def test_no_annotations_gives_empty_result(tmp_path):
    path = make_pdf(tmp_path / "plain.pdf", ["text"])

    assert read_links(path) == {}


def test_offsets_of_joins_words_across_spaces():
    assert offsets_of(("Hello", "accessible"), "Hello accessible world") == (0, 16)


def test_offsets_of_is_case_insensitive():
    assert offsets_of(("hello",), "Say HELLO there") == (4, 9)


def test_offsets_of_tolerates_soft_hyphen_in_text():
    assert offsets_of(("accessible",), "ac­cessible") == (0, 11)


def test_offsets_of_tolerates_soft_hyphen_in_words():
    assert offsets_of(("ac­cessible",), "accessible") == (0, 10)


def test_offsets_of_tolerates_line_break_whitespace():
    assert offsets_of(("foo", "bar"), "foo\n  bar") == (0, 9)


def test_offsets_of_returns_none_when_absent_or_empty():
    assert offsets_of(("nope",), "Hello") is None
    assert offsets_of((), "Hello") is None


def test_p17_page_three_links():
    # Page 3 has 5 /URI and 3 internal /GoTo annotations, and all 8 have words under them.
    sample = SAMPLES / "p17.pdf"
    if not sample.exists():
        pytest.skip("samples/p17.pdf not present")

    links = read_links(sample, pages=[3]).get(3, [])

    assert len(links) == 8
    assert [link.kind for link in links].count("uri") == 5
    assert [link.kind for link in links].count("page") == 3
