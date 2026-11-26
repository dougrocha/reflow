"""Alt text for figures.

reflow assumes nothing about alt text. By default (provider "none") every figure
without alt text is reported in review.json for a person to write it.

Optionally, `--alt-text` drafts suggestions with a vision model: `ollama` through a local
Ollama server (providers.ALT_TEXT_MODELS; documents never leave the machine), `anthropic` through the
Anthropic API with the user's own key (the crops leave the machine), or `local` in this process
(Qwen3-VL-8B through torch/transformers, the docling extra, ~17 GB of VRAM; analyze.local_model). Suggestions are a
starting point for manual review, not a finished result: every suggested figure is still listed
in review.json as a warning to check.

Each figure is described from its cropped image plus context: its caption and the
nearest preceding heading and paragraph. Suggestions are cached in the output directory
by image + context + model + prompt version, so re-running doesn't run the model again.
"""

from __future__ import annotations

import contextvars
import hashlib
import io
import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Protocol

from reflow import geometry
from reflow.analyze.anchors import crop_sha
from reflow.analyze.assist import Transcription, name_with, transcribe_with
from reflow.analyze.local_model import LocalModel
from reflow.analyze.vision import (
    AnthropicVision,
    OllamaVision,
    ProviderError,
    VisionImage,
    first_json_object,
)
from reflow.extract.crops import render_figures
from reflow.model import (Document, HEADING_ROLES, Node, PageInfo, Role, Source, figure_role,
                          is_model_text, mark_ai_draft)
from reflow.names import ALT_CACHE, FIGURES_DIR
from reflow.providers import ALT_TEXT_MODELS, ALT_TEXT_PROVIDERS
from reflow.run import Run
from reflow.text_quality import squash

# node id → (file name under names.FIGURES_DIR, width, height)
type Figures = dict[str, tuple[str, int, int]]

PROMPT_VERSION = 3  # part of the cache key: bump with any change to the prompt or parsing

KINDS = ["photo", "logo", "chart", "diagram", "map", "table", "text", "other"]
# Kinds whose suggested description especially needs checking (noted in review.json).
COMPLEX_KINDS = {"chart", "diagram", "map", "table", "other"}
MAX_ALT = 150
MAX_NEW_TOKENS = 1024  # a full answer is a few hundred tokens; generation stops at end-of-turn
CONTEXT_CHARS = 600

CONF_MODEL = 0.5   # a draft through a model: it is still checked against the document

SYSTEM_PROMPT = """You write alternative text for images taken from PDF documents, for people \
using screen readers (WCAG 2.1 success criterion 1.1.1).

Return JSON with these fields:
- kind: what the image is (photo, logo, chart, diagram, map, table, text, other).
- decorative: true only if the image conveys no information (borders, background textures, \
dividers, purely ornamental graphics). Then alt and description are empty strings.
- alt: one complete sentence of about 15 to 20 words (at most 150 characters) giving the \
image's subject and its key takeaway in this document. Leave the details to description. \
Don't start with "image of" or "picture of". Don't repeat the caption word for word. \
Never end with an ellipsis.
- description: for charts, diagrams, maps, tables and other complex images, a fuller \
description (at most 120 words, in complete sentences) of the structure and the key \
information, so someone who can't see it gets the same information. If the image is mostly \
text or a table, transcribe the text. Empty for simple photos and logos.
- uncertain: true if the image is unclear or you aren't confident in the description.

Only state text, numbers and facts that are visible in the image. If a value can't be read, \
say so instead of guessing. Use the document context only to interpret the image.
- An image may be a collage of several photos: give one short clause per photo.
- Name a signature, a stamp or a logo as such. Never read a signature as a name.
- Call text you cannot read unreadable instead of guessing it."""

SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": KINDS},
        "decorative": {"type": "boolean"},
        "alt": {"type": "string"},
        "description": {"type": "string"},
        "uncertain": {"type": "boolean"},
    },
    "required": ["kind", "decorative", "alt", "description", "uncertain"],
    "additionalProperties": False,
}

