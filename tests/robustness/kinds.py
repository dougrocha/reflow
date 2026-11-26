"""The damage kinds of the robustness harness: KINDS is the only list.

A kind damages a good PDF on purpose, the way a badly made PDF is damaged inside, and says what
reflow should publish from the result. Each one has

  id, blurb   a stable name, and one line on what it does to the file
  transform   (src, dst, rng) -> Damage: writes the damaged copy of `src` to `dst`, and says
              which pages (and, where it can, which region) it damaged. It raises Unsupported
              when the document has nothing the kind can damage (a precondition), never a
              no-op copy
  pixels      SAME when the damaged file must render exactly as the clean one: the harness
              renders both with pdfium and scores the cell invalid when they differ. CHANGES
              when the damage is visible (rotation, outlines, a raster page)
  expect      what reflow should publish: `same` (the semantic projection of the clean
              conversion: text in reading order, table cells, link text, field values and the
              language), `flagged(code)` (a review fact with that code over the damaged region),
              or `repaired(code)` (both)
  enforced    the bases on which a miss fails pytest. Every other cell is a scorecard miss, so
              the suite lands while most cells are red, and a fix turns its cells on
  target      what the fixes of tracker #303 aim for, where that is more than `expect`
  unenforced  why a base outside `enforced` is not in it: base (or "*" for every other base)
              to the reason, which the scorecard prints beside the cell. A cell the kind can
              damage that is neither enforced nor explained fails the robustness session

Most transforms use pikepdf. Outlines and rasterizing use Ghostscript, deleting the tag tree and
flattening forms use qpdf, as the issue (#305) names them.
"""

from __future__ import annotations

import random
import shutil
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum
from io import BytesIO
from pathlib import Path

import pikepdf
from pikepdf import Array, ContentStreamInstruction, Dictionary, Name, Operator, String

# Codes a kind may expect before reflow raises them. Every other expected code is one reflow has
# (reflow.analyze.confidence.CODES).
PLANNED_CODES: dict[str, str] = {}
# Codes no kind expects that a clean control must still never raise (a false alarm), and that the
# real files' "flagged" column counts: text-disputed, which tounicode-shifted expected until the
# font repair of #309 took over its damage.
WATCHED_CODES = frozenset({"text-disputed"})


class Unsupported(Exception):
    """The document has nothing this kind can damage: a precondition, not a failure."""


class Pixels(Enum):
    SAME = "same"
    CHANGES = "changes"


@dataclass(frozen=True)
class Expect:
    same: bool
    code: str | None = None   # the review fact that must cover the damaged region ...
    also: tuple[str, ...] = ()   # ... or one of these, where another path catches the damage

    @property
    def codes(self) -> tuple[str, ...]:
        return (self.code, *self.also) if self.code else ()

    def __str__(self) -> str:
        if self.code is None:
            return "same"
        named = " or ".join(self.codes)
        return f"repaired({named})" if self.same else f"flagged({named})"


def same() -> Expect:
    return Expect(True)


def flagged(code: str, *also: str) -> Expect:
    return Expect(False, code, also)


def repaired(code: str) -> Expect:
    return Expect(True, code)


Box = tuple[float, float, float, float]   # x0, top, x1, bottom in points, as document.json


@dataclass(frozen=True)
class Damage:
    pages: frozenset[int]                         # 1-based
    boxes: dict[int, Box] = field(default_factory=dict)   # a page without one: the whole page
    note: str = ""


Transform = Callable[[Path, Path, random.Random], Damage]
ALL = frozenset({"*"})


@dataclass(frozen=True)
class DamageKind:
    id: str
    blurb: str
    transform: Transform
    pixels: Pixels
    expect: Expect
    enforced: frozenset[str] = frozenset()
    target: Expect | None = None
    tool: str = "pikepdf"
    unenforced: dict[str, str] = field(default_factory=dict, hash=False, compare=False)

    def enforced_on(self, base: str) -> bool:
        return "*" in self.enforced or base in self.enforced

    def why_unenforced(self, base: str) -> str:
        """The reason the kind is not enforced on `base`, or "" when none is written."""
        if self.enforced_on(base):
            return ""
        return self.unenforced.get(base, self.unenforced.get("*", ""))


# ---------------------------------------------------------------------------- pikepdf plumbing

def save(pdf: pikepdf.Pdf, dst: Path) -> None:
    """Every pikepdf file of a run is saved this way, the clean control too, so the bytes of a
    mutant differ from the control's only by its damage."""
    pdf.save(dst, deterministic_id=True)


def _pike(fn: Callable[[pikepdf.Pdf, random.Random, Path], Damage]) -> Transform:
    def run(src: Path, dst: Path, rng: random.Random) -> Damage:
        with pikepdf.open(src) as pdf:
            damage = fn(pdf, rng, src)
            save(pdf, dst)
        return damage
    run.__name__ = run.__qualname__ = fn.__name__
    return run


def _inherited(page: pikepdf.Page, key: str):
    node = page.obj
    while node is not None:
        if key in node:
            return node[key]
        node = node.get("/Parent")
    return None


def _resources(page: pikepdf.Page) -> Dictionary:
    res = _inherited(page, "/Resources")
    return res if res is not None else Dictionary()


def _fonts(page: pikepdf.Page) -> dict[str, Dictionary]:
    fonts = _resources(page).get("/Font")
    return {str(k): v for k, v in fonts.items()} if fonts is not None else {}


