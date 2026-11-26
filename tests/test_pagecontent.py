"""PlumberPage: from_page reads pdfminer's layout directly and must give what pdfplumber's dicts
give; summary joins the pieces of a page's MCIDs and must give what the chars one by one give."""

import random
from collections import defaultdict
from pathlib import Path

import pdfplumber
import pikepdf
import pytest
from conftest import make_tagged_pdf
from pikepdf import Dictionary, Name

from reflow import geometry
from reflow.extract.pagecontent import GRAPHIC_KINDS, WORD_GAP, PlumberPage

FW9 = Path(__file__).resolve().parent.parent / "samples" / "fw9.pdf"


def from_dicts(page, words: bool) -> PlumberPage:
    """What pdfplumber's own dicts give for the page: the reference for from_page."""
    chars: dict[int, list[tuple]] = defaultdict(list)
    graphics: dict[int, list] = defaultdict(list)
    for char in page.chars:
        if char.get("mcid") is not None:
            chars[char["mcid"]].append((char["text"], char["x0"], char["top"],
                                        char["x1"], char["bottom"], char["size"] or 0.0))
    for kind in GRAPHIC_KINDS:
        for obj in page.objects.get(kind, ()):
            if obj.get("mcid") is not None:
                graphics[obj["mcid"]].append((obj["x0"], obj["top"], obj["x1"], obj["bottom"]))
    found = ([(w["text"], w["x0"], w["x1"], w["top"], w["bottom"])
              for w in page.extract_words(keep_blank_chars=False, x_tolerance_ratio=WORD_GAP)]
             if words else None)
    return PlumberPage(len(page.chars), dict(chars), dict(graphics), found)


def test_words_split_at_a_word_space_written_as_kerning(tmp_path):
    # The words under a link (#324): an 8 pt space written as TJ kerning is a word gap, and a
    # digit run kerned tight is one word.
    from conftest import make_kerned_table_pdf

    with pdfplumber.open(make_kerned_table_pdf(tmp_path / "kerned.pdf")) as plumber:
        page = PlumberPage.from_page(plumber.pages[0], words=True)
    assert [w[0] for w in page.words] == ["two", "words", "2553", "electing", "1706", "W-9"]


def same_content(path: Path, pages: range) -> list[PlumberPage]:
    found = []
    with pdfplumber.open(path) as plumber:
        for n in pages:
            page = plumber.pages[n - 1]
            lean = PlumberPage.from_page(page, words=True)
            dicts = from_dicts(page, True)
            assert lean.chars, f"page {n} has no tagged chars to compare"
            assert (lean.total, lean.chars, lean.graphics, lean.words) == \
                   (dicts.total, dicts.chars, dicts.graphics, dicts.words)
            found.append(lean)
    return found


def reboxed(source: Path, target: Path, **page_keys) -> Path:
    with pikepdf.open(source) as pdf:
        for page in pdf.pages:
            for key, value in page_keys.items():
                page.obj[f"/{key}"] = pikepdf.Array(value) if isinstance(value, list) else value
        pdf.save(target)
    return target


@pytest.fixture(scope="module")
def tagged(tmp_path_factory):
    return make_tagged_pdf(tmp_path_factory.mktemp("lean") / "tagged.pdf")


def test_tagged_chars_and_figure_match_the_dicts(tagged):
    same_content(tagged, range(1, 3))


def test_a_media_box_that_does_not_start_at_the_origin(tagged, tmp_path):
    same_content(reboxed(tagged, tmp_path / "offset.pdf", MediaBox=[10, 20, 622, 812]),
                 range(1, 3))


def test_a_rotated_page(tagged, tmp_path):
    same_content(reboxed(tagged, tmp_path / "turned.pdf", Rotate=90), range(1, 3))


def test_a_real_form():
    same_content(FW9, range(1, 3))


def test_one_marked_content_id_with_rects_and_an_image(tmp_path):
    pdf = pikepdf.new()
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.Helvetica))
    image = pikepdf.Stream(pdf, bytes(100 * 50), Type=Name.XObject, Subtype=Name.Image,
                           Width=100, Height=50, ColorSpace=Name.DeviceGray,
                           BitsPerComponent=8)
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = Dictionary(Font=Dictionary(F1=font), XObject=Dictionary(Im0=image))
    page.Contents = pdf.make_stream(
        b"/P << /MCID 0 >> BDC BT /F1 12 Tf 72 700 Td (Caption) Tj ET EMC\n"
        b"/Figure << /MCID 1 >> BDC 10 10 40 40 re f q 100 0 0 50 72 500 cm /Im0 Do Q "
        b"200 200 30 30 re f EMC\n")
    path = tmp_path / "mixed.pdf"
    pdf.save(path)
    (content,) = same_content(path, range(1, 2))
    assert len(content.graphics[1]) == 3


def reference_summary(content: PlumberPage, mcids: list[int]):
    """The box and text of the chars under these MCIDs, taken char by char in content order."""
    chars = [c for m in mcids for c in content.chars.get(m, ())]
    boxes = [(c[1], c[2], c[3], c[4]) for c in chars]
    if not boxes:
        boxes = [b for m in mcids for b in content.graphics.get(m, ())]
    out, prev = [], None
    for char in chars:
        text, x0, _, _, bottom, size = char
        if prev is not None and text.strip() and not out[-1].isspace():
            if abs(bottom - prev[4]) > size / 2 or x0 - prev[3] > size / 3:
                out.append(" ")
        out.append(text)
        prev = char
    return geometry.union(boxes), " ".join("".join(out).split())


def random_content(rng: random.Random) -> PlumberPage:
    texts = ["a", "b", "W", " ", "  ", "", "fi", "(cid:7)", " ", "-", "x\n", "ا"]
    chars, graphics = {}, {}
    for mcid in range(rng.randint(0, 7)):
        if rng.random() < 0.15:
            continue
        run, x, top = [], rng.uniform(0, 300), rng.uniform(0, 700)
        for _ in range(rng.randint(1, 12)):
            size = rng.choice([0.0, 6.0, 10.0, 11.5])
            x += rng.choice([0.0, size * 0.1, size * 0.5, size, -20.0, 40.0])
            top += rng.choice([0.0, 0.0, 0.0, size, -size / 2, size * 3])
            run.append((rng.choice(texts), x, top, x + size * 0.5, top + size, size))
        chars[mcid] = run
    for mcid in range(rng.randint(0, 9)):
        if rng.random() < 0.4:
            graphics[mcid] = [(rng.uniform(0, 100), rng.uniform(0, 100), 150.0, 160.0)
                              for _ in range(rng.randint(1, 3))]
    return PlumberPage(0, chars, graphics)


def test_summary_is_what_the_chars_give_one_by_one():
    rng = random.Random(7)
    for _ in range(400):
        content = random_content(rng)
        for _ in range(12):
            mcids = [rng.randint(0, 9) for _ in range(rng.randint(0, 6))]
            assert content.summary(mcids) == reference_summary(content, mcids), \
                (content.chars, mcids)