@dataclass
class FigureContext:
    page: int | None
    caption: str
    heading: str
    paragraph: str
    # The words printed within NEARBY_PT of the figure and the figure's own labels: from the
    # document, never from a model.
    printed_nearby: str = ""

    def as_text(self) -> str:
        parts = [f"Page: {self.page}"]
        for label, value in (("Caption", self.caption), ("Section heading", self.heading),
                             ("Nearby text", self.paragraph),
                             ("Printed near the image", self.printed_nearby)):
            if value:
                parts.append(f"{label}: {value[:CONTEXT_CHARS]}")
        return "Context from the document:\n" + "\n".join(parts)


@dataclass
class AltResult:
    kind: str
    decorative: bool
    alt: str
    description: str
    uncertain: bool


class Provider(Protocol):
    name: str
    cache_id: str  # provider + model, part of the cache key
    confidence: float
    workers: int  # parallel requests

    def describe(self, image: bytes, media_type: str, ctx: FigureContext) -> AltResult: ...

    def close(self) -> None: ...


MIN_CUT = 20  # a shortened alt keeps at least this many characters
ABBREVIATIONS = {"vs", "etc", "e.g", "i.e", "approx", "fig", "no", "dr", "mr", "mrs", "ms", "st"}
TRAILING_ELLIPSIS = re.compile(r"\s*(?:…|\.{3})$")
SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
CLAUSE_END = re.compile(r"[,;:](?=\s)|\s(?=[—–]\s)")  # comma, semicolon, colon, spaced dash


def _balanced(text: str) -> bool:
    return text.count("(") == text.count(")")


def _fits(cuts: list[int], text: str, limit: int) -> int | None:
    """The last candidate cut that keeps enough text and doesn't split a parenthesis."""
    for cut in reversed(cuts):
        if MIN_CUT <= cut <= limit and _balanced(text[:cut]):
            return cut
    return None


def shorten_alt(text: str, limit: int = MAX_ALT) -> tuple[str, bool]:
    """Fit alt text into `limit` characters without cutting a phrase in half.

    Returns (text, changed). Cuts go at the last sentence end that fits, then at the last clause
    break (comma, semicolon, colon, spaced dash), and only as a last resort at a word boundary.
    Never appends an ellipsis. `changed` is True whenever the text was shortened or lost a
    trailing ellipsis from the model, so the caller can flag it for a person to check.
    """
    text = squash(text)
    changed = False
    if TRAILING_ELLIPSIS.search(text):  # the model's own ellipsis: the alt was left unfinished
        text = TRAILING_ELLIPSIS.sub("", text).rstrip(" ,;:")
        changed = True
    if len(text) <= limit:
        if changed and text[-1:] not in ".!?":
            text += "."
        return text, changed

    sentence_cuts = []
    for m in SENTENCE_END.finditer(text):
        words = text[:m.start()].split()
        word = words[-1].lower() if words else ""
        # not an initial or an abbreviation such as "U.S.", "e.g." or "vs."
        if len(word) > 1 and "." not in word and word not in ABBREVIATIONS:
            sentence_cuts.append(m.end())
    cut = _fits(sentence_cuts, text, limit)
    if cut is not None:
        return text[:cut], True

    clause_cuts = [m.start() for m in CLAUSE_END.finditer(text)]
    cut = _fits(clause_cuts, text, limit)
    if cut is not None:
        return text[:cut].rstrip(" ,;:—–") + ".", True

    space = text.rfind(" ", 0, limit + 1)
    if space > 0:
        return text[:space].rstrip(" ,;:—–"), True
    return text, True  # one unbroken word longer than the limit: flagged, not cut mid-word


