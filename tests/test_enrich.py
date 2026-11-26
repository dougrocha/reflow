import json
import pytest

from reflow.analyze import enrich
from reflow.analyze.enrich import (MAX_ALT, AltResult, FigureContext, cache_key, enrich_figures,
                                   make_provider, parse_answer, parse_result, shorten_alt)
from reflow.analyze.vision import ProviderError, first_json_object
from reflow.model import Document, Node, PageInfo, Role, Source
from reflow.analyze.review import queue as review_items
from reflow.render.site import render_html


class FakeProvider:
    name = "fake"
    cache_id = "fake:1"
    confidence = 0.8
    workers = 2

    def __init__(self, results):
        self.results = results  # figure caption → AltResult or Exception
        self.calls = []

    def describe(self, image, media_type, ctx):
        self.calls.append(ctx)
        r = self.results[ctx.caption]
        if isinstance(r, Exception):
            raise r
        return r


def figure(caption, page=1):
    return Node(Role.FIGURE, page=page, bbox=(0, 0, 10, 10), confidence=0.2,
                attrs={"needs": "alt"},
                children=[Node(Role.CAPTION, page=page, text=caption)] if caption else [])


def setup(tmp_path, *figs):
    doc = Document(root=Node(Role.DOC, children=[
        Node(Role.H2, page=1, text="Results"), Node(Role.P, page=1, text="Error drops from 56 to 20 layers."), *figs]),
        pages=[PageInfo(1, 612, 792)], meta={"lang": "en"})
    doc.ensure_unique_ids()  # as convert does; these figures share page, bbox and text
    images = {}
    for i, f in enumerate(figs):
        path = tmp_path / f"f{i}.webp"
        path.write_bytes(f"image {i}".encode())
        images[f.id] = path
    return doc, images


PHOTO = AltResult("photo", False, "Staff member checking a mosquito trap", "", False)
CHART = AltResult("chart", False, "Line chart of training error by depth",
                  "Two lines; the 56-layer line stays above the 20-layer line.", False)
DECOR = AltResult("other", True, "", "", False)


def test_suggestions_are_applied_and_every_one_is_flagged_for_checking(tmp_path):
    photo, chart, decor = figure("Photo"), figure("Figure 1. Error"), figure("Border")
    doc, images = setup(tmp_path, photo, chart, decor)
    provider = FakeProvider({"Photo": PHOTO, "Figure 1. Error": CHART, "Border": DECOR})
    stats = enrich_figures(doc, images, provider)
    assert (stats["described"], stats["decorative"], stats["failed"]) == (3, 1, 0)

    assert photo.attrs["alt"] == PHOTO.alt and "needs" not in photo.attrs
    assert photo.source == Source.LLM and photo.attrs["warn"] == "suggested-alt"
    assert chart.attrs["description"] == CHART.description
    assert chart.attrs["warn"] == "suggested-description"
    assert decor.attrs["decorative"] is True and decor.attrs["alt"] == ""
    assert provider.calls[0].heading == "Results" and provider.calls[0].paragraph == "Error drops from 56 to 20 layers."

    review = {r["id"]: r["severity"] for r in review_items(doc)}
    assert review == {photo.id: "warning", chart.id: "warning", decor.id: "warning"}


def test_failures_keep_the_placeholder_and_are_errors(tmp_path):
    fig = figure("Photo")
    doc, images = setup(tmp_path, fig)
    stats = enrich_figures(doc, images, FakeProvider({"Photo": ProviderError("bad JSON")}))
    assert stats["failed"] == 1 and "bad JSON" in stats["errors"][0]
    assert fig.attrs == {"needs": "alt"}
    assert review_items(doc)[0]["severity"] == "error"


def test_cache_avoids_calling_the_model_again(tmp_path):
    doc, images = setup(tmp_path, figure("Photo"))
    cache = tmp_path / "cache.json"
    enrich_figures(doc, images, FakeProvider({"Photo": PHOTO}), cache_path=cache)
    doc2, _ = setup(tmp_path, figure("Photo"))
    again = FakeProvider({"Photo": ProviderError("should not be called")})
    stats = enrich_figures(doc2, images, again, cache_path=cache)
    assert stats["cached"] == 1 and again.calls == []


