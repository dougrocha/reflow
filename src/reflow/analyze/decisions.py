"""The decision store: a reviewer's answers to the review queue (docs/review-decisions.md).

The store is a JSONL file the converter only reads. `read` takes one bounded snapshot of it and
parses every line once, at the boundary, into the frozen records below; `fold` picks the active
decisions from them. Nothing past `parse_record` touches a raw dict.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from reflow.analyze.anchors import Anchor

VERSION = 1
MAX_LINE = 8 * 1024           # bytes in a store line, newline excluded
NO_HEADER = "no-header"

# The limits on a correction's `now` (invalid_now).
NOW_MAX_CHARS = 200
NOW_MAX_GROWTH = 4            # times the length of `was`
BIDI_CHARS = frozenset("؜‎‏‪‫‬‭‮"
                       "⁦⁧⁨⁩")

_ID = re.compile(r"^[0-9a-z]{1,13}-[0-9a-f]{8}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_FONT_MAP = re.compile(r"^fm-[0-9a-f]{16}$")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")


class Kind(StrEnum):
    CORRECT = "correct"
    KEEP = "keep"
    ALT = "alt"
    DISMISS = "dismiss"
    UNDO = "undo"
    REATTACH = "reattach"
    PROPOSAL = "proposal"
    FONT_MAP = "font-map"


class Verdict(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"



# -- records -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Header:
    id: str
    slug: str
    created: str


@dataclass(frozen=True)
class Range:
    """OCR's reading and up to 20 characters of the node's text on each side. No offsets."""
    was: str
    before: str
    after: str
    rule: str


@dataclass(frozen=True, kw_only=True)
class Base:
    id: str
    at: str
    pdf: str


@dataclass(frozen=True, kw_only=True)
class Correct(Base):
    kind: ClassVar[Kind] = Kind.CORRECT
    anchor: Anchor
    range: Range
    now: str
    supersedes: str | None = None


@dataclass(frozen=True, kw_only=True)
class Keep(Base):
    kind: ClassVar[Kind] = Kind.KEEP
    anchor: Anchor
    range: Range
    supersedes: str | None = None


@dataclass(frozen=True, kw_only=True)
class AltText(Base):
    kind: ClassVar[Kind] = Kind.ALT
    anchor: Anchor
    text: str
    decorative: bool
    supersedes: str | None = None


@dataclass(frozen=True, kw_only=True)
class Dismiss(Base):
    kind: ClassVar[Kind] = Kind.DISMISS
    findings: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class Undo(Base):
    kind: ClassVar[Kind] = Kind.UNDO
    of: str


@dataclass(frozen=True, kw_only=True)
class Reattach(Base):
    kind: ClassVar[Kind] = Kind.REATTACH
    from_: str
    to: str


@dataclass(frozen=True, kw_only=True)
class ProposalVerdict(Base):
    kind: ClassVar[Kind] = Kind.PROPOSAL
    proposal: str
    verdict: Verdict
    text: str


@dataclass(frozen=True, kw_only=True)
class FontMapVerdict(Base):
    """A reviewer's answer to a font-map item (ingest.font_map): accept keeps the decoded text,
    reject puts the text layer's reading back on every node the map changed."""
    kind: ClassVar[Kind] = Kind.FONT_MAP
    map: str
    verdict: Verdict


Record = Correct | Keep | AltText | Dismiss | Undo | Reattach | ProposalVerdict | FontMapVerdict
TextDecision = Correct | Keep
Stackable = Correct | Keep | AltText | Dismiss | ProposalVerdict | FontMapVerdict


@dataclass(frozen=True)
class Unsupported:
    """A record with a version or a kind this reader does not know: kept, listed, never applied."""
    id: str | None
    v: object
    kind: object


@dataclass(frozen=True)
class Invalid:
    """A line that is a JSON object but not a valid record of its (known) kind."""
    id: str | None
    reason: str


Parsed = Record | Unsupported | Invalid


class _Bad(Exception):
    pass


def _str(d: Mapping[str, Any], key: str, *, empty: bool = True) -> str:
    v = d.get(key)
    if not isinstance(v, str) or (not empty and not v):
        raise _Bad(f"{key} must be a{'' if empty else ' non-empty'} string")
    return v


def _hex(d: Mapping[str, Any], key: str, pattern: re.Pattern[str], width: int) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not pattern.match(v):
        raise _Bad(f"{key} must be {width} lower-case hex digits")
    return v


