"""A model's LaTeX as MathML: the subset it reads, the allow-list it writes, the text it keeps."""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from reflow.render import mathml
from reflow.render.mathml import ALLOWED, Unsupported, inline, to_mathml


def text_of(markup: str) -> str:
    return re.sub(r"<[^>]+>", "", str(markup)).replace("&lt;", "<").replace("&gt;", ">") \
        .replace("&amp;", "&")


@pytest.mark.parametrize("latex, expected", [
    (r"x_2", "<msub><mi>x</mi><mn>2</mn></msub>"),
    (r"x^2", "<msup><mi>x</mi><mn>2</mn></msup>"),
    (r"x_a^2", "<msubsup><mi>x</mi><mi>a</mi><mn>2</mn></msubsup>"),
    (r"T_{f}", "<msub><mi>T</mi><mi>f</mi></msub>"),
    (r"\frac{1.497}{4.00}", "<mfrac><mn>1.497</mn><mn>4.00</mn></mfrac>"),
    (r"\sqrt{x}", "<msqrt><mi>x</mi></msqrt>"),
    (r"\sqrt[3]{x}", "<mroot><mi>x</mi><mrow><mn>3</mn></mrow></mroot>"),
    (r"\Delta T", "<mi>Δ</mi><mi>T</mi>"),
    (r"103.13^\circ C", "<msup><mn>103.13</mn><mo>°</mo></msup><mi>C</mi>"),
    (r"a \times b \cdot c", "<mi>a</mi><mo>×</mo><mi>b</mi><mo>·</mo><mi>c</mi>"),
    (r"A \rightarrow B", "<mi>A</mi><mo>→</mo><mi>B</mi>"),
    (r"A \rightleftharpoons B", "<mi>A</mi><mo>⇌</mo><mi>B</mi>"),
    (r"\text{mol of NaCl}", "<mtext>mol of NaCl</mtext>"),
    (r"\mathrm{Na}_2", '<msub><mrow><mi mathvariant="normal">N</mi>'
                       '<mi mathvariant="normal">a</mi></mrow><mn>2</mn></msub>'),
    (r"\left( x \right)", "<mo>(</mo><mi>x</mi><mo>)</mo>"),
    (r"\overline{T}", "<mover><mi>T</mi><mo>¯</mo></mover>"),
    (r"x = -7.48", "<mi>x</mi><mo>=</mo><mo>-</mo><mn>7.48</mn>"),
    (r"1.86\,^\circ C", "<mn>1.86</mn><msup><mtext>\u00a0</mtext><mo>°</mo></msup><mi>C</mi>"),
    (r"\%", "<mo>%</mo>"),
    (r"\boxed{\begin{array}{c} x = 2 \end{array}}", "<mrow><mi>x</mi><mo>=</mo><mn>2</mn></mrow>"),
])
def test_the_subset_chemistry_worksheets_need_becomes_mathml(latex, expected):
    assert to_mathml(latex) == f"<math>{expected}</math>"


def test_a_display_formula_says_so():
    assert to_mathml("x", display=True) == '<math display="block"><mi>x</mi></math>'


@pytest.mark.parametrize("latex", [
    r"\begin{matrix} a \end{matrix}", r"\begin{array}{cc} a \end{array}", r"\begin{array}{c} a & b \end{array}",
    r"\begin{array}{c} a", r"\begin{array}{c} a \end{matrix}", r"\unknown{x}", r"\href{javascript:alert(1)}{x}",
    r"\includegraphics{x}", "a & b", r"a \\ b", "{a", "a}", "x^2^3", "x_1_2", r"\frac{1}",
    r"\text{a \textbf{b}}", r"\text{a", "", r"\sqrt[3", r"\left", "x" * (mathml.MAX_SOURCE + 1),
    "{" * (mathml.MAX_DEPTH + 2) + "x" + "}" * (mathml.MAX_DEPTH + 2),
    " ".join(["x"] * (mathml.MAX_ELEMENTS + 1)),
])
def test_what_is_not_in_the_subset_is_unsupported(latex):
    with pytest.raises(Unsupported):
        to_mathml(latex)


def test_a_formula_that_does_not_read_stays_as_the_escaped_text_the_model_wrote():
    assert str(inline(r"see $\begin{x}<b>$ and $y$")) == (
        r"see $\begin{x}&lt;b&gt;$ and <math><mi>y</mi></math>")


def test_text_with_no_formula_that_reads_is_left_to_the_caller():
    for text in ["plain words", r"see $\begin{x}<b>$ now", "costs $5 to $10", "", "x_1 = 2"]:
        assert inline(text) is None


def test_math_between_words_is_math_and_the_words_are_escaped():
    html = str(inline("so <b>$T_f$</b> & $\\Delta$"))
    assert html == ("so &lt;b&gt;<math><msub><mi>T</mi><mi>f</mi></msub></math>&lt;/b&gt; &amp; "
                    "<math><mi>Δ</mi></math>")