def _code_width(font: Dictionary) -> int | None:
    """Bytes per character code: 1 for a simple font, 2 for a Type0 font in Identity-H or -V;
    None for any other CMap."""
    if font.get("/Subtype") != Name.Type0:
        return 1
    return 2 if font.get("/Encoding") in (Name("/Identity-H"), Name("/Identity-V")) else None


def unicode_map(font: Dictionary) -> dict[int, str] | None:
    """Code -> the text the clean font gives for it: its /ToUnicode, else a simple font's
    encoding. None when neither says."""
    from pdfminer.cmapdb import CMapParser, FileUnicodeMap
    from pdfminer.encodingdb import EncodingDB
    from pdfminer.psparser import LIT

    if "/ToUnicode" in font and isinstance(font.ToUnicode, pikepdf.Stream):
        cmap = FileUnicodeMap()
        CMapParser(cmap, BytesIO(font.ToUnicode.read_bytes())).run()
        return {int(k): v for k, v in cmap.cid2unichr.items()}
    if font.get("/Subtype") not in (Name.Type1, Name.TrueType, Name.MMType1):
        return None
    base = str(font.get("/BaseFont", "/"))[1:].split("+")[-1]
    if base in ("Symbol", "ZapfDingbats"):
        return None
    enc, name, diff = font.get("/Encoding"), "StandardEncoding", None
    if isinstance(enc, Name):
        name = str(enc)[1:]
    elif isinstance(enc, Dictionary):
        name = str(enc.get("/BaseEncoding", Name.StandardEncoding))[1:]
        diff = [int(x) if isinstance(x, int) else LIT(str(x)[1:]) for x in enc.get("/Differences", [])]
    try:
        return EncodingDB.get_encoding(name, diff)
    except KeyError:
        return None


def _space_width(font: Dictionary, umap: dict[int, str]) -> float | None:
    """The width of code 32 in glyph units (1000 an em), if code 32 is a space."""
    if umap.get(32) != " ":
        return None
    if "/Widths" in font:
        first = int(font.get("/FirstChar", 0))
        widths = font.Widths
        return float(widths[32 - first]) if 0 <= 32 - first < len(widths) else None
    from pdfminer.fontmetrics import FONT_METRICS
    metrics = FONT_METRICS.get(str(font.get("/BaseFont", "/"))[1:])
    return float(metrics[1][" "]) if metrics and " " in metrics[1] else None


def _write_contents(pdf: pikepdf.Pdf, page: pikepdf.Page, instructions: list) -> None:
    page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(instructions))


def _ins(op: str, *operands) -> ContentStreamInstruction:
    return ContentStreamInstruction(list(operands), Operator(op))


SHOWS = {"Tj", "TJ", "'", '"'}


def _walk_text(page: pikepdf.Page):
    """(instruction, font name, font size) for each instruction of the page's own content, with
    the font state that q, Q and Tf leave."""
    state, stack = (None, 0.0), []
    for ins in pikepdf.parse_content_stream(page):
        op = str(ins.operator)
        if op == "q":
            stack.append(state)
        elif op == "Q" and stack:
            state = stack.pop()
        elif op == "Tf":
            state = (str(ins.operands[0]), float(ins.operands[1]))
        yield ins, state[0], state[1]


def _codes(data: bytes, width: int) -> list[bytes]:
    return [data[i:i + width] for i in range(0, len(data), width)]


def _shown_bytes(page: pikepdf.Page) -> dict[str, int]:
    """Bytes each font of the page shows in its own content."""
    count: dict[str, int] = {}
    for ins, font, _ in _walk_text(page):
        if str(ins.operator) in SHOWS and font is not None:
            items = ins.operands[-1]
            items = items if isinstance(items, Array) else [items]
            count[font] = count.get(font, 0) + sum(len(bytes(s)) for s in items
                                                   if isinstance(s, String))
    return count


def _font_box(src: Path, page_number: int, base_font: str) -> Box | None:
    """The region the chars of a font cover on the clean page (pdfplumber's top-down points)."""
    import pdfplumber

    with pdfplumber.open(src) as doc:
        chars = [c for c in doc.pages[page_number - 1].chars if c["fontname"] == base_font]
    if not chars:
        return None
    return (min(c["x0"] for c in chars), min(c["top"] for c in chars),
            max(c["x1"] for c in chars), max(c["bottom"] for c in chars))


# ------------------------------------------------------------------- one font's text layer

def _damage_font(rewrite: Callable[[pikepdf.Pdf, Dictionary, dict[int, str], int], None]):
    """A transform that rewrites one font of page 1, the one that shows the most bytes there,
    in a private copy, so the other pages keep the clean font."""
    def transform(pdf: pikepdf.Pdf, rng: random.Random, src: Path) -> Damage:
        page = pdf.pages[0]
        fonts = _fonts(page)
        for name, _ in sorted(_shown_bytes(page).items(), key=lambda kv: -kv[1]):
            font = fonts.get(name)
            if font is None or (width := _code_width(font)) is None:
                continue
            umap = unicode_map(font)
            if umap and any(t.isalpha() for t in umap.values()):
                break
        else:
            raise Unsupported("page 1 shows no text in a font whose text this kind can rewrite")
        copy = _private_font(pdf, page, name)
        rewrite(pdf, copy, umap, width)
        base = str(font.get("/BaseFont", "/"))[1:]
        box = _font_box(src, 1, base)
        return Damage(frozenset({1}), {1: box} if box else {}, f"font {name} ({base})")
    transform.__name__ = rewrite.__name__
    return transform


