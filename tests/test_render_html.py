import hashlib
import json
import re

import pytest

from reflow.analyze.sections import nest_sections
from reflow.analyze.review import queue
from reflow.model import MARK_RANK, Document, Mark, Node, PageInfo, Role, Span, mark_order
from reflow.render import chunks as render_chunks
from reflow.render import site as render_site
from reflow.render import html as render
from reflow.render.chunks import Chunk, resolve_links
from reflow.render.manifest import TEXT_SIZE_BASE, decode_size, encode_size, make_manifest
from reflow.render.toc import nest_toc, toc_entries
from reflow.render.links import anchor, linkify
from reflow.render.site import render_html


def doc_with(nodes, pages=2):
    return Document(root=Node(Role.DOC, children=nest_sections(nodes)),
                    pages=[PageInfo(n, 612, 792) for n in range(1, pages + 1)],
                    meta={"lang": "en-US"})


def strip_nodes(markup):
    """The markup without data-node, for tests that check the rest of a block."""
    return re.sub(r' data-node="[^"]*"', "", markup)


def write(tmp_path, doc, **kw):
    summary = render_html(doc, "Annual Report", tmp_path, **{"page_widths": (800,), **kw})
    return summary, (tmp_path / "index.html").read_text()


def test_shell_has_one_h1_lang_and_page_markers(tmp_path):
    doc = doc_with([
        Node(Role.H1, page=1, text="Cover title"),
        Node(Role.H2, page=1, text="Overview"),
        Node(Role.P, page=1, text="Text <with> & markup"),
        Node(Role.ARTIFACT, page=1, text="Page 1"),
        Node(Role.P, page=2, text="More."),
    ])
    _, index = write(tmp_path, doc)
    assert '<html lang="en-US">' in index
    assert len(re.findall(r"<h1[ >]", index)) == 1
    assert '<p class="title">Cover title</p>' in strip_nodes(index)
    chunk = (tmp_path / "chunks/0001.html").read_text()
    assert '<p class="title">Cover title</p>' in strip_nodes(chunk)
    assert "Text &lt;with&gt; &amp; markup" in index
    assert 'id="p1"' in index and 'id="p2"' in index
    assert "Page 1</p>" not in index  # artifacts aren't rendered as content
    assert "View original page 1" in index


def test_source_title_keeps_its_node_and_escapes_markup(tmp_path):
    title = Node(Role.H1, page=1, text='Research <results> & "findings"')
    _, index = write(tmp_path, doc_with([title], pages=1))
    expected = (f'<p class="title" data-node="{title.id}">'
                'Research &lt;results&gt; &amp; &#34;findings&#34;</p>')
    for markup in [index, (tmp_path / "chunks/0001.html").read_text()]:
        assert expected in markup
        assert len(re.findall(r"<h1[ >]", markup)) == 1


def test_bilingual_heading_tags_its_short_spanish_half(tmp_path):
    text = "AHCCCS Complete Care / AHCCCS Cuidado Completo"
    spanish = ("Las vacunas son seguras y eficaces para los niños. Si usted tiene preguntas, "
               "hable con su médico. Es importante que usted y sus hijos se vacunen a tiempo.")
    english = ("The program provides health care for all members and their families. " * 8)
    _, index = write(tmp_path, doc_with([Node(Role.H2, page=1, text=text),
        Node(Role.P, page=1, text=english), Node(Role.P, page=1, text=english),
        Node(Role.P, page=1, text=spanish), Node(Role.P, page=1, text=spanish)], pages=1))

    assert '<span lang="es">AHCCCS Cuidado Completo</span>' in index


def test_english_slash_heading_and_unconfirmed_translation_are_untagged(tmp_path):
    _, index = write(tmp_path, doc_with([Node(Role.H2, page=1, text="Part A / Part B")], pages=1))
    assert "<span lang=" not in index
    _, index = write(tmp_path, doc_with([Node(Role.H2, page=1,
        text="AHCCCS Complete Care / AHCCCS Cuidado Completo")], pages=1))
    assert "<span lang=" not in index


def test_chunks_split_at_sections_and_stay_valid_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(render_chunks, "MIN_BYTES", 10)
    monkeypatch.setattr(render_chunks, "FIRST_TARGET_BYTES", 10)
    doc = doc_with([Node(Role.H2, page=1, text="One"), Node(Role.P, page=1, text="a"),
                    Node(Role.H2, page=2, text="Two"), Node(Role.P, page=2, text="b")])
    summary, index = write(tmp_path, doc)
    assert summary["chunks"] == 2
    chunk2 = (tmp_path / "chunks" / "0002.html").read_text()
    assert len(re.findall(r"<h1[ >]", chunk2)) == 1
    assert '<div class="chunk-content">' in chunk2
    assert 'href="chunks/0002.html"' in index  # no-JS link to the standalone page
    toc = json.loads((tmp_path / "toc.json").read_text())
    assert [(e["text"], e["href"].split("#")[0]) for e in toc] == [
        ("One", "chunks/0001.html"), ("Two", "chunks/0002.html")]


def test_lists_use_native_numbering(tmp_path):
    def items(*markers):
        return [Node(Role.LI, page=1, text=f"item {m}", attrs={"marker": m}) for m in markers]
    doc = doc_with([
        Node(Role.LIST, page=1, attrs={"ordered": False}, children=items("•", "•")),
        Node(Role.LIST, page=1, attrs={"ordered": True}, children=items("a.", "b.")),
        Node(Role.LIST, page=1, attrs={"ordered": True}, children=items("3.", "4.")),
    ], pages=1)
    _, index = write(tmp_path, doc)
    assert "<ul><li>item •</li>" in strip_nodes(index)
    assert '<ol type="a"><li>item a.</li>' in strip_nodes(index)
    assert '<ol start="3">' in strip_nodes(index)


def test_figures_without_alt_get_a_placeholder_and_go_to_review(tmp_path):
    fig = Node(Role.FIGURE, page=1, bbox=(0, 0, 10, 10), confidence=0.2,
               attrs={"needs": "alt"})
    doc = doc_with([fig], pages=1)
    _, index = write(tmp_path, doc, figures={fig.id: ("f.webp", 20, 20)})
    assert 'alt="Picture on page 1; no description yet"' in index and "Figure on page" not in index
    assert "description needed" not in index
    assert 'width="20" height="20"' in index
    assert not (tmp_path / "review.json").exists()  # output.publish writes it, from review.queue
    assert [r["needs"] for r in queue(doc)] == ["alt"]


def test_captioned_figures_name_their_kind_and_do_not_repeat_the_caption(tmp_path):
    fig = Node(Role.FIGURE, page=1, bbox=(0, 0, 10, 10), confidence=0.9,
               attrs={"kind": "chart"},
               children=[Node(Role.CAPTION, page=1, text="Figure 1. Training error")])
    _, index = write(tmp_path, doc_with([fig], pages=1), figures={fig.id: ("f.webp", 20, 20)})
    assert 'alt="Chart"' in index
    assert '<figcaption>Figure 1. Training error</figcaption>' in index
    assert 'alt="Figure 1. Training error"' not in index
    assert "description needed" not in index


def test_a_captioned_figure_is_named_once_by_its_caption(tmp_path):
    # The <figcaption> names the figure: no aria-describedby reads it again, and an alt that
    # repeats it with other spacing or case gives way to the figure's kind.
    fig = Node(Role.FIGURE, page=1, bbox=(0, 0, 10, 10), confidence=0.9,
               attrs={"alt": "figure 2.  Revenue by year"},
               children=[Node(Role.CAPTION, page=1, text="Figure 2. Revenue by year")])
    _, index = write(tmp_path, doc_with([fig], pages=1), figures={fig.id: ("f.webp", 20, 20)})
    figure = re.search(r"<figure.*?</figure>", index, re.S).group(0)
    assert "aria-describedby" not in figure and "aria-label" not in figure
    assert 'alt="Figure"' in figure
    assert figure.count("Revenue by year") == 1


def test_a_figure_with_no_image_file_keeps_its_alt_as_a_labelled_placeholder(tmp_path):
    fig = Node(Role.FIGURE, page=1, bbox=(0, 0, 10, 10), confidence=0.9,
               attrs={"alt": "Map of the service area"})
    _, index = write(tmp_path, doc_with([fig], pages=1))  # no figures: the crop failed
    figure = re.search(r"<figure.*?</figure>", index, re.S).group(0)
    assert "<img" not in figure
    assert ('<p class="figure-alt" role="img" aria-label="Map of the service area">'
            '<span class="figure-alt-label" data-generated>Image not available: </span>'
            '<span>Map of the service area</span></p>') in figure


def test_a_figure_with_no_description_and_no_image_says_only_that_the_image_is_missing(tmp_path):
    # The fallback alt is for a screen reader; the reading flow shows no generic page note.
    fig = Node(Role.FIGURE, page=1, bbox=(0, 0, 10, 10), confidence=0.2, attrs={"needs": "alt"})
    _, index = write(tmp_path, doc_with([fig], pages=1))
    assert ('<p class="figure-alt" role="img" aria-label="Picture on page 1; no description yet">'
            '<span class="figure-alt-label" data-generated>Image not available</span></p>') in index
    assert ">Picture on page" not in index