def test_render_uses_alt_description_and_decorative(tmp_path):
    photo, chart, decor = figure("Photo"), figure("Figure 1. Error"), figure(None)
    doc, images = setup(tmp_path, photo, chart, decor)
    enrich_figures(doc, images, FakeProvider({"Photo": PHOTO, "Figure 1. Error": CHART, "": DECOR}))
    figs = {n.id: ("x.webp", 10, 10) for n in (photo, chart, decor)}
    render_html(doc, "T", tmp_path, figures=figs, page_widths=(800,))
    index = (tmp_path / "index.html").read_text()
    assert f'alt="{PHOTO.alt}"' in index
    assert 'alt=""' in index
    assert "<details data-generated><summary>Image description</summary><p>Two lines;" in index
    items = review_items(doc)
    assert (sum(i["severity"] == "error" for i in items),
            sum(i["severity"] == "warning" for i in items)) == (0, 3)


NCHS_ALT = ("Pie chart showing distribution of published QuickStats by topic from 2005–2019, with "
            "Mortality as the largest category at 178, followed by Health care utilization and "
            "insurance coverage at 139, Chronic conditions at 108, and Other at 90.")


def test_parse_result_validates_and_trims():
    long = "word " * 60
    r = parse_result({"kind": "nope", "decorative": False, "alt": long, "description": "",
                      "uncertain": False})
    assert r.kind == "other" and len(r.alt) <= MAX_ALT
    assert not r.alt.endswith(("…", "...")) and r.uncertain  # cut mid-sentence: flagged
    with pytest.raises(ProviderError):
        parse_result({"kind": "photo", "decorative": False, "alt": "", "description": "",
                      "uncertain": False})
    assert parse_result({"kind": "logo", "decorative": True, "alt": "x", "description": "y",
                         "uncertain": False}).alt == ""


def test_flags_are_real_booleans_or_true_false_strings_only():
    base = {"kind": "photo", "alt": "A dog on a lawn.", "description": ""}
    assert parse_result({**base, "decorative": "false"}).decorative is False
    assert parse_result({**base, "decorative": " TRUE "}).decorative is True
    assert parse_result({**base, "decorative": False, "uncertain": "true"}).uncertain is True
    assert parse_result({**base, "decorative": False, "uncertain": False}).uncertain is False
    for bad in ("yes", "no", 1, 0, None, ["false"]):
        with pytest.raises(ProviderError):
            parse_result({**base, "decorative": bad})
    with pytest.raises(ProviderError):
        parse_result({**base, "decorative": False, "uncertain": "maybe"})


def test_long_alt_from_the_nchs_chart_is_cut_at_a_clause_not_mid_phrase():
    assert len(NCHS_ALT) > MAX_ALT
    r = parse_result({"kind": "chart", "decorative": False, "alt": NCHS_ALT,
                      "description": "A pie chart. Each segment is labelled.", "uncertain": False})
    assert r.alt == ("Pie chart showing distribution of published QuickStats by topic from "
                     "2005–2019, with Mortality as the largest category at 178.")
    assert r.uncertain and r.description == "A pie chart. Each segment is labelled."


def test_shorten_keeps_short_alt_untouched():
    assert shorten_alt("A doctor examining a child.") == ("A doctor examining a child.", False)


def test_sentence_end_is_preferred_to_a_clause_break():
    text = ("Bar chart of deaths by year. Deaths rise sharply after 2010, peak in 2019, then fall "
            "again across the rest of the period shown, with a dip in 2020 and 2021 too.")
    assert len(text) > MAX_ALT
    assert shorten_alt(text) == ("Bar chart of deaths by year.", True)


def test_decimals_and_abbreviations_are_not_sentence_ends():
    text = ("Rates rose from 3.5 to 9.2 per 100 in U.S. counties between 2010 and 2019 "
            "and in every region after that, a steady climb that continued through the final "
            "year of e.g. the series.")
    out, changed = shorten_alt(text)
    # no sentence end at "3.5", "9.2", "U.S." or "e.g."; the clause break after "that" is used
    assert out == ("Rates rose from 3.5 to 9.2 per 100 in U.S. counties between 2010 and 2019 and "
                   "in every region after that.") and changed


