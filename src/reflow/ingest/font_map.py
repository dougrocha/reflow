"""Repair a broken font across the document by patching its /ToUnicode from OCR votes (#309,
tracker #303). This module is the only writer of its fact codes:

  font-map        a document warning: reflow decoded the codes of a font whose text layer read
                  as other letters, from what the text witness read on the page images. It lists
                  every decided code as before → after with its votes, and stays in the review
                  queue until a reviewer accepts it or rejects it (docs/review-decisions.md)
  font-ambiguous  a document warning: the witness reads a font's letters as others, and this
                  repair abstained (a font type it does not decode, an /ActualText over the
                  font, or a patch that failed its checks). Nothing is applied for that font

`repair` runs inside reflow.prepare right after the text-trust preflight (ingest.text_trust),
before replacement OCR and before any read, so every reader of the working copy (pdfium's
lines, pdfplumber's table cells, link words and tag text) sees one decode.

  1. Votes. Each page the preflight witnessed and disputed votes when it is judged gibberish,
     or judged doubt because a font's /ToUnicode reads its letters as other letters than its
     encoding names them (fontcensus `remapped`): then only that font's codes vote. A layer word
     and the witness word over it vote, position by position, when they are the same length;
     a length mismatch casts no vote. A code is the font's /ToUnicode inverted (one-to-one), and
     votes are keyed by the font dictionary and the code. Pages merge in page order.
  2. Decide (`decide`, pure). A code changes only when it has MIN_VOTES votes from MIN_WORDS
     distinct words (each identical word counted at most SAME_WORD_CAP times, so a running
     header can't dominate), AGREE of them for one reading, and both its reading and the new
     one are single letters: a digit or symbol never changes. A reading in the Private Use
     Area is no letter but says nothing either (a /ToUnicode into it maps the code to no
     text), so it changes to a letter like one. Undecided codes stay as they are.
  3. Abstain, with font-ambiguous and nothing applied for the font, when a font the votes would
     change is a Type0 (CID) font, a Type3 font, has no /ToUnicode or one that is not
     one-to-one, or has /ActualText over it on a page that uses it.
  4. Apply. The decided map goes into the font's /ToUnicode in a private sibling of the working
     copy, which replaces it only when every page that uses the font renders pixel-identical,
     and pdfium and pdfplumber read every decided code as decided and every other code as it
     was. A failed check keeps the unpatched copy and says why (font-ambiguous).

`Repair.changes` keeps, for each page, the characters the patch changed (box, before, after);
`mark_nodes` uses them to keep the text layer's reading of every node the map changed in
`attrs["layer_text"]`, which a reviewer's reject puts back (analyze.decisions_apply).
"""

from __future__ import annotations

import hashlib
import io
import os
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium

from reflow.fontcensus import _remapped, fonts, form_fonts
from reflow.ingest import witness
from reflow.ingest.text_trust import FACT_DISPUTED, CensusChar, TrustReport, census
from reflow.model import BBox, Document, Fact, Node, PageKind, Role, Source
from reflow.pdfobjects import private_use

__all__ = ["CODES", "POLICY", "Repair", "decide", "repair", "mark_nodes", "LAYER_TEXT"]

FACT_MAP = "font-map"
FACT_AMBIGUOUS = "font-ambiguous"
CODES = (FACT_MAP, FACT_AMBIGUOUS)
# The version of the vote rules below. It joins the text witness's identity in the model key
# (reflow.saved.identify), so a change to it makes a saved model stale.
# 2: a private-use reading is decided like an unknown letter (#342).
POLICY = 2
LAYER_TEXT = "layer_text"   # node.attrs: the text layer's reading of a node the map changed

MIN_VOTES = 3        # votes a code needs, after the cap
MIN_WORDS = 2        # distinct words they come from
SAME_WORD_CAP = 2    # votes one identical word casts for one code
AGREE = 0.8          # share of a code's votes for one reading
RENDER_SCALE = 1.0   # the pixel check renders at 72 dpi
LISTED = 60          # decided codes listed in the font-map fact before a count
NEAR = 36.0          # points from a node's box within which its text is looked up (mark_nodes)
# Nodes whose text is not the page's: a row's label, a table's or a list's summary, a figure's alt
NOT_PAGE_TEXT = frozenset({Role.DOC, Role.SECTION, Role.TABLE, Role.TR, Role.LIST, Role.FIGURE,
                           Role.FORM})