def _private_font(pdf: pikepdf.Pdf, page: pikepdf.Page, name: str) -> Dictionary:
    """A copy of the page's font `name` that only this page uses."""
    res = Dictionary({k: v for k, v in _resources(page).items()})
    copy = pdf.make_indirect(Dictionary({k: v for k, v in res.Font[name].items()}))
    res.Font = Dictionary({**{k: v for k, v in res.Font.items()}, name: copy})
    page.obj.Resources = res
    return copy


def _cmap(pdf: pikepdf.Pdf, mapping: dict[int, str], width: int) -> pikepdf.Stream:
    lines = ["/CIDInit /ProcSet findresource begin", "12 dict begin", "begincmap",
             "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
             "/CMapName /Adobe-Identity-UCS def", "/CMapType 2 def",
             "1 begincodespacerange", f"<{'00' * width}> <{'FF' * width}>", "endcodespacerange"]
    items = [(c, t) for c, t in sorted(mapping.items()) if t]
    for i in range(0, len(items), 100):
        chunk = items[i:i + 100]
        lines.append(f"{len(chunk)} beginbfchar")
        lines += [f"<{c:0{2 * width}X}> <{t.encode('utf-16-be').hex().upper()}>" for c, t in chunk]
        lines.append("endbfchar")
    lines += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    return pdf.make_stream("\n".join(lines).encode())


def shift_letter(ch: str) -> str:
    """The next letter of the alphabet (z to a), anything else as it is."""
    for first in ("a", "A"):
        if first <= ch <= chr(ord(first) + 25):
            return chr((ord(ch) - ord(first) + 1) % 26 + ord(first))
    return ch


def _missing_tounicode(pdf, font: Dictionary, umap: dict[int, str], width: int) -> None:
    if "/ToUnicode" not in font:
        raise Unsupported("the font has no /ToUnicode to remove")
    del font["/ToUnicode"]


def _shifted_tounicode(pdf, font: Dictionary, umap: dict[int, str], width: int) -> None:
    font.ToUnicode = _cmap(pdf, {c: "".join(map(shift_letter, t)) for c, t in umap.items()}, width)


def _pua_tounicode(pdf, font: Dictionary, umap: dict[int, str], width: int) -> None:
    font.ToUnicode = _cmap(pdf, {c: (t if t.isspace() else chr(0xE000 + (c & 0x7FF)))
                                 for c, t in umap.items()}, width)


LIGATURES = {b"fi": (0x81, "/fi", "\ufb01"), b"fl": (0x8D, "/fl", "\ufb02")}   # cp1252 holes


def _broken_ligature(pdf: pikepdf.Pdf, rng: random.Random, src: Path) -> Damage:
    """On page 1, every fi and fl a standard Helvetica or Times font shows is drawn as the
    ligature glyph, which the text layer (a /ToUnicode) reads as a bare "f": "specifcally".
    The glyph is wider than the font's f, as a dropped ligature is in a real file."""
    from pdfminer.fontmetrics import FONT_METRICS

    page = pdf.pages[0]
    fonts = _fonts(page)
    shown = _shown_bytes(page)
    names = []
    for name, font in fonts.items():
        base = str(font.get("/BaseFont", "/"))[1:]
        metrics = FONT_METRICS.get(base, (None, {}))[1]
        desc = font.get("/FontDescriptor")
        if (font.get("/Subtype") != Name.Type1 or "/Widths" in font or shown.get(name, 0) == 0
                or (desc is not None and any(k in desc for k in ("/FontFile", "/FontFile2",
                                                                    "/FontFile3")))
                or metrics.get("\ufb01", 0) <= metrics.get("f", 0)):
            continue
        names.append(name)
    out, hit = [], set()
    for ins, font_name, _ in _walk_text(page):
        op = str(ins.operator)
        if op in ("Tj", "TJ") and font_name in names:
            items = [ins.operands[0]] if op == "Tj" else list(ins.operands[0])
            new = []
            for item in items:
                if isinstance(item, String):
                    data = bytes(item)
                    for pair, (code, _, _) in LIGATURES.items():
                        data = data.replace(pair, bytes([code]))
                    if data != bytes(item):
                        hit.add(font_name)
                    item = String(data)
                new.append(item)
            out.append(_ins("Tj", new[0]) if op == "Tj" else _ins("TJ", Array(new)))
        else:
            out.append(ins)
    if not hit:
        raise Unsupported("page 1 shows no fi or fl in a standard Helvetica or Times font")
    _write_contents(pdf, page, out)
    boxes = []
    for name in sorted(hit):
        font = _private_font(pdf, page, name)
        umap = unicode_map(font) or {}
        enc = font.get("/Encoding")
        base_enc = enc if isinstance(enc, Name) else Name.StandardEncoding
        font.Encoding = Dictionary(Type=Name.Encoding, BaseEncoding=base_enc, Differences=Array(
            [x for code, glyph, _ in LIGATURES.values() for x in (code, Name(glyph))]))
        font.ToUnicode = _cmap(pdf, {**{c: t for c, t in umap.items() if c < 256},
                                     **{code: "f" for code, _, _ in LIGATURES.values()}}, 1)
        if (box := _font_box(src, 1, str(font.BaseFont)[1:])) is not None:
            boxes.append(box)
    box = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes),
           max(b[3] for b in boxes)) if boxes else None
    return Damage(frozenset({1}), {1: box} if box else {}, f"fonts {sorted(hit)}")


# ------------------------------------------------------------------- content-stream rewrites