def test_clause_break_is_used_when_there_is_no_sentence_end():
    text = ("Grouped bar chart comparing vaccination rates across age groups, sexes and regions "
            "for each year from 2010 to 2019, with the lowest rates among adults over sixty.")
    out, changed = shorten_alt(text)
    assert out == ("Grouped bar chart comparing vaccination rates across age groups, sexes and "
                   "regions for each year from 2010 to 2019.") and changed


def test_clause_break_does_not_split_a_parenthesis_or_a_year_range():
    text = ("Line chart of cases (confirmed, probable and suspected, all regions combined) "
            "from 2005–2019 showing a long decline with a sharp spike in one year and a plateau.")
    out, changed = shorten_alt(text)
    assert out.count("(") == out.count(")") and "2005–2019" in out and changed


def test_word_cut_is_the_last_resort_flagged_and_never_mid_word():
    out, changed = shorten_alt("word " * 60)
    assert changed and len(out) <= MAX_ALT and out.endswith("word")


def test_model_ellipsis_is_removed_and_flagged():
    out, changed = shorten_alt("Pie chart of deaths by year, followed by Health…")
    assert out == "Pie chart of deaths by year, followed by Health." and changed


@pytest.mark.parametrize("text", [
    "Short, then " + "long " * 80,
    "One. Two. Three. " * 20,
    "U.S. vs. e.g. " * 30,
    "Numbers 3.5, 9.2; ratios 1:2 — " * 10,
])
def test_shortened_alt_never_ends_with_an_ellipsis(text):
    out, changed = shorten_alt(text)
    assert len(out) <= MAX_ALT and not out.endswith(("…", "...")) and out.strip() and changed


def test_one_unbroken_word_is_kept_whole_and_flagged_rather_than_cut_mid_word():
    out, changed = shorten_alt("a" * 300)
    assert out == "a" * 300 and changed


def test_unfinished_description_is_flagged_not_shipped_silently():
    r = parse_result({"kind": "chart", "decorative": False, "alt": "Bar chart of deaths.",
                      "description": "Bars rise from 2010 to 2019, and the", "uncertain": False})
    assert r.uncertain and r.description == "Bars rise from 2010 to 2019, and the"


def test_truncated_answer_keeps_a_finished_alt_marked_uncertain_without_description():
    cut = ('{\n  "kind": "chart",\n  "decorative": false,\n  "alt": "Bar chart of deaths by year.",'
           '\n  "description": "A vertical bar chart shows deaths rising from 2010 to 20')
    r = parse_answer(cut, truncated=True)
    assert (r.kind, r.alt, r.description, r.uncertain) == ("chart", "Bar chart of deaths by year.",
                                                          "", True)


def test_truncated_answer_without_a_finished_alt_is_an_error():
    cut = '{"kind": "chart", "decorative": false, "alt": "Bar chart of deat'
    with pytest.raises(ProviderError):
        parse_answer(cut, truncated=True)
    with pytest.raises(ProviderError):
        parse_answer(cut, truncated=False)


def test_complete_answer_is_used_even_when_generation_hit_the_limit():
    text = ('{"kind": "photo", "decorative": false, "alt": "A doctor examining a child.", '
            '"description": "", "uncertain": false}')
    r = parse_answer(text, truncated=True)
    assert r.alt == "A doctor examining a child." and r.uncertain is False


def test_first_json_object_from_free_text():
    assert first_json_object('Sure!\n```json\n{"a": 1}\n```')["a"] == 1
    with pytest.raises(ProviderError):
        first_json_object("no json here")


def test_default_is_none_and_only_none_or_local_exist():
    assert make_provider("none").name == "none"
    for name in ("claude", "cloud", "gpt"):
        with pytest.raises(ValueError):
            make_provider(name)


def test_none_provider_leaves_figures_flagged(tmp_path):
    fig = figure("Photo")
    doc, images = setup(tmp_path, fig)
    stats = enrich_figures(doc, images, make_provider("none"))
    assert stats["described"] == 0 and fig.attrs == {"needs": "alt"}
    assert review_items(doc)[0]["severity"] == "error"