def _flag(value) -> bool:
    """A model's yes/no answer: a real boolean, or the string "true" or "false". bool() would
    read "false" as true, so anything else is rejected."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ProviderError(f"expected true or false, got {value!r}")


def parse_result(data: dict) -> AltResult:
    try:
        kind = data["kind"] if data["kind"] in KINDS else "other"
        result = AltResult(kind=kind, decorative=_flag(data["decorative"]),
                           alt=squash(str(data["alt"])),
                           description=squash(str(data.get("description", ""))),
                           uncertain=_flag(data.get("uncertain", False)))
    except (KeyError, TypeError) as e:
        raise ProviderError(f"malformed answer: {e}") from e
    if result.decorative:
        result.alt = result.description = ""
        return result
    if not result.alt:
        raise ProviderError("empty alt text for an informative image")
    result.alt, changed = shorten_alt(result.alt)
    if not result.alt:
        raise ProviderError("alt text is only an ellipsis")
    # A description that doesn't end a sentence may have been cut off: flag it for checking.
    unfinished = bool(result.description) and not re.search(r"[.!?][\"')\]]*$",
                                                            result.description)
    if changed or unfinished:
        result.uncertain = True
    return result


def parse_answer(text: str, truncated: bool = False) -> AltResult:
    """A model's free-text answer as an AltResult.

    `truncated` means generation hit MAX_NEW_TOKENS before the end of the answer. A complete
    JSON object is still used. Otherwise only a finished alt is kept, marked uncertain, and the
    description is dropped: cut-off text is never shipped.
    """
    try:
        return parse_result(first_json_object(text))
    except ProviderError:
        if not truncated:
            raise
    alt = re.search(r'"alt"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if not alt:
        raise ProviderError("answer cut off at the token limit before the alt text was finished")
    try:
        value = json.loads(f'"{alt.group(1)}"')
    except json.JSONDecodeError as e:
        raise ProviderError(f"cut-off answer has a bad alt string: {e}") from e
    kind = re.search(r'"kind"\s*:\s*"(\w+)"', text)
    return parse_result({"kind": kind.group(1) if kind else "other", "decorative": False,
                         "alt": value, "description": "", "uncertain": True})


class NoneProvider:
    name = cache_id = "none"
    confidence = 0.0
    workers = 1

    def describe(self, image: bytes, media_type: str, ctx: FigureContext) -> AltResult:
        raise ProviderError("no provider")

    def close(self) -> None:
        pass


class ModelProvider:
    """Figure description and transcription through a vision client (anything with
    `ask(system, prompt, image, schema, max_tokens) -> Reply`: analyze.vision's OllamaVision and
    AnthropicVision, or analyze.local_model.LocalModel). No I/O on construction: the server, the
    SDK and the key are touched on the first uncached figure, so a fully cached run needs none
    of them."""
    confidence = CONF_MODEL
    workers = 1   # one model server; the Anthropic route is opt-in and small

    def __init__(self, name: str, client, max_tokens: int = MAX_NEW_TOKENS) -> None:
        self.name, self.client, self.max_tokens = name, client, max_tokens
        self.cache_id = f"{name}:{client.cache_id}"

    def describe(self, image: bytes, media_type: str, ctx: FigureContext) -> AltResult:
        reply = self.client.ask(SYSTEM_PROMPT,
                                ctx.as_text() + "\n\nAnswer with the JSON object only.",
                                VisionImage(image, media_type), SCHEMA, self.max_tokens)
        return parse_answer(reply.text, reply.truncated)

    def transcribe(self, image: bytes, media_type: str, told: str = "") -> Transcription:
        return transcribe_with(self.client, image, media_type, told=told)

    def read_signature(self, image: bytes, media_type: str) -> str:
        """A signature's reading; assist.name_with asks only an Ollama on this machine."""
        return name_with(self.client, image, media_type)

    def close(self) -> None:
        """Unload the model when the client holds one in this process (LocalModel)."""
        if (close := getattr(self.client, "close", None)) is not None:
            close()


def make_provider(name: str, model: str | None = None, **kw) -> Provider:
    """The alt-text provider `name` (providers.ALT_TEXT_PROVIDERS), with `model` instead of its default."""
    if name == "local":
        return ModelProvider("local", LocalModel(**({"model": model} if model else {}), **kw))
    if name == "none":
        return NoneProvider()
    if name == "ollama":
        return ModelProvider("ollama", OllamaVision(model or ALT_TEXT_MODELS["ollama"]))
    if name == "anthropic":
        # thinking tokens count against max_tokens: leave room for a 120-word description
        return ModelProvider("anthropic", AnthropicVision(
            model or ALT_TEXT_MODELS["anthropic"], flag="--alt-text anthropic"), max_tokens=4096)
    raise ValueError(f"unknown alt-text provider: {name!r} (use {', '.join(ALT_TEXT_PROVIDERS)})")


