"""reflow.ingest.ocr_trust outside the policy: hOCR in and out, colour, the sidecar, imports."""
import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from reflow.ingest import ocr_trust
from reflow.ingest.ocr_trust import (POLICY_VERSION, Grade, PageGrade, Word, clean_hocr,
                                     grade_words, page_of, parse_hocr, pen_share, read_sidecar,
                                     sidecar_path, write_sidecar)

HOCR = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
 <div class='ocr_page' id='page_1' title='image "x.png"; bbox 0 0 600 400'>
  <span class='ocr_line' id='line_1_1' title="bbox 10 10 590 40; x_size 30">
   <span class='ocrx_word' id='word_1_1' title='bbox 10 10 90 40; x_wconf 93'>Typed</span>
   <span class='ocrx_word' id='word_1_2' title='bbox 100 10 120 40; x_wconf 12'>7</span>
   <span class='ocrx_word' id='word_1_3' title='bbox 130 10 210 40; x_wconf 91'><strong>words</strong></span>
   <span class='ocrx_word' id='word_1_4' title='bbox 220 10 230 40; x_wconf 20'>,</span>
   <span class='ocrx_word' id='word_1_5' title='bbox 240 10 320 40; x_wconf 90'>here</span>
  </span>
  <span class='ocr_line' id='line_1_2' title="bbox 10 60 590 90; x_size 30">
   <span class='ocrx_word' id='word_1_6' title='bbox 10 60 90 90; x_wconf 9'>#&amp;x</span>
   <span class='ocrx_word' id='word_1_7' title='bbox 100 60 190 90; x_wconf 92'>below</span>
  </span>
 </div></body></html>
"""


def test_parse_hocr_reads_words_lines_and_confidence():
    words = parse_hocr(HOCR)
    assert [w.text for w in words] == ["Typed", "7", "words", ",", "here", "#&x", "below"]
    assert [w.conf for w in words] == [93, 12, 91, 20, 90, 9, 92]
    assert [w.line for w in words] == [0, 0, 0, 0, 0, 1, 1]
    assert words[0].box == (10, 10, 90, 40) and words[0].id == "word_1_1"


def test_clean_hocr_drops_each_unread_word_alone():
    raw = parse_hocr(HOCR)
    words = grade_words(raw, None)
    assert [w.grade for w in words] == [Grade.READ, Grade.UNREAD, Grade.READ, Grade.UNREAD,
                                        Grade.READ, Grade.UNREAD, Grade.READ]
    out = clean_hocr(HOCR, words, [w.id for w in raw])
    kept = parse_hocr(out)
    assert [w.text for w in kept] == ["Typed", "words", "here", "below"]   # digit, comma, junk gone
    assert "word_1_2" not in out and "word_1_4" not in out and "word_1_6" not in out
    assert "<strong>words</strong>" in out and "line_1_2" in out


def test_clean_hocr_lowers_the_top_of_a_line_that_lost_a_tall_word():
    """OCRmyPDF sizes a line's font from its box: a dropped tall "é" must not leave its height."""
    hocr = """<div class='ocr_page' title='bbox 0 0 600 400'>
  <span class='ocr_line' id='line_1_1' title="bbox 10 0 590 40; baseline 0 -8; x_size 30">
   <span class='ocrx_word' id='word_1_1' title='bbox 10 18 90 40; x_wconf 93'>House</span>
   <span class='ocrx_word' id='word_1_2' title='bbox 100 0 120 40; x_wconf 12'>é</span>
   <span class='ocrx_word' id='word_1_3' title='bbox 130 20 210 40; x_wconf 91'>have</span>
  </span>
  <span class='ocr_line' id='line_1_2' title="bbox 10 60 590 90; textangle 90; x_size 30">
   <span class='ocrx_word' id='word_1_4' title='bbox 10 60 90 90; x_wconf 9'>#&amp;x</span>
   <span class='ocrx_word' id='word_1_5' title='bbox 100 70 190 90; x_wconf 92'>below</span>
  </span>
  <span class='ocr_line' id='line_1_3' title="bbox 10 100 590 130; x_size 30">
   <span class='ocrx_word' id='word_1_6' title='bbox 10 110 90 130; x_wconf 92'>kept</span>
  </span>
</div>"""
    raw = parse_hocr(hocr)
    out = clean_hocr(hocr, grade_words(raw, None), [w.id for w in raw])
    assert "bbox 10 18 590 40; baseline 0 -8" in out           # bottom and baseline stay
    assert "bbox 10 60 590 90; textangle 90" in out            # a turned line is left alone
    assert "bbox 10 100 590 130" in out                        # a line that lost nothing too
    assert [w.text for w in parse_hocr(out)] == ["House", "have", "below", "kept"]