# ------------------------------------------------------------------------------- the fonts

@dataclass
class Font:
    """One font dictionary of the working copy, keyed by its object number."""
    key: tuple[int, int]
    resource: str              # a resource name it is drawn under ("/T1_0")
    base: str                  # /BaseFont, subset prefix kept
    umap: dict[int, str]       # code -> its /ToUnicode reading (empty when it has none)
    unsupported: str = ""      # why this repair can't decode it ("" when it can)
    remapped: bool = False     # its /ToUnicode reads letters as others than its encoding
    cid: bool = False          # a Type0 font: pdfium names it with its subset prefix
    pages: set[int] = field(default_factory=set)

    @property
    def inverse(self) -> dict[str, int]:
        """Reading -> code, for the readings exactly one code gives."""
        count = Counter(self.umap.values())
        return {t: c for c, t in self.umap.items() if count[t] == 1}

    @property
    def shared(self) -> frozenset[str]:
        """The readings two codes or more give: the map is not one-to-one there."""
        count = Counter(self.umap.values())
        return frozenset(t for t, n in count.items() if n > 1)

    @property
    def label(self) -> str:
        return f"{self.resource.lstrip('/')} ({self.base})"


def _parse_tounicode(stream: pikepdf.Stream) -> dict[int, str]:
    from pdfminer.cmapdb import CMapParser, FileUnicodeMap

    cmap = FileUnicodeMap()
    CMapParser(cmap, io.BytesIO(stream.read_bytes())).run()
    return {int(k): v for k, v in cmap.cid2unichr.items()}


def _font(key: tuple[int, int], resource: str, font: pikepdf.Dictionary) -> Font:
    subtype = str(font.get("/Subtype", ""))
    base = str(font.get("/BaseFont", "/")).lstrip("/")
    umap: dict[int, str] = {}
    why = ""
    if subtype == "/Type0":
        why = "a Type0 (CID) font, whose codes can be more than one byte"
    elif subtype == "/Type3":
        why = "a Type3 font"
    elif not isinstance(font.get("/ToUnicode"), pikepdf.Stream):
        why = "a font with no /ToUnicode"
    else:
        try:
            umap = _parse_tounicode(font.ToUnicode)
        except Exception as error:  # noqa: BLE001 - an unreadable map is one more reason to abstain
            why = f"a /ToUnicode that does not parse ({type(error).__name__})"
        else:
            if any(c > 0xFF for c in umap):
                why = "a /ToUnicode with codes of more than one byte"
    return Font(key, resource, base, umap, why, _remapped(font), cid=subtype == "/Type0")


def font_table(pdf: pikepdf.Pdf) -> tuple[dict[tuple[int, int], Font], dict[int, dict[str, list[tuple[int, int]]]]]:
    """Every indirect font dictionary the pages draw with (their forms' too), and, per page, the
    name pdfium reports for a font -> the fonts that carry it. pdfium names a simple font
    without its subset prefix and a Type0 font with it. A direct font dictionary has no key, so
    no vote can reach it."""
    table: dict[tuple[int, int], Font] = {}
    by_page: dict[int, dict[str, list[tuple[int, int]]]] = {}
    for number, page in enumerate(pdf.pages, start=1):
        names: dict[str, list[tuple[int, int]]] = {}
        resources = page.obj.get("/Resources")
        for resource, font in (*fonts(resources), *form_fonts(resources)):
            if not font.is_indirect:
                continue
            key = font.objgen
            if key not in table:
                table[key] = _font(key, str(resource), font)
            table[key].pages.add(number)
            name = _pdfium_name(table[key])
            if key not in names.setdefault(name, []):
                names[name].append(key)
        by_page[number] = names
    return table, by_page


# ------------------------------------------------------------------------------- the votes

Vote = tuple[tuple[int, int], int | str]   # (font, code); a str "code" is a reading (unsupported font)


@dataclass(frozen=True)
class Decided:
    font: tuple[int, int]
    code: int
    before: str
    after: str
    votes: int        # for `after`, after the cap
    total: int        # every vote the code got, after the cap


@dataclass
class Ballots:
    """For each (font, code): reading -> word text -> votes it cast. Merged in page order."""
    votes: dict[Vote, dict[str, Counter[str]]] = field(default_factory=dict)

    def cast(self, key: Vote, reading: str, word: str) -> None:
        self.votes.setdefault(key, {}).setdefault(reading, Counter())[word] += 1