# --- applying results -------------------------------------------------------------------

TEXT_ROLES = HEADING_ROLES | {Role.P, Role.LI, Role.TD, Role.TH, Role.CAPTION, Role.NOTE}


def printed(node: Node) -> bool:
    """Whether the node's text is the document's own, not a model's transcription (assist.py)."""
    return not is_model_text(node)


NEARBY_PT = 40.0   # printed text this close to a figure is part of its context


def _printed_nearby(figure: Node, texts: list[Node]) -> str:
    labels = [c.text for c in figure.children if c.attrs.get("figure_label") and c.text]
    own = {id(n) for n in figure.walk()}
    near = [n.text for n in texts if n.page == figure.page and n.text and n.bbox
            and id(n) not in own and geometry.axis_gap(n.bbox, figure.bbox) <= NEARBY_PT]
    return " ".join(squash(t) for t in [*labels, *near])


def figure_contexts(doc: Document) -> dict[str, FigureContext]:
    """Caption, nearest preceding heading and paragraph, and the words printed near it, for
    each figure, in reading order."""
    out: dict[str, FigureContext] = {}
    texts = [n for n in doc.root.walk()
             if n.role in TEXT_ROLES and n.text and n.bbox and printed(n)]
    heading = paragraph = ""
    for node in doc.flow():
        if node.role != Role.FIGURE and not printed(node):
            continue    # a model's transcription is not the page's words
        if node.role in HEADING_ROLES:
            heading = node.text or ""
        elif node.role == Role.P and node.text:
            paragraph = node.text
        elif node.role == Role.FIGURE:
            caption = next((c.text or "" for c in node.children if c.role == Role.CAPTION), "")
            nearby = _printed_nearby(node, texts) if node.bbox else ""
            out[node.id] = FigureContext(node.page, caption, heading, paragraph, nearby)
    return out


# --- figures that need no description -----------------------------------------------------

TINY_PT = 48.0          # a figure under this on both sides is an icon
TINY_SHARE = 0.005      # so is one under this share of its page
RULE_PT = 4.0           # a figure this thin is a ruling
BAND_PT = 12.0          # a figure this thin and BAND_RATIO times as long is a band
BAND_RATIO = 15.0
BLANK_STDDEV = 3.0      # grey levels: a crop this even is a blank band
REPEAT_PAGES = 3        # the same crop on this many pages is a logo or a letterhead
EDGE_PT = 2.0           # a figure this close to the page's edge is printed to it
BLEED_RATIO = 4.0       # a band across the page this many times as wide as tall is decoration


def crop_print(image: bytes) -> tuple[str, float]:
    """A crop's identity across pages (a hash of its 16x16 grey thumbnail, which survives the
    sub-pixel shifts of rendering the same mark at another place) and its grey-level spread."""
    from PIL import Image, ImageStat

    with Image.open(io.BytesIO(image)) as im:
        grey = im.convert("L")
        spread = ImageStat.Stat(grey).stddev[0]
        thumb = grey.resize((16, 16), Image.Resampling.BOX)
        ratio = round(im.width / max(1, im.height), 1)
    levels = bytes(v >> 4 for v in thumb.tobytes())
    return hashlib.sha1(levels + f"{ratio}".encode()).hexdigest(), spread


