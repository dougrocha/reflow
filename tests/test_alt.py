from reflow.model import Document, Node, PageInfo, Role
from reflow.render.alt import figure_alt, figure_headings, figure_role

BOX = (10.0, 20.0, 200.0, 140.0)


def fig(*, caption=None, **attrs):
    kids = [Node(Role.CAPTION, page=1, bbox=BOX, text=caption)] if caption else []
    return Node(Role.FIGURE, page=1, bbox=BOX, attrs=attrs, children=kids)


def resolved(node, heading=None):
    a = figure_alt(node, "4", heading)
    return a.text, a.source


def test_an_unread_figure_gives_reflows_note_even_with_a_caption():
    node = fig(caption="Signed", unread=True, alt="Signature, not transcribed.", kind="signature")
    assert resolved(node) == ("Signature, not transcribed.", "reflow")
    assert figure_role(node) == "content"


def test_an_authored_alt_is_the_authors_unless_it_repeats_the_caption():
    assert resolved(fig(alt="The plant at dusk")) == ("The plant at dusk", "author")
    assert resolved(fig(alt="A distinct alt", caption="Fig. 1")) == ("A distinct alt", "author")
    assert resolved(fig(alt="Fig.  1", caption="fig. 1", kind="photo")) == ("Photo", "caption-kind")
    assert resolved(fig(caption="Fig. 1")) == ("Figure", "caption-kind")
    assert resolved(fig(caption="Fig. 1", kind="other")) == ("Figure", "caption-kind")


def test_everything_a_model_gave_is_an_ai_draft():
    ai = {"provenance": "ai-drafted", "reviewed": False}
    assert resolved(fig(alt="Five photos", caption="Cover", **ai)) == ("Five photos", "ai-draft")
    assert resolved(fig(alt="Cover", caption="Cover", kind="photo", **ai)) == ("Photo", "ai-draft")
    decorative = fig(alt="", decorative=True, kind="other", **ai)
    assert resolved(decorative) == ("", "ai-draft")
    assert figure_role(decorative) == "decorative"
    assert figure_role(fig(alt="Acme", kind="logo", **ai)) == "logo"


def test_a_reviewers_alt_is_its_own_source_and_never_the_authors():
    assert resolved(fig(alt="Checked", provenance="reviewer")) == ("Checked", "reviewer")
    assert resolved(fig(alt="x", decorative=True, provenance="reviewer")) == ("", "reviewer")


def test_a_decorative_figure_has_an_empty_alt():
    node = fig(alt="ignored", decorative=True)
    assert resolved(node) == ("", "decorative") and figure_role(node) == "decorative"


def test_the_fallback_names_the_page_and_the_heading_and_never_figure_on_page():
    assert resolved(fig(), "Message from the  General Manager") == (
        "Picture on page 4 under “Message from the General Manager”; no description yet",
        "fallback")
    text, source = resolved(fig())
    assert text == "Picture on page 4; no description yet" and source == "fallback"
    assert "Figure on page" not in text


def test_a_stored_role_wins_over_the_decorative_flag():
    assert figure_role(fig(figure_role="icon", decorative=True)) == "icon"
    assert figure_role(fig()) == "content"


def test_the_heading_of_a_figure_is_the_nearest_one_before_it():
    first, second = fig(), fig()
    second.bbox = (10.0, 300.0, 200.0, 400.0)
    doc = Document(root=Node(Role.DOC, children=[
        first, Node(Role.H2, page=1, bbox=(0, 150, 100, 160), text="Costs"), second]),
        pages=[PageInfo(1, 612, 792)])
    assert figure_headings(doc) == {second.id: "Costs"}