def _obj(d: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    v = d.get(key)
    if not isinstance(v, dict):
        raise _Bad(f"{key} must be an object")
    return v


def _number(v: object) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise _Bad("not a finite number")
    return float(v)


def _anchor(d: Mapping[str, Any], *, text: bool) -> Anchor:
    a = _obj(d, "anchor")
    page = a.get("page")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise _Bad("anchor.page must be an integer from 1")
    box = a.get("bbox")
    if not isinstance(box, list) or len(box) != 4:
        raise _Bad("anchor.bbox must hold four numbers")
    sha = _hex(a, "text_sha" if text else "crop_sha", _HEX16, 16)
    other = a.get("crop_sha" if text else "text_sha", "")
    if not isinstance(other, str) or other and not _HEX16.match(other):
        raise _Bad("anchor sha must be 16 hex digits or empty")
    text_sha, crop_sha = (sha, other) if text else (other, sha)
    return Anchor(page, (_number(box[0]), _number(box[1]), _number(box[2]), _number(box[3])),
                  text_sha, crop_sha, _str(a, "hint"))


def _range(d: Mapping[str, Any]) -> Range:
    r = _obj(d, "range")
    return Range(_str(r, "was", empty=False), _str(r, "before"), _str(r, "after"),
                 _str(r, "rule"))


def _supersedes(d: Mapping[str, Any]) -> str | None:
    if "supersedes" not in d:
        return None
    return _str(d, "supersedes", empty=False)


def _hex_id(d: Mapping[str, Any], key: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not _ID.match(v):
        raise _Bad(f"{key} must be a record id")
    return v


def parse_header(obj: Mapping[str, Any]) -> Header | None:
    """The store header, or None when the object is not a valid version-1 header."""
    if obj.get("v") != VERSION or isinstance(obj.get("v"), bool) or obj.get("kind") != "store":
        return None
    try:
        return Header(_hex(obj, "id", _HEX16, 16), _str(obj, "slug"), _str(obj, "created"))
    except _Bad:
        return None


def parse_record(obj: Mapping[str, Any]) -> Parsed:
    """One JSON object of the store as a typed record. An unknown `v` or `kind` is Unsupported,
    a known kind with a wrong field is Invalid. The only place that reads a raw record."""
    ident = obj.get("id") if isinstance(obj.get("id"), str) else None
    v, kind = obj.get("v"), obj.get("kind")
    if (isinstance(v, bool) or v != VERSION or not isinstance(kind, str)
            or kind not in {k.value for k in Kind}):
        return Unsupported(ident, v, kind)
    try:
        base = {"id": _hex_id(obj, "id"), "at": _str(obj, "at"), "pdf": _hex(obj, "pdf", _HEX64, 64)}
        match Kind(kind):
            case Kind.CORRECT:
                t = _obj(obj, "target")
                return Correct(**base, anchor=_anchor(t, text=True), range=_range(t),
                               now=_str(obj, "now"), supersedes=_supersedes(obj))
            case Kind.KEEP:
                t = _obj(obj, "target")
                return Keep(**base, anchor=_anchor(t, text=True), range=_range(t),
                            supersedes=_supersedes(obj))
            case Kind.ALT:
                alt = _obj(obj, "alt")
                if not isinstance(alt.get("decorative"), bool):
                    raise _Bad("alt.decorative must be true or false")
                return AltText(**base, anchor=_anchor(_obj(obj, "target"), text=False),
                               text=_str(alt, "text"), decorative=alt["decorative"],
                               supersedes=_supersedes(obj))
            case Kind.DISMISS:
                found = obj.get("findings")
                if (not isinstance(found, list) or not found
                        or not all(isinstance(f, str) and f for f in found)):
                    raise _Bad("findings must be a list of fingerprints")
                return Dismiss(**base, findings=tuple(found))
            case Kind.UNDO:
                return Undo(**base, of=_str(obj, "of", empty=False))
            case Kind.REATTACH:
                return Reattach(**base, from_=_hex(obj, "from", _HEX64, 64),
                                to=_hex(obj, "to", _HEX64, 64))
            case Kind.PROPOSAL:
                pid, verdict = _str(obj, "proposal"), obj.get("verdict")
                if not pid.startswith("pv-"):
                    raise _Bad("proposal must be a proposal id")
                if not isinstance(verdict, str) or verdict not in {x.value for x in Verdict}:
                    raise _Bad("verdict must be accept or reject")
                return ProposalVerdict(**base, proposal=pid, verdict=Verdict(verdict),
                                       text=_hex(obj, "text", _HEX64, 64))
            case Kind.FONT_MAP:
                fm, verdict = _str(obj, "map"), obj.get("verdict")
                if not _FONT_MAP.match(fm):
                    raise _Bad("map must be a font map id")
                if not isinstance(verdict, str) or verdict not in {x.value for x in Verdict}:
                    raise _Bad("verdict must be accept or reject")
                return FontMapVerdict(**base, map=fm, verdict=Verdict(verdict))
    except _Bad as e:
        return Invalid(ident, str(e))
    raise AssertionError(kind)   # pragma: no cover


def invalid_now(now: str, was: str) -> str | None:
    """Why a correction's `now` cannot be applied, or None: one line, no control or bidi
    character, at most NOW_MAX_CHARS characters and NOW_MAX_GROWTH times the length of `was`.
    The apply step orphans a correction with a reason as `invalid-now`."""
    if not now:
        return "empty"
    if len(now) > NOW_MAX_CHARS:
        return f"longer than {NOW_MAX_CHARS} characters"
    if len(now) > NOW_MAX_GROWTH * len(was):
        return f"longer than {NOW_MAX_GROWTH} times the word it replaces"
    for ch in now:
        if ch in BIDI_CHARS:
            return "holds a bidirectional control character"
        if unicodedata.category(ch) in ("Cc", "Zl", "Zp"):
            return "holds a control or line-break character"
    return None


# -- the snapshot --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """A parsed record and where its line starts (a byte offset in the file) and its number."""
    offset: int
    line: int          # 1-based, the header is line 1
    record: Parsed


@dataclass(frozen=True)
class Snapshot:
    """The one read of a conversion. `store` is the header's id (None without a valid header),
    `bytes` the offset after the last complete line, `lines` the complete lines (the header,
    unreadable lines and records). `entries` are the parsed lines after the header, empty when
    there is an error."""
    store: str | None
    bytes: int
    lines: int
    torn: int                       # 1 when bytes follow the last newline, else 0
    unreadable: tuple[int, ...]     # byte offset of each complete line that is not a JSON object
    entries: tuple[Entry, ...]
    error: str | None               # "no-header", else None
    slug: str | None = None


def _object(line: bytes) -> Mapping[str, Any] | None:
    if len(line) > MAX_LINE:
        return None

    def refuse(token: str) -> Any:
        raise ValueError(token)

    try:
        obj = json.loads(line.decode("utf-8"), parse_constant=refuse)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def read(path: Path | str) -> Snapshot:
    """Read the store once, up to its size at that moment, and cut it after the last newline.
    A missing file, and an empty one, is an empty store; a path that is not a file is a
    ValueError."""
    path = Path(path)
    try:
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise ValueError(f"{path} is not a file")
            data = f.read(st.st_size)
    except FileNotFoundError:
        return Snapshot(None, 0, 0, 0, (), (), None)
    except IsADirectoryError:
        raise ValueError(f"{path} is not a file") from None
    cut = data.rfind(b"\n") + 1
    torn = 1 if len(data) > cut else 0
    if not data:
        return Snapshot(None, 0, 0, 0, (), (), None)
    unreadable: list[int] = []
    entries: list[Entry] = []
    header: Header | None = None
    offset, number = 0, 0
    for raw in data[:cut].split(b"\n")[:-1]:
        number += 1
        obj = _object(raw)
        if obj is None:
            unreadable.append(offset)
        elif number == 1:
            header = parse_header(obj)
        else:
            entries.append(Entry(offset, number, parse_record(obj)))
        offset += len(raw) + 1
    if header is None:
        return Snapshot(None, cut, number, torn, tuple(unreadable), (), NO_HEADER)
    return Snapshot(header.id, cut, number, torn, tuple(unreadable), tuple(entries), None,
                    header.slug)


def pending(snapshot: Snapshot, *, store: str | None, bytes_: int) -> tuple[Entry, ...]:
    """The records of `snapshot` a build whose ledger names `store` and `bytes_` has not read:
    all of them when the store is another, else those whose line starts at or after `bytes_`."""
    if snapshot.store != store:
        return snapshot.entries
    return tuple(e for e in snapshot.entries if e.offset >= bytes_)


# -- the fold ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Fold:
    """The active decisions in store order and the counts the ledger publishes."""
    active: tuple[Stackable, ...]
    reattach: tuple[Reattach, ...]
    records: int                     # record lines read (duplicates, invalid, unsupported too)
    torn: int
    unreadable: int
    duplicate: int
    invalid: tuple[Invalid, ...]
    unsupported: tuple[Unsupported, ...]
    error: str | None
    store: str | None


def _round_half_up(x: float) -> int:
    return math.floor(x + 0.5)


def _family(rec: Stackable) -> list[tuple]:
    """The family keys a record sits on. Each key has its own stack."""
    match rec:
        case Correct() | Keep():
            a = rec.anchor
            return [("text", rec.pdf, a.page, *map(_round_half_up, a.bbox), a.text_sha,
                     rec.range.before, rec.range.was, rec.range.after)]
        case AltText():
            return [("alt", rec.pdf, rec.anchor.page, rec.anchor.crop_sha)]
        case Dismiss():
            return [("dismiss", rec.pdf, f) for f in dict.fromkeys(rec.findings)]
        case ProposalVerdict():
            return [("proposal", rec.pdf, rec.proposal)]
        case FontMapVerdict():
            return [("font-map", rec.pdf, rec.map)]


def _kin(rec: Stackable) -> type | tuple[type, ...] | None:
    """What a `supersedes` of this record may name."""
    if isinstance(rec, (Correct, Keep)):
        return (Correct, Keep)
    if isinstance(rec, AltText):
        return AltText
    return None


def fold(snapshot: Snapshot) -> Fold:
    """A pure function of the records in store order.

    The first record with an id wins. A record sits on the stack of its family and replaces the
    one beneath it; `supersedes` also puts it on the stack of an earlier record of its kind.
    `undo` takes a record off its stacks. A record is active when it is on top of every stack it
    sits on (a dismissal: of any, each fingerprint being its own family)."""
    seen: dict[str, Parsed] = {}
    stacks: dict[tuple, list[Stackable]] = {}
    keys: dict[str, list[tuple]] = {}
    order: list[Stackable] = []
    undone: set[str] = set()
    invalid: list[Invalid] = []
    unsupported: list[Unsupported] = []
    reattach: list[Reattach] = []
    duplicate = 0

    for entry in snapshot.entries:
        rec = entry.record
        ident = rec.id
        if ident is not None and ident in seen:
            duplicate += 1
            continue
        if ident is not None:
            seen[ident] = rec
        if isinstance(rec, Unsupported):
            unsupported.append(rec)
        elif isinstance(rec, Invalid):
            invalid.append(rec)
        elif isinstance(rec, Reattach):
            reattach.append(rec)
        elif isinstance(rec, Undo):
            target = seen.get(rec.of)
            if not isinstance(target, (Correct, Keep, AltText, Dismiss, ProposalVerdict,
                                       FontMapVerdict)):
                invalid.append(Invalid(rec.id, "undo names no earlier decision"))
            elif target.id not in undone:
                undone.add(target.id)
                for key in keys[target.id]:
                    stacks[key].remove(target)
        else:
            own = _family(rec)
            sup = getattr(rec, "supersedes", None)
            if sup is not None:
                target = seen.get(sup)
                kin = _kin(rec)
                if kin is not None and isinstance(target, kin) and target.id in keys:
                    own = own + [k for k in keys[target.id][:1] if k not in own]
                else:
                    invalid.append(Invalid(rec.id, "supersedes names no earlier record of its kind"))
            keys[rec.id] = own
            order.append(rec)
            for key in own:
                stacks.setdefault(key, []).append(rec)

    def top(key: tuple, rec: Stackable) -> bool:
        return stacks[key][-1] is rec

    active = tuple(
        rec for rec in order if rec.id not in undone
        and (any if isinstance(rec, Dismiss) else all)(top(k, rec) for k in keys[rec.id]))
    return Fold(active, tuple(reattach), len(snapshot.entries), snapshot.torn,
                len(snapshot.unreadable), duplicate, tuple(invalid), tuple(unsupported),
                snapshot.error, snapshot.store)


def eligible_pdfs(fold_: Fold, current: str) -> frozenset[str]:
    """The `pdf` values whose records may apply to the PDF with hash `current`: its own and
    those reattached to it. One hop: reattachments do not chain."""
    return frozenset({current, *(r.from_ for r in fold_.reattach if r.to == current)})