def figure_html(tmp_path, fig, pages=1):
    doc = doc_with([fig], pages=pages)
    _, index = write(tmp_path, doc, figures={fig.id: ("f.webp", 200, 100)})
    return re.search(r"<figure.*?</figure>", index, re.S).group(0)


def test_a_content_picture_is_shown_at_its_printed_width(tmp_path):
    fig = Node(Role.FIGURE, page=1, bbox=(72, 100, 340.5, 300), confidence=0.9,
               attrs={"alt": "Map of the service area", "figure_role": "content"})
    figure = figure_html(tmp_path, fig)
    assert 'data-figure="content"' in figure and "hidden" not in figure
    assert 'style="--w:268.5pt"' in figure
    assert 'data-node="1:' in figure    # still traces to its box


def test_decorative_art_is_out_of_the_reading_flow_but_keeps_its_node(tmp_path):
    fig = Node(Role.FIGURE, page=1, bbox=(0, 0, 612, 94), confidence=0.9,
               attrs={"alt": "", "decorative": True, "figure_role": "decorative"})
    figure = figure_html(tmp_path, fig)
    assert re.match(r'<figure id="[^"]+" data-figure="decorative" hidden data-node="1:', figure)
    assert 'alt=""' in figure and "--w" not in figure


def test_decorative_art_with_a_caption_still_shows_the_caption(tmp_path):
    fig = Node(Role.FIGURE, page=1, bbox=(0, 0, 612, 94), confidence=0.9,
               attrs={"alt": "", "decorative": True, "figure_role": "decorative"},
               children=[Node(Role.CAPTION, text="Our offices", page=1, bbox=(0, 95, 100, 105))])
    figure = figure_html(tmp_path, fig)
    assert " hidden" not in figure and "<figcaption>Our offices</figcaption>" in figure


def test_a_model_s_decorative_answer_hides_its_badge_with_the_figure(tmp_path):
    fig = Node(Role.FIGURE, page=1, bbox=(0, 0, 612, 94), confidence=0.9,
               attrs={"alt": "", "decorative": True, "figure_role": "decorative",
                      "provenance": "ai-drafted"})
    _, index = write(tmp_path, doc_with([fig], pages=1), figures={fig.id: ("f.webp", 20, 20)})
    assert '<div class="ai-draft" hidden data-provenance="ai-drafted"' in index


@pytest.mark.parametrize("role", ["logo", "icon"])
def test_a_logo_or_icon_is_a_chip_with_its_description(tmp_path, role):
    fig = Node(Role.FIGURE, page=1, bbox=(500, 10, 560, 40), confidence=0.9,
               attrs={"alt": "Circular icon with the letters ER", "figure_role": role})
    figure = figure_html(tmp_path, fig)
    assert f'data-figure="{role}"' in figure and "--w" not in figure
    assert ('<span class="figure-chip-label" aria-hidden="true" data-generated>'
            'Circular icon with the letters ER</span>') in figure


@pytest.mark.parametrize("provenance", ["reviewer", "ai-drafted"])
def test_a_captioned_chip_shows_its_caption_not_the_kind(tmp_path, provenance):
    fig = Node(Role.FIGURE, page=1, bbox=(500, 10, 560, 40), confidence=0.9,
               attrs={"alt": "", "kind": "logo", "figure_role": "logo", "provenance": provenance},
               children=[Node(Role.CAPTION, text="Acme Water", page=1, bbox=(500, 41, 560, 50))])
    figure = figure_html(tmp_path, fig)
    assert 'alt="Logo"' in figure and "figure-chip-label" not in figure
    assert "<figcaption>Acme Water</figcaption>" in figure


def test_a_chip_with_no_description_shows_no_generic_label(tmp_path):
    fig = Node(Role.FIGURE, page=1, bbox=(500, 10, 560, 40), confidence=0.9,
               attrs={"needs": "alt", "figure_role": "icon"})
    figure = figure_html(tmp_path, fig)
    assert 'alt="Picture on page 1; no description yet"' in figure
    assert "figure-chip-label" not in figure


def test_a_decorative_figure_with_no_image_file_stays_silent(tmp_path):
    fig = Node(Role.FIGURE, page=1, bbox=(0, 0, 10, 10), confidence=0.9,
               attrs={"alt": "Swirl", "decorative": True})
    _, index = write(tmp_path, doc_with([fig], pages=1))
    assert "figure-alt" not in index and "Swirl" not in index


def test_a_page_link_with_no_chunk_is_never_an_empty_href():
    with pytest.raises(ValueError, match="page 9"):
        resolve_links('<a href="<p9>">page 9</a>', {})


def test_figure_captions_keep_their_marks(tmp_path):
    from reflow.model import Mark
    cap = Node(Role.CAPTION, page=1, text="Figure 1. Training error",
               marks=[Mark(0, 9, "strong", "Figure 1.")])
    fig = Node(Role.FIGURE, page=1, bbox=(0, 0, 10, 10), confidence=0.9, children=[cap])
    _, index = write(tmp_path, doc_with([fig], pages=1), figures={fig.id: ("f.webp", 20, 20)})
    assert "<strong>Figure 1.</strong> Training error</figcaption>" in index


def test_tables_render_with_caption_header_scope_and_colspan(tmp_path):
    from reflow.analyze.table_nodes import table_node
    from reflow.extract.tables.cells import TableData
    t = table_node(TableData(1, (0, 0, 10, 10), [["Year", "Revenue"], ["Total", None]]))
    t.children.insert(0, Node(Role.CAPTION, page=1, text="Table 1. Revenue"))
    _, index = write(tmp_path, doc_with([t], pages=1))
    assert '<caption>Table 1. Revenue</caption>' in index
    assert '<thead><tr><th scope="col">Year</th><th scope="col">Revenue</th></tr></thead>' in index
    assert '<td colspan="2">Total</td>' in index
    assert 'role="region" aria-label="Table 1. Revenue"' in index
    assert 'tabindex="0"' not in index


def test_row_headers_and_rowspan(tmp_path):
    t = Node(Role.TABLE, page=1, children=[
        Node(Role.TR, page=1, text="r0", children=[
            Node(Role.TH, page=1, text="Model"), Node(Role.TH, page=1, text="Error")]),
        Node(Role.TR, page=1, text="r1", children=[
            Node(Role.TH, page=1, text="ResNet", attrs={"scope": "row", "rowspan": 2}),
            Node(Role.TD, page=1, text="3.57")]),
    ])
    _, index = write(tmp_path, doc_with([t], pages=1))
    assert '<th scope="row" rowspan="2">ResNet</th>' in index
    assert '<thead><tr><th scope="col">Model</th>' in index


def test_every_leading_header_row_is_in_thead(tmp_path):
    # Name spans both header rows, so the second header row must share the thead with it.
    def th(text, **attrs):
        return Node(Role.TH, page=1, text=text, attrs=attrs)
    t = Node(Role.TABLE, page=1, children=[
        Node(Role.TR, page=1, text="r0", children=[
            th("Name", rowspan=2), th("Scores", colspan=2)]),
        Node(Role.TR, page=1, text="r1", children=[th("2024"), th("2025")]),
        Node(Role.TR, page=1, text="r2", children=[
            Node(Role.TD, page=1, text="Alice"), Node(Role.TD, page=1, text="10"),
            Node(Role.TD, page=1, text="20")]),
    ])
    _, index = write(tmp_path, doc_with([t], pages=1))
    thead = re.search(r"<thead>(.*?)</thead>", index).group(1)
    assert thead == ('<tr><th scope="col" rowspan="2">Name</th>'
                     '<th scope="col" colspan="2">Scores</th></tr>'
                     '<tr><th scope="col">2024</th><th scope="col">2025</th></tr>')
    assert "<tbody><tr><td>Alice</td><td>10</td><td>20</td></tr></tbody>" in index


def test_header_band_ids_and_relationships_reach_the_html(tmp_path):
    from reflow.analyze.row_headers import apply
    from pages.checks import parse_html

    def cell(role, text, **attrs):
        return Node(role, page=1, text=text, attrs=attrs)

    table = Node(Role.TABLE, page=1, id="scores", children=[
        Node(Role.TR, page=1, children=[
            cell(Role.TH, "Name", rowspan=2), cell(Role.TH, "Scores", colspan=2)]),
        Node(Role.TR, page=1, children=[cell(Role.TH, "2024"), cell(Role.TH, "2025")]),
        Node(Role.TR, page=1, children=[
            cell(Role.TD, "Alice"), cell(Role.TD, "10"), cell(Role.TD, "20")]),
        Node(Role.TR, page=1, children=[
            cell(Role.TD, "Bob"), cell(Role.TD, "30"), cell(Role.TD, "40")]),
    ])
    doc = doc_with([table], pages=1)
    apply(doc.root)
    _, index = write(tmp_path, doc)
    rendered = parse_html(index).find_all("table")[0]
    assert [c.attrs["id"] for c in rendered.find_all("thead")[0].find_all("th")] == [
        "scores-h0-0", "scores-h0-1", "scores-h1-1", "scores-h1-2"]
    for row in rendered.find_all("tbody")[0].find_all("tr"):
        cells = row.find_all("th", "td")
        assert cells[0].tag == "th" and cells[0].attrs["scope"] == "row"
        assert [c.attrs["headers"] for c in cells] == [
            "scores-h0-0", "scores-h0-1 scores-h1-1", "scores-h0-1 scores-h1-2"]


