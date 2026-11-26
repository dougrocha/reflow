"""Opt-in transcription after OCR, preserving its baseline and recording every draft.

The provider is injectable: `--assist local` asks TRANSCRIPTION_MODEL through Ollama, under the
GPU lease (analyze.vision.OllamaVision). Cache identity includes the image bytes, model and
prompt. A draft's bbox is the crop sent to the model, not an invented word-level alignment.
Markdown is stored as text with its line breaks intact.

Every reading is checked against its crop before it is shown (`check`): nothing but
`[illegible]`, a repetition loop, or more letters than the crop's ink can hold rejects it. A
rejected region is read once more, told what failed (`read`), and then left as it was. An
unread crop says what came of it in `reading` (DRAFTED, REJECTED, UNAVAILABLE), which its
note shows.

A signature crop is sent only to a model on this machine (a signature is personal data), and
asked for the signer's name, which is taken only when the line it signs prints it (`signer`).
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from collections.abc import Callable
from difflib import SequenceMatcher
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import pypdfium2 as pdfium
from PIL import Image

from reflow.analyze import ask, sections
from reflow.analyze.figures import read_alt
from reflow.analyze.vision import (OCR_PROMPT, OllamaVision, ProviderError, ProviderUnavailable,
                                   VisionImage, first_json_object)
from reflow.ingest.ocr import MIN_LETTER_SHARE, letter_share
from reflow.ingest.ocr_trust import DARK
from reflow.model import (FACT_OCR_MISSING, FACT_OCR_PAGE, FACT_SCAN_NOISY, BBox, Document, Fact, Node, PageKind, Role, Source, ai_draft_attrs,
                          make_id, mark_ai_draft)

if TYPE_CHECKING:
    from reflow.run import Run

TRANSCRIPTION_PROMPT = """Transcribe exactly, in reading order, as Markdown. Include handwriting.
Add nothing. Write [illegible] for unreadable words. Do not guess signatures or names.
Write each formula or line of calculation exactly as it is written, as LaTeX between dollar signs, one pair for the whole line: subscripts as x_1, fractions as \\frac{a}{b}, degrees as ^\\circ, units and chemical formulas in \\mathrm{}. Keep ordinary words outside the dollar signs.
Return a JSON object with only these fields:
- text: the exact Markdown transcription, or an empty string if there is no text.
- handwritten: a boolean, true if the image contains handwriting, including signatures.
The handwritten field is metadata, never add editorial labels to the transcription."""
PROMPT_VERSION = 2
SCALE = 2.0
TRANSCRIBE_TOKENS = 4096


@dataclass(frozen=True)
class Transcription:
    text: str
    handwritten: bool


class Truncated(ProviderError):
    """The reply reached the token limit: a reading cut off, most often in a repetition loop."""


class Transcriber(Protocol):
    cache_id: str

    def transcribe(self, image: bytes, media_type: str, told: str = "") -> Transcription: ...


TRANSCRIPTION_SCHEMA = {"type": "object", "required": ["text", "handwritten"],
                        "properties": {"text": {"type": "string"},
                                       "handwritten": {"type": "boolean"}}}


def transcribe_with(client, image: bytes, media_type: str,
                    max_tokens: int = TRANSCRIBE_TOKENS, told: str = "") -> Transcription:
    """The transcription `client` (anything with `ask`) gives of the picture: TRANSCRIPTION_PROMPT
    and its JSON answer. `told` is why the last reading was rejected, for a reread. A draft that
    reached the token limit is discarded."""
    reply = client.ask(TRANSCRIPTION_PROMPT, " ".join(filter(None, ["Transcribe this image.", told])),
                       VisionImage(image, media_type), TRANSCRIPTION_SCHEMA, max_tokens)
    if reply.truncated:
        raise Truncated(f"transcription reached the {max_tokens}-token limit; draft discarded")
    return parse_transcription(reply.text)


class OllamaTranscriber:
    """Transcription through a local Ollama server. By default it asks
    TRANSCRIPTION_PROMPT for the same JSON answer as the local model, so only the model changes.
    With `ocr_prompt`, it asks as an OCR model is asked (OCR_PROMPT, plain text back); such a
    model cannot report handwriting, so `handwritten` stays False and the region's flag
    decides. Either way the text is a draft that assist.draft adds next to OCR's reading."""

    def __init__(self, model: str, host: str | None = None, ocr_prompt: bool = False,
                 max_tokens: int = TRANSCRIBE_TOKENS) -> None:
        self.client = OllamaVision(model, host=host)
        self.ocr_prompt, self.max_tokens = ocr_prompt, max_tokens
        self.cache_id = f"ollama:{model}" + (":ocr" if ocr_prompt else "")

    def read_signature(self, image: bytes, media_type: str) -> str:
        return name_with(self.client, image, media_type)

    def transcribe(self, image: bytes, media_type: str, told: str = "") -> Transcription:
        if not self.ocr_prompt:
            return transcribe_with(self.client, image, media_type, self.max_tokens, told)
        # An OCR model's prompt is its task's own: a reread asks it the same.
        reply = self.client.ask("", OCR_PROMPT, VisionImage(image, media_type), None,
                                self.max_tokens)
        if reply.truncated:
            raise Truncated(f"transcription reached the {self.max_tokens}-token limit; "
                            "draft discarded")
        return Transcription(reply.text.strip(), False)


