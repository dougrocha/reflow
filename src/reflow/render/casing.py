"""Sentence case for display only (docs/display-case.md).

A heading, a form label or a short run printed in capitals reads more easily in sentence case.
The words stay as printed: the renderer keeps them in the DOM, so the download, a copy and a
screen reader get the capitals. It puts the sentence-case form beside them in `data-sc`, and the
stylesheet draws that form instead (`content: attr(data-sc) / ""`, which screen readers skip).

The form is safe or it is not given. A word takes the case the document itself prints it in
elsewhere ("ASSESSMENT" → "assessment", "SAN" → "San"), or lower case when it is a common word.
A word with a digit (W-4, D430), a known acronym (EIN, USCIS) or a short word the document never
prints otherwise (CA, LLC) keeps its capitals. A longer word the document never prints otherwise
leaves the whole run as printed, since it may be a name or an acronym."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

LETTERS = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")
APOSTROPHE = re.compile(r"(.+?)(['’].*)")
TOKEN = re.compile(r"\S+")
RUN_MAX_WORDS = 8          # a paragraph past this is body text, left as printed
LABEL_MAX_WORDS = 24       # a heading or label past this is left as printed
SHORT = 3                  # an unknown word this short is taken for an acronym and kept

# Acronyms that are also, or look like, words: always kept in capitals.
KEEP = frozenset("""
    US USA UK EU UN ID IT HR PO IRS SSN EIN ITIN TIN LLC LLP INC DBA FAQ PDF CEO CFO ADA HIPAA
    COVID USCIS FEMA HUD DMV NPI CPT ICD HMO PPO EPO HSA FSA ACA AZ CA NY TX FL OK DC TV AM PM
    ER ICU ADHD AIDS HIV GED SAT ACT IEP USDA OSHA EEOC FBI CIA NASA NOAA EPA DOT DOE GAAP
""".split())

# Common words, in lower case, for a word the document prints only in capitals.
COMMON = frozenset("""
    a about above after again against all also am an and any are as at be because been before
    being below between both but by can could did do does doing down during each else every few
    for from further had has have having he her here hers him his how i if in into is it its
    just may me might more most must my no nor not now of off on once only or other our out over
    own same shall she should so some such than that the their them then there these they this
    those through to too under until up upon very was we were what when where which while who
    whom why will with within without would yes you your yours
    account action additional address agency agent amount annual answer appeal applicant
    application apply approval area assessment attach attachment authorization benefit benefits
    bill birth board box business call card care case certification change check city claim
    clerk code complete completed contact copy county coverage current date day days department
    description details district document documents due email employee employer enter fax fee
    file filing find first follow form full health help home important include income
    information instructions insurance last line list location mail mailing member middle month
    name new next no notice number office only optional other owner page paid part pay payment
    period person phone place plan please policy print program property provide provider
    questions read reason receive record report request required review section see service
    services sign signature social state statement status street submit summary tax telephone
    total type use value year years zip identification relative representation construction
    ownership reassessment applicable contents introduction general frequently asked eligibility
    emergency medical patient doctor hospital prescription pharmacy dental vision family children
    child spouse parent signed agreement terms conditions notes note remember important warning
    initial trust alternate daytime evening cell mobile work apartment suite unit relationship
    single married male female use clerk
""".split())

_END = (".", ":", "?", "!")


def in_capitals(text: str) -> bool:
    """Two capital letters or more and no lower-case one."""
    return sum(c.isupper() for c in text) >= 2 and not any(c.islower() for c in text)


class Casing:
    """The case each word takes in the document's own text, and the display form of a run."""

    def __init__(self, texts: Iterable[str]):
        self.lower: Counter[str] = Counter()
        self.title: Counter[str] = Counter()
        for text in texts:
            prev = ""
            for token in TOKEN.findall(text or ""):
                start = not prev or prev.endswith(_END)
                for word in LETTERS.findall(token):
                    if word.islower():
                        self.lower[word] += 1
                    elif not start and word[0].isupper() and word[1:].islower() and len(word) > 1:
                        self.title[word.lower()] += 1   # mid-sentence: a name
                prev = token

    def _word(self, word: str) -> str | None:
        """The display form of one word in capitals, or None when it is not safe."""
        if m := APOSTROPHE.fullmatch(word):     # ASSESSOR'S: the word, then its ending in lower case
            base = self._word(m[1])
            return None if base is None else base + m[2].lower()
        low = word.lower()
        if word in KEEP or len(word) == 1:
            return word
        if self.lower[low]:
            return low
        if self.title[low]:
            return low[0].upper() + low[1:]
        if low in COMMON:
            return low
        return word if len(word) <= SHORT else None

    def display(self, text: str, max_words: int) -> str | None:
        """`text` in sentence case, the same length as it, or None: when it is not wholly in
        capitals, has more than `max_words` words, holds a word that is not safe to recase, or
        would not change."""
        if not text or not in_capitals(text):
            return None
        tokens = list(TOKEN.finditer(text))
        if len(tokens) > max_words:
            return None
        out = list(text)
        first = True
        for m in tokens:
            token = m.group()
            if any(c.isdigit() for c in token):
                first = token.endswith(_END)
                continue    # W-4, D430, 1099-MISC, 2023: as printed
            for w in LETTERS.finditer(token):
                shown = self._word(w.group())
                if shown is None:
                    return None
                if first and shown != w.group():
                    shown = shown[0].upper() + shown[1:]
                first = False
                if len(shown) != len(w.group()):
                    return None
                out[m.start() + w.start():m.start() + w.end()] = shown
            if token.endswith(_END):
                first = True
        shown = "".join(out)
        return shown if shown != text and len(shown) == len(text) else None
