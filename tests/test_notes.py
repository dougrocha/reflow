import re
from reflow.analyze import notes
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent
from reflow.model import Node, Role, Span

BODY = 10.0


def body_line(text, top, sup=()):
    return Line(1, (50, top, 280, top + 10), text, BODY, False, False, "F", sup=tuple(sup))


def foot_line(text, top):
    return Line(1, (50, top, 280, top + 7), text, 7.0, False, False, "F")


def page(*lines):
    return PageContent(1, 612, 792, list(lines), [])


def para(text, top, bottom, role=Role.P):
    return Node(role, page=1, bbox=(50, top, 280, bottom), text=text)


def scene():
    """A body paragraph with a superscript 1 after "competitions" and the note at the foot."""
    text = "Deep nets win competitions1 , where we also won."
    marker = text.index("1")
    lines = page(body_line(text, 100, sup=[(marker, marker + 1)]),
                 foot_line("1 http://example.org/challenge", 720))
    body = para(text, 100, 110)
    foot = para("1 http://example.org/challenge", 720, 727)
    return lines, body, foot, marker


def test_footnote_becomes_a_note_with_a_reference_span():
    lines, body, foot, marker = scene()
    nodes = [body, foot]
    assert notes.attach_footnotes(nodes, [lines], BODY) == 1
    assert foot.role == Role.NOTE
    assert foot.attrs["marker"] == "1"
    assert foot.text == "http://example.org/challenge"
    assert body.spans == [Span(marker, marker + 1, "noteref", foot.id)]
    assert body.text[body.spans[0].start:body.spans[0].end] == "1"


def test_note_moves_to_the_end_of_its_page_in_order():
    text = "Alpha one1 beta two2 gamma."
    one, two = text.index("1"), text.index("2")
    lines = page(body_line(text, 100, sup=[(one, one + 1), (two, two + 1)]),
                 foot_line("1 First note.", 700), foot_line("2 Second note.", 712))
    body = para(text, 100, 110)
    first = para("1 First note.", 700, 707)
    second = para("2 Second note.", 712, 719)
    after = Node(Role.P, page=1, bbox=(50, 300, 280, 310), text="Middle paragraph.")
    nodes = [body, first, second, after]
    assert notes.attach_footnotes(nodes, [lines], BODY) == 2
    assert [n.text for n in nodes] == [text, "Middle paragraph.", "First note.", "Second note."]
    assert [s.target for s in body.spans] == [first.id, second.id]


def test_no_reference_means_no_change():
    lines = page(body_line("Plain body text.", 100), foot_line("1 Not a note.", 720))
    body, foot = para("Plain body text.", 100, 110), para("1 Not a note.", 720, 727)
    nodes = [body, foot]
    assert notes.attach_footnotes(nodes, [lines], BODY) == 0
    assert foot.role == Role.P and foot.text == "1 Not a note."
    assert body.spans == [] and nodes == [body, foot]


def test_body_size_text_at_the_foot_is_not_a_note():
    text = "Deep nets win competitions1 , where"
    at = text.index("1")
    lines = page(body_line(text, 100, sup=[(at, at + 1)]),
                 body_line("1 A last paragraph set at the body size.", 720))
    body = para(text, 100, 110)
    last = para("1 A last paragraph set at the body size.", 720, 730)
    assert notes.attach_footnotes([body, last], [lines], BODY) == 0


def test_a_marker_that_differs_does_not_pair():
    lines, body, foot, _ = scene()
    foot.set_text("2 http://example.org/challenge")
    lines.lines[1].text = foot.text
    assert notes.attach_footnotes([body, foot], [lines], BODY) == 0


def test_find_references_reads_raised_runs_that_match():
    lines = page(body_line("a1 b2", 100, sup=[(1, 2), (4, 5)]))
    assert notes.find_references(lines, {"2"}) == [(0, "2", 4)]
    assert notes.find_references(lines, {"1", "2"}) == [(0, "1", 1), (0, "2", 4)]
    assert notes.find_references(lines, {"9"}) == []


def test_link_spans_on_the_note_shift_with_the_stripped_marker():
    lines, body, foot, _ = scene()
    foot.spans = [Span(2, 9, "link", "http://example.org/challenge")]
    notes.attach_footnotes([body, foot], [lines], BODY)
    assert foot.spans == [Span(0, 7, "link", "http://example.org/challenge")]


def two_refs_scene():
    text = "First mention1 here and second mention1 there."
    ones = [i for i, ch in enumerate(text) if ch == "1"]
    lines = page(body_line(text, 100, sup=[(i, i + 1) for i in ones]),
                 foot_line("1 The note.", 720))
    return lines, para(text, 100, 110), para("1 The note.", 720, 727)


def test_every_reference_to_a_note_is_marked():
    lines, body, foot = two_refs_scene()
    assert notes.attach_footnotes([body, foot], [lines], BODY) == 1
    assert [s.target for s in body.spans] == [foot.id, f"{foot.id}#2"]
    assert foot.attrs["refs"] == 2


def test_rendered_references_have_unique_ids_and_each_has_a_backlink():
    from reflow.render.html import NodeRenderer
    lines, body, foot = two_refs_scene()
    notes.attach_footnotes([body, foot], [lines], BODY)
    r = NodeRenderer({}, {})
    html = r.render(body) + r.render(foot)
    ids = re.findall(r'\bid="([^"]+)"', html)
    assert len(ids) == len(set(ids))
    assert f'id="r-{foot.id}"' in html and f'id="r-{foot.id}-2"' in html
    backs = re.findall(r'role="doc-backlink" href="[^"]*#?[^"]*r-([^"\x00]+)', html)
    assert len(backs) == 2


def letter_scene(letters):
    """Raised letters after "word" in the body, a foot paragraph per letter."""
    text = "Some words" + "".join(f"{c} and" for c in letters) + " end."
    runs = []
    at = 0
    for c in letters:
        i = text.index(f"{c} and", at)
        runs.append((i, i + 1))
        at = i + 1
    feet = [foot_line(f"{c} Note {c}.", 700 + 10 * k) for k, c in enumerate(letters)]
    lines = page(body_line(text, 100, sup=runs), *feet)
    body = para(text, 100, 110)
    foots = [para(f"{c} Note {c}.", 700 + 10 * k, 707 + 10 * k) for k, c in enumerate(letters)]
    return lines, body, foots


def test_a_lone_raised_letter_is_not_a_note_marker():
    lines, body, foots = letter_scene("a")
    assert notes.attach_footnotes([body, *foots], [lines], BODY) == 0
    assert foots[0].role == Role.P


def test_lettered_footnotes_in_sequence_pair():
    lines, body, foots = letter_scene("abc")
    assert notes.attach_footnotes([body, *foots], [lines], BODY) == 3
    assert [f.attrs["marker"] for f in foots] == ["a", "b", "c"]