def _words(chars: Sequence[CensusChar]) -> list[list[CensusChar]]:
    """The page's words, split as text_trust.layer_lines splits them: at spaces, line breaks and
    where the next character starts on another line."""
    out: list[list[CensusChar]] = []
    word: list[CensusChar] = []
    for c in chars:
        if c.ch.isspace():
            if word:
                out.append(word)
            word = []
            continue
        if word:
            last = word[-1]
            height = max(1.0, last.box[3] - last.box[1])
            if abs(c.box[1] - last.box[1]) > 0.6 * height and abs(c.box[3] - last.box[3]) > 0.6 * height:
                out.append(word)
                word = []
        word.append(c)
    if word:
        out.append(word)
    return out


def _box(word: Sequence[CensusChar]) -> BBox:
    return (min(c.box[0] for c in word), min(c.box[1] for c in word),
            max(c.box[2] for c in word), max(c.box[3] for c in word))


def _holds(outer: BBox, inner: BBox) -> bool:
    """The centre of `inner` is in `outer`, widened by half its height."""
    cx, cy = (inner[0] + inner[2]) / 2, (inner[1] + inner[3]) / 2
    pad = (outer[3] - outer[1]) / 2
    return outer[0] - pad <= cx <= outer[2] + pad and outer[1] - pad <= cy <= outer[3] + pad


def pairs(chars: Sequence[CensusChar], words: Sequence[witness.WitnessWord]
          ) -> list[tuple[list[CensusChar], str]]:
    """Each layer word with the one witness word over it, when each is the other's only match."""
    layer = [(w, _box(w)) for w in _words(chars)]
    held: dict[int, list[int]] = defaultdict(list)
    held_by: dict[int, list[int]] = defaultdict(list)
    for j, ww in enumerate(words):
        for i, (_, box) in enumerate(layer):
            if _holds(box, ww.box):
                held[i].append(j)
                held_by[j].append(i)
    return [(layer[i][0], words[js[0]].text) for i, js in sorted(held.items())
            if len(js) == 1 and len(held_by[js[0]]) == 1]


def cast(ballots: Ballots, chars: Sequence[CensusChar], words: Sequence[witness.WitnessWord],
         voter, eligible) -> None:
    """The page's votes: `voter(char)` is the char's (font key, code-or-reading) or None, and
    `eligible(font key)` whether this page may vote for that font."""
    for layer, seen in pairs(chars, words):
        text = "".join(c.ch for c in layer)
        if len(text) != len(seen) or text == seen:
            if text == seen:   # agreement votes too: it confirms the codes as they read
                for c in layer:
                    if (key := voter(c)) is not None and eligible(key[0]):
                        ballots.cast(key, c.ch, text)
            continue
        for c, got in zip(layer, seen, strict=True):
            if (key := voter(c)) is not None and eligible(key[0]):
                ballots.cast(key, got, text)


def decide(ballots: Ballots, reading: dict[Vote, str]) -> tuple[list[Decided], list[Vote]]:
    """(decided changes, codes undecided but voted against) from the ballots. Pure. `reading`
    is each voted code's current text."""
    decided: list[Decided] = []
    doubtful: list[Vote] = []
    for key in sorted(ballots.votes, key=lambda k: (k[0], str(k[1]))):
        tally = {r: sum(min(n, SAME_WORD_CAP) for n in words.values())
                 for r, words in ballots.votes[key].items()}
        total = sum(tally.values())
        top, n = max(sorted(tally.items()), key=lambda kv: kv[1])
        before = reading.get(key, "")
        if top == before:
            continue
        distinct = len(ballots.votes[key][top])
        runner = max((v for r, v in tally.items() if r != top), default=0)
        ok = (n >= MIN_VOTES and distinct >= MIN_WORDS and n >= AGREE * total and runner < n
              and len(top) == 1 and len(before) == 1 and top.isalpha()
              and (before.isalpha() or private_use(before)))
        if ok:
            decided.append(Decided(key[0], key[1] if isinstance(key[1], int) else -1, before,
                                   top, n, total))
        else:
            doubtful.append(key)
    return decided, doubtful


# ------------------------------------------------------------------------------- the repair

