"""The page shells render_html writes around the chunk HTML (reflow.render.shell)."""

from reflow.render.chunks import Chunk
from reflow.render.shell import chunk_page, index_page

CHUNKS = [Chunk(1, [1, 2], "<p>one</p>", "Intro & <more>"), Chunk(2, [3], "<p>two</p>"),
          Chunk(3, [4, 5], "<p>three</p>", "Last")]
COMMON = dict(title='Fees "2026" & <rates>', lang="en", rtl=False, page_count=1234, version="")


def index(**kw) -> str:
    args = dict(COMMON, chunks=CHUNKS, inlined=["<p>one</p>"], toc=[], toc_complete=True)
    return index_page(**(args | kw))


def chunk(i: int, **kw) -> str:
    args = dict(COMMON, chunk=CHUNKS[i], content=CHUNKS[i].html,
                prev=CHUNKS[i - 1] if i else None,
                next=CHUNKS[i + 1] if i + 1 < len(CHUNKS) else None)
    return chunk_page(**(args | kw))


def test_the_title_is_escaped_everywhere_it_appears():
    page = index()
    assert "<title>Fees &#34;2026&#34; &amp; &lt;rates&gt;</title>" in page
    assert "<h1>Fees &#34;2026&#34; &amp; &lt;rates&gt;</h1>" in page
    assert "<rates>" not in page
    assert '<a href="index.html">Fees &#34;2026&#34; &amp; &lt;rates&gt;</a> · 1,234 pages' in chunk(1)


def test_the_chunk_html_goes_in_unescaped():
    assert 'data-loaded="1"><p>one</p></div>' in index()
    assert '<div class="chunk-content"><p>two</p></div>' in chunk(1)


def test_language_and_direction():
    assert '<html lang="en">' in index()
    page = index(lang="ar", rtl=True)
    assert '<html lang="ar" dir="rtl">' in page


def test_a_version_busts_the_cache_of_the_assets_and_chunks():
    assert 'href="assets/viewer.css"' in index() and "?v=" not in index()
    page = index(version="ab12")
    assert '<link rel="stylesheet" href="assets/viewer.css?v=ab12">' in page
    assert '<script src="assets/viewer.js?v=ab12" defer></script>' in page
    assert 'data-src="chunks/0002.html?v=ab12"' in page
    assert 'href="assets/viewer.css?v=ab12"' in chunk(0, version="ab12")


def test_the_first_chunks_are_inline_and_the_rest_are_stubs_with_their_heading():
    page = index()
    assert ('<div class="chunk" data-src="chunks/0001.html" data-first="1" data-last="2" '
            'data-loaded="1"><p>one</p></div>') in page
    assert ('<div class="chunk" data-src="chunks/0002.html" data-first="3" data-last="3">\n'
            '<p class="chunk-stub"><a href="chunks/0002.html">Continue reading: pages 3</a></p>') in page
    assert "Continue reading: pages 4–5 (Last)</a>" in page
    assert "Continue reading: pages 1–2" not in page


def test_the_contents_nest_and_say_when_they_are_cut():
    toc = [{"href": "chunks/0001.html#a", "text": "A & B", "children": [
               {"href": "chunks/0001.html#a1", "text": "A1", "children": []}]},
           {"href": "chunks/0003.html#c", "text": "C", "children": []}]
    page = index(toc=toc, toc_complete=False)
    assert ('<ol role="list">\n<li><a href="chunks/0001.html#a">A &amp; B</a><ol role="list">\n'
            '<li><a href="chunks/0001.html#a1">A1</a></li>\n</ol>\n</li>\n'
            '<li><a href="chunks/0003.html#c">C</a></li>\n</ol>') in page
    assert 'All headings are in <a href="toc.json">toc.json</a>.' in page
    assert "toc-note" not in index(toc=toc, toc_complete=True)
    assert '<nav class="toc"' not in index()


def test_the_index_links_the_original_pdf():
    assert '1,234 pages · <a href="original.pdf">Download the original PDF</a>' in index()


def test_a_chunk_page_links_its_neighbours_and_resolves_from_the_folder_above():
    first, middle, last = chunk(0), chunk(1), chunk(2)
    assert '<base href="../">' in middle
    assert '<a class="skip" href="chunks/0002.html#content">' in middle
    assert "<title>Fees &#34;2026&#34; &amp; &lt;rates&gt;: pages 3</title>" in middle
    assert '<a href="chunks/0001.html" rel="prev">Previous: pages 1–2</a>' in middle
    assert '<a href="chunks/0003.html" rel="next">Next: pages 4–5</a>' in middle
    assert 'rel="prev"' not in first and 'rel="next"' in first
    assert 'rel="next"' not in last and '<a href="index.html">Contents</a>' in last
