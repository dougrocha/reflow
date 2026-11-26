"""The document's language and text direction, for <html lang> and dir.

The PDF's /Lang is whatever its author typed: "en-US", "EN", "en_US", "Korean" or nothing.
A well-formed BCP 47 tag is kept as written, a language name is mapped to its tag, and
when neither works the letters of the text decide: Hangul, Arabic or Hebrew script each
name one language unambiguously, and Han script names Chinese (Traditional or Simplified) or,
with kana beside it, Japanese. Latin-script text is read by its function words, which
can fill in a missing /Lang or override one the text clearly contradicts, pooled and block
by block (recorded as a review item). So does a document that is mostly one of those
scripts, under a /Lang for a language that is not written in it (a Chinese summary of
benefits tagged en-US). Anything else keeps the default, "en".
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from reflow.model import Document, Role

DEFAULT_LANG = "en"

# A primary language subtag of two or three letters (ISO 639), then subtags of up to eight
# characters. Five-to-eight-letter primary subtags ("Korean") are registered-only, so they
# are not tags: they fall through to the name table.
_TAG = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{1,8})*$", re.IGNORECASE)

# English names of common languages, as they appear in /Lang values.
LANGUAGE_NAMES = {
    "arabic": "ar", "bengali": "bn", "burmese": "my", "chinese": "zh", "czech": "cs",
    "danish": "da", "dutch": "nl", "english": "en", "farsi": "fa", "filipino": "fil",
    "finnish": "fi", "french": "fr", "german": "de", "greek": "el", "haitian creole": "ht",
    "hebrew": "he", "hindi": "hi", "hungarian": "hu", "indonesian": "id", "italian": "it",
    "japanese": "ja", "khmer": "km", "korean": "ko", "lao": "lo", "malay": "ms",
    "norwegian": "no", "persian": "fa", "polish": "pl", "portuguese": "pt",
    "punjabi": "pa", "romanian": "ro", "russian": "ru", "serbian": "sr", "somali": "so",
    "spanish": "es", "swahili": "sw", "swedish": "sv", "tagalog": "tl", "tamil": "ta",
    "thai": "th", "turkish": "tr", "ukrainian": "uk", "urdu": "ur", "vietnamese": "vi",
    "yiddish": "yi",
}

# Scripts are matched per letter by code point range. The first range that holds a letter
# names its script; letters outside every range count as "latin" (they are alphabetic,
# and Latin is the script of everything else in practice).
_SCRIPT_RANGES = [
    ("hangul", ((0x1100, 0x11FF), (0x3130, 0x318F), (0xA960, 0xA97F), (0xAC00, 0xD7FF))),
    ("arabic", ((0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF),
                (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))),
    ("hebrew", ((0x0590, 0x05FF), (0xFB1D, 0xFB4F))),
    ("syriac", ((0x0700, 0x074F),)),
    ("thaana", ((0x0780, 0x07BF),)),
    ("greek", ((0x0370, 0x03FF), (0x1F00, 0x1FFF))),
    ("cyrillic", ((0x0400, 0x04FF),)),
    ("thai", ((0x0E00, 0x0E7F),)),
    ("devanagari", ((0x0900, 0x097F),)),
    ("gujarati", ((0x0A80, 0x0AFF),)),
    ("khmer", ((0x1780, 0x17FF), (0x19E0, 0x19FF))),
    ("kana", ((0x3040, 0x30FF),)),
    ("han", ((0x3400, 0x4DBF), (0x4E00, 0x9FFF))),
]

# Right-to-left scripts, and the languages that are written in them.
RTL_SCRIPTS = frozenset({"arabic", "hebrew", "syriac", "thaana"})
RTL_LANGS = frozenset({"ar", "ckb", "dv", "fa", "he", "iw", "ps", "sd", "syr", "ug", "ur", "yi"})

# A script that names one language when a document has no usable /Lang (Han is read by
# script_language: Chinese or Japanese, Traditional or Simplified). Cyrillic, Devanagari and
# Syriac are left out: each is written by several languages.
SCRIPT_LANG = {"hangul": "ko", "arabic": "ar", "hebrew": "he", "greek": "el", "thai": "th",
               "gujarati": "gu", "khmer": "km"}

# The primary language subtags that are written in each of those scripts. A /Lang outside
# the set contradicts a text that is mostly in the script.
SCRIPT_LANGS = {
    "hangul": frozenset({"ko"}),
    "arabic": frozenset({"ar", "ckb", "fa", "ps", "sd", "ug", "ur"}),
    "hebrew": frozenset({"he", "iw", "yi"}),
    "han": frozenset({"zh", "yue", "ja"}),
}

# The share of a document's letters, and the least number of them, that one non-Latin script
# needs before it outranks a /Lang written for another language.
STRONG_SCRIPT_SHARE = 0.6
MIN_SCRIPT_LETTERS = 40

# The languages that are not written in the Latin script: a text that is nearly all Latin
# letters cannot be in one of them, however few words it has. Cyrillic and Devanagari
# languages are left out with their scripts (Serbian is written in both Cyrillic and Latin).
NON_LATIN_LANGS = frozenset(SCRIPT_LANG.values()).union(*SCRIPT_LANGS.values())
# The most of a document's letters that the /Lang's own script may hold when the rest are
# Latin and the /Lang is still contradicted: a Japanese document is far more Japanese than this.
MAX_DECLARED_SCRIPT_SHARE = 0.05
# A short Latin text that reads as another language by at least this many function words, and
# by none of the /Lang's, is too short to overrule the /Lang but says enough to flag it.
MIN_SHORT_STOPWORDS = 3

# Characters of common words that Traditional and Simplified Chinese write differently, as
# pairs in the same order. Whichever set a text uses more names its script.
_TRADITIONAL = "這們為說開關體學會與電時國後務險費醫藥計資類項點從對應該買單據員際網號處義療內長當發實經師認證無請聯條擔擇選價額"
_SIMPLIFIED = "这们为说开关体学会与电时国后务险费医药计资类项点从对应该买单据员际网号处义疗内长当发实经师认证无请联条担择选价额"
if not (len(_TRADITIONAL) == len(_SIMPLIFIED)):
    raise RuntimeError('language: expected len(_TRADITIONAL) == len(_SIMPLIFIED)')
_HAN_VARIANT = {**{c: "zh-Hant" for c in _TRADITIONAL}, **{c: "zh-Hans" for c in _SIMPLIFIED}}
# Characters in both sets of pairs (会, 后...) say nothing about the script.
for _c in set(_TRADITIONAL) & set(_SIMPLIFIED):
    del _HAN_VARIANT[_c]


def normalize_tag(value: str | None) -> str | None:
    """A BCP 47 tag for a /Lang value, or None when it names no language.

    Well-formed tags are kept as written ("en-US" stays "en-US"). Underscores and spaces
    stand in for hyphens ("en_US" becomes "en-US"). A language name maps to its tag, with
    any parenthesised qualifier dropped ("Korean (South Korea)" becomes "ko").
    """
    if not value:
        return None
    s = value.strip()
    if not s:
        return None
    name = re.sub(r"\s*\(.*\)$", "", s).strip().lower()
    if name in LANGUAGE_NAMES:
        return LANGUAGE_NAMES[name]
    s = re.sub(r"[\s_]+", "-", s)
    return s if _TAG.match(s) else None


def script_of(ch: str) -> str | None:
    """The script of a letter, or None for anything that is not a letter."""
    if not ch.isalpha():
        return None
    if ch < "Ͱ":  # below every script range in _SCRIPT_RANGES
        return "latin"
    code = ord(ch)
    for name, ranges in _SCRIPT_RANGES:
        if any(lo <= code <= hi for lo, hi in ranges):
            return name
    return "latin"


_ASCII_LETTER = re.compile(r"[A-Za-z]")


def script_language(script: str | None, texts: Iterable[str]) -> str | None:
    """The language the text's dominant script names, or None when it names none. Han script
    is Japanese when it has kana beside it, else Chinese: zh-Hant, zh-Hans, or zh when the
    text uses neither set of variant characters more."""
    if script in SCRIPT_LANG:
        return SCRIPT_LANG[script]
    if script != "han":
        return None
    variants: Counter = Counter()
    letters: Counter = Counter()
    for text in texts:
        for ch in text:
            if variant := _HAN_VARIANT.get(ch):
                variants[variant] += 1
            if kind := script_of(ch):
                letters[kind] += 1
    if letters["kana"] >= 0.1 * (letters["han"] + letters["kana"]) and letters["kana"] >= 3:
        return "ja"
    ranked = variants.most_common(2)
    if ranked and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
        return ranked[0][0]
    return "zh"


def count_scripts(texts: Iterable[str]) -> Counter:
    counts: Counter = Counter()
    for text in texts:
        if text.isascii():  # most blocks: count their letters without a per-character loop
            counts["latin"] += len(_ASCII_LETTER.findall(text))
            continue
        for ch in text:
            script = script_of(ch)
            if script:
                counts[script] += 1
    return counts


def is_rtl_text(text: str) -> bool:
    if text.isascii():
        return False
    counts = count_scripts([text])
    rtl = sum(counts[s] for s in RTL_SCRIPTS)
    return rtl > 0 and rtl > sum(counts.values()) - rtl


def is_ltr_text(text: str) -> bool:
    """True when the text has letters and is not mainly in a right-to-left script."""
    return any(c.isalpha() for c in text) and not is_rtl_text(text)


def _text_of(doc: Document) -> Iterable[str]:
    for node in doc.root.walk():
        if node.role == Role.ARTIFACT:
            continue
        text = node.text or " ".join(node.attrs.get("lines") or [])
        if text:
            yield text


# Function words that name one Latin-script language. A word that is common to two of the
# languages (la, de, con, para, como, ...) is left out, so it can never decide the language.
STOPWORDS = {
    "en": {"the", "and", "of", "to", "is", "are", "with", "for", "that", "this", "from", "by",
           "be", "which", "you", "your", "not", "have", "will", "at", "it", "on", "were",
           "their", "they", "can", "more", "these", "when", "about", "if", "into", "than",
           "there", "been", "should", "must", "our"},
    "es": {"el", "los", "las", "es", "son", "pero", "sus", "usted", "ustedes", "también",
           "puede", "pueden", "muy", "entre", "sobre", "hay", "tiene", "cuando", "según",
           "este", "esta", "estos", "estas", "más", "desde", "ese", "esa", "sin", "todos", "y"},
    "fr": {"le", "les", "des", "et", "est", "une", "du", "dans", "pour", "avec", "qui", "sur",
           "pas", "sont", "vous", "nous", "cette", "aux", "ces", "leur", "leurs", "ou",
           "ce", "je", "elle", "aussi", "être"},
    "de": {"der", "die", "und", "ist", "nicht", "mit", "das", "sie", "ein", "eine", "zu", "auf",
           "für", "auch", "sich", "dem", "den", "im", "werden", "wird", "oder", "von", "aber",
           "wie", "bei", "sind", "nach", "durch"},
    "pt": {"não", "uma", "são", "você", "também", "ao", "pelo", "pela", "seu", "sua", "seus",
           "suas", "com", "é", "ser", "foi", "pode"},
    "it": {"il", "gli", "della", "delle", "nella", "nelle", "sono", "anche", "questo", "questa",
           "degli", "dello", "perché", "molto", "nel", "sul", "che", "per", "non"},
}
_LANG_OF_WORD = {word: lang for lang, words in STOPWORDS.items() for word in words}
_WORD = re.compile(r"[^\W\d_]+")
# A function word stands alone: the "com" of azblue.com or of an email address, and the "non" of
# "Non-Participating", are pieces of another word, not Portuguese or Italian.
_FUNCTION_WORD = re.compile(r"(?<![\w@/.-])[^\W\d_]+(?![\w@/-]|\.\w)")

# A detection needs this many function words, and the winner must have at least this many
# times the count of the runner-up. Short or mixed text stays undetected.
MIN_STOPWORDS = 8
MARGIN = 3

# Require repeated block-level evidence before tagging a short translated heading.
MIN_SECOND_LANGUAGE_BLOCKS = 2


def detect_latin_language(texts: Iterable[str], *, min_stopwords: int = MIN_STOPWORDS) -> str | None:
    """The language of Latin-script text by its function words, or None when unclear."""
    hits: Counter = Counter()
    for text in texts:
        for word in _FUNCTION_WORD.findall(text.lower()):
            lang = _LANG_OF_WORD.get(word)
            if lang:
                hits[lang] += 1
    ranked = hits.most_common(2)
    if not ranked:
        return None
    (top, n), *rest = ranked
    second = rest[0][1] if rest else 0
    return top if n >= min_stopwords and n >= MARGIN * second else None


# What a short block says about its own language: the language's name in that language.
# "Si usted habla español" names Spanish, in Spanish, and a Portuguese or German tagline
# carries too few function words to say so without it.
SELF_NAMES = {"es": {"español"}, "fr": {"français"}, "de": {"deutsch"}, "pt": {"português"},
              "it": {"italiano"}}
_LANG_OF_NAME = {name: lang for lang, names in SELF_NAMES.items() for name in names}

# The least number of letters in one word, and the share of the block's letters in one script,
# before the script names the block's language: three Greek letters of a formula are no text. A bare Chinese reading (no kana, no variant
# characters) needs more of them, because a few kanji could as well be Japanese.
MIN_SCRIPT_LETTERS_IN_BLOCK = 3
MIN_SCRIPT_SHARE_IN_BLOCK = 0.5
MIN_BARE_HAN_LETTERS = 8
# A Latin-script reading needs nearly all of the block's letters to be Latin: a block that
# carries a second script (two taglines extracted as one) is not read as one language.
MIN_LATIN_SHARE = 0.9
# A block on a one-language page takes the page's language unless the function words of other
# languages in it outnumber the page language's by more than this factor.
PAGE_LANG_OTHERS = 2
MIN_PAGE_LANG_WORD = 4


def _dominant_script(text: str) -> tuple[str | None, int, int]:
    """The script holding most of a block's letters (kana counts as Han), its letter count,
    and the block's total."""
    counts = count_scripts([text])
    if "kana" in counts:
        counts["han"] += counts.pop("kana")
    if not counts:
        return None, 0, 0
    script, n = counts.most_common(1)[0]
    return script, n, sum(counts.values())