def test_page_of_reads_ocrmypdfs_page_file_name(tmp_path):
    assert page_of(tmp_path / "000003_ocr_hocr.hocr") == 3
    with pytest.raises(ValueError):
        page_of(tmp_path / "ocr_hocr.hocr")


def _page(colour, paper=(255, 255, 255)):
    image = Image.new("RGB", (400, 300), paper)
    ImageDraw.Draw(image).rectangle((100, 100, 200, 140), fill=colour)
    return image


def test_pen_share():
    purple = _page((110, 40, 160))
    assert pen_share(purple, (90, 90, 210, 150)) >= 0.9
    black = _page((20, 20, 20))
    ImageDraw.Draw(black).rectangle((300, 10, 320, 20), fill=(200, 20, 20))   # colour elsewhere
    assert pen_share(black, (90, 90, 210, 150)) <= 0.05
    tinted = _page((25, 22, 20), paper=(232, 214, 180))
    assert pen_share(tinted, (90, 90, 210, 150)) <= 0.05
    assert pen_share(_page((20, 20, 20)).convert("L"), (90, 90, 210, 150)) is None


def _grade(page=1, mode="redo"):
    return PageGrade(page, mode, (600, 400), (Word((1, 2, 3, 4), "w", 50, Grade.DOUBT, None, 1.0,
                                                   layer_text="w"),))


def test_sidecar_round_trip_and_why_it_is_unusable(tmp_path):
    working = tmp_path / "working.pdf"
    working.write_bytes(b"%PDF-1.7 one")
    assert read_sidecar(working).status == "absent"
    write_sidecar(working, [_grade(2, "force"), _grade(1)])
    read = read_sidecar(working)
    assert read.status == "ok" and read.pages == {1: _grade(1), 2: _grade(2, "force")}
    working.write_bytes(b"%PDF-1.7 two")                   # another PDF: stale
    assert read_sidecar(working) == ocr_trust.SidecarRead("stale", {})
    write_sidecar(working, [_grade()])
    data = json.loads(sidecar_path(working).read_text())
    sidecar_path(working).write_text(json.dumps({**data, "policy": POLICY_VERSION + 1}))
    assert read_sidecar(working).status == "stale"
    sidecar_path(working).write_text(sidecar_path(working).read_text()[:40])
    assert read_sidecar(working) == ocr_trust.SidecarRead("corrupt", {})
    sidecar_path(working).write_text(json.dumps({**data, "pages": [{"page": 1}]}))
    assert read_sidecar(working).status == "corrupt"


ALLOWED = {"__future__", "PIL", "ocrmypdf"}


def test_ocr_trust_imports_only_the_standard_library_and_pillow():
    """System Python runs OCRmyPDF and loads the file by path: no reflow, no numpy."""
    tree = ast.parse(Path(ocr_trust.__file__).read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            found.add((node.module or "").split(".")[0])
    foreign = {m for m in found - ALLOWED if m not in sys.stdlib_module_names}
    assert not foreign, foreign
    guarded = [n for n in tree.body if isinstance(n, ast.If)
               and "find_spec" in ast.unparse(n.test)]
    assert guarded and any("ocrmypdf" in ast.unparse(n) for n in guarded)
    top = {(n.module or "").split(".")[0] for n in tree.body if isinstance(n, ast.ImportFrom)}
    assert "ocrmypdf" not in top            # only inside the guarded block


def test_ocr_trust_loads_under_the_system_python_that_runs_ocrmypdf():
    system = Path("/usr/bin/python3")
    if not system.exists():
        pytest.skip("no system Python")
    probe = ("import importlib.util as u, sys; s = u.spec_from_file_location('t', sys.argv[1]); "
             "m = u.module_from_spec(s); sys.modules['t'] = m; s.loader.exec_module(m); print(m.Grade.DOUBT)")
    done = subprocess.run([str(system), "-c", probe, ocr_trust.__file__], capture_output=True,
                          text=True)
    if "No module named 'PIL'" in done.stderr:
        pytest.skip("system Python has no Pillow")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "doubt"