# --- the guard, the model route, grounding and the figures that need no description ----

def _png(path, size=(40, 30), noisy=True):
    from PIL import Image
    image = Image.new("L", size, 255)
    if noisy:
        image.putdata([(x * 7 + y * 13) % 256 for y in range(size[1]) for x in range(size[0])])
    image.save(path, "WEBP" if path.suffix == ".webp" else "PNG")
    return path


def test_authored_decorative_and_unread_figures_are_never_drafted_even_from_the_cache(tmp_path):
    authored = Node(Role.FIGURE, page=1, bbox=(0, 0, 100, 100), attrs={"alt": "From the tags", "needs": "alt"})
    decorative = Node(Role.FIGURE, page=1, bbox=(0, 200, 100, 300), attrs={"decorative": True, "needs": "alt"})
    unread = Node(Role.FIGURE, page=1, bbox=(0, 400, 100, 500),
                  attrs={"unread": True, "kind": "signature", "needs": "alt"})
    figs = (authored, decorative, unread)
    doc, images = setup(tmp_path, *figs)
    provider = FakeProvider({})
    contexts = enrich.figure_contexts(doc)
    cache = {cache_key(images[f.id].read_bytes(), contexts[f.id], provider): {
        "kind": "photo", "decorative": False, "alt": "A model's words", "description": "",
        "uncertain": False} for f in figs}
    path = tmp_path / "cache.json"
    path.write_text(json.dumps(cache))
    stats = enrich_figures(doc, images, provider, cache_path=path)
    assert provider.calls == [] and stats["cached"] == stats["described"] == 0
    assert authored.attrs["alt"] == "From the tags" and "provenance" not in unread.attrs
    assert unread.attrs["kind"] == "signature" and "alt" not in unread.attrs


class ScriptedVision:
    cache_id = "scripted"

    def __init__(self, text):
        self.text, self.sent = text, []

    def ask(self, system, prompt, image, schema=None, max_tokens=None):
        from reflow.analyze.vision import Reply
        self.sent.append(image)
        return Reply(self.text, False)


def test_the_model_provider_parses_an_answer_and_sends_the_crop_as_webp(tmp_path):
    client = ScriptedVision(json.dumps({"kind": "photo", "decorative": False, "alt": "A pump.",
                                        "description": "", "uncertain": False}))
    fig = figure("Pump")
    doc, _ = setup(tmp_path, fig)
    images = {fig.id: _png(tmp_path / "p0001-0.webp")}
    stats = enrich_figures(doc, images, enrich.ModelProvider("ollama", client))
    assert stats["described"] == 1 and fig.attrs["alt"] == "A pump."
    assert client.sent[0].media_type == "image/webp"


def test_a_full_cache_hit_needs_no_server_and_no_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:9")    # a closed port
    for name in ("ollama", "anthropic"):
        fig = figure("Pump")
        doc, images = setup(tmp_path, fig)
        provider = make_provider(name)
        ctx = enrich.figure_contexts(doc)[fig.id]
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps({cache_key(images[fig.id].read_bytes(), ctx, provider): {
            "kind": "photo", "decorative": False, "alt": "A pump.", "description": "",
            "uncertain": False}}))
        stats = enrich_figures(doc, images, provider, cache_path=path)
        assert (stats["cached"], stats["failed"]) == (1, 0) and fig.attrs["alt"] == "A pump."


def test_the_prompt_version_and_the_printed_words_are_part_of_the_cache_key(monkeypatch):
    provider = FakeProvider({})
    ctx = FigureContext(1, "c", "h", "p", "Q1 2024")
    key = cache_key(b"x", ctx, provider)
    assert "Printed near the image: Q1 2024" in ctx.as_text()
    assert cache_key(b"x", FigureContext(1, "c", "h", "p", "Q2 2024"), provider) != key
    monkeypatch.setattr(enrich, "PROMPT_VERSION", enrich.PROMPT_VERSION + 1)
    assert cache_key(b"x", ctx, provider) != key


