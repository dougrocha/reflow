"""What a text box asks for, read from its label.

The renderer gives a text box `type`, `autocomplete` and `inputmode` from this table, so that a
phone keyboard shows digits for a ZIP code and a browser can fill in the reader's own email
(WCAG 1.3.5, Identify Input Purpose). The table is the only place that knows the keywords.

Rules are deliberately narrow, because leaving an attribute out is always safe and a wrong one
is not:
- A rule matches the whole label, not a word in it: "City, state, and ZIP code" asks for three
  things, "Property address" is not the reader's own, and neither gets a token.
- `autocomplete` names the reader's own data. When the label, its tooltip or an enclosing
  heading is about someone else (an employer, an agent, a physician, a spouse...), the token is
  left out. The `type` and `inputmode` stay: they say what the answer looks like, not whose it is.
- `type` is set only for email and telephone numbers. Never `date` or `number`: they change
  the value format, and the filled PDF must get what the reader typed. A date gets no
  `inputmode` either: a numeric keypad (iOS) has no "/" to type "10/12/2024" with.
- A telephone box narrower than a whole number is one part of it (an area code or the local
  digits): it gets no `autocomplete`, as the full number would not fit.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from reflow.text_quality import squash


@dataclass(frozen=True)
class InputPurpose:
    type: str | None = None          # "email" or "tel"; None keeps type="text"
    autocomplete: str | None = None  # an HTML autofill token for the reader's own data
    inputmode: str | None = None     # the virtual keyboard


# Words about someone other than the reader. Any of them in the label, tooltip or an enclosing
# heading withholds the autocomplete token. Patient, insured and the like are here because the
# person filling in a form is often not the one it is about.
OTHER_PERSON = frozenset("""
    employer agent attorney lawyer physician doctor provider practitioner dentist pharmacy
    hospital clinic facility spouse dependent child children parent guardian caregiver
    beneficiary payee representative relative emergency witness notary preparer requester
    requestor patient insured subscriber policyholder guarantor sponsor landlord tenant
    referring referrer caller supervisor pcp nurse therapist specialist pharmacist other another third
""".split())

# Words that may come before the noun of a label: "Your email", "Daytime telephone".
_Q = r"(?:your|my|the|please|enter|primary|secondary|contact|best)"
_PHONE_Q = r"(?:day|daytime|evening|night|alternate|alt|home|work|business|office|cell|cellular|mobile|fax)"
_ADDRESS_Q = r"(?:street|mailing|home|residential|residence|physical)"

_RULES: list[tuple[re.Pattern[str], InputPurpose]] = [(re.compile(f"^(?:{pattern})$"), purpose) for pattern, purpose in [
    (rf"(?:{_Q} )*email(?: address)?",
     InputPurpose("email", "email", "email")),
    (rf"(?:{_Q} |{_PHONE_Q} )*(?:phone|telephone|tel|cellphone|cell|mobile|fax)(?: (?:number|no|phone|telephone))*",
     InputPurpose("tel", "tel", "tel")),
    (rf"(?:{_Q} )*(?:zip|zipcode)(?: code)?",
     InputPurpose(None, "postal-code", "numeric")),
    (rf"(?:{_Q} )*postal code",
     InputPurpose(None, "postal-code", None)),
    (rf"(?:{_Q} )*(?:city|town|city town|city or town)",
     InputPurpose(None, "address-level2", None)),
    (rf"(?:{_Q} )*(?:state|province|state province|state or province)",
     InputPurpose(None, "address-level1", None)),
    (rf"(?:{_Q} |{_ADDRESS_Q} )*address",  # not "Address line 1": a line of it
     InputPurpose(None, "street-address", None)),
    (rf"(?:{_Q} )*(?:first|given)(?: name)?",
     InputPurpose(None, "given-name", None)),
    (rf"(?:{_Q} )*(?:last|family)(?: name)?|surname",
     InputPurpose(None, "family-name", None)),
    (r"(?:(?:your|my|full|legal) )+name",
     InputPurpose(None, "name", None)),
    (rf"(?:{_Q} )*(?:date of birth|dob|birth date|birthdate|birthday|birth day)(?: mm dd yyyy| mm dd yy)?",
     InputPurpose(None, "bday", None)),
    (rf"(?:{_Q} )*(?:ssn|social security(?: number| no)?|ssn itin|ssn or itin|itin|ein|tin|"
     r"taxpayer identification number|employer identification number)(?: number| no)?",
     InputPurpose(None, None, "numeric")),
    (rf"(?:{_Q} )*(?:npi|national provider identifier)(?: number| no)?",
     InputPurpose(None, None, "numeric")),
    (r"(?:(?:total|dollar|payment) )*amount(?: paid| due| owed)?",
     InputPurpose(None, None, "decimal")),
]]
_FAX = re.compile(r"\bfax\b")
_AREA_CODE = re.compile(r"\barea code\b")
_WHOLE_NUMBER_PT = 80  # a telephone box at least this wide holds a whole number


def _normal(text: str) -> str:
    """Lower case, without the parenthetical hints, possessives and punctuation."""
    text = re.sub(r"\([^)]*\)", " ", text.lower())
    text = re.sub(r"^\W*\d+[a-z]?[.):]?\s+", "", text)   # a line number: "6 City", "1a. First name"
    text = re.sub(r"e[\s-]mail", "email", text)
    text = re.sub(r"['’]s\b", "", text)
    return squash(re.sub(r"[^a-z]+", " ", text))


def _all_words(text: str) -> str:
    """Lower case words, with the parenthetical hints kept ("Email (spouse)", "(area code)")."""
    return squash(re.sub(r"[^a-z]+", " ", text.lower()))


def _words(*texts: str | None) -> set[str]:
    out: set[str] = set()
    for text in texts:
        for word in _all_words(text or "").split():
            out.add(word)
            out.add(word.removesuffix("s"))
    return out


def _match(label: str) -> InputPurpose | None:
    words = _normal(label)
    if not words:
        return None
    for pattern, purpose in _RULES:
        if pattern.match(words):
            return purpose
    return None


def input_purpose(label: str | None, tooltip: str | None = None, aria: str | None = None, *,
                  width: float | None = None, context: Iterable[str] = ()) -> InputPurpose | None:
    """The purpose of the text box named by its printed `label` (else its `aria` name or
    `tooltip`), or None when nothing is certain. `context` is the text that names whose data
    this is: the headings the box sits under and the labels of the boxes before it. `width` is the box's width in points, when known."""
    texts = [t for t in (label, aria, tooltip) if t and t.strip()]
    if not texts:
        return None
    found = _match(texts[0])
    if found is None:
        return None
    # The other names a box has (its tooltip, say) must not say something else.
    for other in texts[1:]:
        again = _match(other)
        if again is not None and again != found:
            return None
    if found.type == "tel":
        every = " ".join(_all_words(t) for t in texts)
        if _FAX.search(every):
            found = InputPurpose("tel", None, "tel")
        elif _AREA_CODE.search(every):
            found = InputPurpose("tel", "tel-area-code", "numeric")
        elif width is not None and width < _WHOLE_NUMBER_PT:
            found = InputPurpose("tel", None, "tel")
    if found.autocomplete and OTHER_PERSON & _words(*texts, *context):
        found = InputPurpose(found.type, None, found.inputmode)
    return found
