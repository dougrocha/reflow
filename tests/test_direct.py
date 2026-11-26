"""The prompt-only pipeline (analyze.direct, analyze.direct_html): the model's markup as
nodes of the allowed set only, the cache, the spending cap, retries, the join across page breaks,
and a whole conversion with a fake model."""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path

import pytest

from reflow.analyze import direct, vision
from reflow.analyze.direct import Cache, PageInput, Reading, Spend, join_across_pages, read_pages
from reflow.analyze.direct_html import page_nodes
from reflow.analyze.vision import ProviderError
from reflow.model import Node, Role
from reflow.providers import DIRECT_PROMPTS
from reflow.run import Options

REPO = Path(__file__).resolve().parents[1]
SIZE = (612.0, 792.0)


def nodes_of(markup: str, page: int = 1, markdown: bool = False):
    nodes, repairs = page_nodes(markup, page, SIZE, markdown=markdown)
    return nodes, repairs


def roles(nodes):
    return [n.role for n in nodes]


# ---------------------------------------------------------------------------- the converter

def test_headings_paragraphs_marks_and_links_become_nodes():
    nodes, repairs = nodes_of(
        '<h1>Title</h1><h3>Part I</h3><p>Go to <a href="https://irs.gov/w9">irs.gov</a> for '
        '<strong>the form</strong> and <em>notes</em><sup>1</sup>.</p>')
    assert roles(nodes) == [Role.H2, Role.H3, Role.P]
    assert nodes[0].attrs["font_size"] == 1000.0   # the title candidate, as Docling's
    p = nodes[2]
    assert p.text == "Go to irs.gov for the form and notes1."
    assert [(s.kind, s.target, p.text[s.start:s.end]) for s in p.spans] == [
        ("link", "https://irs.gov/w9", "irs.gov")]
    assert {(m.kind, m.text) for m in p.marks} == {("strong", "the form"), ("em", "notes"),
                                                    ("sup", "1")}
    assert repairs.count() == 0


def test_an_unsafe_link_keeps_its_text_and_loses_its_target():
    nodes, _ = nodes_of('<p><a href="javascript:alert(1)">click</a> here</p>')
    assert nodes[0].text == "click here" and nodes[0].spans == []


def test_script_and_style_are_dropped_with_their_content_and_unknown_tags_are_opened():
    nodes, repairs = nodes_of('<p>Kept <blink>this</blink></p><script>alert("x")</script>'
                              '<style>p{}</style><marquee><p>and this</p></marquee>')
    assert [n.text for n in nodes] == ["Kept this", "and this"]
    assert repairs.dropped == {"script": 1, "style": 1}
    assert repairs.unwrapped == {"blink": 1, "marquee": 1}


def test_wrappers_with_no_meaning_are_opened_silently():
    nodes, repairs = nodes_of("<div><section><p>One</p></section><span>Two</span></div>")
    assert [n.text for n in nodes] == ["One", "Two"] and repairs.count() == 0


def test_a_line_break_keeps_the_lines_and_one_text():
    nodes, _ = nodes_of("<p>12 Main St<br>Phoenix, AZ</p>")
    assert nodes[0].text == "12 Main St Phoenix, AZ"
    assert nodes[0].attrs["lines"] == ["12 Main St", "Phoenix, AZ"]


def test_lists_keep_order_markers_and_nesting():
    nodes, _ = nodes_of('<ol start="3"><li>Three<ul><li>a</li><li>b</li></ul></li><li>Four</li></ol>')
    (lst,) = nodes
    assert lst.role == Role.LIST and lst.attrs["ordered"] is True
    assert [(li.text, li.attrs["marker"]) for li in lst.children] == [("Three", "3."), ("Four", "4.")]
    (nested,) = lst.children[0].children
    assert nested.attrs["ordered"] is False and [li.text for li in nested.children] == ["a", "b"]


def test_a_table_keeps_its_caption_header_spans_and_row_headers():
    nodes, _ = nodes_of(
        '<table><caption>Rates</caption><thead><tr><th>Plan</th><th colspan="2">Cost</th></tr>'
        '</thead><tbody><tr><th scope="row">A</th><td>1</td><td rowspan="99">2</td></tr></tbody>'
        '</table>')
    (table,) = nodes
    caption, head, body = table.children
    assert caption.role == Role.CAPTION and caption.text == "Rates"
    assert [(c.role, c.text, c.attrs) for c in head.children] == [
        (Role.TH, "Plan", {}), (Role.TH, "Cost", {"colspan": 2})]
    assert body.children[0].attrs == {"scope": "row"}
    assert body.children[2].attrs == {}   # a span past MAX_SPAN is dropped