def _drop_spaces(pdf: pikepdf.Pdf, rng: random.Random, src: Path) -> Damage:
    """Word gaps as TJ kerning, as pdfTeX writes them: every space a simple font shows with Tj
    or TJ becomes a kerning number that moves the next glyph just as far, Tc and Tw included."""
    changed = set()
    for number, page in enumerate(pdf.pages, 1):
        fonts, out, dirty = _fonts(page), [], False
        tc = tw = 0.0
        stack: list[tuple[float, float]] = []
        for ins, font_name, size in _walk_text(page):
            op = str(ins.operator)
            if op == "q":
                stack.append((tc, tw))
            elif op == "Q" and stack:
                tc, tw = stack.pop()
            elif op == "Tc":
                tc = float(ins.operands[0])
            elif op == "Tw":
                tw = float(ins.operands[0])
            elif op == '"':
                tw, tc = float(ins.operands[0]), float(ins.operands[1])
            font = fonts.get(font_name) if font_name else None
            if op in ("Tj", "TJ") and font is not None and size and _code_width(font) == 1:
                umap = unicode_map(font) or {}
                width = _space_width(font, umap)
                if width is not None:
                    gap = round(-(width + (tc + tw) * 1000 / size), 4)
                    items = [ins.operands[0]] if op == "Tj" else list(ins.operands[0])
                    new: list = []
                    split = False
                    for item in items:
                        if isinstance(item, String) and b" " in bytes(item):
                            split = True
                            for i, part in enumerate(bytes(item).split(b" ")):
                                if i:
                                    new.append(gap)
                                if part:
                                    new.append(String(part))
                        else:
                            new.append(item)
                    if split:
                        out.append(_ins("TJ", Array(new)))
                        dirty = True
                        continue
            out.append(ins)
        if dirty:
            _write_contents(pdf, page, out)
            changed.add(number)
    if not changed:
        raise Unsupported("no word space shown by a simple font whose code 32 is a space")
    return Damage(frozenset(changed))


def _one_glyph_per_tj(pdf: pikepdf.Pdf, rng: random.Random, src: Path) -> Damage:
    """Every Tj and TJ split into one Tj per glyph (and a bare [n] TJ per kerning number)."""
    changed = set()
    for number, page in enumerate(pdf.pages, 1):
        fonts, out, dirty = _fonts(page), [], False
        for ins, font_name, _ in _walk_text(page):
            op = str(ins.operator)
            font = fonts.get(font_name) if font_name else None
            width = _code_width(font) if font is not None else None
            if op in ("Tj", "TJ") and width:
                items = [ins.operands[0]] if op == "Tj" else list(ins.operands[0])
                pieces: list = []
                for item in items:
                    if isinstance(item, String):
                        pieces += [_ins("Tj", String(c)) for c in _codes(bytes(item), width)]
                    else:
                        pieces.append(_ins("TJ", Array([item])))
                if len(pieces) > 1:
                    out += pieces
                    dirty = True
                    continue
            out.append(ins)
        if dirty:
            _write_contents(pdf, page, out)
            changed.add(number)
    if not changed:
        raise Unsupported("no Tj or TJ shows more than one glyph")
    return Damage(frozenset(changed))


POSITION = {"Td", "TD", "Tm", "T*"}
STATE = {"Tf": "Tf", "Tc": "Tc", "Tw": "Tw", "Tz": "Tz", "TL": "TL", "Ts": "Ts", "Tr": "Tr",
         "g": "fill", "rg": "fill", "k": "fill", "sc": "fill", "scn": "fill", "cs": "fill",
         "G": "stroke", "RG": "stroke", "K": "stroke", "SC": "stroke", "SCN": "stroke",
         "CS": "stroke"}
MARKS = {"BDC", "BMC", "EMC"}


def _text_units(ops: list) -> list[tuple[int, int]]:
    """(first, last) instruction index of each BT..ET object at the top level, with the BDC or
    BMC just before and the EMC just after it when both are there."""
    names = [str(i.operator) for i in ops]
    units, i = [], 0
    while i < len(ops):
        if names[i] == "BT":
            j = names.index("ET", i)
            first, last = i, j
            if i > 0 and names[i - 1] in ("BDC", "BMC") and j + 1 < len(ops) and names[j + 1] == "EMC":
                first, last = i - 1, j + 1
            units.append((first, last))
            i = last + 1
        else:
            i += 1
    return units


def _unit_state(ops: list, unit: tuple[int, int]) -> tuple[set[str], set[str]] | None:
    """(state it sets, state it sets before its first text show), or None for a unit that does
    anything a moved unit could not do (draw, clip, change other graphics state)."""
    sets, before, shown = set(), set(), False
    for ins in ops[unit[0]:unit[1] + 1]:
        op = str(ins.operator)
        if op in SHOWS:
            shown = True
        elif op in STATE:
            sets.add(STATE[op])
            if not shown:
                before.add(STATE[op])
        elif op not in POSITION | MARKS | {"BT", "ET"}:
            return None
    return sets, before