def test_table_header_attributes_are_escaped(tmp_path):
    from pages.checks import parse_html

    table = Node(Role.TABLE, page=1, children=[Node(Role.TR, page=1, children=[
        Node(Role.TH, page=1, text="Name", attrs={"id": 'header"<&'}),
        Node(Role.TD, page=1, text="Alice", attrs={"headers": ['header"<&', "other"]}),
    ])])
    _, index = write(tmp_path, doc_with([table], pages=1))
    cells = parse_html(index).find_all("table")[0].find_all("th", "td")
    assert cells[0].attrs == {"scope": "col", "id": 'header"<&'}
    assert cells[1].attrs == {"headers": 'header"<& other'}


def test_row_headers_stay_in_tbody(tmp_path):
    # A row whose first cell is a row header (scope="row") is body, not part of the thead.
    t = Node(Role.TABLE, page=1, children=[
        Node(Role.TR, page=1, text="r0", children=[Node(Role.TH, page=1, text="Model")]),
        Node(Role.TR, page=1, text="r1", children=[
            Node(Role.TH, page=1, text="ResNet", attrs={"scope": "row"}),
            Node(Role.TD, page=1, text="3.57")]),
    ])
    _, index = write(tmp_path, doc_with([t], pages=1))
    assert "<thead><tr><th scope=\"col\">Model</th></tr></thead>" in index
    assert '<tbody><tr><th scope="row">ResNet</th><td>3.57</td></tr></tbody>' in index


def test_list_continued_on_the_next_page_renders_on_that_page(tmp_path):
    def page_list(page, marker, text):
        return Node(Role.LIST, page=page, attrs={"ordered": True},
                    children=[Node(Role.LI, page=page, text=text, attrs={"marker": marker})])
    nodes = [page_list(1, "9.", "Ninth item"), page_list(2, "10.", "Tenth item")]
    _, index = write(tmp_path, doc_with(nodes, pages=2))
    page2 = index[index.index('data-page="2"'):]
    assert "Tenth item" in page2 and '<ol start="10"' in page2
    assert "Tenth item" not in index[:index.index('data-page="2"')]


def test_manifest_lists_pages_chunks_and_contents(tmp_path):
    doc = doc_with([Node(Role.H2, page=1, text="Overview"), Node(Role.P, page=2, text="x")])
    render_html(doc, "Annual Report", tmp_path, labels={1: "i", 2: "1"}, page_widths=(800,))
    m = json.loads((tmp_path / "manifest.json").read_text())
    assert (m["title"], m["lang"], m["pageCount"]) == ("Annual Report", "en-US", 2)
    assert m["v"] == 2
    assert m["pages"]["1"]["label"] == "i"   # a label that is not the default is listed
    assert m["pageSize"] == [612, 792]  # most common size
    assert m["chunks"] == [{"href": "chunks/0001.html", "first": 1, "last": 2}]
    assert m["toc"][0]["text"] == "Overview" and m["toc"][0]["page"] == 1

    assert m["labelOffset"] == -1 and m["imageWidths"] == [800]  # page 2 prints "1"
    assert "2" not in m["pages"]

    render_html(doc, "Annual Report", tmp_path, labels={1: "3", 2: "4"}, page_widths=())
    m2 = json.loads((tmp_path / "manifest.json").read_text())
    assert m2["labelOffset"] == 2 and "imageWidths" not in m2 and m2["pages"] == {}


def manifest_of(doc, **kw):
    return make_manifest(doc, **{"title": "T", "lang": "en", "rtl": False, "ltr_lang": None,
                                 "chunks": [], "toc": [], "toc_complete": True, "title_on_page": False,
                                 "labels": {},
                                 "page_widths": (800,), "version": "", **kw})


def test_manifest_carries_version_digest_when_given():
    doc = doc_with([Node(Role.P, page=1, text="x")], pages=1)
    digest = hashlib.sha256(doc.to_json(indent=None).encode()).hexdigest()[:12]
    m = manifest_of(doc, version=digest)
    assert m["v"] == digest
    assert manifest_of(doc)["v"] == 2


def test_manifest_says_when_the_title_is_the_file_name():
    from reflow.model import Fact, Source
    doc = doc_with([Node(Role.P, page=1, text="x")], pages=1)
    assert "titleFromFileName" not in manifest_of(doc)
    doc.root.facts.append(Fact("title", Source.HEURISTIC, "from the filename", {"origin": "filename"}))
    assert manifest_of(doc, title="claim-form")["titleFromFileName"] is True


def test_standalone_assets_and_chunk_slots_carry_version(tmp_path, monkeypatch):
    monkeypatch.setattr(render_chunks, "MIN_BYTES", 10)
    monkeypatch.setattr(render_chunks, "FIRST_TARGET_BYTES", 10)
    figure = Node(Role.FIGURE, page=2, bbox=(0, 0, 10, 10))
    doc = doc_with([Node(Role.H2, page=1, text="One"),
                    Node(Role.H2, page=2, text="Two"), figure])
    _, index = write(tmp_path, doc, version="abc123", figures={figure.id: ("f.webp", 20, 20)})
    for markup in [index, (tmp_path / "chunks/0002.html").read_text()]:
        assert 'href="assets/viewer.css?v=abc123"' in markup
        assert 'src="assets/viewer.js?v=abc123"' in markup
    assert 'data-src="chunks/0002.html?v=abc123"' in index
    assert 'href="pages/0001.webp?v=abc123"' in index
    chunk = (tmp_path / "chunks/0002.html").read_text()
    assert 'href="pages/0002.webp?v=abc123"' in chunk
    assert 'src="figures/f.webp?v=abc123"' in chunk


def test_standalone_assets_without_version_keep_bare_urls(tmp_path):
    _, index = write(tmp_path, doc_with([Node(Role.P, page=1, text="Text")], pages=1))
    for markup in [index, (tmp_path / "chunks/0001.html").read_text()]:
        assert 'href="assets/viewer.css"' in markup
        assert 'src="assets/viewer.js"' in markup
        assert '?v=' not in markup


def test_manifest_gives_each_page_its_transcript_size(tmp_path):
    """"text" holds [characters, blocks, lines] per page: list items and table rows are
    blocks, a paragraph that breaks its lines counts each line, and the H1 title is a paragraph.
    Artifacts are not printed, and a page with no text is [0, 0, 0]. The counts are stored on a
    geometric scale (encode_size) and decoded here."""
    table = Node(Role.TABLE, page=3, children=[
        Node(Role.CAPTION, page=3, text="Fees"),
        Node(Role.TR, page=3, children=[Node(Role.TD, page=3, text="ab"),
                                        Node(Role.TD, page=3, text="c")])])
    doc = doc_with([
        Node(Role.H1, page=1, text="Title"),
        Node(Role.H2, page=1, text="Overview"),
        Node(Role.P, page=1, text="ab cd", attrs={"lines": ["ab", "cd"]}),
        Node(Role.ARTIFACT, page=1, text="Page 1"),
        Node(Role.LIST, page=2, attrs={"ordered": False}, children=[
            Node(Role.LI, page=2, text="xy", attrs={"marker": "•"}),
            Node(Role.LI, page=2, text="z", attrs={"marker": "•"})]),
        table,
    ], pages=4)
    render_html(doc, "Annual Report", tmp_path, page_widths=(800,))
    m = json.loads((tmp_path / "manifest.json").read_text())
    decoded = [[decode_size(k) for k in page] for page in m["text"]]
    assert decoded == [[18, 3, 4], [3, 2, 2], [7, 2, 2], [0, 0, 0]]


def test_manifest_holds_top_level_contents_only_when_large(tmp_path, monkeypatch):
    monkeypatch.setattr(render_site, "TOC_MAX_INLINE", 3)
    nodes = [Node(Role.H2, page=1, text="Section")] + [
        Node(Role.H3, page=1, text=f"Provider {i}") for i in range(5)]
    write(tmp_path, doc_with(nodes, pages=1))
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert [e["text"] for e in manifest["toc"]] == ["Section"]
    assert manifest["tocComplete"] is False
    assert len(json.loads((tmp_path / "toc.json").read_text())) == 6


def test_linkify_urls():
    html = linkify("Visit https://example.com for more")
    assert '<a href="https://example.com">https://example.com</a>' in str(html)

    html = linkify("Check out www.example.org")
    assert '<a href="https://www.example.org">www.example.org</a>' in str(html)


def test_linkify_urls_with_paths():
    html = linkify("See https://example.com/path/to/resource")
    assert '<a href="https://example.com/path/to/resource">https://example.com/path/to/resource</a>' in str(html)


def test_linkify_urls_trailing_punctuation():
    html = linkify("Visit https://example.com.")
    assert '<a href="https://example.com">https://example.com</a>.' in str(html)

    html = linkify("See https://example.com, it's great")
    assert '<a href="https://example.com">https://example.com</a>,' in str(html)