def test_the_printed_words_near_a_figure_and_its_labels_are_its_context(tmp_path):
    label = Node(Role.P, page=1, bbox=(10, 10, 40, 20), text="Q1", attrs={"figure_label": True})
    fig = Node(Role.FIGURE, page=1, bbox=(0, 0, 100, 100), children=[label])
    near = Node(Role.P, page=1, bbox=(0, 120, 100, 130), text="Revenue  2024")
    far = Node(Role.P, page=1, bbox=(0, 300, 100, 310), text="Far away")
    doc = Document(root=Node(Role.DOC, children=[fig, near, far]), pages=[PageInfo(1, 612, 792)])
    assert enrich.figure_contexts(doc)[fig.id].printed_nearby == "Q1 Revenue 2024"


def ctx(text):
    return FigureContext(1, "", "", "", text)


def alt(text, description=""):
    return AltResult("chart", False, text, description, False)


@pytest.mark.parametrize("draft, context, grounded", [
    ("1,497 visitors", "1497 visitors in all", True),
    ("1 497 visitors", "1,497", True),
    ("1,497 visitors", "1,479 visitors", False),
    ("Held on January 2, 2025.", "Date: 1/2/2025", True),
    ("Held on 01/02/2025.", "2 January 2025", True),
    ("Held on January 3, 2025.", "1/2/2025", False),
    ("A 3.5% rise", "rose 3.50 %", True),
    ("A 3,5 % rise", "rose 3.5%", True),
    ("A 4% rise", "rose 3.5%", False),
    ("Founded in 1998", "since 1998", True),
    ("Founded in 1998", "since 1989", False),
    ("The sign reads “Welcome to Carmel”.", 'A sign: "welcome to  carmel"', True),
    ("The sign reads \"Open Daily\".", "Closed", False),
    ("Three people and 2 dogs", "", True),
    ("Revenue for 2024, 2025.", "", False),
    ("Revenue for 2024, 2025.", "Years 2024 and 2025", True),
    ("Revenue for 2024, 2026.", "Years 2024 and 2025", False),
])
def test_ground_flags_numbers_dates_and_quotations_the_page_lacks(draft, context, grounded):
    result = enrich.ground(alt(draft), ctx(context))
    assert result.uncertain is (not grounded)
    assert result.alt == draft    # never edited


def test_a_cached_answer_naming_an_absent_number_is_uncertain_too(tmp_path):
    fig = figure("Pump")
    doc, images = setup(tmp_path, fig)
    provider = FakeProvider({})
    c = enrich.figure_contexts(doc)[fig.id]
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({cache_key(images[fig.id].read_bytes(), c, provider): {
        "kind": "photo", "decorative": False, "alt": "A pump rated 4,400 gallons.",
        "description": "", "uncertain": False}}))
    enrich_figures(doc, images, provider, cache_path=path)
    assert fig.attrs["warn"] == "suggested-alt-uncertain"
    assert json.loads(path.read_text()).popitem()[1]["uncertain"] is False   # the cache keeps the answer