def parse_transcription(answer: str) -> Transcription:
    data = first_json_object(answer)
    if not isinstance(data.get("text"), str) or not isinstance(data.get("handwritten"), bool):
        raise ProviderError("transcription needs string text and boolean handwritten")
    return Transcription(data["text"].strip(), data["handwritten"])


def cache_key(image: bytes, provider: Transcriber, told: str = "") -> str:
    digest = hashlib.sha256(image)
    digest.update(f"\0{provider.cache_id}\0{PROMPT_VERSION}\0{TRANSCRIPTION_PROMPT}".encode())
    if told:
        digest.update(f"\0{told}".encode())
    return digest.hexdigest()


# -- checking a reading against its crop ------------------------------------------------------

ILLEGIBLE = "[illegible]"
LATEX_COMMAND = re.compile(r"\\[A-Za-z]+")   # its letters are not written on the crop
MAX_ATTEMPTS = 2       # a first reading and one reread, told what failed
MAX_REPEATS = 3        # a run of words back to back more often than this is a repetition loop
REPEAT_RUN = 8         # words: the longest run looked for
REPEAT_LETTERS = 4     # a run with fewer letters and digits than this may well repeat ("0 0 0")
# Dark pixels at SCALE: fewer per letter read is more than the ink can hold. Readings that
# passed by eye on the #291 crops used 20 to 475 a letter (a 7 pt footer to a signature).
INK_PER_LETTER = 8
INK_SLACK = 10         # letters: a short reading of a faint crop is never invented


class Problem(StrEnum):
    NOTHING = "nothing"        # no letter or digit but [illegible]
    REPEATED = "repeated"      # a repetition loop
    INVENTED = "invented"      # more letters than the crop's ink can hold
    CUT_OFF = "cut-off"        # the reply reached the token limit
    FAILED = "failed"          # the call failed: a timeout, a reply that is not the JSON asked for


# What a reread is told, for each problem that rejected the last reading.
TOLD = {
    Problem.NOTHING: "the last answer read no text",
    Problem.REPEATED: "the last answer repeated itself",
    Problem.INVENTED: "the last answer had more text than the image holds",
    Problem.CUT_OFF: "the last answer was cut off",
    Problem.FAILED: "the last answer failed",
}


def ink(image: bytes) -> int:
    """The crop's dark pixels, as ingest.ocr_trust counts ink."""
    return sum(Image.open(io.BytesIO(image)).convert("L").histogram()[:DARK])