def _is_latin_text(text: str) -> bool:
    script, n, total = _dominant_script(text)
    return script == "latin" and n >= MIN_LATIN_SHARE * total


def _has_word(text: str) -> bool:
    """Whether the block has a word of four letters or more: not a form number or a code."""
    return any(len(word) >= MIN_PAGE_LANG_WORD for word in _WORD.findall(text))


def _script_block_language(text: str) -> str | None:
    """The language a block's script names, or None when the script is shared or too thin."""
    script, n, total = _dominant_script(text)
    if (script in (None, "latin") or script in RTL_SCRIPTS or n < MIN_SCRIPT_SHARE_IN_BLOCK * total
            or not any(sum(map(str.isalpha, word)) >= MIN_SCRIPT_LETTERS_IN_BLOCK
                       for word in text.split())):
        return None
    tag = script_language(script, [text])
    return None if tag == "zh" and n < MIN_BARE_HAN_LETTERS else tag


def _latin_block_language(text: str) -> str | None:
    """The Latin-script language of one block. Short blocks need four function words; a block
    that names its own language needs one of that language's and none of any other's."""
    words = _FUNCTION_WORD.findall(text.lower())
    minimum = max(4, MIN_STOPWORDS // 2) if len(words) < 40 else MIN_STOPWORDS
    tag = detect_latin_language([text], min_stopwords=minimum)
    if tag:
        return tag
    named = {_LANG_OF_NAME[w] for w in words if w in _LANG_OF_NAME}
    if len(named) != 1:
        return None
    (named_lang,) = named
    hits = {_LANG_OF_WORD[w] for w in words if w in _LANG_OF_WORD}
    return named_lang if hits == {named_lang} else None


def block_language(text: str, doc_lang: str, ltr_lang: str | None,
                   page_lang: str | None = None) -> str | None:
    """A block's language when it differs from the document's primary language.

    A block in a script that names one language (Hangul, Greek, Thai, Han, Gujarati, Khmer)
    gets that language. A Latin-script block is read by its function words. RTL documents
    keep the pooled language for their LTR blocks, including short headings. A block that
    says nothing of its own takes `page_lang`, the language of a page that reads as one
    language, unless it holds clearly more function words of other languages than of that one.
    """
    if not is_ltr_text(text):
        return None
    primary = doc_lang.split("-", 1)[0].lower()
    tag = _script_block_language(text)
    if tag is None:
        if primary in RTL_LANGS or ltr_lang:
            tag = ltr_lang
        elif _is_latin_text(text):
            tag = _latin_block_language(text)
            if tag is None and page_lang and _has_word(text):
                hits = Counter(_LANG_OF_WORD[w] for w in _FUNCTION_WORD.findall(text.lower())
                               if w in _LANG_OF_WORD)
                others = sum(hits.values()) - hits[page_lang]
                tag = page_lang if others <= PAGE_LANG_OTHERS * hits[page_lang] else None
    return tag if tag and tag.split("-", 1)[0].lower() != primary else None


@dataclass(frozen=True)
class LanguageChoice:
    tag: str                 # for <html lang>
    rtl: bool                # the document is right-to-left overall
    declared: str | None     # the /Lang tag, when it names a language
    detected: str | None     # the language the text reads as, when clear (or, unsure, likely)
    issue: str | None        # "language-missing" or "language-mismatch", when the text decided
    unsure: bool = False     # the text is too short to decide: the /Lang is published, flagged


# The last answer of resolve_language: (the texts, the /Lang it saw, the choice).
_resolved: tuple[tuple[str, ...], object, LanguageChoice] | None = None


def _strong_script(counts: Counter, script: str | None) -> bool:
    """Whether one non-Latin script holds most of the document's letters."""
    total = sum(counts.values())
    return (script in SCRIPT_LANGS and counts[script] >= MIN_SCRIPT_LETTERS
            and counts[script] >= STRONG_SCRIPT_SHARE * total)


def _written_in(detected: str, declared: str, script: str | None) -> bool:
    """Whether the /Lang is a language the text can be in: the detected language's primary
    subtag, or any language written in the dominant script."""
    primary = declared.split("-", 1)[0].lower()
    return primary == detected.split("-", 1)[0].lower() or primary in SCRIPT_LANGS.get(script, ())


def _latin_not_declared(counts: Counter, declared: str) -> bool:
    """Whether the text is in the Latin script and the /Lang names a language that is not
    written in it: /Lang ja over English text. The /Lang's own script holds almost none of the
    letters, so it is not a Japanese document with some English in it."""
    primary = declared.split("-", 1)[0].lower()
    if primary not in NON_LATIN_LANGS:
        return False
    total = sum(counts.values())
    scripts = {script for script, langs in SCRIPT_LANGS.items() if primary in langs}
    scripts |= {script for script, lang in SCRIPT_LANG.items() if lang == primary}
    own = sum(counts[script] for script in scripts)
    return counts["latin"] >= STRONG_SCRIPT_SHARE * total and own <= MAX_DECLARED_SCRIPT_SHARE * total


def _short_text_language(texts: Iterable[str], declared: str) -> str | None:
    """The language a text too short to detect leans to, when it is not the /Lang's: at least
    MIN_SHORT_STOPWORDS function words of one language, by the usual margin, and none of the
    /Lang's. None when the /Lang is a language the function words cannot see."""
    primary = declared.split("-", 1)[0].lower()
    if primary not in STOPWORDS:
        return None
    texts = list(texts)
    words = [w for text in texts for w in _FUNCTION_WORD.findall(text.lower())]
    if any(_LANG_OF_WORD.get(w) == primary for w in words):
        return None
    return detect_latin_language(texts, min_stopwords=MIN_SHORT_STOPWORDS)


def _blocks_agree(texts: Iterable[str], detected: str, declared: str) -> bool:
    """Whether more blocks read as the detected language by themselves than as the declared
    one. Pooled words can come from names ("Clinica El Rio", "Los Arcos") that no block reads
    as a language: they do not outvote the sentences that read as the /Lang."""
    primary = declared.split("-", 1)[0].lower()
    own = Counter(_latin_block_language(text) for text in texts if _is_latin_text(text))
    as_declared = sum(n for tag, n in own.items() if tag and tag.split("-", 1)[0] == primary)
    return own[detected] > as_declared


def resolve_language(doc: Document) -> LanguageChoice:
    """The language to publish, and what the PDF said about it.

    A usable /Lang is kept unless the text clearly reads as another Latin-script language
    and more of its blocks read as that language by themselves than as the /Lang, in which
    case the text wins and the mismatch is reported. A /Lang for a language that is not written
    in the Latin script loses to a text of at least MIN_SCRIPT_LETTERS Latin letters however
    few words it has: the detected language wins, else "en", as when there is no /Lang. Below
    that, or when a short Latin text leans to another Latin-script language, the /Lang is kept
    and the mismatch reported as unsure. Without a usable /Lang the text decides when it can,
    else the script decides, else "en".
    """
    global _resolved
    texts = tuple(_text_of(doc))
    # review.queue and render_html both ask, about the same text: the second ask is the first's.
    last = _resolved
    if last is not None and last[0] == texts and last[1] == doc.meta.get("lang"):
        return last[2]
    counts = count_scripts(texts)
    if "kana" in counts:
        counts["han"] += counts.pop("kana")  # Japanese writes both: one script for the decision
    dominant = counts.most_common(1)[0][0] if counts else None
    declared = normalize_tag(doc.meta.get("lang"))
    detected = detect_latin_language(texts) if dominant == "latin" else None
    by_script = script_language(dominant, texts)
    issue, unsure = None, False
    if declared is None:
        if detected:
            tag, issue = detected, "language-missing"
        else:
            tag = by_script or DEFAULT_LANG
    elif (detected and not _written_in(detected, declared, dominant)
          and _blocks_agree(texts, detected, declared)):
        tag, issue = detected, "language-mismatch"
    elif (by_script and _strong_script(counts, dominant)
          and not _written_in(by_script, declared, dominant)):
        tag, issue, detected = by_script, "language-mismatch", by_script
    elif dominant == "latin" and _latin_not_declared(counts, declared):
        issue = "language-mismatch"
        if counts["latin"] >= MIN_SCRIPT_LETTERS:
            tag = detected or DEFAULT_LANG
        else:
            tag, unsure = declared, True
    elif (dominant == "latin" and detected is None
          and (lean := _short_text_language(texts, declared))
          and not _written_in(lean, declared, dominant)):
        tag, issue, detected, unsure = declared, "language-mismatch", lean, True
    else:
        tag = declared
    primary = tag.split("-", 1)[0].lower()
    rtl = primary in RTL_LANGS or dominant in RTL_SCRIPTS
    choice = LanguageChoice(tag, rtl, declared, detected, issue, unsure)
    _resolved = (texts, doc.meta.get("lang"), choice)
    return choice


def ltr_block_language(doc: Document) -> str | None:
    """The language of the left-to-right blocks of a right-to-left document, or None.

    The blocks are pooled, so a short heading ("HEPATITIS B") is read with the English
    sentences around it. Only a confident detection counts: a wrong language is worse than
    none for a screen reader.
    """
    return detect_latin_language(text for text in _text_of(doc) if is_ltr_text(text))


def page_languages(doc: Document, doc_lang: str) -> dict[int, str]:
    """The language of each page that reads as one language other than the document's.

    The page's Latin-script text, pooled, must read as that language, at least two of its
    blocks must read as it by themselves (a directory's names pool into a language they are
    not in), and no block of the page may read as another one by itself, in words or in script (an English paragraph on a Spanish page makes it
    a mixed page, which keeps only its per-block tags). A page of a Spanish notice in an
    English document then tags its short headings and list items too, which carry too few
    function words to say so alone.
    """
    primary = doc_lang.split("-", 1)[0].lower()
    by_page: dict[int, list[str]] = {}
    mixed: set[int] = set()  # pages with a block in a script that names another language
    for node in doc.root.walk():
        text = node.text if node.role != Role.ARTIFACT else None
        if not (text and node.page is not None and is_ltr_text(text)):
            continue
        if _is_latin_text(text):
            by_page.setdefault(node.page, []).append(text)
        elif _script_block_language(text):
            mixed.add(node.page)
    langs: dict[int, str] = {}
    for page, texts in by_page.items():
        pooled = detect_latin_language(texts)
        if not pooled or pooled.split("-", 1)[0].lower() == primary or page in mixed:
            continue
        own = Counter(_latin_block_language(text) for text in texts)
        del own[None]
        if set(own) == {pooled} and own[pooled] >= MIN_SECOND_LANGUAGE_BLOCKS:
            langs[page] = pooled
    return langs


def second_block_language(doc: Document, doc_lang: str, ltr_lang: str | None = None) -> str | None:
    """Most common non-primary language detected across document blocks, with repetition."""
    counts = Counter(tag for text in _text_of(doc)
                     if is_ltr_text(text) and _is_latin_text(text)
                     and (tag := block_language(text, doc_lang, ltr_lang)) is not None)
    if not counts:
        return None
    tag, count = counts.most_common(1)[0]
    return tag if count >= MIN_SECOND_LANGUAGE_BLOCKS else None


def document_language(doc: Document) -> tuple[str, bool]:
    """(BCP 47 tag for <html lang>, whether the document is right-to-left overall)."""
    choice = resolve_language(doc)
    return choice.tag, choice.rtl