def test_linkify_emails():
    html = linkify("Contact us at support@example.com")
    assert '<a href="mailto:support@example.com">support@example.com</a>' in str(html)


def test_linkify_phone_numbers():
    html = linkify("Call (480) 827-5780")
    assert '<a href="tel:+14808275780">(480) 827-5780</a>' in str(html)

    html = linkify("Phone: 480-827-5780")
    assert '<a href="tel:+14808275780">480-827-5780</a>' in str(html)

    html = linkify("Dial 1-800-555-1212")
    assert '<a href="tel:+18005551212">1-800-555-1212</a>' in str(html)


def test_linkify_escapes_html():
    html = linkify("Text <with> & markup")
    assert "&lt;with&gt;" in str(html)
    assert "&amp;" in str(html)


def test_linkify_leaves_abbreviations_alone():
    html = linkify("Dr. Smith")
    assert "<a" not in str(html)

    html = linkify("Suite 100")
    assert "<a" not in str(html)


def test_linkify_mixed_content():
    html = linkify("Email support@example.com or visit www.example.com for help")
    assert '<a href="mailto:support@example.com">support@example.com</a>' in str(html)
    assert '<a href="https://www.example.com">www.example.com</a>' in str(html)
    assert "for help" in str(html)


def test_linkify_returns_markup():
    from markupsafe import Markup
    html = linkify("Visit https://example.com")
    assert isinstance(html, Markup)


def test_paragraph_with_url_is_linkified(tmp_path):
    doc = doc_with([Node(Role.P, page=1, text="Visit https://example.com for more")], pages=1)
    _, index = write(tmp_path, doc)
    assert '<a href="https://example.com">https://example.com</a>' in index


def test_list_items_with_urls_are_linkified(tmp_path):
    doc = doc_with([
        Node(Role.LIST, page=1, attrs={"ordered": False}, children=[
            Node(Role.LI, page=1, text="Visit https://example.com", attrs={"marker": "•"})
        ])
    ], pages=1)
    _, index = write(tmp_path, doc)
    assert '<a href="https://example.com">https://example.com</a>' in index


def test_table_cells_with_urls_are_linkified(tmp_path):
    t = Node(Role.TABLE, page=1, children=[
        Node(Role.TR, page=1, text="r0", children=[
            Node(Role.TH, page=1, text="Website"), Node(Role.TH, page=1, text="Link")]),
        Node(Role.TR, page=1, text="r1", children=[
            Node(Role.TD, page=1, text="Example"), Node(Role.TD, page=1, text="https://example.com")]),
    ])
    doc = doc_with([t], pages=1)
    _, index = write(tmp_path, doc)
    assert '<a href="https://example.com">https://example.com</a>' in index


def test_status_follows_heading_and_paragraph_text():
    r = render.NodeRenderer({}, {})
    h = Node(Role.H3, page=1, text="Hasley, Patricia, ANP (F)",
             attrs={"status": "Accepting New Patients"})
    p = Node(Role.P, page=1, text="Dyer, Kim", attrs={"status": "Not Accepting New Patients"})
    assert r.render(h).endswith('Hasley, Patricia, ANP (F) '
                                '<span class="status">— Accepting New Patients</span></h3>')
    assert strip_nodes(r.render(p)) == ('<p>Dyer, Kim '
                           '<span class="status">— Not Accepting New Patients</span></p>')
    assert "status" not in r.render(Node(Role.P, page=1, text="Plain"))



DAY = {"status": [
    {"label": "Closed / Holiday", "label_boxes": [[410.8, 872.9, 495.8, 884.2]],
     "swatch": [387.1, 870.0, 405.5, 888.5], "evidence": [33.0, 236.0, 60.0, 249.0]},
    {"label": "Teacher PD or Work Day/ Student Holiday",
     "label_boxes": [[586.5, 872.4, 719.6, 883.6], [586.5, 884.4, 670.9, 895.6]],
     "swatch": [563.5, 870.0, 581.7, 888.5], "evidence": [33.0, 236.0, 60.0, 249.0]}],
    "notes": [{"text": "Labor Day", "box": [39.9, 1042.9, 146.7, 1053.4]}]}


def test_a_calendar_day_prints_its_number_then_each_copied_label_with_its_source():
    r = render.NodeRenderer({}, {})
    day = Node(Role.TD, page=1, bbox=(40, 238, 50, 247), text="02", attrs={"day": DAY})
    table = Node(Role.TABLE, page=1, children=[
        Node(Role.TR, page=1, text="row 0", children=[Node(Role.TH, page=1, text="M")]),
        Node(Role.TR, page=1, text="row 1", children=[day])])
    (cell,) = re.findall(r"<td[^>]*>(.*?)</td>", r.render(table))
    assert cell == (
        '02 — <span class="day-status" data-from="key" data-bbox="[[410.8,872.9,495.8,884.2]]">'
        'Closed / Holiday</span>'
        ' — <span class="day-status" data-from="key" '
        'data-bbox="[[586.5,872.4,719.6,883.6],[586.5,884.4,670.9,895.6]]">'
        'Teacher PD or Work Day/ Student Holiday</span>'
        ' — <span class="day-note" data-from="list" data-bbox="[[39.9,1042.9,146.7,1053.4]]">'
        'Labor Day</span>')


def test_a_cell_without_annotations_renders_as_before():
    r = render.NodeRenderer({}, {})
    table = Node(Role.TABLE, page=1, children=[
        Node(Role.TR, page=1, text="row 1", children=[Node(Role.TD, page=1, text="03")])])
    assert re.findall(r"<td[^>]*>(.*?)</td>", r.render(table)) == ["03"]

def test_address_lines_render_with_line_breaks():
    r = render.NodeRenderer({}, {})
    node = Node(Role.P, page=1, text="1 Main St Gilbert, AZ 85295 (480) 827-5780",
                attrs={"lines": ["1 Main St", "Gilbert, AZ 85295", "(480) 827-5780",
                                 "Website: www.example.com <b>"]})
    assert strip_nodes(r.render(node)) == (
        '<p>1 Main St<br>Gilbert, AZ 85295<br><a href="tel:+14808275780">(480) 827-5780</a>'
        '<br>Website: <a href="https://www.example.com">www.example.com</a> &lt;b&gt;</p>')


def test_blocks_carry_their_node_id(tmp_path):
    # The inspector maps a box to its transcript block by data-node; the id="n…" headings and
    # tables keep their existing anchors.
    doc = doc_with([
        Node(Role.H2, id="7:abc123", page=1, text="Overview"),
        Node(Role.P, id="8:def456", page=1, text="Body text."),
        Node(Role.LIST, id="9:ghi789", page=1,
             children=[Node(Role.LI, id="10:jkl012", page=1, text="One")]),
    ])
    _, index = write(tmp_path, doc)
    assert 'id="n7-abc123" data-node="7:abc123"' in index
    assert 'data-node="8:def456"' in index
    assert 'data-node="9:ghi789"' in index
    assert '<li data-node="10:jkl012">One</li>' in index


def test_a_list_item_renders_its_nested_list_inside_it(tmp_path):
    # An index heading with its sub-entries, and a numbered item with lettered sub-items.
    doc = doc_with([
        Node(Role.LIST, page=1, attrs={"ordered": False}, children=[
            Node(Role.LI, page=1, text="Passive activity:", attrs={}, children=[
                Node(Role.LIST, page=1, attrs={"ordered": False}, children=[
                    Node(Role.LI, page=1, text="Losses 23", attrs={})])])]),
        Node(Role.LIST, page=1, attrs={"ordered": True}, children=[
            Node(Role.LI, page=1, text="If you lived with", attrs={"marker": "8."}, children=[
                Node(Role.LIST, page=1, attrs={"ordered": True}, children=[
                    Node(Role.LI, page=1, text="You can't claim", attrs={"marker": "a."})])])]),
    ], pages=1)
    _, index = write(tmp_path, doc)
    markup = strip_nodes(index)
    assert "<li>Passive activity:<ul><li>Losses 23</li></ul></li>" in markup
    assert '<li>If you lived with<ol type="a"><li>You can&#39;t claim</li></ol></li>' in markup


def test_legal_labels_are_shown_as_printed(tmp_path):
    # Rule 4(a): (a) > (1) > (A). The browser would number these "a.", "1.", "A.", so each label
    # is text in the item, and the list draws no numbers of its own.
    def item(marker, text, *children):
        return Node(Role.LI, page=1, text=text, attrs={"marker": marker}, children=list(children))
    lettered = Node(Role.LIST, page=1, attrs={"ordered": True}, children=[
        item("(A)", "name the court;"), item("(B)", "be directed to the defendant.")])
    doc = doc_with([
        Node(Role.LIST, page=1, attrs={"ordered": True}, children=[
            item("(a)", "Contents.", Node(Role.LIST, page=1, attrs={"ordered": True}, children=[
                item("(1)", "A summons must:", lettered)]))]),
        Node(Role.LIST, page=1, attrs={"ordered": True}, children=[
            item("(ii)", "a copy of the document;"), item("(iii)", "a computation of damages;")]),
    ], pages=1)
    _, index = write(tmp_path, doc)
    markup = strip_nodes(index)
    assert ('<ol class="legal" role="list"><li><span class="marker">(a)</span> Contents.'
            '<ol class="legal" role="list"><li><span class="marker">(1)</span> A summons must:'
            '<ol class="legal" role="list"><li><span class="marker">(A)</span> name the court;</li>'
            '<li><span class="marker">(B)</span> be directed to the defendant.</li></ol>'
            '</li></ol></li></ol>') in markup
    assert ('<ol class="legal" role="list"><li><span class="marker">(ii)</span> a copy of the document;'
            '</li><li><span class="marker">(iii)</span> a computation of damages;</li></ol>') in markup