def _alnum(text: str) -> int:
    return sum(c.isalnum() for c in text)


def _loops(text: str) -> bool:
    """Whether a run of up to REPEAT_RUN words comes more than MAX_REPEATS times back to back."""
    words = re.findall(r"\w[^\s|]*", text.casefold())
    for n in range(1, REPEAT_RUN + 1):
        span = n * (MAX_REPEATS + 1)
        for i in range(len(words) - span + 1):
            run = words[i:i + n]
            if _alnum("".join(run)) >= REPEAT_LETTERS and words[i:i + span] == run * (MAX_REPEATS + 1):
                return True
    return False


def check(text: str, dark: int, writing: bool) -> list[Problem]:
    """What rejects a reading of a crop with `dark` pixels of ink. `writing`: the crop is known to
    hold writing (an unread crop), so a reading of nothing is a failure, not an empty page."""
    read = text.replace(ILLEGIBLE, "")
    problems = []
    if writing and not _alnum(read):
        problems.append(Problem.NOTHING)
    if _loops(text):
        problems.append(Problem.REPEATED)
    if _alnum(LATEX_COMMAND.sub("", read)) > dark / INK_PER_LETTER + INK_SLACK:
        problems.append(Problem.INVENTED)
    return problems


@dataclass(frozen=True)
class Read:
    """What came of reading one region: the reading that passed, or the problems of the last."""
    result: Transcription | None
    key: str = ""              # the passing reading's cache key
    problems: tuple[Problem, ...] = ()
    cached: bool = False
    error: str = ""            # the last failed call's message


def read(image: bytes, provider: Transcriber, store: ask.Store, writing: bool,
         call: bool = True) -> Read:
    """The first reading of `image` that passes `check`, from the cache or else from `provider`,
    with one reread told what failed. `call` False reads the cache only. ProviderUnavailable when
    a call is needed and none can be made."""
    dark, told, last = ink(image), "", Read(None)
    for _ in range(MAX_ATTEMPTS):
        key = cache_key(image, provider, told)
        result, failure, cached = None, None, False
        if (entry := store.get(key)) is not None:
            try:
                result, cached = parse_transcription(json.dumps(entry)), True
            except ProviderError:
                pass
        if result is None:
            if not call:
                raise ProviderUnavailable("the model could not be asked")
            try:
                result = provider.transcribe(image, "image/png", told)
                store.put(key, "transcription", model=provider.cache_id, **asdict(result))
            except ProviderError as e:
                failure = e
        problems = (check(result.text, dark, writing) if result is not None else
                    [Problem.CUT_OFF if isinstance(failure, Truncated) else Problem.FAILED])
        if not problems:
            return Read(result, key, cached=cached)
        last = Read(None, problems=tuple(problems), error=str(failure or ""))
        told = f"Read it again: {'; '.join(TOLD[p] for p in problems)}."
    return last


# -- a signature's name -------------------------------------------------------------------------