def test_a_figure_keeps_its_alt_caption_and_box_and_is_labelled_ai_drafted():
    nodes, _ = nodes_of('<figure><img alt="A bar chart of rates" data-box="100 250 600 500">'
                        '<figcaption>Figure 1</figcaption></figure>')
    (fig,) = nodes
    assert fig.attrs["alt"] == "A bar chart of rates" and fig.attrs["provenance"] == "ai-drafted"
    assert fig.bbox == (61.2, 198.0, 367.2, 396.0)
    assert [(c.role, c.text) for c in fig.children] == [(Role.CAPTION, "Figure 1")]


@pytest.mark.parametrize("box", ["", "1 2 3", "500 10 100 20", "0 0 2000 10", "a b c d"])
def test_a_bad_figure_box_is_ignored(box):
    (fig,), _ = nodes_of(f'<img alt="Logo" data-box="{box}">')
    assert fig.bbox is None


def test_an_empty_alt_is_decorative_and_a_missing_one_needs_a_person():
    (dec,), _ = nodes_of('<img alt="">')
    (missing,), _ = nodes_of("<img>")
    assert dec.attrs.get("decorative") is True
    assert missing.attrs.get("needs") == "alt" and "provenance" not in missing.attrs


def test_form_fields_and_a_group_of_choices():
    nodes, _ = nodes_of(
        '<label>Name <input type="text"></label>'
        '<fieldset><legend>Status</legend><label><input type="radio" checked> Single</label>'
        '<label><input type="radio"> Married</label></fieldset>'
        '<label>State <select><option>AZ</option><option>NM</option></select></label>')
    name, group, state = nodes
    assert (name.role, name.text, name.attrs["kind"]) == (Role.FIELD, "Name", "text")
    assert group.role == Role.GROUP and group.text == "Status"
    single, married = group.children
    assert single.attrs["kind"] == "radio" and single.attrs["checked"] is True
    assert single.attrs["group"] == married.attrs["group"]
    assert state.attrs["kind"] == "combo" and state.attrs["options"] == ["AZ", "NM"]
    assert len({n.attrs["fid"] for n in (name, single, married, state)}) == 4


def test_a_control_in_a_table_cell_is_its_child_not_its_text():
    (table,), _ = nodes_of('<table><tr><td>Line 3a <label><input type="checkbox"> LLC</label>'
                           "</td></tr></table>")
    cell = table.children[0].children[0]
    assert cell.text == "Line 3a"
    assert [(c.role, c.text) for c in cell.children] == [(Role.FIELD, "LLC")]


def test_a_control_in_a_list_item_is_its_child_and_is_rendered():
    """The W-4's "Enter the amount here 3(a) $ <input>" items lost their controls."""
    from reflow.render.html import NodeRenderer
    (lst,), _ = nodes_of('<ol><li>Multiply by $2,200 <label>3(a) $ <input type="text"></label>'
                         '<ul><li>Sub</li></ul></li><li>Plain</li></ol>')
    item = lst.children[0]
    assert item.text == "Multiply by $2,200"
    assert [(c.role, c.text) for c in item.children] == [(Role.FIELD, "3(a) $"), (Role.LIST, None)]
    html = str(NodeRenderer({}, {}).render(lst))
    assert html.count("<input") == 1 and html.count("<li") == 3


def test_headers_footers_and_asides():
    nodes, _ = nodes_of("<header>Form W-9</header><p>Body</p><aside>1 A note</aside>"
                        "<footer>Page 2</footer>")
    assert roles(nodes) == [Role.ARTIFACT, Role.P, Role.NOTE, Role.ARTIFACT]


def test_an_answer_in_a_code_fence_is_read():
    nodes, _ = nodes_of("```html\n<p>Fenced</p>\n```")
    assert [n.text for n in nodes] == ["Fenced"]


def test_an_ocr_models_markdown_goes_through_the_same_converter():
    nodes, _ = nodes_of("# Title\n\nSome **bold** text.\n\n- one\n- two\n\n"
                        "| A | B |\n|---|---|\n| 1 | 2 |\n\n<table><tr><td>x</td></tr></table>",
                        markdown=True)
    assert roles(nodes) == [Role.H2, Role.P, Role.LIST, Role.TABLE, Role.TABLE]
    assert {(m.kind, m.text) for m in nodes[1].marks} == {("strong", "bold")}