def test_roman_and_letter_runs_get_the_right_type_and_start(tmp_path):
    def items(*markers):
        return [Node(Role.LI, page=1, text=f"item {m}", attrs={"marker": m}) for m in markers]
    doc = doc_with([
        Node(Role.LIST, page=1, attrs={"ordered": True}, children=items("ii.", "iii.")),
        Node(Role.LIST, page=1, attrs={"ordered": True}, children=items("i.", "ii.", "iii.")),
        Node(Role.LIST, page=1, attrs={"ordered": True}, children=items("i.", "j.")),
        Node(Role.LIST, page=1, attrs={"ordered": True}, children=items("IV.", "V.")),
    ], pages=1)
    _, index = write(tmp_path, doc)
    markup = strip_nodes(index)
    assert '<ol type="i" start="2"><li>item ii.</li>' in markup
    assert '<ol type="i"><li>item i.</li>' in markup
    assert '<ol type="a" start="9"><li>item i.</li>' in markup
    assert '<ol type="I" start="4"><li>item IV.</li>' in markup


def test_a_charts_folded_labels_stay_readable_text(tmp_path):
    fig = Node(Role.FIGURE, page=1, bbox=(0, 0, 10, 10), confidence=0.2,
               attrs={"needs": "alt"},
               children=[Node(Role.P, page=1, bbox=(1, 1, 5, 3), text=t,
                              attrs={"figure_label": True}) for t in ["Mortality", "20 <5%"]])
    _, index = write(tmp_path, doc_with([fig], pages=1), figures={fig.id: ("f.webp", 20, 20)})
    assert ("<details><summary>Text in the figure</summary>"
            "<ul><li>Mortality</li><li>20 &lt;5%</li></ul></details>") in index


def test_manifest_lists_the_image_widths_written():
    doc = doc_with([Node(Role.P, page=1, text="x")], pages=1)
    m = manifest_of(doc, page_widths=(800, 1200))
    assert m["imageWidths"] == [800, 1200]
    assert "imageWidths" not in manifest_of(doc, page_widths=())


def _long_pages(count, per_page=8):
    """`count` pages of body text (about 1 KB each of HTML) under a section heading on page 1."""
    nodes = [Node(Role.H2, page=1, text="One")]
    for n in range(1, count + 1):
        nodes += [Node(Role.P, page=n, text=f"Page {n} text. " * 30) for _ in range(per_page)]
    return nodes


def test_the_first_chunk_is_capped_and_the_shell_inlines_the_first_section(tmp_path, monkeypatch):
    monkeypatch.setattr(render_chunks, "FIRST_TARGET_BYTES", 4_000)
    monkeypatch.setattr(render_chunks, "MIN_BYTES", 10_000)
    nodes = _long_pages(8) + [Node(Role.H2, page=9, text="Two"), Node(Role.P, page=9, text="b")]
    summary, index = write(tmp_path, doc_with(nodes, pages=9))
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    chunks = manifest["chunks"]
    first_bytes = len((tmp_path / "chunks/0001.html").read_text().encode())
    assert chunks[0]["last"] < 8  # a small first chunk, not the whole first section
    assert first_bytes < 4_000 + 3_000
    # The shell carries the chunks up to the page that opens section two, from the same list.
    inline = index.count('data-loaded="1"')
    assert 1 < inline < len(chunks)
    assert chunks[inline - 1]["last"] == 8 and chunks[inline]["first"] == 9
    assert 'id="p8"' in index and 'id="p9"' not in index
    assert 'Continue reading: pages 9' in index
    assert summary["chunks"] == len(chunks)


def test_a_first_section_on_page_one_is_the_first_chunk_and_the_inlined_section(tmp_path, monkeypatch):
    monkeypatch.setattr(render_chunks, "MIN_BYTES", 10)
    monkeypatch.setattr(render_chunks, "FIRST_TARGET_BYTES", 4_000)
    # Page 1 alone is over the cap, so chunk 1 is page 1; page 2 opens the next section.
    nodes = _long_pages(1, per_page=60) + [Node(Role.H2, page=2, text="Two"),
                                           Node(Role.P, page=2, text="b")]
    _, index = write(tmp_path, doc_with(nodes, pages=2))
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert [(c["first"], c["last"]) for c in manifest["chunks"]] == [(1, 1), (2, 2)]
    assert 'id="p1"' in index and 'id="p2"' not in index
    assert index.count('data-loaded="1"') == 1
    toc = json.loads((tmp_path / "toc.json").read_text())
    assert [(e["text"], e["href"].split("#")[0]) for e in toc] == [
        ("One", "chunks/0001.html"), ("Two", "chunks/0002.html")]


def test_a_cover_page_chunk_has_no_label_and_no_empty_parentheses(tmp_path, monkeypatch):
    monkeypatch.setattr(render_chunks, "FIRST_TARGET_BYTES", 10)
    monkeypatch.setattr(render_chunks, "MIN_BYTES", 10 ** 9)  # the shell never stops at a section
    monkeypatch.setattr(render_chunks, "TARGET_BYTES", 1)  # ...but takes only the first chunk
    nodes = [Node(Role.P, page=1, text="Cover"), Node(Role.H2, page=2, text="Intro"),
             Node(Role.P, page=2, text="b")]
    _, index = write(tmp_path, doc_with(nodes, pages=2))
    assert "()" not in index and "( )" not in index
    assert 'Continue reading: pages 2 (Intro)' in index


def test_manifest_text_sizes_are_within_about_five_percent_of_the_counts():
    worst = 0.0
    for n in list(range(0, 5000)) + [10 ** 5, 10 ** 6, 3 * 10 ** 7]:
        back = decode_size(encode_size(n))
        if n >= 30:
            worst = max(worst, abs(back - n) / n)
        else:
            assert abs(back - n) <= 1  # small counts are off by at most one
    assert worst <= 0.053  # 4.9% from the scale, plus integer rounding near 30
    assert decode_size(encode_size(1499)) in range(1425, 1575)
    assert encode_size(0) == 0


def test_manifest_marks_the_size_encoding(tmp_path):
    _, _ = write(tmp_path, doc_with([Node(Role.P, page=1, text="Cover")], pages=1))
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["textBase"] == TEXT_SIZE_BASE
    assert len(manifest["text"]) == 1


def field_html(tmp_path, **attrs):
    attrs.setdefault("kind", "text")
    attrs.setdefault("fid", "f1-0")
    text = attrs.pop("text", None)
    doc = doc_with([Node(Role.FIELD, page=1, text=text, attrs=attrs)])
    return write(tmp_path, doc)[1] + (tmp_path / "chunks/0001.html").read_text()


def test_a_field_with_no_label_is_named_from_its_pdf_field_name(tmp_path):
    out = field_html(tmp_path, name="topmostSubform[0].Page1[0].Patient_Name[0]", label="none")
    assert re.search(r'<input type="text"[^>]*aria-label="Patient Name"', out)


def test_unnamed_checkboxes_and_fields_get_a_generic_accessible_name(tmp_path):
    out = field_html(tmp_path, kind="checkbox", name="", label="none")
    assert re.search(r'<input type="checkbox"[^>]*aria-label="Checkbox on page 1"', out)


def test_a_field_with_a_printed_label_or_tooltip_keeps_its_own_name(tmp_path):
    printed = field_html(tmp_path / "a", name="Text1", text="Last name", label="printed") \
        if (tmp_path / "a").mkdir() is None else ""
    assert '<label for="f1-0"' in printed and "aria-label" not in printed.split("<label")[0] + \
        printed.split("</label>")[1].split("</div>")[0]
    tip = field_html(tmp_path / "b", name="Text1", aria="Date of birth", tooltip="Date of birth",
                     label="tooltip") if (tmp_path / "b").mkdir() is None else ""
    assert 'aria-label="Date of birth"' in tip and "aria-label=\"Text1\"" not in tip


@pytest.mark.parametrize("name", ["Text1", "Text Field 3", "undefined", "undefined_2",
                                  "Check Box1", "fill_4", "Field12", "7",
                                  "topmostSubform[0].Page1[0].Text1[0]"])
@pytest.mark.parametrize("kind,generic", [("text", "Form field on page 1"),
                                          ("checkbox", "Checkbox on page 1"),
                                          ("radio", "Option on page 1")])