def _shuffle_text(pdf: pikepdf.Pdf, rng: random.Random, src: Path) -> Damage:
    """The order of the text objects (BT..ET, with the marked-content pair around each) in the
    page's content stream shuffled, in runs of adjacent objects that each set every piece of
    text state the run uses before showing text; the last object of a run stays last, so the
    state it leaves is unchanged. The page renders the same; only the drawing order moves."""
    changed = set()
    for number, page in enumerate(pdf.pages, 1):
        ops = list(pikepdf.parse_content_stream(page))
        units, runs, run = _text_units(ops), [], []
        for unit in units:
            if run and unit[0] != run[-1][0][1] + 1:
                runs.append(run)
                run = []
            state = _unit_state(ops, unit)
            if state is None:
                runs.append(run)
                run = []
                continue
            run.append((unit, state))
        runs.append(run)
        new, moved = list(ops), False
        for run in runs:
            # A run is moved only if each of its units sets all the state the run uses.
            used = set().union(*(s for _, (s, _) in run)) if run else set()
            if len(run) < 3 or any(not used <= before for _, (_, before) in run):
                continue
            head = [u for u, _ in run[:-1]]
            order = list(range(len(head)))
            while order == sorted(order):
                rng.shuffle(order)
            pieces = [ops[head[k][0]:head[k][1] + 1] for k in order]
            start = head[0][0]
            flat = [ins for piece in pieces for ins in piece]
            new[start:start + len(flat)] = flat
            moved = True
        if moved:
            _write_contents(pdf, page, new)
            changed.add(number)
    if not changed:
        raise Unsupported("no run of three text objects that can be moved without changing "
                          "what they draw")
    return Damage(frozenset(changed))


def _doubled_text(pdf: pikepdf.Pdf, rng: random.Random, src: Path) -> Damage:
    """Every top-level text object drawn twice in place, as some exporters fake bold or leave a
    second layer: `q BT..ET Q BT..ET`, so both copies start from the same state."""
    changed = set()
    for number, page in enumerate(pdf.pages, 1):
        ops = list(pikepdf.parse_content_stream(page))
        names = [str(i.operator) for i in ops]
        out, i = [], 0
        while i < len(ops):
            if names[i] == "BT":
                j = names.index("ET", i)
                block = ops[i:j + 1]
                if any(str(b.operator) in SHOWS for b in block):
                    out += [_ins("q"), *block, _ins("Q"), *block]
                else:
                    out += block
                i = j + 1
            else:
                out.append(ops[i])
                i += 1
        if len(out) != len(ops):
            _write_contents(pdf, page, out)
            changed.add(number)
    if not changed:
        raise Unsupported("no text object in the pages' own content")
    return Damage(frozenset(changed))


# Operators whose effect outlives a text object: the graphics state a moved text object needs,
# and what its removal must leave in place for the content after it.
_STATE_OPS = {"cm", "w", "J", "j", "M", "d", "ri", "i", "gs", "CS", "cs", "SC", "SCN", "sc", "scn",
              "G", "g", "RG", "rg", "K", "k", "Tc", "Tw", "Tz", "TL", "Tf", "Tr", "Ts"}
_MARKED = {"BDC", "BMC", "EMC"}


def _scale(m) -> float:
    a, b, c, d = (float(v) for v in m[:4])
    return abs(a * d - b * c) ** 0.5


def _text_to_annotation(pdf: pikepdf.Pdf, rng: random.Random, src: Path) -> Damage:
    """Page 1's largest visible upright text object (outside an artifact) taken out of the page's content
    and drawn by a stamp annotation's appearance over the page instead, with the graphics state
    it was drawn in: the page draws the same, and no reader of the page's text sees those words,
    as with a title or stamp some tools leave in an annotation."""
    import numpy as np
    import pypdfium2 as pdfium

    page = pdf.pages[0]
    if int(_inherited(page, "/Rotate") or 0) % 360:
        raise Unsupported("page 1 is rotated")
    ops = list(pikepdf.parse_content_stream(page))
    names = [str(i.operator) for i in ops]
    state: list = []
    ctm, tr, size = 1.0, 0, 0.0
    stack: list = []
    marked: list[bool] = []          # per open marked-content sequence: is it an artifact
    best: tuple[float, int, int, list] | None = None   # (size, start, end, state before it)
    i = 0
    while i < len(ops):
        op = names[i]
        if op == "q":
            stack.append((list(state), ctm, tr, size))
        elif op == "Q" and stack:
            state, ctm, tr, size = stack.pop()
        elif op in ("BDC", "BMC"):
            marked.append(str(ops[i].operands[0]) == "/Artifact" or any(marked))
        elif op == "EMC" and marked:
            marked.pop()
        elif op == "BT" and "ET" in names[i:]:
            j = names.index("ET", i)
            before, shown, tm, upright = list(state), 0, 1.0, True
            for k in range(i + 1, j):
                o = names[k]
                if o == "Tf":
                    size = float(ops[k].operands[1])
                elif o == "Tr":
                    tr = int(ops[k].operands[0])
                elif o == "Tm":
                    tm = _scale(ops[k].operands)
                    upright = not (float(ops[k].operands[1]) or float(ops[k].operands[2]))
                elif o in SHOWS and tr not in (3, 7):
                    items = ops[k].operands[-1]
                    items = items if isinstance(items, Array) else [items]
                    shown += sum(len(bytes(s)) for s in items if isinstance(s, String))
                if o in _STATE_OPS:
                    state.append(ops[k])
            effective = size * tm * ctm
            if (shown >= 8 and upright and not any(marked)
                    and (best is None or effective > best[0])):
                best = (effective, i, j, before)
            i = j + 1
            continue
        if op in _STATE_OPS:
            state.append(ops[i])
            if op == "cm":
                ctm *= _scale(ops[i].operands)
            elif op == "Tf":
                size = float(ops[i].operands[1])
            elif op == "Tr":
                tr = int(ops[i].operands[0])
        i += 1
    if best is None:
        raise Unsupported("page 1 shows no visible upright text object of eight characters "
                          "or more")
    _, start, end, before = best
    block = [o for o in ops[start:end + 1] if str(o.operator) not in _MARKED]
    box = Array([float(v) for v in _inherited(page, "/MediaBox")])
    appearance = pdf.make_stream(
        pikepdf.unparse_content_stream([_ins("q"), *before, *block, _ins("Q")]),
        Type=Name.XObject, Subtype=Name.Form, BBox=box,
        Resources=Dictionary({k: v for k, v in _resources(page).items()}))
    annot = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Stamp, Rect=box, F=4,
                                         AP=Dictionary(N=appearance)))
    page.obj.Annots = Array([*page.obj.get("/Annots", []), annot])
    kept = [o for o in ops[start + 1:end] if str(o.operator) in _STATE_OPS]
    _write_contents(pdf, page, ops[:start] + kept + ops[end + 1:])
    # The region: what page 1 draws with its annotations and not without them.
    buf = BytesIO()
    pdf.save(buf, deterministic_id=True)
    doc = pdfium.PdfDocument(buf.getvalue())
    try:
        with_annots, without = (
            np.asarray(doc[0].render(scale=1, draw_annots=a).to_pil().convert("L"), dtype=int)
            for a in (True, False))
    finally:
        doc.close()
    ys, xs = np.nonzero(np.abs(with_annots - without) > 32)
    if not len(xs):
        raise Unsupported("page 1's largest text object draws nothing")
    return Damage(frozenset({1}), {1: (float(xs.min()), float(ys.min()), float(xs.max() + 1),
                                       float(ys.max() + 1))},
                  "the largest text object of page 1 drawn by a stamp annotation")