@dataclass
class Repair:
    """What the repair did: the decided codes it applied, per font, and the characters of each
    page it changed (box, before, after; every visible one, changed or not); the fonts it
    abstained on, with why."""
    map_id: str = ""
    applied: dict[tuple[int, int], list[Decided]] = field(default_factory=dict)
    labels: dict[tuple[int, int], str] = field(default_factory=dict)
    changes: dict[int, list[tuple[BBox, str, str]]] = field(default_factory=dict)
    abstained: dict[str, str] = field(default_factory=dict)   # font label -> why
    undecided: list[str] = field(default_factory=list)        # voted against, too few votes
    voted: list[int] = field(default_factory=list)            # the pages whose votes counted

    @property
    def codes(self) -> int:
        return sum(len(v) for v in self.applied.values())


def _vote_pages(doc: Document, report: TrustReport) -> list[int]:
    disputed = {p.number for p in doc.pages if any(f.code == FACT_DISPUTED for f in p.facts)}
    return [n for n in sorted(report.witnessed) if n in disputed
            and report.judged.get(n) is not None and report.judged[n].suspect]


def _has_actual_text(pdf: pikepdf.Pdf, pages: Iterable[int]) -> list[int]:
    """The pages whose content, or a form they draw, marks text with /ActualText."""
    found = []
    for n in sorted(pages):
        page = pdf.pages[n - 1]
        try:
            data = b"".join(s.read_bytes() for s in _contents(page))
            if b"/ActualText" in data or _actual_in(page.obj.get("/Resources"), set()):
                found.append(n)
        except Exception:  # noqa: BLE001 - an unreadable stream can't be checked: abstain
            found.append(n)
    return found


def _actual_in(resources, seen: set) -> bool:
    """/ActualText in `resources`' /Properties, or in a form they draw (each form once)."""
    if not isinstance(resources, pikepdf.Dictionary):
        return False
    props = resources.get("/Properties")
    if isinstance(props, pikepdf.Dictionary) and any(
            isinstance(v, pikepdf.Dictionary) and "/ActualText" in v for _, v in props.items()):
        return True
    xobjects = resources.get("/XObject")
    if not isinstance(xobjects, pikepdf.Dictionary):
        return False
    for _, xobject in xobjects.items():
        if not isinstance(xobject, pikepdf.Stream) or xobject.get("/Subtype") != pikepdf.Name.Form:
            continue
        key = xobject.objgen if xobject.is_indirect else (id(xobject), -1)
        if key in seen:
            continue
        seen.add(key)
        if b"/ActualText" in xobject.read_bytes() or _actual_in(xobject.get("/Resources"), seen):
            return True
    return False


def _contents(page: pikepdf.Page) -> list[pikepdf.Stream]:
    contents = page.obj.get("/Contents")
    if isinstance(contents, pikepdf.Stream):
        return [contents]
    if isinstance(contents, pikepdf.Array):
        return [s for s in contents if isinstance(s, pikepdf.Stream)]
    return []


def _pdfium_name(font: Font) -> str:
    return font.base if font.cid else font.base.rpartition("+")[2]


def _cmap(mapping: dict[int, str]) -> bytes:
    lines = ["/CIDInit /ProcSet findresource begin", "12 dict begin", "begincmap",
             "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
             "/CMapName /Adobe-Identity-UCS def", "/CMapType 2 def",
             "1 begincodespacerange", "<00> <FF>", "endcodespacerange"]
    items = [(c, t) for c, t in sorted(mapping.items()) if t]
    for i in range(0, len(items), 100):
        chunk = items[i:i + 100]
        lines.append(f"{len(chunk)} beginbfchar")
        lines += [f"<{c:02X}> <{t.encode('utf-16-be').hex().upper()}>" for c, t in chunk]
        lines.append("endbfchar")
    lines += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    return "\n".join(lines).encode()


def _read_chars(path: Path, pages: Iterable[int], mixed: set[int]) -> dict[int, list[CensusChar]]:
    pdf = pdfium.PdfDocument(path)
    try:
        out = {}
        for n in pages:
            page = pdf[n - 1]
            try:
                out[n] = census(page, invisible=n in mixed)
            finally:
                page.close()
        return out
    finally:
        pdf.close()


def _plumber_chars(path: Path, pages: Iterable[int]) -> dict[int, list[tuple[str, str]]]:
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        return {n: [(c["fontname"], c["text"]) for c in pdf.pages[n - 1].chars] for n in pages}