def test_figures_that_need_no_description_are_marked_with_their_reason_and_never_sent(tmp_path):
    def fig(page, bbox, **attrs):
        return Node(Role.FIGURE, page=page, bbox=bbox, attrs={"needs": "alt", **attrs})
    rule = fig(1, (0, 100, 500, 102))
    band = fig(1, (0, 110, 400, 120))
    blank = fig(1, (0, 200, 200, 300))
    tiny = fig(1, (0, 400, 30, 430))
    logos = [fig(p, (500, 10, 600, 60)) for p in (1, 2, 3)]
    authored_logo = logos[2]
    authored_logo.attrs.update(alt="Acme")
    authored_logo.attrs.pop("needs")
    photo = fig(1, (0, 500, 300, 700))
    figs = [rule, band, blank, tiny, *logos, photo]
    doc = Document(root=Node(Role.DOC, children=figs),
                   pages=[PageInfo(p, 612, 792) for p in (1, 2, 3)])
    doc.ensure_unique_ids()
    images = {}
    for i, f in enumerate(figs):
        path = tmp_path / f"f{i}.webp"
        if f is blank:
            _png(path, (200, 100), noisy=False)
        elif f in logos:
            _png(path, (100, 50))
        else:
            _png(path, (300, 200) if f is photo else (60, 40))
        images[f.id] = path
    # a different photo, so it is not a repeat of the logo
    from PIL import Image
    Image.effect_noise((300, 200), 80).save(images[photo.id], "WEBP")
    counts = enrich.mark_roles(doc, images)
    assert counts == {"ruling": 2, "blank": 1, "tiny": 1, "repeated": 2}
    roles = {id(f): f.attrs.get("figure_role") for f in figs}
    assert [roles[id(f)] for f in (rule, band, blank, tiny)] == ["decorative"] * 3 + ["icon"]
    assert [roles[id(f)] for f in logos] == ["logo"] * 3 and photo.attrs.get("figure_role") is None
    assert authored_logo.attrs["alt"] == "Acme" and not authored_logo.attrs.get("decorative")
    for f in (rule, band, blank, tiny, *logos[:2]):
        assert f.attrs["decorative"] is True and f.attrs["alt"] == "" and "needs" not in f.attrs
        (fact,) = [x for x in f.facts if x.code == "decorative-guessed"]
        assert fact.data["reason"] in {"ruling", "blank", "tiny", "repeated"}
    provider = FakeProvider({"": AltResult("photo", False, "A photo.", "", False)})
    enrich_figures(doc, images, provider)
    assert len(provider.calls) == 1    # only the photo reaches the model
    items = review_items(doc)
    assert not any(i["needs"] == "alt" for i in items)
    enrich.store_roles(doc)
    assert photo.attrs["figure_role"] == "content"


def test_a_models_transcription_is_never_the_context_a_draft_is_checked_against():
    drafted = Node(Role.P, page=1, bbox=(0, 110, 100, 120), text="Total 9999",
                   attrs={"provenance": "ai-drafted", "reviewed": False})
    heading = Node(Role.H2, page=1, bbox=(0, 0, 100, 10), text="Model heading",
                   attrs={"provenance": "ai-drafted", "reviewed": False})
    fig = Node(Role.FIGURE, page=1, bbox=(0, 0, 100, 100))
    doc = Document(root=Node(Role.DOC, children=[heading, drafted, fig]),
                   pages=[PageInfo(1, 612, 792)])
    c = enrich.figure_contexts(doc)[fig.id]
    assert (c.heading, c.paragraph, c.printed_nearby) == ("", "", "")
    assert enrich.ground(AltResult("chart", False, "A total of 9999.", "", False), c).uncertain
    from reflow.render.alt import figure_headings
    assert figure_headings(doc) == {}


def test_a_band_printed_across_the_page_edge_with_no_words_is_decorative(tmp_path):
    # az-accident-insurance-flyer p1: a blue banner across the top of the page.
    def fig(bbox, *children):
        return Node(Role.FIGURE, page=1, bbox=bbox, attrs={"needs": "alt"}, children=list(children))
    banner = fig((0, 0, 612, 94))
    footer = fig((0, 720, 612, 792))
    titled = fig((0, 0, 612, 77), Node(Role.P, text="Employee benefits", page=1,
                                        bbox=(20, 20, 300, 40), attrs={"figure_label": True}))
    inset = fig((36, 0, 576, 94))            # inside the margins: not across the page
    tall = fig((0, 0, 612, 300))             # a photo printed to the edge
    figs = [banner, footer, titled, inset, tall]
    doc = Document(root=Node(Role.DOC, children=figs), pages=[PageInfo(1, 612, 792)])
    doc.ensure_unique_ids()
    from PIL import Image
    images = {}
    for i, f in enumerate(figs):
        path = tmp_path / f"f{i}.webp"
        Image.effect_noise((300, 60), 80).rotate(i * 7).save(path, "WEBP")
        images[f.id] = path
    assert enrich.mark_roles(doc, images) == {"bleed": 2}
    assert [f.attrs.get("figure_role") for f in figs] == ["decorative", "decorative", None, None, None]
    for f in (banner, footer):
        assert f.attrs["decorative"] is True and f.attrs["alt"] == ""
        (fact,) = [x for x in f.facts if x.code == "decorative-guessed"]
        assert fact.data["reason"] == "bleed"