def mark_roles(doc: Document, images: dict[str, Path]) -> dict[str, int]:
    """Set `figure_role` on the figures that need no description, before any is sent to a
    model: a ruling, a blank band or a band of colour printed across the top or bottom edge of
    the page (with no words in it) is decorative, a crop repeated on REPEAT_PAGES pages is a
    logo, a tiny one an icon. One that has no alt of its own is also marked decorative with an
    empty alt, and carries a `decorative-guessed` fact with its reason, so it is listed for a
    person to check instead of as missing alt text. Returns the count per reason."""
    sizes = {p.number: p.width * p.height for p in doc.pages}
    pages = {p.number: p for p in doc.pages}
    figures = [n for n in doc.root.walk()
               if n.role == Role.FIGURE and n.bbox and not n.attrs.get("unread")]
    prints: dict[str, tuple[str, float]] = {}
    for n in figures:
        if n.id in images:
            try:
                prints[n.id] = crop_print(images[n.id].read_bytes())
            except (OSError, ValueError):
                pass
    pages_of: dict[str, set[int | None]] = {}
    for n in figures:
        if n.id in prints:
            pages_of.setdefault(prints[n.id][0], set()).add(n.page)
    counts: dict[str, int] = {}
    for n in figures:
        x0, top, x1, bottom = n.bbox
        w, h = x1 - x0, bottom - top
        short, long_ = min(w, h), max(w, h)
        crop = prints.get(n.id)
        if short < RULE_PT or (short < BAND_PT and long_ >= BAND_RATIO * max(short, 1e-6)):
            reason, role = "ruling", "decorative"
        elif crop and crop[1] < BLANK_STDDEV:
            reason, role = "blank", "decorative"
        elif bleeds(n, pages.get(n.page or 0)):
            reason, role = "bleed", "decorative"
        elif crop and len(pages_of[crop[0]]) >= REPEAT_PAGES:
            reason, role = "repeated", "logo"
        elif (w < TINY_PT and h < TINY_PT) or w * h < TINY_SHARE * sizes.get(n.page or 0, 0):
            reason, role = "tiny", "icon"
        else:
            continue
        n.attrs["figure_role"] = role
        if n.attrs.get("alt") or n.attrs.get("decorative") or n.attrs.get("provenance"):
            continue
        n.attrs.update(decorative=True, alt="")
        n.attrs.pop("needs", None)
        n.facts = [f for f in n.facts if f.code != "alt-missing"]
        n.fact("decorative-guessed", Source.HEURISTIC, DECORATIVE_REASONS[reason], reason=reason)
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def bleeds(n: Node, page: PageInfo | None) -> bool:
    """Whether a figure is a band printed across the whole width of the page at its top or
    bottom edge, wide and short, with no words in it (no caption, no labels): the coloured
    banner of a flyer, not a picture of anything."""
    if page is None or not n.bbox or n.children:
        return False
    x0, top, x1, bottom = n.bbox
    across = x0 <= EDGE_PT and x1 >= page.width - EDGE_PT
    at_edge = top <= EDGE_PT or bottom >= page.height - EDGE_PT
    return across and at_edge and x1 - x0 >= BLEED_RATIO * (bottom - top)


def store_roles(doc: Document) -> None:
    """Write every figure's role (model.figure_role) into the model, after drafting: a
    model's decorative or logo answer is a role too."""
    for n in doc.root.walk():
        if n.role == Role.FIGURE:
            n.attrs["figure_role"] = figure_role(n)


DECORATIVE_REASONS = {
    "ruling": "a thin line or band",
    "blank": "an even, empty area",
    "bleed": "a band printed across the edge of the page, with no words in it",
    "repeated": f"the same picture on {REPEAT_PAGES} or more pages, such as a logo or letterhead",
    "tiny": "too small to need a description",
}


def apply_result(node: Node, result: AltResult, confidence: float) -> None:
    node.attrs.pop("needs", None)
    node.attrs.pop("warn", None)
    node.attrs.update(alt=result.alt, decorative=result.decorative, kind=result.kind)
    mark_ai_draft(node)
    node.facts = [f for f in node.facts if f.code != "alt-missing"]
    if result.description and not result.decorative:
        node.attrs["description"] = result.description
    else:
        node.attrs.pop("description", None)
    node.source = Source.LLM
    node.confidence = confidence
    # Always a suggestion for a person to check; say what makes it worth a closer look.
    if result.uncertain:
        node.attrs["warn"] = "suggested-alt-uncertain"
    elif not result.decorative and result.kind in COMPLEX_KINDS:
        node.attrs["warn"] = "suggested-description"
    else:
        node.attrs["warn"] = "suggested-alt"