SIGNATURE_PROMPT = """This image is cropped around a handwritten signature in a document. \
Write the name the signature spells, exactly as written, or [illegible] if you cannot read it \
letter by letter. Do not guess a name. Answer with the name only."""
SIGNATURE_TOKENS = 32
# A name is two to NAME_WORDS words, each an initial ("J.") or two letters or more: never OCR's
# letter fragments ("B o frrr r"), and never a first name alone.
NAME_WORDS = 4
NAME_WORD = re.compile(r"[^\W\d_]{2,}(?:[-'’.][^\W\d_]*)*|[^\W\d_a-z]\.?")
# A word of the reading is like a word of the printed line at this SequenceMatcher ratio, after
# case-folding: one letter in eight misread ("Dieinger" for "Dieringer" is 0.94).
NAME_MATCH = 0.8
# Words printed on or under a signature line that name no one: a role, a form's own words, a
# title. A reading word that is one of them is never part of a name, and on the printed line one
# ends a name, like a comma does.
NOT_NAMES = frozenset("""
    president vice secretary treasurer chair chairman chairwoman chairperson director manager
    officer member clerk notary public speaker house senate governor lieutenant mayor senator
    representative commissioner agent official authorized approved attest witness witnesses
    applicant applicants owner owners tenant landlord buyer seller borrower lender guarantor
    employer employee parent guardian spouse signer signatory signature signatures signed sign
    print printed type typed name names title date dated here seal by of the and or for to
    mr mrs ms dr miss sir madam chief executive financial operating general assistant deputy
    partner principal trustee administrator superintendent supervisor coordinator board
    committee council district county city state department company corporation inc llc
    please below above line full legal first last middle initial initials
""".split())
# A printed name field: "Name:", "Print Name", "Name of Applicant:", "By:". Only the name after
# one is a person's name; printed words alone may as well be a role ("Managing Attorney").
NAME_FIELD = re.compile(r"\b(?:name|by)\b[^:\n]{0,30}:|\bname\b", re.IGNORECASE)
# A model's way of saying it read no name; never taken as one, whatever the line prints.
NON_ANSWER = re.compile(r"illegible|unknown|unreadable|unclear|not\s+legible|can(?:no|')t\s+read|"
                        r"can\s+not\s+read|no\s+signature|no\s+name|\bnone\b|\bn/?a\b|blank",
                        re.IGNORECASE)


def signer(reading: str, printed: str | None = None) -> str | None:
    """The signer's name a model's `reading` of a signature gives, or None, which keeps
    "Signature" alone. A name is never guessed: it is taken only when `printed`, the line the
    signature signs (figures.SignatureMatch.role), prints it in a name field: the one whole
    printed name after the field (`printed_name`), word for word in order (`_as_printed`), never
    pieced from two. Two words
    at least, one more than an initial (never a first name alone), no word of NOT_NAMES, and
    never a non-answer (NON_ANSWER).

    A model's certainty does not stand in for print: qwen3-vl:8b drafted another signer's
    hand-lettered "King" at 0.907, and "Sara Dienger" for Dieringer at 0.82, while right names
    scored 0.863 (#345)."""
    words = reading.split()
    if (not 2 <= len(words) <= NAME_WORDS or NON_ANSWER.search(reading)
            or not all(NAME_WORD.fullmatch(w) for w in words)
            or all(len(w.strip(".'’-")) < 2 for w in words)):   # initials alone ("J. S.")
        return None
    name = printed_name(printed or "")
    return _as_printed(words, name) if len(name) == len(words) else None


def printed_name(printed: str) -> list[str]:
    """The name a printed line gives in a name field (NAME_FIELD), word by word: the run of
    capitalized words of letters that starts right after it, ended by any other mark ("/", ",",
    a digit), a word in lower case, or a word of NOT_NAMES. Empty without a field, or when the
    field's value is blank ("Name: ____ Title: ...") or anything else."""
    field = NAME_FIELD.search(printed)
    name: list[str] = []
    for token in re.findall(r"[^\W\d_]+|[^\s.'’-]", printed[field.end():] if field else ""):
        if token[0].isalpha() and token[0].isupper() and token.casefold() not in NOT_NAMES:
            name.append(token)
        else:
            break
    return name


def _as_printed(words: list[str], name: list[str]) -> str | None:
    """`words` read as the printed `name`, word for word in order: a word like its printed word
    (NAME_MATCH), spelled as printed, or an initial, its first letter. None when one is not."""
    out = []
    for word, printed in zip(words, name, strict=True):
        bare = word.casefold().strip(".'’-")
        if bare in NOT_NAMES:
            return None
        if len(bare) < 2:
            if not printed.casefold().startswith(bare):
                return None
            out.append(word)
        elif SequenceMatcher(None, bare, printed.casefold()).ratio() >= NAME_MATCH:
            out.append(printed)
        else:
            return None
    return " ".join(out)