@pytest.mark.parametrize("text", ["$$x^2$$", r"\[x^2\]"])
def test_double_dollars_and_brackets_are_display_math(text):
    assert str(inline(text)) == '<math display="block"><msup><mi>x</mi><mn>2</mn></msup></math>'


def test_parentheses_delimit_inline_math():
    assert str(inline(r"\(x\)")) == "<math><mi>x</mi></math>"


@pytest.mark.parametrize("text", [
    "costs $5 to $10", "$5 and $6", "paid $1,200 on 3/4 and $300 on 4/5", "US$ 5", "$", "$$",
    "price $5.00", "a $ b", "$100",
])
def test_prices_are_not_math(text):
    assert inline(text) is None


def test_a_price_beside_a_formula_stays_a_price():
    html = str(inline("it cost $5 and $x_1$ here"))
    assert html == "it cost $5 and <math><msub><mi>x</mi><mn>1</mn></msub></math> here"


def test_a_line_of_bare_latex_is_a_formula_and_a_line_of_words_is_not():
    html = str(inline("so\n1.86^\\circ\\mathrm{C} = \\boxed{7.48}\nthe \\Delta of a thing"))
    assert html.count("<math") == 1 and html.endswith("<br>the \\Delta of a thing")
    assert "<mn>7.48</mn>" in html and "boxed" not in html


def test_lines_keep_their_breaks_and_urls_keep_their_links():
    html = str(inline("a\nb https://example.org/x $y$"))
    assert html.startswith('a<br>b <a href="https://example.org/x"')
    assert html.endswith("<math><mi>y</mi></math>")


def test_a_formulas_text_reads_as_the_plain_text_of_its_formula():
    html = inline(r"$\Delta T_f = (2)(1.86^\circ\mathrm{C/m})$")
    assert text_of(html) == "ΔTf=(2)(1.86°C/m)"


HOSTILE = [
    r"$\text{<script>alert(1)</script>}$", "$<img src=x onerror=alert(1)>$",
    r"$\mathrm{</mtext><script>alert(1)</script>}$", '$x" onmouseover="alert(1)$',
    r"$\text{&lt;b&gt;}$", "$x$<script>alert(1)</script>", r"$\text{\href{javascript:x}{y}}$",
    "$<math><mglyph><style></style></mglyph></math>$", "$a\u202e$", "$\x00$",
]


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found: list[tuple[str, list[tuple[str, str | None]]]] = []

    def handle_starttag(self, tag, attrs):
        self.found.append((tag, attrs))


def tags_of(markup: str):
    parser = Tags()
    parser.feed(markup)
    return parser.found


@pytest.mark.parametrize("text", HOSTILE)
def test_hostile_model_text_writes_no_element_or_attribute_but_the_allow_list(text):
    html = str(inline(f"{text} and \\(y\\)"))   # a formula that reads beside it
    assert "<math" in html
    for tag, attrs in tags_of(html):
        assert tag in ALLOWED.keys() | {"br", "a"}
        assert all(name in ALLOWED.get(tag, {}) or tag == "a" for name, _ in attrs)
    assert "<script" not in html and "<img" not in html


PIECES = st.sampled_from(
    list("\\{}^_$()[]<>&\"' \n=+-x1.,;:%#~!ab|")
    + [r"\frac", r"\text", r"\mathrm", r"\sqrt", r"\left", r"\right", r"\circ", r"\Delta",
       r"\begin", "$$", r"\(", r"\)", r"\[", r"\]"])


@given(st.lists(PIECES, max_size=40).map("".join))
def test_whatever_the_model_writes_only_the_allow_list_is_written(text):
    for tag, attrs in tags_of(str(inline(text) or '')):
        assert tag in ALLOWED.keys() | {"br", "a"}
        assert all(name in ALLOWED.get(tag, {}) or tag == "a" for name, _ in attrs)


def test_the_web_sanitizer_keeps_exactly_the_elements_this_module_writes():
    ts = (Path(__file__).parent.parent / "web/src/reflow/sanitize.ts").read_text()
    listed = re.search(r"MATH_ELEMENTS = new Set\(\[(.*?)\]\)", ts, re.S)
    assert listed
    assert set(re.findall(r'"(\w+)"', listed.group(1))) == set(ALLOWED)
    attributes = re.search(r"MATH_ATTRIBUTES[^=]*= \{(.*?)\n\};", ts, re.S)
    assert attributes
    written = {(tag, name, next(iter(values)))
               for tag, attrs in ALLOWED.items() for name, values in attrs.items()}
    kept = set(re.findall(r'(\w+): \{ (\w+): "(\w+)" \}', attributes.group(1)))
    assert kept == written


def test_a_formula_with_a_space_inside_its_dollars_needs_a_tex_mark_to_be_one():
    assert str(inline("$ x_1 $")) == "<math><msub><mi>x</mi><mn>1</mn></msub></math>"
    assert str(inline("$ a = b $")) == "<math><mi>a</mi><mo>=</mo><mi>b</mi></math>"
    assert inline("it cost $ five and then $ six") is None