# --- grounding: a draft may not name a number or a quotation the page does not hold ------

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"], 1)} | {m[:3]: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"], 1)} | {"sept": 9}
_MONTH = r"(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
DATES = [
    re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})\b"),
    re.compile(r"\b(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4})\b"),
    re.compile(rf"\b{_MONTH}\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<y>\d{{4}})\b", re.I),
    re.compile(rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+{_MONTH}\s+(?P<y>\d{{4}})\b", re.I),
]
UNITS = r"%|\$|€|£|¢|°|km|kg|mg|ml|cm|mm|ft|lbs?|mi|oz|in|m|g|l"
# one number: thousands groups ("1,497", "1 497"), or digits with a decimal point or a decimal
# comma of one or two places ("3,5"), so a list such as "2024, 2025" is two numbers
NUMBER = re.compile(rf"(?<![\w.])(?:[$€£]\s?)?(?:\d{{1,3}}(?:[,\u00a0 ]\d{{3}})+(?!\d)(?:\.\d+)?"
                    rf"|\d+(?:\.\d+|,\d{{1,2}}(?!\d))?)(?:\s?(?:{UNITS})(?![a-z]))?", re.I)
QUOTE = re.compile(r"[\"“”](.+?)[\"“”]")


def _dates(text: str) -> tuple[set[tuple[int, int, int]], str]:
    """The dates in `text` as (year, month, day), and the text with them blanked out."""
    found: set[tuple[int, int, int]] = set()
    for pattern in DATES:
        def take(m: re.Match) -> str:
            month = (MONTHS.get(m.group("mon").lower().rstrip(".")) if "mon" in m.groupdict()
                     else int(m.group("m")))
            found.add((int(m.group("y")), month or 0, int(m.group("d"))))
            return " "
        text = pattern.sub(take, text)
    return found, text


def _number(token: str) -> str | None:
    """A number token in one form: no currency or unit, no thousands separators, a decimal
    point, no trailing decimal zeros. None for a lone digit with no unit: too common to check."""
    unit = bool(re.search(rf"(?:{UNITS})$|^[$€£]", token.strip(), re.I))
    digits = re.sub(r"^[$€£]\s?|\s?(?:" + UNITS + r")$", "", token.strip(), flags=re.I)
    digits = digits.replace("\u00a0", " ")
    if re.fullmatch(r"\d{1,3}(?:[, ]\d{3})+(?:\.\d+)?", digits):
        digits = re.sub(r"[, ]", "", digits)
    elif re.fullmatch(r"\d+,\d{1,2}", digits):
        digits = digits.replace(",", ".")
    digits = digits.replace(" ", "")
    if not re.fullmatch(r"\d+(?:\.\d+)?", digits):
        return None
    if "." in digits:
        digits = digits.rstrip("0").rstrip(".")
    digits = digits.lstrip("0") or "0"
    if sum(c.isdigit() for c in digits) < 2 and not unit:
        return None
    return digits


def _numbers(text: str) -> set[str]:
    return {n for m in NUMBER.finditer(text) if (n := _number(m.group(0)))}


def _fold(text: str) -> str:
    return squash(text.replace("’", "'").replace("‘", "'")).casefold()


def ground(result: AltResult, ctx: FigureContext) -> AltResult:
    """The result with uncertain=True when its alt or description states a number, a date or a
    quotation that the context (caption, heading, nearby paragraph, printed_nearby) does not
    hold. Never edits the text. It is a narrow check: a colour, a count of people or what a
    photo shows is not checked, and every draft is still reviewed."""
    if result.decorative:
        return result
    context = " ".join((ctx.caption, ctx.heading, ctx.paragraph, ctx.printed_nearby))
    draft = f"{result.alt} {result.description}"
    held_dates, context_rest = _dates(context)
    held = _numbers(context_rest) | {str(v) for d in held_dates for v in d}
    dates, draft_rest = _dates(draft)
    folded = _fold(context)
    ok = (dates <= held_dates and _numbers(draft_rest) <= held
          and all(_fold(q) in folded for q in QUOTE.findall(draft)))
    return result if ok else replace(result, uncertain=True)


def cache_key(image: bytes, ctx: FigureContext, provider: Provider) -> str:
    h = hashlib.sha256(image)
    h.update(f"\0{provider.cache_id}\0{PROMPT_VERSION}\0{ctx.as_text()}".encode())
    return h.hexdigest()