def name_with(client: OllamaVision, image: bytes, media_type: str) -> str:
    """The reading `client` gives of a signature crop (SIGNATURE_PROMPT). Only a server on this
    machine is asked (OllamaVision.post's `local_only`): ProviderUnavailable for another."""
    reply = client.ask("", SIGNATURE_PROMPT, VisionImage(image, media_type), None,
                       SIGNATURE_TOKENS, local_only=True)
    if reply.truncated:
        raise Truncated(f"the signature's reading reached the {SIGNATURE_TOKENS}-token limit")
    return reply.text.strip()


def names_signatures(provider: object) -> bool:
    """Whether `provider` may be sent a signature, which is personal data: only a model on this
    machine, through Ollama, is."""
    client = getattr(provider, "client", None)
    return isinstance(client, OllamaVision) and client.local() and isinstance(provider, Namer)


@runtime_checkable
class Namer(Protocol):
    """A transcriber that reads a signature crop's name (name_with)."""
    cache_id: str

    def read_signature(self, image: bytes, media_type: str) -> str: ...


def read_name(image: bytes, provider: Namer, store: ask.Store, call: bool = True
              ) -> tuple[str, str, bool]:
    """The reading of a signature crop, from the cache or else from `provider`: the reading,
    its cache key, and whether it came from the cache. ProviderUnavailable when a call is needed
    and none can be made; ProviderError when the call fails."""
    digest = hashlib.sha256(image)
    digest.update(f"\0{provider.cache_id}\0{SIGNATURE_PROMPT}".encode())
    key = digest.hexdigest()
    entry = store.get(key)
    if entry is not None and isinstance(entry.get("text"), str):
        return entry["text"], key, True
    if not call:
        raise ProviderUnavailable("the model could not be asked")
    reading = provider.read_signature(image, "image/png")
    store.put(key, "signature", model=provider.cache_id, text=reading)
    return reading, key, False


@dataclass(frozen=True)
class Region:
    page: int
    bbox: BBox
    reason: str
    handwritten: bool = False
    node: str | None = None   # an unread figure's id: its draft goes right after it
    signs: str | None = None  # a signature's printed line (its `signs` attr), for `signer`


def _handwritten(facts: list[Fact]) -> bool:
    return any(f.data.get("handwritten") is True for f in facts)