def _renders(path: Path, pages: Iterable[int]) -> dict[int, bytes]:
    pdf = pdfium.PdfDocument(path)
    try:
        out = {}
        for n in pages:
            page = pdf[n - 1]
            try:
                bitmap = page.render(scale=RENDER_SCALE)
                try:
                    out[n] = hashlib.sha256(bitmap.to_pil().tobytes()).hexdigest()
                finally:
                    bitmap.close()
            finally:
                page.close()
        return out
    finally:
        pdf.close()


def _checked(before: dict[int, list], after: dict[int, list], swap: dict[str, dict[str, str]],
             font_of, exact: bool) -> str:
    """Why `after` is not `before` with exactly the decided swaps, or "" when it is. `swap` is
    font -> before -> after; `font_of(char)` names the char's font in the same namespace.
    `exact`: every char that reads a decided code's old text must read its new text. pdfplumber
    keeps a code's whole reading in one char, so that holds there; pdfium splits a ligature's
    reading ("fi") into chars, so there a char may also stay as it was."""
    for n in sorted(before):
        a, b = before[n], after[n]
        if len(a) != len(b):
            return f"page {n} reads {len(b)} characters instead of {len(a)}"
        for x, y in zip(a, b, strict=True):
            want = swap.get(font_of(x), {}).get(_ch(x), _ch(x))
            if _ch(y) != want and (exact or _ch(y) != _ch(x)):
                return f"page {n} reads {_ch(y)!r} where {want!r} was decided"
    return ""


def _ch(c) -> str:
    return c.ch if isinstance(c, CensusChar) else c[1]


def repair(doc: Document, working: Path, report: TrustReport, *, lock) -> Repair:
    """Learn the code → character map of each font the witness reads otherwise, and patch it into
    the working copy (module docstring). Writes font-map and font-ambiguous onto the document's
    root; changes nothing when no page votes. Holds `lock` (the pdfium lock) throughout."""
    out = Repair()
    pages = _vote_pages(doc, report)
    if not pages:
        return out
    mixed = {p.number for p in doc.pages if p.kind is PageKind.MIXED}
    with lock:
        with pikepdf.open(working) as pk:
            table, names = font_table(pk)
        chars = _read_chars(working, pages, mixed)

        ballots = Ballots()
        readings: dict[Vote, str] = {}
        for n in pages:
            gibberish = report.judged[n].verdict == "gibberish"

            def voter(c: CensusChar, n=n) -> Vote | None:
                keys = names[n].get(c.font, [])
                if len(keys) != 1:
                    return None
                font = table[keys[0]]
                if font.unsupported or c.ch in font.shared:
                    key: Vote = (font.key, c.ch)
                else:
                    code = font.inverse.get(c.ch)
                    if code is None:
                        return None
                    key = (font.key, code)
                readings[key] = c.ch
                return key

            cast(ballots, chars[n], report.witnessed[n], voter,
                 lambda k, g=gibberish: g or table[k].remapped)
            out.voted.append(n)
        decided, doubtful = decide(ballots, readings)
        if not decided:
            return out

        by_font: dict[tuple[int, int], list[Decided]] = defaultdict(list)
        for d in decided:
            by_font[d.font].append(d)
        with pikepdf.open(working) as pk:
            actual = {k: _has_actual_text(pk, table[k].pages) for k in by_font}
        for key in list(by_font):
            font = table[key]
            shared = sorted(t for t in font.shared if t.isalpha())
            if font.unsupported:
                out.abstained[font.label] = f"it is {font.unsupported}"
            elif shared:
                out.abstained[font.label] = ("its /ToUnicode is not one-to-one: two codes or more "
                                             f"read ‘{shared[0]}’")
            elif actual[key]:
                out.abstained[font.label] = ("an /ActualText on page "
                                             f"{', '.join(map(str, actual[key]))} overrides its text")
            else:
                continue
            del by_font[key]
        if by_font:
            why = _apply(working, table, by_font, mixed, out)
            if why:
                for key in by_font:
                    out.abstained[table[key].label] = f"the patched font failed a check: {why}"
                out.applied.clear()
                out.changes.clear()
            out.undecided = [_undecided(table[k[0]].label, readings[k], ballots.votes[k])
                             for k in doubtful if k[0] in out.applied]
    _record(doc, out, table)
    return out