def test_every_node_is_from_the_model_and_ids_are_unique_and_stable():
    markup = "<p>Same</p><p>Same</p><ul><li>Same</li></ul>"
    first, _ = nodes_of(markup)
    again, _ = nodes_of(markup)
    ids = [n.id for root in first for n in root.walk()]
    assert len(ids) == len(set(ids)) and ids == [n.id for root in again for n in root.walk()]
    assert all(n.source.value == "llm" for root in first for n in root.walk())


# ---------------------------------------------------------------------------- reading pages

def page(number: int, text: str = "words") -> PageInput:
    return PageInput(number, 3, f"image {number}".encode(), text)


class FakeReader:
    model, provider, prompt, reads_text = "claude-haiku-5-5", "anthropic", "test-1", True

    def __init__(self, fail: dict[int, int] | None = None, cost: float = 0.001) -> None:
        self.fail = dict(fail or {})   # page -> requests that fail before one succeeds
        self.cost = cost
        self.calls: list[int] = []
        self.lock = threading.Lock()

    def read(self, p: PageInput) -> Reading:
        with self.lock:
            self.calls.append(p.number)
            if self.fail.get(p.number, 0) > 0:
                self.fail[p.number] -= 1
                raise ProviderError("bad answer")
        return Reading(f"<p>Page {p.number}</p>", False, 1000, 500, self.cost, 0.1)


def test_a_cached_page_makes_no_request(tmp_path):
    cache, reader = Cache(tmp_path), FakeReader()
    first = read_pages(page, [1, 2], reader, cache, Spend(reader.model, 5.0), jobs=2)
    again = read_pages(page, [1, 2], reader, cache, Spend(reader.model, 5.0), jobs=2)
    assert reader.calls.count(1) == 1 and reader.calls.count(2) == 1
    assert [r.cached for r in first] == [False, False] and [r.cached for r in again] == [True, True]
    assert again[0].reading.markup == "<p>Page 1</p>" and again[0].reading.dollars == 0.001


def test_the_key_is_the_model_prompt_image_and_text():
    a = page(1)
    assert a.key("m", "p") == page(1).key("m", "p")
    assert len({a.key("m", "p"), a.key("n", "p"), a.key("m", "q"), page(2).key("m", "p"),
                page(1, "other").key("m", "p")}) == 5


def test_a_failed_page_is_asked_again_on_its_own(tmp_path):
    reader = FakeReader(fail={2: 2})
    results = read_pages(page, [1, 2, 3], reader, Cache(tmp_path), Spend(reader.model, 5.0), jobs=1)
    assert all(r.reading is not None for r in results)
    assert reader.calls == [1, 2, 2, 2, 3]


def test_a_page_that_keeps_failing_is_left_unread_and_the_others_are_kept(tmp_path):
    reader = FakeReader(fail={2: 10})
    results = read_pages(page, [1, 2, 3], reader, Cache(tmp_path), Spend(reader.model, 5.0), jobs=1)
    assert [r.reading is None for r in results] == [False, True, False]
    assert results[1].error == "bad answer" and reader.calls.count(2) == 1 + direct.RETRIES


def test_the_run_stops_at_its_cap_and_keeps_what_it_finished(tmp_path):
    reader = FakeReader(cost=0.02)
    cache = Cache(tmp_path)
    read_pages(page, [3], reader, cache, Spend(reader.model, 5.0), jobs=1)   # page 3 is cached
    reader.calls.clear()
    spend = Spend(reader.model, 0.05)
    spend.most = lambda p: 0.02
    results = read_pages(page, [1, 2, 3], reader, cache, spend, jobs=1)
    assert reader.calls == [1, 2]
    assert [r.reading is not None for r in results] == [True, True, True]
    spend = Spend(reader.model, 0.03)
    spend.most = lambda p: 0.02
    results = read_pages(page, [4, 5, 3], reader, cache, spend, jobs=1)
    assert [r.reading is not None for r in results] == [True, False, True]   # the cache is free
    assert results[1].error == "the run reached its spending cap" and spend.stopped