def test_junk_pdf_field_names_fall_back_to_a_generic_name(tmp_path, name, kind, generic):
    out = field_html(tmp_path, kind=kind, name=name, label="none")
    assert f'aria-label="{generic}"' in out


def test_the_contents_nest_by_section_not_by_heading_level():
    doc = doc_with([Node(Role.H2, page=1, text="A"), Node(Role.H4, page=1, text="Deep"),
                    Node(Role.H3, page=1, text="B"), Node(Role.H2, page=1, text="C")], pages=1)
    chunk = Chunk(1)
    chunk.pages.append(1)
    tree = nest_toc(toc_entries(doc, [chunk]))
    assert [(e["text"], [c["text"] for c in e["children"]]) for e in tree] == [
        ("A", ["Deep", "B"]), ("C", [])]


def contents_of(nodes, pages=1):
    chunk = Chunk(1)
    chunk.pages.extend(range(1, pages + 1))
    return nest_toc(toc_entries(doc_with(nodes, pages=pages), [chunk]))


def test_a_heading_that_repeats_the_one_before_it_adds_no_contents_entry():
    # audit p12: a signature name set as an h2 and, at once, an h3 of the same text
    tree = contents_of([Node(Role.H2, page=1, text="Brian Weber"),
                        Node(Role.H3, page=1, text="Brian  weber"),
                        Node(Role.H3, page=1, text="Chief Executive")])
    assert [(e["text"], [c["text"] for c in e["children"]]) for e in tree] == [
        ("Brian Weber", ["Chief Executive"])]


def test_an_entity_name_printed_at_the_top_of_two_pages_has_one_contents_entry():
    # audit p3 and p4
    tree = contents_of([Node(Role.H2, page=1, text="Mosquito District"),
                        Node(Role.H2, page=2, text="Mosquito District"),
                        Node(Role.H3, page=2, text="Statement")], pages=2)
    assert [(e["text"], [c["text"] for c in e["children"]]) for e in tree] == [
        ("Mosquito District", ["Statement"])]


def test_a_repeat_that_only_the_depth_cut_made_adjacent_is_dropped_from_the_shown_contents(tmp_path):
    # audit p3 and p4: h4 entries sit between the two, below the depth the contents shows
    doc = doc_with([Node(Role.H2, page=1, text="Mosquito District"),
                    Node(Role.H5, page=1, text="Table of Contents"),
                    Node(Role.H2, page=2, text="Mosquito District"),
                    Node(Role.H3, page=2, text="Statement")], pages=2)
    summary, index = write(tmp_path, doc)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert [e["text"] for e in manifest["toc"]] == ["Mosquito District", "Statement"]
    assert [e["text"] for e in json.loads((tmp_path / "toc.json").read_text())] == [
        "Mosquito District", "Table of Contents", "Mosquito District", "Statement"]
    assert manifest["tocComplete"] is False


def test_a_repeat_that_is_not_the_previous_entry_or_is_shallower_stays_in_the_contents():
    tree = contents_of([Node(Role.H2, page=1, text="A"), Node(Role.H3, page=1, text="B"),
                        Node(Role.H3, page=2, text="A"), Node(Role.H2, page=3, text="A")], pages=3)
    assert [(e["text"], [c["text"] for c in e["children"]]) for e in tree] == [
        ("A", ["B", "A"]), ("A", [])]


def test_one_page_document_says_1_page(tmp_path):
    _, index = write(tmp_path, doc_with([Node(Role.P, page=1, text="Text")], pages=1))
    chunk = (tmp_path / "chunks/0001.html").read_text()
    for markup in (index, chunk):
        assert "1 page<" in markup or "1 page ·" in markup
        assert "1 pages" not in markup


def test_page_label_names_the_pdf_page_and_its_printed_number(tmp_path):
    doc = doc_with([Node(Role.P, page=1, text="a"), Node(Role.P, page=2, text="b")])
    _, index = write(tmp_path, doc, labels={1: "1", 2: "7"})
    assert "<span>Page 1</span>" in index and "View original page 1</a>" in index
    assert "<span>Page 2 (printed 7)</span>" in index
    assert "View original page 2 (printed 7)</a>" in index


def test_the_standalone_stylesheet_is_the_shared_rules_then_the_pages_own(tmp_path):
    _, index = write(tmp_path, doc_with([Node(Role.P, page=1, text="x")], pages=1))
    sheet = (tmp_path / "assets/viewer.css").read_text()
    assert sheet.index(".reflow-transcript .footnote {") < sheet.index(".doc-header {")
    assert '<main id="content" class="reflow-transcript"' in index



def test_headings_printed_in_capitals_are_marked_and_keep_their_case(tmp_path):
    # The stylesheet spaces .caps headings a little; the words stay as printed.
    doc = doc_with([Node(Role.H2, page=1, text="3.1 CAD SYSTEM"),
                    Node(Role.H3, page=1, text="Section 1 – Project Description"),
                    Node(Role.H3, page=1, text="FAQs"),
                    Node(Role.P, page=1, text="ACCIDENT INSURANCE")], pages=1)
    html = strip_nodes(write(tmp_path, doc)[1])
    assert re.search(r'<h2 id="[^"]*" class="caps">3\.1 CAD SYSTEM</h2>', html)
    assert re.search(r'<h3 id="[^"]*">Section 1 – Project Description</h3>', html)
    assert re.search(r'<h3 id="[^"]*">FAQs</h3>', html)
    assert "<p>ACCIDENT INSURANCE</p>" in html


def test_a_paragraph_wholly_in_bold_is_a_lead_in(tmp_path):
    lead = "Accidents can happen at any time"
    part = "Note: the rest is plain"
    doc = doc_with([_marked(lead, _m(lead, lead, "strong")),
                    _marked(part, _m(part, "Note:", "strong"))], pages=1)
    html = strip_nodes(write(tmp_path, doc)[1])
    assert f'<p class="lead"><strong>{lead}</strong></p>' in html
    assert "<p><strong>Note:</strong> the rest is plain</p>" in html


# --- inline marks: one emitter for spans and marks ------------------------------------------

def _marked(text, *marks, spans=()):
    return Node(Role.P, page=1, text=text, marks=list(marks), spans=list(spans))


def _m(text, word, kind, **kw):
    at = text.index(word)
    return Mark(at, at + len(word), kind, word, **kw)


def test_bold_inside_a_link_nests_in_the_anchor():
    text = "see the Appeals Guide now"
    node = _marked(text, _m(text, "Appeals", "strong"),
                   spans=[Span(4, 21, "link", "https://x.test/guide")])
    assert str(render.inline(node)) == (
        'see <a href="https://x.test/guide" rel="noopener">the <strong>Appeals</strong> Guide</a> now')


def test_a_link_inside_bold_reopens_the_strong_around_the_anchor():
    text = "one two three"
    node = _marked(text, Mark(0, 13, "strong", text), spans=[Span(4, 7, "link", "https://x.test")])
    assert str(render.inline(node)) == (
        '<strong>one </strong><a href="https://x.test" rel="noopener"><strong>two</strong></a>'
        '<strong> three</strong>')


def test_a_url_crossing_a_bold_boundary_is_one_anchor():
    text = "visit https://example.com/forms today"
    node = _marked(text, _m(text, "example.com/forms", "strong"))
    html = str(render.inline(node))
    assert html == ('visit <a href="https://example.com/forms">https://<strong>example.com/forms'
                    '</strong></a> today')


_OPEN = {"strong": "<strong>", "em": "<em>", "ins": "<ins>", "del": "<del>",
         "doubt": '<span class="ocr-doubt"', "sup": "<sup>"}


@pytest.mark.parametrize("a,b", [(a, b) for a in _OPEN for b in _OPEN if MARK_RANK[a] != MARK_RANK[b]])
def test_two_marks_on_one_word_nest_in_the_models_order(a, b):
    # The HTML nests marks in the model's order (mark_order), inside the anchor, whatever order
    # the node lists them in.
    text = "see word now"
    marks = [_m(text, "word", a), _m(text, "word", b)]
    node = _marked(text, *marks, spans=[Span(4, 8, "link", "https://x.test")])
    html = str(render.inline(node))
    first, second = sorted(marks, key=mark_order)
    assert html.index("<a ") < html.index(_OPEN[first.kind]) < html.index(_OPEN[second.kind])


def test_a_raised_marker_no_note_claims_is_a_sup():
    # az-accident-insurance-flyer p1: "each year.¹" read "each year.1" before the sup mark.
    text = "42.2 million times each year.1"
    node = _marked(text, Mark(len(text) - 1, len(text), "sup", "1"))
    assert str(render.inline(node)) == "42.2 million times each year.<sup>1</sup>"


def test_a_note_reference_is_raised_once():
    # The reference's raised run is also a sup mark (marks_for reads every raised run); the
    # <sup> around the anchor already raises it, so the mark adds no second one.
    text = "won competitions1 here"
    at = text.index("1")
    bare = _marked(text, spans=[Span(at, at + 1, "noteref", "1:ab12")])
    marked = _marked(text, Mark(at, at + 1, "sup", "1"),
                     spans=[Span(at, at + 1, "noteref", "1:ab12")])
    want = ('won competitions<sup><a role="doc-noteref" href="<p1:n-1:ab12>" '
            'id="r-1:ab12">1</a></sup> here')
    assert str(render.inline(bare)) == want
    assert str(render.inline(marked)) == want