def enrich_figures(doc: Document, images: dict[str, Path], provider: Provider,
                   cache_path: Path | None = None,
                   on_progress: Callable[[int, int], None] | None = None) -> dict:
    """Draft only figures flagged as missing alt text, preserving authored descriptions."""
    stats = {"described": 0, "decorative": 0, "cached": 0, "failed": 0, "errors": []}
    if isinstance(provider, NoneProvider):
        return stats
    cache: dict[str, dict] = {}
    if cache_path and cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cache = {}

    contexts = figure_contexts(doc)
    figures = [n for n in doc.root.walk()
               if n.role == Role.FIGURE and n.id in images and n.id in contexts
               and not n.attrs.get("alt") and not n.attrs.get("decorative")
               and not n.attrs.get("unread")
               and (n.attrs.get("needs") == "alt"
                    or any(f.code == "alt-missing" for f in n.facts))]
    jobs = []
    for node in figures:
        path = images[node.id]
        image = path.read_bytes()
        jobs.append((node, image, _media_type(path), contexts[node.id],
                     cache_key(image, contexts[node.id], provider)))

    def run(job) -> tuple[Node, str, AltResult | Exception, bool]:
        node, image, media_type, ctx, key = job
        if key in cache:
            return node, key, AltResult(**cache[key]), True
        try:
            return node, key, provider.describe(image, media_type, ctx), False
        except ProviderError as e:
            return node, key, e, False

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, provider.workers)) as pool:
        # each job in a copy of this context, so the GPU lease's wait reaches the step's timing
        copies = [contextvars.copy_context() for _ in jobs]
        for node, key, result, cached in pool.map(lambda c, j: c.run(run, j), copies, jobs):
            done += 1
            if on_progress:
                on_progress(done, len(jobs))
            if isinstance(result, Exception):
                stats["failed"] += 1
                stats["errors"].append(f"{node.id}: {result}")
                continue
            # the one boundary every answer crosses, fresh or replayed from the cache
            apply_result(node, ground(result, contexts[node.id]), provider.confidence)
            cache[key] = asdict(result)
            stats["cached" if cached else "described"] += 1
            stats["decorative"] += result.decorative

    if cache_path:
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return stats


@dataclass(frozen=True)
class Described:
    """What the figure pass produced: the crops for the renderer, and what alt text did."""
    files: Figures
    provider: str
    stats: dict[str, int] = field(default_factory=dict)   # described, cached, decorative, failed, skipped
    errors: list[str] = field(default_factory=list)


def describe(run: Run, provider: Provider) -> Described:
    """Crop every FIGURE with a bbox to stage/FIGURES_DIR (pdfium, under run.workers.pdfium),
    then enrich_figures with `provider` and the carried alt-text cache. The alt attributes
    land on the nodes; the files are returned for the renderer. The one pass besides publish
    that writes files, because the crops must exist before alt text runs."""

    nodes = [n for n in run.doc.root.walk() if n.role == Role.FIGURE and n.bbox]
    names = [(n.page, n.bbox, f"p{n.page:04d}-{i}.webp") for i, n in enumerate(nodes)]
    out = run.stage / FIGURES_DIR
    with run.step("figures"):
        sizes = render_figures(run.working, names, out, executor=run.workers.pool,
                               jobs=run.workers.jobs, lock=run.workers.pdfium) if names else {}
    files: Figures = {n.id: (name, *sizes[name]) for n, (_, _, name) in zip(nodes, names)}
    for n in nodes:   # the picture's identity, for a reviewer's decision to find it again
        n.attrs["crop_sha"] = crop_sha((out / files[n.id][0]).read_bytes())
    label = f"Describing figures ({provider.name})"
    crops = {i: out / f[0] for i, f in files.items()}
    with run.step("alt_text"):
        skipped = mark_roles(run.doc, crops)          # never sent to a model
        stats = enrich_figures(run.doc, crops, provider, cache_path=run.stage / ALT_CACHE,
                               on_progress=lambda d, n: run.progress(label, d, n))
        store_roles(run.doc)
    stats["skipped"] = sum(skipped.values())
    errors = stats.pop("errors")
    return Described(files, provider.name, stats, errors)


def _media_type(path: Path) -> str:
    return {".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".gif": "image/gif"}.get(path.suffix.lower(), "image/png")
