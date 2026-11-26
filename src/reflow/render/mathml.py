"""The math in an AI-drafted reading, as MathML.

A model reads a crop of a formula as LaTeX between dollar signs. The text is untrusted: it is
parsed with a small grammar (the subset chemistry and arithmetic worksheets need: fractions,
roots, sub- and superscripts, Greek letters, degrees, arrows, upright text), and the markup is
written from the parsed tree through a fixed allow-list of MathML elements and attributes. A
formula the grammar does not read stays as the escaped text the model wrote. Nothing the model
wrote reaches the page as markup.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from markupsafe import Markup, escape

from reflow.render.links import linkify

# What the renderer may write: element -> its attributes -> the values each may take.
ALLOWED: dict[str, dict[str, frozenset[str]]] = {
    "math": {"display": frozenset({"block"})},
    "mrow": {}, "mn": {}, "mo": {}, "mtext": {}, "mfrac": {}, "msqrt": {}, "mroot": {},
    "msub": {}, "msup": {}, "msubsup": {}, "mover": {},
    "mi": {"mathvariant": frozenset({"normal"})},
}
MAX_SOURCE = 600   # characters of one formula; a longer one is not a formula a crop holds
MAX_DEPTH = 24
MAX_ELEMENTS = 400


class Unsupported(ValueError):
    """The text is not in the subset this module reads."""


@dataclass
class Element:
    tag: str
    children: list[Element | str] = field(default_factory=list)
    attrs: dict[str, str] = field(default_factory=dict)


def _write(element: Element, depth: int = 0) -> str:
    if depth > MAX_DEPTH or element.tag not in ALLOWED:
        raise Unsupported(element.tag)
    allowed = ALLOWED[element.tag]
    attrs = ""
    for name, value in element.attrs.items():
        if value not in allowed.get(name, frozenset()):
            raise Unsupported(f"{element.tag}@{name}")
        attrs += f' {name}="{value}"'
    body = "".join(escape(c) if isinstance(c, str) else _write(c, depth + 1)
                   for c in element.children)
    return f"<{element.tag}{attrs}>{body}</{element.tag}>"


# -- the grammar -------------------------------------------------------------------------------

TOKEN = re.compile(r"\s+|\\[A-Za-z]+|\\.|\d+(?:\.\d+)?|[A-Za-z]|.", re.S)

GREEK = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi pi rho sigma "
         "tau upsilon phi chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi "
         "Omega varepsilon vartheta varphi varrho varsigma").split()
LETTERS = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "zeta": "ζ",
    "eta": "η", "theta": "θ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν",
    "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ",
    "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ",
    "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ",
    "Omega": "Ω", "varepsilon": "ε", "vartheta": "ϑ", "varphi": "φ", "varrho": "ϱ",
    "varsigma": "ς", "infty": "∞", "partial": "∂", "ell": "ℓ",
}
OPERATORS = {
    "times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓", "circ": "°", "degree": "°",
    "bullet": "•", "ast": "*", "prime": "′", "%": "%", "$": "$", "&": "&", "#": "#",
    "{": "{", "}": "}", "to": "→", "rightarrow": "→", "leftarrow": "←", "longrightarrow": "→",
    "leftrightarrow": "↔", "Rightarrow": "⇒", "Leftarrow": "⇐", "Leftrightarrow": "⇔",
    "rightleftharpoons": "⇌", "uparrow": "↑", "downarrow": "↓", "leq": "≤", "le": "≤",
    "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠", "approx": "≈", "sim": "∼", "equiv": "≡",
    "propto": "∝", "ldots": "…", "cdots": "…", "dots": "…", "sum": "∑", "int": "∫",
    "langle": "⟨", "rangle": "⟩", "lbrace": "{", "rbrace": "}", "lvert": "|", "rvert": "|",
    "|": "‖", "mid": "|",
}
NAMES = {"log", "ln", "sin", "cos", "tan", "exp", "min", "max", "lim"}
SPACES = {",", ";", ":", " ", "quad", "qquad"}   # one no-break space in the text
TEXTS = {"text", "textrm", "textit", "textbf", "mbox", "textnormal"}
UPRIGHT = {"mathrm", "operatorname"}


class _Parser:
    def __init__(self, source: str) -> None:
        self.source = source
        self.tokens = [m for m in TOKEN.finditer(source) if not m.group().isspace()]
        self.at = 0
        self.count = 0

    def peek(self) -> str | None:
        return self.tokens[self.at].group() if self.at < len(self.tokens) else None

    def take(self) -> str:
        token = self.peek()
        if token is None:
            raise Unsupported("the formula ends early")
        self.at += 1
        return token

    def make(self, tag: str, *children: Element | str, **attrs: str) -> Element:
        self.count += 1
        if self.count > MAX_ELEMENTS:
            raise Unsupported("too many elements")
        return Element(tag, list(children), attrs)

    def row(self, depth: int, upright: bool, closing: str | None) -> list[Element]:
        if depth > MAX_DEPTH:
            raise Unsupported("too deep")
        out: list[Element] = []
        while (token := self.peek()) is not None and token != closing:
            if token == "}":
                raise Unsupported("a stray }")
            item = self.item(depth, upright)
            if item is not None:
                out.append(item)
        return out

    def group(self, depth: int, upright: bool) -> Element:
        """One `{...}` or one token, as a single element."""
        if self.peek() == "{":
            self.take()
            items = self.row(depth + 1, upright, "}")
            if self.peek() != "}":
                raise Unsupported("a { is not closed")
            self.take()
            return items[0] if len(items) == 1 else self.make("mrow", *items)
        item = self.item(depth, upright, scripts=False)
        if item is None:
            raise Unsupported("an argument is missing")
        return item

    def item(self, depth: int, upright: bool, scripts: bool = True) -> Element | None:
        base = self.atom(depth, upright)
        if base is None:
            return None
        sub = sup = None
        while scripts and self.peek() in ("^", "_"):
            mark = self.take()
            script = self.group(depth + 1, upright)
            if mark == "^" and sup is None:
                sup = script
            elif mark == "_" and sub is None:
                sub = script
            else:
                raise Unsupported("a double script")
        if sub and sup:
            return self.make("msubsup", base, sub, sup)
        if sub:
            return self.make("msub", base, sub)
        if sup:
            return self.make("msup", base, sup)
        return base

    def atom(self, depth: int, upright: bool) -> Element | None:
        token = self.take()
        if token == "{":
            self.at -= 1
            return self.group(depth, upright)
        if token in ("^", "_"):    # TeX's {}^\circ: a script after a space or at the start
            self.at -= 1
            return self.make("mrow")
        if token[0].isdigit():
            return self.make("mn", token)
        if len(token) == 1 and token.isalpha():
            return self.make("mi", token, **({"mathvariant": "normal"} if upright else {}))
        if token[0] == "\\":
            return self.command(token[1:], depth, upright)
        if token == "~":
            return self.make("mtext", "\u00a0")
        if token == "&":
            raise Unsupported(token)
        if token == "'":
            return self.make("mo", "′")
        if token.isalpha():       # a letter outside ASCII
            return self.make("mi", token)
        return self.make("mo", token)

    def raw(self) -> str:
        """The text of a balanced `{...}`, as written."""
        if self.take() != "{":
            raise Unsupported("text needs braces")
        start, depth = self.tokens[self.at - 1].end(), 1
        while depth:
            token = self.take()
            depth += (token == "{") - (token == "}")
            if token[0] == "\\" and token[1:] not in ("%", "&", "$", "#", "_"):
                raise Unsupported("a command inside text")
        return self.source[start:self.tokens[self.at - 1].start()].strip()

    def command(self, name: str, depth: int, upright: bool) -> Element | None:
        if name in LETTERS:
            return self.make("mi", LETTERS[name])
        if name in OPERATORS:
            return self.make("mo", OPERATORS[name])
        if name in NAMES:
            return self.make("mi", name)
        if name in SPACES:
            return self.make("mtext", "\u00a0")
        if name == "!":
            return None
        if name in ("frac", "dfrac", "tfrac"):
            return self.make("mfrac", self.group(depth + 1, upright), self.group(depth + 1, upright))
        if name == "sqrt":
            if self.peek() == "[":
                self.take()
                index = self.row(depth + 1, upright, "]")
                if self.peek() != "]":
                    raise Unsupported("a [ is not closed")
                self.take()
                return self.make("mroot", self.group(depth + 1, upright), self.make("mrow", *index))
            return self.make("msqrt", self.group(depth + 1, upright))
        if name in ("overline", "bar"):
            return self.make("mover", self.group(depth + 1, upright), self.make("mo", "\u00af"))
        if name == "begin":       # the one-cell array a boxed answer is written in
            if self.raw() != "array" or self.raw() not in ("c", "l", "r"):
                raise Unsupported("an environment")
            items = self.row(depth + 1, upright, "\\end")
            if self.take() != "\\end" or self.raw() != "array":
                raise Unsupported("an array is not closed")
            return self.make("mrow", *items)
        if name == "boxed":        # the box is not drawn; the answer in it is
            return self.group(depth + 1, upright)
        if name in TEXTS:
            return self.make("mtext", self.raw())
        if name in UPRIGHT:
            return self.group(depth + 1, True)
        if name in ("left", "right"):
            delimiter = self.take()
            if delimiter == ".":
                return None
            if delimiter[0] == "\\":
                return self.command(delimiter[1:], depth, upright)
            return self.make("mo", delimiter)
        raise Unsupported(f"\\{name}")


def to_mathml(latex: str, display: bool = False) -> Markup:
    """`latex` as a `<math>` element. Unsupported when it is not in the subset."""
    if len(latex) > MAX_SOURCE:
        raise Unsupported("too long")
    parser = _Parser(latex)
    items = parser.row(0, False, None)
    if not items:
        raise Unsupported("empty")
    return Markup(_write(Element("math", list(items), {"display": "block"} if display else {})))


# -- a reading with math in it ------------------------------------------------------------------

# $$..$$, \[..\], \(..\) and $..$. A single $ opens a formula when no space follows it and
# closes it when no space comes before it and no digit after it, so prices ("$5 to $10") stay
# prices. A pair with a space inside it is a formula only when it holds a TeX mark: `$ x = 1 $`.
SPAN = re.compile(r"\$\$(?P<d1>.+?)\$\$|\\\[(?P<d2>.+?)\\\]|\\\((?P<i1>.+?)\\\)"
                  r"|(?<![\\$])\$(?P<i2>[^\s$](?:[^$]*?[^\s$\\])?)\$(?![\d$])"
                  r"|(?<![\\$])\$(?P<i3>[^$]*[\\_^=][^$]*)\$(?![\d$])", re.S)


def inline(text: str) -> Markup | None:
    """`text` as HTML: its text escaped and its URLs linked, and each formula as MathML when it
    reads, or as the text the model wrote when it does not. None when no formula of `text`
    reads, so the caller sets the text as it would any other.

    A formula is written between dollar signs. A line with no dollar signs at all that is LaTeX
    from end to end (a command, and no word of ordinary text) is one formula too."""
    found = list(SPAN.finditer(text))
    out: list[Markup] = []
    if not found:
        for line in text.split("\n"):
            formula = _bare(line)
            out.append(formula if formula is not None else Markup(linkify(line)))
        return Markup("<br>").join(out) if any("<math" in o for o in out) else None
    at = 0
    for span in found:
        out.append(_text(text[at:span.start()]))
        latex = (span.group("d1") or span.group("d2") or span.group("i1") or span.group("i2")
                 or span.group("i3"))
        try:
            out.append(to_mathml(latex, display=bool(span.group("d1") or span.group("d2"))))
        except Unsupported:
            out.append(escape(span.group(0)))
        at = span.end()
    out.append(_text(text[at:]))
    return Markup("").join(out) if any("<math" in o for o in out) else None


BARE = re.compile(r"\\[A-Za-z]+")


def _bare(line: str) -> Markup | None:
    if not BARE.search(line) or re.search(r"[A-Za-z]{3,}", re.sub(r"\{[^{}]*\}", "", BARE.sub("", line))):
        return None
    try:
        return to_mathml(line)
    except Unsupported:
        return None


def _text(text: str) -> Markup:
    return Markup("<br>").join(Markup(linkify(line)) for line in text.split("\n"))