def test_a_doubt_inside_ins_and_inside_del_nests():
    text = "the acct was paid"
    ins = _marked(text, Mark(4, 12, "ins", "acct was"), _m(text, "acct", "doubt"))
    assert str(render.inline(ins)) == (
        'the <ins><span class="ocr-doubt" title="OCR is unsure of this word">acct</span> was</ins>'
        ' paid')
    gone = _marked(text, Mark(4, 12, "del", "acct was"), _m(text, "was", "doubt"))
    assert str(render.inline(gone)) == (
        'the <del>acct <span class="ocr-doubt" title="OCR is unsure of this word">was</span></del>'
        ' paid')


def test_a_doubt_with_an_alternative_reading_carries_it():
    text = "pay the fee"
    node = _marked(text, _m(text, "fee", "doubt", alt="fte"))
    assert str(render.inline(node)) == (
        'pay the <span class="ocr-doubt" data-alt="fte" title="OCR is unsure of this word. '
        'The document suggests: fte">fee</span>')


UNREAD = ('<del class="unread-word" data-generated><span role="img" '
          'aria-label="struck word, unreadable"></span></del>')


def test_a_del_unread_point_prints_between_the_brackets():
    node = _marked("[]", Mark(1, 1, "del-unread", ""))
    assert str(render.inline(node)) == f"[{UNREAD}]"


def test_a_del_unread_point_inside_an_ins_nests_in_it():
    text = "add [] here"
    node = _marked(text, Mark(4, 6, "ins", "[]"), Mark(5, 5, "del-unread", ""))
    assert str(render.inline(node)) == f"add <ins>[{UNREAD}]</ins> here"


def test_an_address_keeps_a_doubt_on_its_zip_code_between_line_breaks():
    r = render.NodeRenderer({}, {})
    text = "1 Main St Gilbert, AZ 85295"
    node = Node(Role.P, page=1, text=text, attrs={"lines": ["1 Main St", "Gilbert, AZ 85295"]},
                marks=[_m(text, "85295", "doubt")])
    assert strip_nodes(r.render(node)) == (
        '<p>1 Main St<br>Gilbert, AZ <span class="ocr-doubt" title="OCR is unsure of this word">'
        '85295</span></p>')


def test_an_address_whose_text_no_longer_matches_its_lines_keeps_its_marks_on_one_line():
    r = render.NodeRenderer({}, {})
    text = "1 Main Street Gilbert, AZ 85295"           # edited: the lines say "St"
    node = Node(Role.P, page=1, text=text, attrs={"lines": ["1 Main St", "Gilbert, AZ 85295"]},
                marks=[_m(text, "85295", "doubt")])
    html = strip_nodes(r.render(node))
    assert "<br>" not in html and ">85295</span>" in html


def test_a_footnote_cut_of_its_marker_keeps_a_strong_on_the_same_word():
    from reflow.analyze.notes import attach_footnotes
    from reflow.extract.lines import Line
    from reflow.extract.pages import PageContent
    r = render.NodeRenderer({}, {})
    body_text = "Deep nets win competitions1 here"
    at = body_text.index("1")
    foot_text = "1 See the Guide"
    page = PageContent(1, 612, 792, [
        Line(1, (50, 100, 280, 110), body_text, 10.0, False, False, "F", sup=((at, at + 1),)),
        Line(1, (50, 720, 280, 727), foot_text, 7.0, False, False, "F")], [])
    body = Node(Role.P, page=1, bbox=(50, 100, 280, 110), text=body_text)
    note = Node(Role.P, page=1, bbox=(50, 720, 280, 727), text=foot_text,
                marks=[_m(foot_text, "Guide", "strong")])
    assert attach_footnotes([body, note], [page], 10.0) == 1
    assert note.text == "See the Guide"
    assert "See the <strong>Guide</strong>" in r.render(note)


def test_a_mark_keeps_escaping_unchanged():
    node = _marked("a < b & c", Mark(4, 9, "em", "b & c"))
    assert str(render.inline(node)) == "a &lt; <em>b &amp; c</em>"


def test_a_heading_with_a_mark_is_set_through_the_emitter(tmp_path):
    text = "Prior Authorization Request"
    head = Node(Role.H2, page=1, text=text, marks=[_m(text, "Prior", "doubt")])
    _, index = write(tmp_path, doc_with([head], pages=1))
    assert '<span class="ocr-doubt" title="OCR is unsure of this word">Prior</span> Authorization' in index


def test_a_cell_with_a_box_is_traceable_and_one_without_is_not(tmp_path):
    t = Node(Role.TABLE, page=1, children=[
        Node(Role.TR, page=1, text="r0", children=[Node(Role.TH, page=1, text="S")]),
        Node(Role.TR, page=1, text="r1", children=[
            Node(Role.TD, page=1, text="01", bbox=(10.0, 20.0, 20.0, 29.0), id="1:cell"),
            Node(Role.TD, page=1, text="")])])
    _, index = write(tmp_path, doc_with([t], pages=1))
    assert '<td data-node="1:cell">01</td>' in index
    assert '<td></td>' in index


def test_an_unread_figure_is_a_framed_crop_with_reflows_note_and_no_ocr_guess(tmp_path):
    fig = Node(Role.FIGURE, page=1, bbox=(0, 0, 10, 10), confidence=0.2,
               attrs={"unread": True, "kind": "handwriting", "alt": "Handwriting, not transcribed.",
                      "handwritten": True, "ocr_guess": "Sxa ~mth"})
    _, index = write(tmp_path, doc_with([fig], pages=1), figures={fig.id: ("f.webp", 20, 20)})
    assert 'class="unread" data-kind="handwriting"' in index
    assert 'alt="Handwriting, not transcribed."' in index
    assert ('<p class="unread-note" data-generated>Not transcribed: no model was asked to read it. '
            'Check the original page.</p>' in index)
    assert "Sxa" not in index and "~mth" not in index


def test_tracked_changes_print_on_every_text_path():
    """A paragraph set line by line, a heading, a caption, a note: the same marks, and an
    insertion over a link keeps the anchor outermost."""
    r = render.NodeRenderer({}, {})
    text = "court, a [] statutory court"
    marks = [Mark(10, 10, "del-unread", ""), _m(text, "a", "ins")]
    lines = Node(Role.P, page=1, text=text, attrs={"lines": ["court, a []", "statutory court"]},
                 marks=list(marks))
    assert strip_nodes(r.render(lines)) == (
        f"<p>court, <ins>a</ins> [{UNREAD}]<br>statutory court</p>")
    for role, tag in ((Role.H2, "h2"), (Role.CAPTION, "p"), (Role.NOTE, "p")):
        html = r.render(Node(role, page=1, text=text, marks=list(marks)))
        assert f"court, <ins>a</ins> [{UNREAD}] statutory court</{tag}>" in html
    linked = _marked("see the rules", Mark(4, 13, "ins", "the rules"),
                     spans=[Span(8, 13, "link", "https://x.test")])
    assert str(render.inline(linked)) == (
        'see <ins>the </ins><a href="https://x.test" rel="noopener"><ins>rules</ins></a>')


def test_an_unreadable_struck_word_has_no_text_of_its_own():
    import re
    html = str(render.inline(_marked("a [] court", Mark(3, 3, "del-unread", ""))))
    texts = re.sub(r"<[^>]+>", "\0", html).split("\0")
    assert not any(w in t for t in texts for w in ("illegible", "unreadable", "e¥"))


def test_generated_figure_text_is_marked_and_page_text_is_not(tmp_path):
    label = Node(Role.P, page=1, bbox=(0, 0, 5, 5), text="Q1 sales", attrs={"figure_label": True})
    caption = Node(Role.CAPTION, page=1, bbox=(0, 12, 10, 14), text="Fig. 1. Sales")
    drafted = Node(Role.FIGURE, page=1, bbox=(0, 0, 10, 10), children=[label, caption],
                   attrs={"alt": "Bar chart of sales", "description": "Two bars.", "kind": "chart",
                          "provenance": "ai-drafted", "reviewed": False})
    heading = Node(Role.H2, page=1, bbox=(0, 20, 10, 24), text="Results")
    plain = Node(Role.FIGURE, page=1, bbox=(0, 30, 10, 40))
    doc = doc_with([drafted, heading, plain], pages=1)
    _, index = write(tmp_path, doc, figures={drafted.id: ("a.webp", 9, 9), plain.id: ("b.webp", 9, 9)})
    assert '<small class="ai-badge" role="note" data-generated>AI-drafted, not reviewed</small>' in index
    assert "<details data-generated><summary>Image description</summary><p>Two bars." in index
    assert "<details><summary>Text in the figure</summary>" in index
    assert re.search(r"<figcaption[^>]*>Fig\. 1\. Sales</figcaption>", index)
    assert 'alt="Bar chart of sales"' in index
    assert 'alt="Picture on page 1 under “Results”; no description yet"' in index
    assert "Figure on page" not in index