# ------------------------------------------------------------------- page geometry

def _rotate(pdf: pikepdf.Pdf, rng: random.Random, src: Path) -> Damage:
    """/Rotate turned a quarter turn on every page."""
    for page in pdf.pages:
        page.obj.Rotate = (int(_inherited(page, "/Rotate") or 0) + 90) % 360
    return Damage(frozenset(range(1, len(pdf.pages) + 1)))


BOXES = ("/MediaBox", "/CropBox", "/BleedBox", "/TrimBox", "/ArtBox")
OFFSET = (100, 60)


def _offset_crop(pdf: pikepdf.Pdf, rng: random.Random, src: Path) -> Damage:
    """Every page's boxes moved off the origin (MediaBox and CropBox at (100, 60)), and its
    content and annotations moved with them, so it renders the same in other coordinates."""
    dx, dy = OFFSET
    seen = set()
    for page in pdf.pages:
        for key in BOXES:
            box = _inherited(page, key) if key in ("/MediaBox", "/CropBox") else page.obj.get(key)
            if box is None:
                continue
            x0, y0, x1, y1 = (float(v) for v in box)
            page.obj[key] = Array([x0 + dx, y0 + dy, x1 + dx, y1 + dy])
        page.contents_add(pdf.make_stream(f"q 1 0 0 1 {dx} {dy} cm\n".encode()), prepend=True)
        page.contents_add(pdf.make_stream(b"\nQ\n"))
        for annot in page.obj.get("/Annots", []):
            if annot.objgen in seen and annot.objgen != (0, 0):
                continue
            seen.add(annot.objgen)
            if "/Rect" in annot:
                x0, y0, x1, y1 = (float(v) for v in annot.Rect)
                annot.Rect = Array([x0 + dx, y0 + dy, x1 + dx, y1 + dy])
            if "/QuadPoints" in annot:
                annot.QuadPoints = Array([float(v) + (dx if k % 2 == 0 else dy)
                                          for k, v in enumerate(annot.QuadPoints)])
    return Damage(frozenset(range(1, len(pdf.pages) + 1)))


# ------------------------------------------------------------------- injected text

PHRASE = "Quartz zebra mandolin seventeen"
INJECT_SIZE = 10