def regions(doc: Document, working: Path, signatures: bool = False
            ) -> list[tuple[Region, bytes]]:
    """Read OCR's text layer first; rasterize only scans, explicit handwriting regions and the
    unread figures, each a target of its own. A page with unread figures sends them, not the
    page: each reading goes under its crop. A signature is a crop of its own with `signatures`
    (a model on this machine), and never sent otherwise, alone or in its page. A grainy scan,
    whose page scan looks for no writing, is sent whole. Caller holds the pdfium lock. All
    returned coordinates use the model's top-left origin."""
    flagged: dict[int, list[Region]] = {}
    unread: dict[int, list[Region]] = {}
    for node in doc.root.walk():
        if node.source == Source.LLM or not node.page or not node.bbox:
            continue
        if node.role == Role.FIGURE and node.attrs.get("kind") == "signature":
            unread.setdefault(node.page, []).extend(
                [Region(node.page, node.bbox, SIGNATURE, True, node.id, node.attrs.get("signs"))]
                if signatures else [])
            continue
        if node.role == Role.FIGURE and node.attrs.get("unread"):
            unread.setdefault(node.page, []).append(Region(
                node.page, node.bbox, "unread-figure", node.attrs.get("handwritten") is True,
                node.id))
        elif node.attrs.get("handwritten") is True or _handwritten(node.facts):
            flagged.setdefault(node.page, []).append(
                Region(node.page, node.bbox, "ocr-handwriting", True))
    out = []
    pdf = pdfium.PdfDocument(working)
    try:
        for info in doc.pages:
            scan = (info.kind == PageKind.SCANNED or info.image_coverage >= 0.5 or
                    any(f.code in (FACT_OCR_PAGE, FACT_OCR_MISSING) for f in info.facts))
            if (not scan and not flagged.get(info.number) and not unread.get(info.number)
                    and not _handwritten(info.facts)):
                continue
            page = pdf[info.number - 1]
            try:
                height = page.get_height()
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_range()
                    count = textpage.count_rects()
                    poor_regions = False
                    for i in range(count):
                        left, bottom, right, top = textpage.get_rect(i)
                        region_text = textpage.get_text_bounded(left, bottom, right, top)
                        if (len(region_text.strip()) >= 12 and
                                any(c.isalpha() for c in region_text) and
                                letter_share(region_text) < MIN_LETTER_SHARE):
                            poor_regions = True
                finally:
                    textpage.close()
                image = page.render(scale=SCALE).to_pil()
                full = (0.0, 0.0, page.get_width(), height)
                if info.number in unread:
                    targets = flagged.get(info.number, []) + unread[info.number]
                elif scan and letter_share(text) < MIN_LETTER_SHARE:
                    targets = [Region(info.number, full, "ocr-empty" if not text.strip()
                                      else "ocr-low-letter-share")]
                elif scan and poor_regions:
                    targets = [Region(info.number, full, "ocr-low-letter-share-region")]
                elif _handwritten(info.facts):
                    targets = [Region(info.number, full, "ocr-handwriting", True)]
                elif flagged.get(info.number):
                    targets = flagged[info.number]
                elif scan and any(f.code == FACT_SCAN_NOISY for f in info.facts):
                    targets = [Region(info.number, full, "scan-noisy")]
                else:
                    targets = []
                if flagged.get(info.number) or _handwritten(info.facts):
                    targets = [t if t.node else replace(t, handwritten=True) for t in targets]
                for target in dict.fromkeys(targets):
                    crop = image.crop(tuple(round(v * SCALE) for v in target.bbox))
                    buf = io.BytesIO()
                    crop.save(buf, "PNG")
                    out.append((target, buf.getvalue()))
            finally:
                page.close()
    finally:
        pdf.close()
    return out


# What came of the model's reading of an unread crop (its `reading` attr); none was asked when
# it has none. The crop's note says why one that stays is not transcribed (render.html).
DRAFTED, REJECTED, UNAVAILABLE = "drafted", "rejected", "unavailable"
SIGNATURE = "signature"   # a Region's reason: a signature crop, asked for its signer's name