def _apply(working: Path, table: dict[tuple[int, int], Font],
           by_font: dict[tuple[int, int], list[Decided]], mixed: set[int], out: Repair) -> str:
    """Patch `by_font` into a sibling of `working` and, when it passes the checks, replace
    `working` with it. Returns why it did not, or ""."""
    sibling = working.with_name(working.stem + ".font-map" + working.suffix)
    affected = sorted({n for key in by_font for n in table[key].pages})
    with pikepdf.open(working) as pk:
        for key, ds in by_font.items():
            font = pk.get_object(key)
            mapping = dict(table[key].umap)
            for d in ds:
                mapping[d.code] = d.after
            font.ToUnicode = pk.make_stream(_cmap(mapping))
        pk.save(sibling, linearize=True, deterministic_id=True)
    try:
        swap_pdfium: dict[str, dict[str, str]] = defaultdict(dict)
        swap_plumber: dict[str, dict[str, str]] = defaultdict(dict)
        for key, ds in by_font.items():
            for d in ds:
                swap_plumber[table[key].base][d.before] = d.after
        before = _read_chars(working, affected, mixed)
        after = _read_chars(sibling, affected, mixed)
        # pdfium names a font by its base name, the subset prefix sometimes dropped: a decided
        # font must be the only one under that name on each affected page, or the check can't
        # tell its characters from another font's.
        for key, ds in by_font.items():
            for d in ds:
                swap_pdfium[_pdfium_name(table[key])][d.before] = d.after
        clash = [table[k].label for k, f in table.items() if k not in by_font
                 and _pdfium_name(f) in swap_pdfium and f.pages & set(affected)]
        if clash:
            return f"another font on the same pages has the same name ({clash[0]})"
        if why := _checked(before, after, swap_pdfium, lambda c: c.font, exact=False):
            return f"pdfium: {why}"
        plumbed = _plumber_chars(working, affected), _plumber_chars(sibling, affected)
        if why := _checked(*plumbed, swap_plumber, lambda c: c[0], exact=True):
            return f"pdfplumber: {why}"
        for n in affected:   # pdfium must read the patch wherever pdfplumber does
            if (plumbed[0][n] != plumbed[1][n]) != any(
                    x.ch != y.ch for x, y in zip(before[n], after[n], strict=True)):
                return f"pdfium and pdfplumber disagree on whether page {n} changed"
        if _renders(working, affected) != _renders(sibling, affected):
            return "a page renders differently"
        os.replace(sibling, working)
    finally:
        sibling.unlink(missing_ok=True)
    out.applied = {k: sorted(v, key=lambda d: d.code) for k, v in by_font.items()}
    out.labels = {k: table[k].label for k in by_font}
    for n in affected:   # every character of a page the patch changed, to align nodes with
        if any(a.ch != b.ch for a, b in zip(before[n], after[n], strict=True)):
            out.changes[n] = [(a.box, a.ch, b.ch) for a, b in zip(before[n], after[n], strict=True)
                              if not a.ch.isspace()]
    digest = hashlib.sha256(repr(sorted((table[k].base, d.code, d.before, d.after)
                                        for k, ds in out.applied.items() for d in ds)).encode())
    out.map_id = "fm-" + digest.hexdigest()[:16]
    return ""


def _undecided(label: str, reading: str, votes: dict[str, Counter[str]]) -> str:
    tally = sorted(((sum(min(n, SAME_WORD_CAP) for n in w.values()), r) for r, w in votes.items()),
                   reverse=True)
    said = ", ".join(f"{n} for ‘{r}’" for n, r in tally[:3])
    return f"{label} ‘{reading}’ ({said})"


def code_line(label: str, d: Decided) -> str:
    return f"{label} <{d.code:02X}> ‘{d.before}’ → ‘{d.after}’ ({d.votes} of {d.total} votes)"