def test_a_form_bar_heading_is_rendered_in_its_cell_and_listed_in_the_contents():
    """A section bar of a form table stays in its cell and reaches the Contents."""
    bar = Node(Role.H2, page=1, bbox=(10, 10, 100, 20), text="PATIENT INFORMATION")
    cell = Node(Role.TD, page=1, text="", children=[bar])
    table = Node(Role.TABLE, page=1, bbox=(10, 10, 300, 40),
                 children=[Node(Role.TR, page=1, children=[cell])])
    doc = doc_with([Node(Role.H1, page=1, text="Dental Claim Form"), table])

    class Chunk:
        pages = [1]
        href = "chunks/0001.html"

    assert [(e["text"], e["level"]) for e in toc_entries(doc, [Chunk()])] == [
        ("PATIENT INFORMATION", 2)]
    html = render.NodeRenderer({}, {}, {}).render(table)
    assert f'<td><h2 id="{anchor(bar)}"' in html and "PATIENT INFORMATION</h2></td>" in html


def test_a_fix_renders_as_a_reviewed_span_that_keeps_ocrs_reading():
    text = "Englewood, CO 80110"
    node = _marked(text, _m(text, "80110", "fix", alt="8011O"))
    assert str(render.inline(node)) == (
        'Englewood, CO <span class="reviewed-fix" data-was="8011O" '
        'title="Corrected by a reviewer. OCR read: 8011O">80110</span>')


def test_a_fix_nests_outside_a_doubt_and_inside_em():
    text = "see word now"
    fix = _m(text, "word", "fix", alt="wurd")
    em = _m(text, "word", "em")
    html = str(render.inline(_marked(text, fix, em)))
    assert html.index("<em>") < html.index('class="reviewed-fix"')


def _input_of(html, fid):
    return re.search(rf'<input [^>]*id="{fid}"[^>]*>', html).group(0)


def test_a_text_box_gets_its_type_inputmode_and_autofill_token_from_its_label(tmp_path):
    fields = [("Email address", "email", {}), ("Daytime Telephone", "tel", {"width": 120}),
              ("ZIP CODE", "text", {}), ("Notes", "text", {})]
    doc = doc_with([Node(Role.FIELD, page=1, text=label, attrs={"kind": "text", "fid": f"f1-{i}", **extra})
                    for i, (label, _, extra) in enumerate(fields)])
    html = write(tmp_path, doc)[1] + (tmp_path / "chunks/0001.html").read_text()
    email, tel, zip_code, notes = (_input_of(html, f"f1-{i}") for i in range(4))
    assert 'type="email"' in email and 'autocomplete="email"' in email and 'inputmode="email"' in email
    assert 'type="tel"' in tel and 'autocomplete="tel"' in tel
    assert 'type="text"' in zip_code and 'autocomplete="postal-code"' in zip_code and 'inputmode="numeric"' in zip_code
    assert 'type="text"' in notes and 'autocomplete="off"' in notes and "inputmode" not in notes
    assert 'type="date"' not in html


def test_a_text_box_under_a_heading_about_someone_else_gets_no_autofill_token(tmp_path):
    def section(title, fid):
        return Node(Role.SECTION, page=1, children=[
            Node(Role.H2, page=1, text=title),
            Node(Role.FIELD, page=1, text="Email address", attrs={"kind": "text", "fid": fid})])
    doc = doc_with([section("1. Applicant information", "f1-0"), section("2. Contact: agent or attorney", "f1-1")])
    html = write(tmp_path, doc)[1] + (tmp_path / "chunks/0001.html").read_text()
    own, agent = _input_of(html, "f1-0"), _input_of(html, "f1-1")
    assert 'autocomplete="email"' in own
    assert 'autocomplete="off"' in agent and 'type="email"' in agent


def test_a_password_box_keeps_type_password_and_no_token(tmp_path):
    out = field_html(tmp_path, password=True, text="Email")
    assert 'type="password"' in out and 'autocomplete="off"' in out and "inputmode" not in out


def test_a_text_box_after_the_label_of_someone_else_gets_no_autofill_token(tmp_path):
    """A flat form with no headings: the Phone and Email printed after "Attending Physician"
    are the physician's, and so is every box after it in that place."""
    labels = ["Email", "Attending Physician:", "Phone", "Email"]
    doc = doc_with([Node(Role.FIELD, page=1, text=label, attrs={"kind": "text", "fid": f"f1-{i}"})
                    for i, label in enumerate(labels)])
    html = write(tmp_path, doc)[1] + (tmp_path / "chunks/0001.html").read_text()
    assert 'autocomplete="email"' in _input_of(html, "f1-0")
    assert 'autocomplete="off"' in _input_of(html, "f1-2") and 'type="tel"' in _input_of(html, "f1-2")
    assert 'autocomplete="off"' in _input_of(html, "f1-3") and 'type="email"' in _input_of(html, "f1-3")


def test_a_fieldset_named_for_someone_else_withholds_the_token_from_its_boxes(tmp_path):
    group = Node(Role.GROUP, page=1, text="Spouse", children=[
        Node(Role.FIELD, page=1, text="Email", attrs={"kind": "text", "fid": "f1-0"})])
    html = write(tmp_path, doc_with([group]))[1] + (tmp_path / "chunks/0001.html").read_text()
    assert 'autocomplete="off"' in _input_of(html, "f1-0")


def test_an_accent_mark_is_a_named_span_and_nests_inside_bold():
    node = Node(Role.P, text="FEE: $65.75 per parcel",
                marks=[Mark(0, 11, "strong", "FEE: $65.75"), Mark(5, 11, "red", "$65.75")])
    assert strip_nodes(render.NodeRenderer({}, {}).render(node)) == \
        '<p><strong>FEE: <span class="ink-red">$65.75</span></strong> per parcel</p>'


def test_an_accent_is_left_off_a_link_and_the_text_stays():
    node = Node(Role.P, text="Visit www.example.gov or call 480-827-5780 now",
                spans=[Span(6, 21, "link", "https://www.example.gov")],
                marks=[Mark(6, 21, "red", "www.example.gov"), Mark(30, 42, "red", "480-827-5780")])
    html = strip_nodes(render.NodeRenderer({}, {}).render(node))
    assert "ink-red" not in html
    assert ">www.example.gov</a>" in html and ">480-827-5780</a>" in html


def test_manifest_says_when_page_one_opens_with_a_title(tmp_path):
    cover = [Node(Role.H1, page=1, text="Annual Report"), Node(Role.H2, page=1, text="Overview")]
    render_html(doc_with(cover, pages=1), "Annual Report", tmp_path / "a", page_widths=())
    assert json.loads((tmp_path / "a" / "manifest.json").read_text())["titleOnPage"] is True
    later = [Node(Role.H2, page=1, text="Overview"), Node(Role.H1, page=2, text="Part two")]
    render_html(doc_with(later), "Annual Report", tmp_path / "b", page_widths=())
    assert "titleOnPage" not in json.loads((tmp_path / "b" / "manifest.json").read_text())


def test_a_both_scope_header_emits_no_invalid_scope_value(tmp_path):
    # /Scope /Both heads its row and its column; the HTML scope attribute has no such value
    # (row, col, rowgroup, colgroup). Without a scope a browser infers the cell's role from its
    # position, so the cell keeps both axes instead of losing one to scope="col" or "row".
    t = Node(Role.TABLE, page=1, children=[
        Node(Role.TR, page=1, text="r0", children=[
            Node(Role.TH, page=1, text="Corner", attrs={"scope": "both"}),
            Node(Role.TH, page=1, text="2024")]),
        Node(Role.TR, page=1, text="r1", children=[
            Node(Role.TH, page=1, text="ResNet", attrs={"scope": "row"}),
            Node(Role.TD, page=1, text="3.57")]),
    ])
    _, index = write(tmp_path, doc_with([t], pages=1))
    from pages.checks import parse_html
    assert 'scope="both"' not in index
    corner, _, resnet = parse_html(index).find_all("table")[0].find_all("th")
    assert "scope" not in corner.attrs
    assert resnet.attrs["scope"] == "row"


def test_text_that_looks_like_a_page_link_placeholder_is_text(tmp_path):
    # PDF strings (a tooltip, alt text, a title) can hold NUL: pdfium and pikepdf keep it and
    # squash does not strip it. The page-link placeholder must not be something such text can
    # spell (and a NUL is not valid HTML), or a crafted "\x00p9\x00" aborts the conversion at resolve_links.
    forged = "see \x00p9\x00 and \x00p1:n-x\x00 here"
    doc = doc_with([Node(Role.P, page=1, text=forged),
                    Node(Role.FIELD, page=1, attrs={"kind": "text", "fid": "f1-0", "label": "none",
                                                     "aria": "Name", "tooltip": forged}),
                    Node(Role.P, page=1, text="Chapter 1", spans=[Span(0, 9, "link", "#p1")])],
                   pages=1)
    _, index = write(tmp_path, doc)
    assert 'href="chunks/0001.html#p1"' in index          # the real link still resolves
    assert "see p9 and p1:n-x here" in index   # the forged ones stay as text, minus the NULs
    assert "\x00" not in index