def _blank_spot(src: Path, page_index: int, width: float, height: float) -> tuple[float, float]:
    """The top-left corner (points, from the page's top-left) of a white rectangle of the clean
    rendered page, as far up the page as one exists."""
    import numpy as np
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(src))
    try:
        image = doc[page_index].render(scale=1, grayscale=True).to_numpy()
    finally:
        doc.close()
    ink = (image.reshape(image.shape[0], image.shape[1], -1)[..., 0] < 250).astype(np.int32)
    table = np.pad(ink, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    w, h, margin = int(width) + 8, int(height) + 8, 36
    for top in range(margin, ink.shape[0] - h - margin, 4):
        for left in range(margin, ink.shape[1] - w - margin, 8):
            total = (table[top + h, left + w] - table[top, left + w] - table[top + h, left]
                     + table[top, left])
            if total == 0:
                return left + 4, top + 4
    raise Unsupported("page 1 has no blank spot for the injected line")


def _inject(mode: str):
    def transform(pdf: pikepdf.Pdf, rng: random.Random, src: Path) -> Damage:
        from pdfminer.fontmetrics import FONT_METRICS

        page = pdf.pages[0]
        if int(_inherited(page, "/Rotate") or 0) % 360:
            raise Unsupported("page 1 is rotated")
        size = 0.5 if mode == "tiny" else INJECT_SIZE
        widths = FONT_METRICS["Helvetica"][1]
        width = sum(widths.get(c, 556) for c in PHRASE) * INJECT_SIZE / 1000
        left, top = _blank_spot(src, 0, width, INJECT_SIZE * 1.2)
        crop = _inherited(page, "/CropBox") or _inherited(page, "/MediaBox")
        x0, _, _, y1 = (float(v) for v in crop)
        x, baseline = x0 + left, y1 - top - INJECT_SIZE * 0.8
        res = Dictionary({k: v for k, v in _resources(page).items()})
        fonts = res.get("/Font")
        res.Font = Dictionary({**({k: v for k, v in fonts.items()} if fonts is not None else {}),
                               "/RbInject": pdf.make_indirect(Dictionary(
                                   Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica,
                                   Encoding=Name.WinAnsiEncoding))})
        page.obj.Resources = res
        paint = {"white": "1 g", "tiny": "0 g", "hidden": "3 Tr"}[mode]
        page.contents_add(pdf.make_stream(b"q\n"), prepend=True)
        page.contents_add(pdf.make_stream(
            f"\nQ\nq BT {paint} /RbInject {size} Tf {x:.2f} {baseline:.2f} Td ({PHRASE}) Tj ET Q\n"
            .encode()))
        return Damage(frozenset({1}), {1: (left, top, left + width, top + INJECT_SIZE * 1.2)},
                      f"{PHRASE!r} at ({left}, {top})")
    transform.__name__ = f"inject_{mode}"
    return transform


# ------------------------------------------------------------------- tags, language, forms

def _struct_elems(root) -> Iterable[Dictionary]:
    seen, stack = set(), [root]
    while stack:
        node = stack.pop()
        kids = node.get("/K")
        kids = kids if isinstance(kids, Array) else [kids] if kids is not None else []
        for kid in kids:
            if isinstance(kid, Dictionary) and "/S" in kid and kid.objgen not in seen:
                if kid.objgen != (0, 0):
                    seen.add(kid.objgen)
                yield kid
                stack.append(kid)


def _wrong_tags(pdf: pikepdf.Pdf, rng: random.Random, src: Path) -> Damage:
    """The tag tree's order reversed at every level, and its paragraphs and headings swapped
    (/P to /H1, /H1 to /H6 to /P)."""
    root = pdf.Root.get("/StructTreeRoot")
    if root is None:
        raise Unsupported("the document has no tag tree")
    nodes = [root, *_struct_elems(root)]
    for node in nodes:
        if isinstance(node.get("/K"), Array) and len(node.K) > 1:
            node.K = Array(list(reversed(list(node.K))))
        s = str(node.get("/S", ""))
        if s == "/P":
            node.S = Name.H1
        elif s in ("/H1", "/H2", "/H3", "/H4", "/H5", "/H6"):
            node.S = Name.P
    return Damage(frozenset(range(1, len(pdf.pages) + 1)))


def _all_artifact(pdf: pikepdf.Pdf, rng: random.Random, src: Path) -> Damage:
    """Every page's content an artifact: each marked-content tag turned into /Artifact, and the
    whole page wrapped in one. The tag tree stays, pointing at content that is now artifact."""
    for page in pdf.pages:
        out = [_ins("BMC", Name.Artifact)]
        for ins in pikepdf.parse_content_stream(page):
            if str(ins.operator) in ("BDC", "BMC"):
                out.append(_ins("BMC", Name.Artifact))
            else:
                out.append(ins)
        out.append(_ins("EMC"))
        _write_contents(pdf, page, out)
    pdf.Root.MarkInfo = Dictionary(Marked=True)
    return Damage(frozenset(range(1, len(pdf.pages) + 1)))


def _wrong_lang(pdf: pikepdf.Pdf, rng: random.Random, src: Path) -> Damage:
    """/Lang names a language the text is not in."""
    lang = str(pdf.Root.get("/Lang", ""))
    pdf.Root.Lang = String("de-DE" if lang.lower().startswith("ja") else "ja-JP")
    return Damage(frozenset(range(1, len(pdf.pages) + 1)))


def _external(argv: Callable[[Path, Path], list[str]], precondition=None) -> Transform:
    def transform(src: Path, dst: Path, rng: random.Random) -> Damage:
        if shutil.which(argv(src, dst)[0]) is None:
            raise Unsupported(f"{argv(src, dst)[0]} is not installed")
        if precondition is not None:
            with pikepdf.open(src) as pdf:
                precondition(pdf)
        done = subprocess.run(argv(src, dst), capture_output=True, text=True)
        # qpdf exits 3 when it succeeded with warnings.
        if done.returncode not in (0, 3) or not dst.exists():
            raise RuntimeError(f"{' '.join(argv(src, dst))}: {done.stderr.strip()[-500:]}")
        with pikepdf.open(src) as pdf:
            pages = len(pdf.pages)
        return Damage(frozenset(range(1, pages + 1)))
    return transform


def _has_tags(pdf: pikepdf.Pdf) -> None:
    if "/StructTreeRoot" not in pdf.Root:
        raise Unsupported("the document has no tag tree")


def _has_fields(pdf: pikepdf.Pdf) -> None:
    form = pdf.Root.get("/AcroForm")
    if form is None or not len(form.get("/Fields", [])):
        raise Unsupported("the document has no form fields")


def _has_text(pdf: pikepdf.Pdf) -> None:
    for page in pdf.pages:
        if any(str(i.operator) in SHOWS for i in pikepdf.parse_content_stream(page)):
            return
    raise Unsupported("no page shows text in its own content")


# ------------------------------------------------------------------- the registry

KINDS: list[DamageKind] = [
    DamageKind("tounicode-missing", "The /ToUnicode of page 1's main font removed",
               _pike(_damage_font(_missing_tounicode)), Pixels.SAME, same(),
               enforced=frozenset({"arxiv-p1-2", "i9"})),
    # The font repair (#309) decodes most of the font's codes and raises font-map. A letter
    # seen in one word only (fw9's capitals, i9's 'g', 'j', 'M', 'z', arxiv's fi ligature, most
    # of the short synthetic pages) gets too few votes to decide, so it stays as the layer reads
    # it and no base is the clean text yet: caught by font-map, not repaired.
    DamageKind("tounicode-shifted", "Page 1's main font gets a /ToUnicode that reads each "
               "letter as the next one (\"Gpsn\" for \"Form\")",
               _pike(_damage_font(_shifted_tounicode)), Pixels.SAME,
               flagged("font-map"), target=repaired("font-map"),
               enforced=frozenset({"prose", "tagged", "scan", "fw9", "arxiv-p1-2", "i9"})),
    # A born-digital page keeps its layer: each private-use reading counts as a unicode error
    # of its block (extract.text), and the font repair decodes the codes the witness votes on
    # (#342). A scanned page's layer has no letter left, so it is read again (ingest.ocr
    # unreadable-text): ocr-page catches it there.
    DamageKind("tounicode-pua", "Page 1's main font gets a /ToUnicode into the Private Use Area",
               _pike(_damage_font(_pua_tounicode)), Pixels.SAME,
               flagged("unicode-errors", "ocr-page"), target=repaired("unicode-errors"),
               enforced=ALL),
    DamageKind("dropped-spaces", "Word spaces written as TJ kerning, as pdfTeX writes them",
               _pike(_drop_spaces), Pixels.SAME, same(), enforced=ALL),
    DamageKind("broken-ligature", "fi and fl drawn as one ligature glyph that the text layer "
               "reads as a bare f", _pike(_broken_ligature), Pixels.CHANGES, flagged("dropped-ligature"),
               enforced=frozenset({"prose", "scan"}), target=repaired("dropped-ligature")),
    DamageKind("outlines", "Text drawn as outlines (gs -dNoOutputFonts), which drops the tags too",
               _external(lambda s, d: ["gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE",
                                       "-sDEVICE=pdfwrite", "-dNoOutputFonts", f"-sOutputFile={d}",
                                       str(s)], _has_text),
               Pixels.CHANGES, flagged("ocr-page"), target=repaired("ocr-page"), tool="gs",
               enforced=ALL),
    DamageKind("rasterized", "Every page replaced by a 150 dpi image of itself (gs pdfimage24)",
               _external(lambda s, d: ["gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE",
                                       "-sDEVICE=pdfimage24", "-r150", f"-sOutputFile={d}",
                                       str(s)]),
               Pixels.CHANGES, flagged("ocr-page"), target=repaired("ocr-page"), tool="gs",
               enforced=ALL),
    DamageKind("one-glyph-per-tj", "Every Tj and TJ split into one Tj per glyph",
               _pike(_one_glyph_per_tj), Pixels.SAME, same(), enforced=ALL),
    DamageKind("shuffled-objects", "The text objects (BT..ET) drawn in a shuffled order",
               _pike(_shuffle_text), Pixels.SAME, same(),
               enforced=frozenset({"prose", "tagged", "scan"})),
    DamageKind("rotated", "/Rotate 90 on every page", _pike(_rotate), Pixels.CHANGES, same(),
               enforced=ALL),
    DamageKind("offset-cropbox", "MediaBox and CropBox moved off the origin, content with them",
               _pike(_offset_crop), Pixels.SAME, same(),
               enforced=ALL),
    DamageKind("white-text", f"A line drawn in white on a blank spot of page 1 ({PHRASE!r})",
               _pike(_inject("white")), Pixels.SAME, same(), enforced=ALL),
    DamageKind("tiny-text", "The same line at 0.5 pt", _pike(_inject("tiny")), Pixels.CHANGES,
               same(), enforced=ALL),
    DamageKind("hidden-text", "The same line in render mode 3 (invisible)",
               _pike(_inject("hidden")), Pixels.SAME, same(), enforced=ALL),
    DamageKind("doubled-text", "Every text object drawn twice in place",
               _pike(_doubled_text), Pixels.CHANGES, same(), enforced=ALL),
    DamageKind("text-in-annotation", "Page 1's largest text object drawn by a stamp "
               "annotation's appearance instead of the page", _pike(_text_to_annotation),
               Pixels.SAME, flagged("words-missing"),
               enforced=frozenset({"prose", "tagged", "fw9", "arxiv-p1-2", "i9"})),
    DamageKind("tags-deleted", "The tag tree deleted (qpdf --remove-structure)",
               _external(lambda s, d: ["qpdf", "--remove-structure", str(s), str(d)], _has_tags),
               Pixels.SAME, same(), enforced=frozenset({"tagged", "fw9", "i9"}), tool="qpdf"),
    DamageKind("tags-wrong", "The tag tree's order reversed and its paragraphs and headings "
               "swapped", _pike(_wrong_tags), Pixels.SAME, same(),
               enforced=frozenset({"tagged", "fw9", "i9"})),
    DamageKind("all-artifact", "Every page's content marked as an artifact",
               _pike(_all_artifact), Pixels.SAME, same(), enforced=ALL),
    DamageKind("wrong-lang", "/Lang names a language the text is not in", _pike(_wrong_lang),
               Pixels.SAME, same(),
               enforced=frozenset({"prose", "tagged", "scan", "fw9", "arxiv-p1-2", "i9"})),
    DamageKind("forms-flattened", "Form fields flattened into the page (qpdf "
               "--generate-appearances --flatten-annotations=all)",
               _external(lambda s, d: ["qpdf", "--generate-appearances",
                                       "--flatten-annotations=all", str(s), str(d)], _has_fields),
               # --generate-appearances redraws the fields' appearances, so pixels change.
               # The field names and values are gone from the file, so `same` can't be met:
               # reflow says the form was flattened (ingest.flattened).
               Pixels.CHANGES, flagged("form-flattened"), enforced=frozenset({"fw9", "i9"}),
               tool="qpdf"),
]