def draft(doc: Document, candidates: list[tuple[Region, bytes]], provider: Transcriber,
          cache_path: Path,
          on_progress: Callable[[int, int], None] | None = None) -> dict[str, int]:
    """Apply the readings that pass `check` as drafts; a region with none keeps its baseline. An
    unread crop records what came of it; any other region's failure is a page fact for review.
    An unavailable provider is asked nothing more, and the cache still answers."""
    with ask.Store(cache_path).saving() as store:
        stats = {"drafted": 0, "cached": 0, "rejected": 0, "failed": 0}
        pages = {p.number: p for p in doc.pages}
        crops = {n.id: n for n in doc.root.walk() if n.attrs.get("unread")}
        unavailable = ""
        for i, (region, image) in enumerate(candidates):
            crop = crops.get(region.node or "")
            if region.reason == SIGNATURE and crop is not None and isinstance(provider, Namer):
                try:
                    reading, key, cached = read_name(image, provider, store, call=not unavailable)
                    signed = signer(reading, region.signs)
                    outcome_of = (DRAFTED, repr(reading)) if signed else (
                        REJECTED, f"{reading!r}, no printed name like it")
                except (ProviderError, ProviderUnavailable) as error:
                    if isinstance(error, ProviderUnavailable):
                        unavailable = unavailable or str(error)
                    signed, outcome_of = None, (UNAVAILABLE, unavailable or str(error))
                crop.attrs["reading"] = outcome_of[0]
                crop.attrs.setdefault("why", []).append(f"reading: {outcome_of[0]} ({outcome_of[1]})")
                if signed:
                    stats["cached" if cached else "drafted"] += 1
                    _place(doc, region, Transcription(signed, True), key, provider, image)
                else:
                    stats["rejected" if outcome_of[0] == REJECTED else "failed"] += 1
                if on_progress:
                    on_progress(i + 1, len(candidates))
                continue
            try:
                outcome = read(image, provider, store, crop is not None, call=not unavailable)
            except ProviderUnavailable as error:
                unavailable = unavailable or str(error)
                outcome = Read(None, error=unavailable)
            if outcome.result is None:
                failed = not outcome.problems or set(outcome.problems) == {Problem.FAILED}
                stats["failed" if failed else "rejected"] += 1
                if crop is not None:
                    crop.attrs["reading"] = UNAVAILABLE if failed else REJECTED
                    crop.attrs.setdefault("why", []).append(
                        f"reading: {crop.attrs['reading']} "
                        f"({', '.join(outcome.problems) or outcome.error})")
                else:
                    said = "" if failed else f"no reading passed the checks: {', '.join(outcome.problems)}"
                    pages[region.page].facts.append(Fact(
                        "ai-assist-failed", Source.LLM,
                        "; ".join(filter(None, [said, outcome.error]))))
            else:
                stats["cached" if outcome.cached else "drafted"] += 1
                if crop is not None:
                    crop.attrs["reading"] = DRAFTED
                    crop.attrs["alt"] = read_alt(crop.attrs.get("alt", ""))
                _place(doc, region, outcome.result, outcome.key, provider, image)
            if on_progress:
                on_progress(i + 1, len(candidates))
        store.save(force=True)
    return stats


def _place(doc: Document, region: Region, result: Transcription, key: str,
           provider: Transcriber, image: bytes) -> None:
    """The reading's paragraphs as drafts: an unread crop's right under it, any other on its
    source page. Empty is a valid reading of a region with no text, and adds nothing."""
    after = region.node
    for j, paragraph in enumerate(result.text.split("\n\n")):
        if not paragraph.strip():
            continue
        node = Node(Role.P, page=region.page, bbox=region.bbox, text=paragraph,
                    id=f"{make_id(region.page, region.bbox, key)}:ai-{j}",
                    source=Source.LLM, confidence=0.5,
                    attrs={**ai_draft_attrs(),
                           "handwritten": result.handwritten or region.handwritten,
                           "format": "markdown", "lines": paragraph.splitlines(),
                           "assist_reason": region.reason})
        if region.node is not None:
            node.attrs["assist_for"] = region.node
        mark_ai_draft(node, "AI-drafted, not reviewed", model=provider.cache_id,
                      image_sha256=hashlib.sha256(image).hexdigest())
        if any(n.id == node.id for n in doc.root.walk()):
            continue
        # Keep the engine's baseline intact.
        if after is not None and _insert_after(doc.root, after, node):
            after = node.id
        else:
            sections.insert_by_page(doc.root.children, node, region.page)


def _insert_after(root: Node, node_id: str, new: Node) -> bool:
    """Put `new` right after the node `node_id`, in its parent; False when no node has the id."""
    for parent in root.walk():
        for i, child in enumerate(parent.children):
            if child.id == node_id:
                parent.children.insert(i + 1, new)
                return True
    return False


def transcribe(run: Run, provider: Transcriber) -> None:
    from reflow.names import ASSIST_CACHE

    with run.step("assist"):
        with run.workers.pdfium:
            candidates = regions(run.doc, run.working, signatures=names_signatures(provider))
        stats = draft(run.doc, candidates, provider, run.stage / ASSIST_CACHE,
                      lambda d, n: run.progress("AI transcription (local)", d, n))
        run.doc.meta["ai_assist"] = {"provider": "local", **stats}