def _record(doc: Document, out: Repair, table: dict[tuple[int, int], Font]) -> None:
    for label, why in sorted(out.abstained.items()):
        doc.root.facts.append(Fact(
            FACT_AMBIGUOUS, Source.OCR,
            f"OCR of the page images reads the letters of font {label} as other letters, and "
            f"reflow did not decode it: {why}. The text is kept as the PDF gives it.",
            {"font": label, "reason": why}))
    if not out.applied:
        return
    lines = [code_line(out.labels[k], d) for k, ds in sorted(out.applied.items()) for d in ds]
    shown = ", ".join(f"‘{d.before}’ → ‘{d.after}’" for ds in out.applied.values() for d in ds[:8])
    n = len(out.undecided)
    left = (f" {n} other code{'s' if n != 1 else ''} the OCR reads otherwise had too few votes "
            "and still read as the layer gives them." if n else "")
    doc.root.facts.append(Fact(
        FACT_MAP, Source.OCR,
        f"reflow decoded {out.codes} character code{'s' if out.codes != 1 else ''} of "
        f"{len(out.applied)} font{'s' if len(out.applied) != 1 else ''} whose text layer read "
        f"as other letters, from OCR of the page images ({shown}).{left} Accept the decoded "
        "text, or reject it to publish the text layer's reading.",
        {"map": out.map_id, "fonts": sorted(out.labels.values()), "codes": lines[:LISTED],
         "count": out.codes, "pages": sorted(out.changes), "undecided": out.undecided[:LISTED]}))


# ------------------------------------------------------------------------------- the nodes

def _original(text: str, chars: Sequence[tuple[BBox, str, str]]) -> str:
    """`text` with each character the patch changed put back as the layer read it, where the
    node's text aligns with the patched reading of the characters inside its box (in pdfium's
    order). Same length."""
    after = "".join(b for _, _, b in chars)
    before = "".join(a for _, a, _ in chars)
    out = list(text)
    for i, j, n in SequenceMatcher(None, text, after, autojunk=False).get_matching_blocks():
        for k in range(n):
            out[i + k] = before[j + k]
    return "".join(out)


def _found(text: str, chars: Sequence[tuple[BBox, str, str]], after: str, near: BBox | None
           ) -> str | None:
    """`text` as the layer read it, when its characters (spaces aside) occur in the page's
    patched reading `after` (of `chars`): the reading every occurrence shares, else the one the
    occurrences within NEAR points of the node's box `near` share. None when it does not occur,
    or when the occurrences that count were read differently: a reading is never guessed."""
    compact = "".join(ch for ch in text if not ch.isspace())
    if not compact:
        return None
    starts, at = [], after.find(compact)
    while at >= 0:
        starts.append(at)
        at = after.find(compact, at + 1)

    def reading(i: int) -> str:
        return "".join(c[1] for c in chars[i:i + len(compact)])

    readings = {reading(i) for i in starts}
    if len(readings) > 1 and near is not None:
        x0, top, x1, bottom = near

        def close(i: int) -> bool:
            box = chars[i][0]
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            return x0 - NEAR <= cx <= x1 + NEAR and top - NEAR <= cy <= bottom + NEAR
        readings = {reading(i) for i in starts if close(i)}
    if len(readings) != 1:
        return None
    back = iter(readings.pop())
    return "".join(ch if ch.isspace() else next(back) for ch in text)


def _with_outer(node: Node, outer: BBox | None):
    """Each node of the tree with the box of its nearest ancestor that has one."""
    yield node, outer
    for child in node.children:
        yield from _with_outer(child, node.bbox or outer)


def _union(boxes: Sequence[BBox]) -> BBox | None:
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def mark_nodes(doc: Document, changes: dict[int, list[tuple[BBox, str, str]]]) -> int:
    """Keep the text layer's reading in `attrs["layer_text"]` of every text node the map changed.
    A node whose text occurs in its page's patched reading gets that reading back exactly (a
    form field's printed label sits outside the field's box; see `_found`, for which a node with
    no box, a table cell, takes its nearest ancestor's). Any other node's text is aligned with
    the patched reading of the characters inside its own box (or its contents'), and the
    changed ones are put back. Returns how many."""
    pages = {n: "".join(b for _, _, b in cs) for n, cs in changes.items()}
    marked = 0
    for node, outer in _with_outer(doc.root, None):
        if not node.text or node.page not in changes or node.role in NOT_PAGE_TEXT:
            continue
        box = node.bbox or _union([n.bbox for n in node.walk() if n.bbox is not None])
        was = _found(node.text, changes[node.page], pages[node.page], box or outer)
        if was is None and box is not None:
            x0, top, x1, bottom = box
            inside = [c for c in changes[node.page]
                      if x0 - 1 <= (c[0][0] + c[0][2]) / 2 <= x1 + 1
                      and top - 1 <= (c[0][1] + c[0][3]) / 2 <= bottom + 1]
            if any(a != b for _, a, b in inside):
                was = _original(node.text, inside)
        if was is not None and was != node.text:
            node.attrs[LAYER_TEXT] = was
            marked += 1
    return marked