def test_requests_in_flight_count_against_the_cap():
    spend = Spend("claude-opus-5-5", 1.0)
    a = page(1)
    most = spend.most(a)
    assert most == direct.dollars("claude-opus-5-5", direct.FIXED_INPUT_TOKENS + 5, direct.MAX_TOKENS)
    n = int(1.0 // most)
    assert all(spend.start(a) for _ in range(n)) and not spend.start(a)
    assert not spend.start(a)   # once stopped, it stays stopped
    spend.end(a, 0.1)
    assert spend.spent == 0.1 and not spend.start(a)


def test_a_local_model_costs_nothing():
    assert direct.dollars("glm-ocr", 10_000, 10_000) == 0.0
    assert direct.dollars("claude-opus-5-5", 1_000_000, 100_000) == pytest.approx(6.0)
    assert direct.provider_of("glm-ocr") == "ollama"
    assert direct.provider_of("claude-sonnet-5-5") == "anthropic"


def sent_to_ollama(monkeypatch, model: str, page_input: PageInput, **kw) -> tuple[dict, direct.Reading]:
    """What `OllamaReader.read` sends to a fake Ollama server, and the Reading it makes."""
    requests = []

    def urlopen(request, timeout=None, loopback=False):
        requests.append(json.loads(request.data))
        return io.BytesIO(json.dumps({"message": {"content": "<p>x</p>"}, "prompt_eval_count": 5,
                                      "eval_count": 3}).encode())

    monkeypatch.setattr("reflow.analyze.vision.send", urlopen)
    reading = direct.OllamaReader(model, **kw).read(page_input)
    return requests[0], reading


def test_glm_ocr_gets_the_picture_alone_with_its_task_prompt(monkeypatch):
    reader = direct.OllamaReader("glm-ocr")
    assert reader.reads_text is False and reader.prompt.startswith("glm-ocr-")
    sent, reading = sent_to_ollama(monkeypatch, "glm-ocr", page(1, ""))
    (message,) = sent["messages"]
    assert message["content"] == direct.OCR_PROMPT and len(message["images"]) == 1
    assert "format" not in sent and reading.markdown
    other, reading = sent_to_ollama(monkeypatch, "qwen3-vl:8b", page(1, "the text"))
    assert other["messages"][0]["content"] == direct.SYSTEM_PROMPT
    assert "the text" in other["messages"][1]["content"]
    assert (reading.tokens_in, reading.tokens_out, reading.markdown) == (5, 3, False)


def test_a_thinking_only_model_gets_the_empty_think_turn_under_the_direct_engine_too(monkeypatch):
    sent, _ = sent_to_ollama(monkeypatch, "qwen3-vl:8b", page(1))
    assert sent["messages"][-1] == {"role": "assistant", "content": vision.EMPTY_THINK}
    assert sent["options"]["num_ctx"] == direct.OLLAMA_NUM_CTX
    assert sent["options"]["num_predict"] == direct.OLLAMA_MAX_TOKENS
    sent, _ = sent_to_ollama(monkeypatch, "glm-ocr", page(1))
    assert all(m["role"] != "assistant" for m in sent["messages"])


def test_a_page_with_no_text_layer_says_so():
    assert "no text layer" in direct.user_text(page(1, ""))
    assert "<text-layer>\nwords\n</text-layer>" in direct.user_text(page(1, "words"))


# ---------------------------------------------------------------------------- the join

def table(page_no: int, *rows: list[str], head: bool = False) -> Node:
    def row(cells: list[str], tag: str) -> str:
        return "<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>"
    nodes, _ = page_nodes("<table>" + "".join(
        row(r, "th" if head and i == 0 else "td") for i, r in enumerate(rows)) + "</table>",
        page_no, SIZE)
    return nodes[0]


def test_a_table_that_runs_on_is_joined_and_its_repeated_header_dropped():
    header = ["Name", "Rate"]
    a, b = table(1, header, ["x", "1"], head=True), table(2, header, ["y", "2"], head=True)
    footer, _ = page_nodes("<footer>Page 1</footer>", 1, SIZE)
    joined = join_across_pages([a, *footer, b])
    assert joined == [a, footer[0]]
    assert [[c.text for c in r.children] for r in a.children] == [header, ["x", "1"], ["y", "2"]]
    assert a.attrs["pages"] == [1, 2] and [r.text for r in a.children] == ["row 0", "row 1", "row 2"]


def test_tables_of_other_widths_or_with_text_between_are_kept_apart():
    a, b = table(1, ["a", "b"]), table(2, ["a", "b", "c"])
    assert len(join_across_pages([a, b])) == 2
    c, d = table(1, ["a", "b"]), table(2, ["c", "d"])
    p, _ = page_nodes("<p>Between</p>", 2, SIZE)
    assert len(join_across_pages([c, *p, d])) == 3
    e, f = table(1, ["a", "b"]), table(3, ["c", "d"])
    assert len(join_across_pages([e, f])) == 2


def test_a_list_that_runs_on_is_joined_and_a_paragraph_that_goes_on_too():
    a, _ = page_nodes("<ul><li>one</li></ul><p>The rule applies to</p>", 1, SIZE)
    b, _ = page_nodes("<p>every page.</p>", 2, SIZE)
    c, _ = page_nodes("<ul><li>two</li></ul>", 3, SIZE)
    joined = join_across_pages([*a, *b])
    assert [n.text for n in joined if n.role == Role.P] == ["The rule applies to every page."]
    lists = join_across_pages([a[0], *c])
    assert len(lists) == 2   # page 3 does not follow page 1
    d, _ = page_nodes("<ul><li>two</li></ul>", 2, SIZE)
    (lst,) = join_across_pages([a[0], *d])
    assert [li.text for li in lst.children] == ["one", "two"] and lst.attrs["pages"] == [1, 2]


# ---------------------------------------------------------------------------- options

def test_direct_options_are_checked():
    assert Options(engine="direct").direct_model == "claude-haiku-5-5"
    for bad in ({"direct_model": " "}, {"direct_max_dollars": -1}, {"direct_jobs": 0},
                {"engine": "direct", "scan_fields": True}):
        with pytest.raises(ValueError):
            Options(**bad)


def test_the_cap_jobs_and_cache_are_in_no_key():
    from reflow.steps import options_record
    a = options_record(Options(engine="direct", direct_max_dollars=1, direct_jobs=2))
    b = options_record(Options(engine="direct"))
    assert a == b and a["direct_model"] == "claude-haiku-5-5"


# ---------------------------------------------------------------------------- a whole conversion

class PageReader(FakeReader):
    def read(self, p: PageInput) -> Reading:
        super().read(p)
        return Reading(f"<header>Head</header><h2>Section {p.number}</h2>"
                       f"<p>Text of page {p.number} from {len(p.text)} characters.</p>",
                       False, 2000, 300, self.cost, 0.25)


def test_a_conversion_transcribes_every_page_and_records_what_it_cost(tmp_path):
    from reflow.convert import convert
    reader = PageReader(fail={2: 10})
    out = tmp_path / "out"
    summary = convert(REPO / "samples" / "fw9.pdf", out, engine="direct", direct_reader=reader,
                      direct_cache=tmp_path / "cache", page_images=False)
    meta = summary.direct
    assert meta["model"] == "claude-haiku-5-5" and meta["read"] == 5 and meta["unread"] == {
        "2": "bad answer"}
    assert meta["usd"] == pytest.approx(0.005) and meta["tokens_in"] == 10_000
    assert meta["pages"]["1"] == {"in": 2000, "out": 300, "usd": 0.001, "s": 0.25, "cached": False}
    assert summary.ocr.runner == "none" and not summary.ocr.attempted
    doc = json.loads((out / "document.json").read_text())
    page2 = next(p for p in doc["pages"] if p["number"] == 2)
    assert "direct-unread" in [f["code"] for f in page2.get("facts", [])]
    review = json.loads((out / "review.json").read_text())
    assert any(i.get("page") == 2 and i.get("severity") == "error" for i in review)
    html = "".join(p.read_text() for p in out.glob("*.html"))
    assert "Text of page 1 from" in html and "Section 3" in html and "Head" not in html

    again = convert(REPO / "samples" / "fw9.pdf", tmp_path / "again", engine="direct",
                    direct_reader=PageReader(fail={2: 10}), direct_cache=tmp_path / "cache",
                    page_images=False)
    assert again.direct["cached"] == 5 and again.direct["spent"] == 0
    assert again.direct["pages"]["1"] == {**meta["pages"]["1"], "cached": True}


def test_the_cap_reserves_the_most_a_request_can_cost():
    spend = Spend("claude-opus-5-5", 5.0)
    dense = PageInput(1, 1, b"image", "漢" * 1000)   # 3 bytes, so at most 3 tokens, a character
    assert spend.most(dense) == direct.dollars("claude-opus-5-5", direct.FIXED_INPUT_TOKENS + 3000,
                                               direct.MAX_TOKENS)
    flying = 0
    while spend.start(dense):
        flying += 1
    assert flying * spend.most(dense) <= 5.0 < (flying + 1) * spend.most(dense)


def test_a_paid_answer_that_cannot_be_used_still_counts(tmp_path):
    class Refuses(FakeReader):
        def read(self, p):
            self.calls.append(p.number)
            raise direct.BilledError("model stopped: refusal", 0.01)

    spend = Spend("claude-haiku-5-5", 5.0)
    (result,) = read_pages(page, [1], Refuses(), Cache(tmp_path), spend, jobs=1)
    assert result.reading is None and spend.spent == pytest.approx(0.01 * (1 + direct.RETRIES))


def test_a_field_inside_a_paragraph_is_kept():
    nodes, _ = nodes_of('<p><label>Social security number <input type="text"></label> or '
                        '<label>Employer identification number <input type="text"></label></p>')
    assert [(n.role, n.text) for n in nodes] == [
        (Role.FIELD, "Social security number"), (Role.P, "or"),
        (Role.FIELD, "Employer identification number")]


def test_a_repeated_data_row_is_not_taken_for_a_header():
    a, b = table(1, ["x", "1"]), table(2, ["x", "1"])
    (joined,) = join_across_pages([a, b])
    assert len([r for r in joined.children if r.role == Role.TR]) == 2


def test_a_paragraph_goes_on_across_a_running_footer_and_header():
    a, _ = page_nodes("<p>The rule applies to</p><footer>Page 1</footer>", 1, SIZE)
    b, _ = page_nodes("<header>Form</header><p>every page.</p>", 2, SIZE)
    joined = join_across_pages([*a, *b])
    assert [n.text for n in joined if n.role == Role.P] == ["The rule applies to every page."]
    assert [n.text for n in joined if n.role == Role.ARTIFACT] == ["Page 1", "Form"]


def test_a_field_inside_emphasis_or_with_a_select_keeps_its_label():
    nodes, _ = nodes_of('<p><strong><label>Name <input type="text"></label></strong></p>'
                        '<p><label>State <select><option>AZ</option></select></label></p>')
    assert [(n.role, n.text, n.attrs.get("kind")) for n in nodes] == [
        (Role.FIELD, "Name", "text"), (Role.FIELD, "State", "combo")]


# ---------------------------------------------------------------------------- prompt variants

def test_each_prompt_variant_has_its_own_id_and_the_base_keeps_its_cached_answers(monkeypatch):
    assert direct.prompt_id("base") == "html-1"     # the answers cached under it stay valid
    ids = {direct.prompt_id(v) for v in direct.PROMPTS}
    assert len(ids) == len(direct.PROMPTS)
    for variant, prompt in direct.PROMPTS.items():
        assert prompt.startswith(direct.RULES) and prompt.endswith(direct.DATA_NOTE)
        reader = direct.make_reader("claude-haiku-5-5", variant)
        assert (reader.prompt, reader.system) == (direct.prompt_id(variant), prompt)
    assert direct.PROMPTS["base"] == direct.SYSTEM_PROMPT
    assert tuple(direct.PROMPTS) == DIRECT_PROMPTS
    other, _ = sent_to_ollama(monkeypatch, "qwen3-vl:8b", page(1), variant="forms")
    assert other["messages"][0]["content"] == direct.PROMPTS["forms"]
    assert direct.OllamaReader("glm-ocr", variant="forms").prompt == "glm-ocr-1"


def test_the_prompt_variant_is_an_option_in_the_record():
    from reflow.steps import options_record, uncovered
    assert options_record(Options(engine="direct", direct_prompt="complete"))["direct_prompt"] == "complete"
    assert any("prompt forms" in why for why in uncovered(Options(engine="direct", direct_prompt="forms")))
    with pytest.raises(ValueError):
        Options(direct_prompt="other")


def test_a_conversion_records_its_prompt(tmp_path):
    from reflow.convert import convert
    reader = PageReader()
    reader.prompt = direct.prompt_id("forms")
    summary = convert(REPO / "samples" / "fw9.pdf", tmp_path / "out", engine="direct",
                      direct_reader=reader, direct_cache=tmp_path / "cache", page_images=False,
                      direct_prompt="forms")
    assert summary.direct["prompt"] == "html-forms-1"
